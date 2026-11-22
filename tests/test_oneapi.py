"""AC-04/07/08/11: injected transport, no real credentials or network."""
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from stocktalk.modules.providers import create_llm_client
from stocktalk.modules.providers.oneapi import BASE_URL, OneAPIClient, OneAPIRequestError, NoRedirect, validate_dialogue
from stocktalk.pipeline import Pipeline, load_config


class Response(io.BytesIO):
    pass


def response(data): return Response(json.dumps(data).encode())


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv('ONEAPI_API_KEY','fixture-only-not-a-real-key')
    monkeypatch.setattr('urllib.request.OpenerDirector.open',lambda *a,**k: pytest.fail('unexpected network'))


@pytest.mark.parametrize('flat',[False,True])
def test_factory_configuration_and_auth(flat):
    source={'provider':'oneapi','model':'fixture-model','options':{'base_url':BASE_URL,'api_key_env':'ONEAPI_API_KEY'}}
    client=create_llm_client(source if flat else {'dialogue':source})
    calls=[]
    def transport(req,timeout):
        calls.append(req)
        if req.get_method()=='GET':
            assert req.full_url==BASE_URL+'/models'
            return response({'data':[{'id':'fixture-model'}]})
        assert req.full_url==BASE_URL+'/chat/completions'
        assert req.get_header('Authorization')=='Bearer fixture-only-not-a-real-key'
        assert json.loads(req.data)['model']=='fixture-model'
        assert timeout<=5
        return response({'choices':[{'message':{'content':'ok'}}]})
    client._urlopen=transport
    assert client.chat('system','message')=='ok'
    assert len(calls)==2


def test_missing_key_stops_before_network(monkeypatch):
    monkeypatch.delenv('ONEAPI_API_KEY')
    with pytest.raises(OneAPIRequestError,match='CONFIG_MISSING'):
        OneAPIClient(urlopen=lambda *a,**k: pytest.fail('network')).models()


@pytest.mark.parametrize('status,body,code',[(401,{},'UPSTREAM_AUTH'),(403,{},'UPSTREAM_AUTH'),(429,{},'RATE_LIMITED'),(429,{'error':{'code':'insufficient_quota'}},'QUOTA_EXCEEDED'),(404,{'error':{'code':'model_not_found'}},'MODEL_UNAVAILABLE'),(500,{'error':{'message':'fixture-only-not-a-real-key'}},'UPSTREAM_ERROR')])
def test_error_matrix_is_redacted(status,body,code):
    def transport(req,**kw): raise urllib.error.HTTPError(req.full_url,status,'secret upstream',{},response(body))
    with pytest.raises(OneAPIRequestError) as error: OneAPIClient(urlopen=transport).chat('s','m')
    assert error.value.code==code
    assert 'fixture-only' not in str(error.value)
    assert 'secret upstream' not in str(error.value)


@pytest.mark.parametrize('body',[b'bad json',b'{}',b'{"choices":[{"message":{"content":null}}]}'])
def test_invalid_success_body_does_not_leak_or_replay(body):
    calls=[]
    def transport(*a,**kw): calls.append(1); return Response(body)
    with pytest.raises(OneAPIRequestError): OneAPIClient(urlopen=transport).chat('s','m')
    assert len(calls)==1


def test_models_and_configuration_are_single_gateway():
    assert OneAPIClient(urlopen=lambda *a,**kw: response({'data':[{'id':'b'},{'id':'a'}]})).models()==['a','b']
    for options in ({'base_url':'https://other.invalid/v1'},{'base_url':'http://oneapi.wnbgj.com/v1'},{'api_key_env':'OTHER_KEY'}):
        with pytest.raises(OneAPIRequestError,match='CONFIG_INVALID'): OneAPIClient(**options)
    with pytest.raises(OneAPIRequestError): validate_dialogue({'dialogue':{'provider':'bailian-cli'}})
    with pytest.raises(OneAPIRequestError): NoRedirect().redirect_request(None,None,302,'',{},'https://other.invalid')


def test_post_read_timeout_propagates_through_normal_pipeline_only_once(tmp_path):
    config=load_config();config['output']['dir']=str(tmp_path)
    pipe=Pipeline(config,sleep=lambda _:pytest.fail('outer retry'))
    pipe.stock_client.get_stock_data=lambda _: {'code':'000001','quote':{'name':'平安银行','price':12}}
    calls=[]
    def transport(req,**kwargs): calls.append(req); raise TimeoutError('secret key should never escape')
    pipe.dialogue_gen._llm=OneAPIClient('fixture',urlopen=transport)
    with pytest.raises(OneAPIRequestError) as error: pipe.run('000001')
    assert error.value.code=='UPSTREAM_TIMEOUT'
    assert error.value.outcome_unknown is True
    assert len(calls)==1 and calls[0].get_method()=='POST'


def test_invalid_script_does_not_trigger_pipeline_replay(tmp_path):
    config=load_config();config['output']['dir']=str(tmp_path)
    pipe=Pipeline(config,sleep=lambda _:pytest.fail('outer retry'))
    pipe.stock_client.get_stock_data=lambda _: {'code':'000001','quote':{'name':'平安银行'}}
    calls=[]
    pipe.dialogue_gen._llm=OneAPIClient('fixture',urlopen=lambda *a,**kw: calls.append(1) or response({'choices':[{'message':{'content':'{}'}}]}))
    with pytest.raises(Exception):pipe.run('000001')
    assert len(calls)==1


def test_transport_supervisor_times_out_without_replaying_or_exposing_key(monkeypatch):
    import subprocess
    calls=[]
    def timeout(argv,**kwargs):
        calls.append((argv,kwargs))
        assert 'fixture-only' not in repr(argv)+kwargs['input']
        assert kwargs['timeout']<=60
        raise subprocess.TimeoutExpired(argv,kwargs['timeout'])
    monkeypatch.setattr('subprocess.run',timeout)
    with pytest.raises(OneAPIRequestError) as err:OneAPIClient('fixture').chat('s','m')
    assert err.value.outcome_unknown and len(calls)==1


def test_registered_provider_rejects_model_before_post():
    calls=[]
    client=create_llm_client({'dialogue':{'provider':'oneapi','model':'not-authorized'}})
    def transport(request,**kwargs):
        calls.append(request)
        return response({'data':[{'id':'allowed'}]})
    client._urlopen=transport
    with pytest.raises(OneAPIRequestError,match='MODEL_UNAVAILABLE'):client.chat('s','m')
    assert len(calls)==1 and calls[0].get_method()=='GET'
