"""AC-02/03/06/07/09: HTTP contract plus real readable fixture media, no external APIs."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import shutil

import pytest

from stocktalk.platform.demo import DemoPipeline, DemoPublisher
from stocktalk.platform.desktop import viewer_url
from stocktalk.platform.server import create_app
from stocktalk.platform.store import Conflict, Store
from stocktalk.pipeline import load_config

ORIGIN='http://127.0.0.1:8765'


@pytest.fixture
def app(tmp_path):
    config=load_config();type_root=tmp_path/'types';shutil.copytree(Path(__file__).resolve().parents[1]/'examples/video_types',type_root);config['video_types_dir']=str(type_root)
    app=create_app(config,root=tmp_path,models=lambda:['fixture-model'],pipeline_factory=DemoPipeline,publisher_factory=DemoPublisher)
    app.config['TESTING']=True
    return app


def browser(app):
    client=app.test_client()
    page=client.get('/',base_url=ORIGIN)
    assert page.status_code==200
    with client.session_transaction(base_url=ORIGIN) as session: token=session['csrf']
    headers={'X-CSRF-Token':token,'Origin':ORIGIN,'Idempotency-Key':'fixture'}
    return client,headers


def submit(client,headers,**overrides):
    data={'stock':'000001','type_id':'market-brief','model':'fixture-model',**overrides}
    return client.post('/api/tasks',base_url=ORIGIN,headers=headers,json=data)


def test_http_generation_review_preflight_publication_and_shared_view(app):
    client,headers=browser(app)
    created=submit(client,headers)
    assert created.status_code==202
    task_id=created.json['id']
    assert submit(client,headers).json['id']==task_id
    assert submit(client,headers,stock='000002').status_code==409
    assert app.extensions['worker'].once()
    task=client.get('/api/tasks/'+task_id,base_url=ORIGIN).json
    assert task['status']=='succeeded'
    assert task['result']['approved_script']['type_id']=='market-brief'
    assert {t['speaker'] for t in task['result']['approved_script']['turns']}=={'host'}
    assert 'snapshot' not in task and str(app.extensions['store'].root) not in json.dumps(task)
    video=next(a for a in task['artifacts'] if a['kind']=='mp4')
    downloaded=client.get(f'/api/tasks/{task_id}/artifacts/{video["id"]}',base_url=ORIGIN)
    assert downloaded.status_code==200 and downloaded.mimetype=='video/mp4'
    path=app.extensions['store'].artifact(video['id'],task_id)
    decoded=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(path)]))
    assert {stream['codec_type'] for stream in decoded['streams']}=={'audio','video'}
    check=client.post('/api/preflight',base_url=ORIGIN,headers=headers,json={'platforms':['bilibili']})
    assert check.json['platforms']['bilibili']['ok']
    publication=client.post(f'/api/tasks/{task_id}/publications',base_url=ORIGIN,headers=headers,json={'platform':'bilibili','reviewed':True,'ai_declaration':True})
    assert publication.status_code==202
    assert app.extensions['worker'].once()
    report=client.get(f'/api/tasks/{task_id}/publications',base_url=ORIGIN).json
    assert report['task_id']==task_id and report['publications'][0]['status']=='succeeded'
    duplicate=client.post(f'/api/tasks/{task_id}/publications',base_url=ORIGIN,headers=headers,json={'platform':'bilibili','reviewed':True,'ai_declaration':True})
    assert duplicate.status_code==409
    desktop=client.get('/desktop',base_url=ORIGIN)
    assert desktop.status_code==200 and '生成与发布请使用 Web'.encode() in desktop.data
    assert b'id="create-form"' not in desktop.data and b'class="publish-form"' not in desktop.data
    assert viewer_url(ORIGIN)==ORIGIN+'/desktop'
    assert client.get('/api/tasks?type_id=market-brief&stock=000001&status=succeeded&since=2000-01-01&until=2100-01-01',base_url=ORIGIN).json['tasks'][0]['id']==task_id
    assert client.get('/api/tasks?stock=000002',base_url=ORIGIN).json['tasks']==[]


@pytest.mark.parametrize('bad_origin',['http://evil.invalid','null'])
def test_cross_origin_and_csrf_rejected(app,bad_origin):
    client,headers=browser(app)
    headers['Origin']=bad_origin
    assert submit(client,headers).status_code==403
    assert submit(client,{}).status_code==403
    assert client.get('/api/tasks',base_url='http://evil.invalid').status_code==403
    assert app.extensions['store'].list({})==[]


def test_validation_artifact_escape_and_missing_dependency(app,tmp_path):
    client,headers=browser(app)
    assert submit(client,headers,stock='../../file').status_code==400
    assert submit(client,headers,type_id='unknown').status_code==400
    assert submit(client,headers,model='unauthorized').status_code==503
    assert client.get('/api/tasks/nope',base_url=ORIGIN).status_code==404
    created=submit(client,headers); task_id=created.json['id']
    assert app.extensions['worker'].once()
    store=app.extensions['store'];task=store.get(task_id,private=True)
    video=next(a for a in task['artifacts'] if a['kind']=='mp4')
    path=store.root/video['path'];path.unlink(); path.symlink_to(Path('/etc/hosts'))
    assert client.get(f'/api/tasks/{task_id}/artifacts/{video["id"]}',base_url=ORIGIN).status_code==404


def test_snapshot_survives_type_removal_and_config_changes(app):
    client,headers=browser(app)
    task_id=submit(client,headers).json['id']
    task=app.extensions['store'].get(task_id,private=True)
    assert task['snapshot']['config']['_frozen_type']['id']=='market-brief'
    assert 'ONEAPI_API_KEY' not in task['snapshot']
    shutil.rmtree(Path(task['snapshot']['config']['video_types_dir']))
    assert app.extensions['worker'].once()
    assert app.extensions['store'].get(task_id)['status']=='succeeded'


def test_recovery_no_replay_and_targeted_retry(tmp_path):
    store=Store(tmp_path)
    task_id,_=store.create('key',{'stock':'000001','type_id':'debate','model':'m'},{})
    assert store.claim()['attempt']==1
    pub=store.publish(task_id,'bilibili','default',{'ai_declaration':True})
    assert store.claim_publication()['id']==pub
    assert store.recover()
    assert store.get(task_id)['status']=='interrupted'
    assert store.publications(task_id)[0]['status']=='outcome_unknown'
    assert store.claim() is None and store.claim_publication() is None
    with pytest.raises(Conflict): store.publish(task_id,'bilibili','default',{},pub)
    failed=store.publish(task_id,'douyin','default',{})
    store.finish_publication(failed,'failed',{'code':'EXPLICIT_REJECTION'})
    retry=store.publish(task_id,'douyin','default',{},failed)
    assert retry!=failed
    with pytest.raises(Conflict):store.publish(task_id,'kuaishou','default',{},failed)
    assert store.publications(task_id)[0]['status']=='outcome_unknown'


@pytest.mark.parametrize('url',['https://evil.invalid','http://localhost:8765','file:///tmp/index','http://127.0.0.1:8765/other','http://user@127.0.0.1:8765'])
def test_desktop_address_is_local(url):
    with pytest.raises(ValueError):viewer_url(url)


def test_two_renders_have_separate_immutable_outputs(app):
    client,headers=browser(app)
    first=submit(client,headers).json['id'];assert app.extensions['worker'].once()
    before=app.extensions['store'].get(first,private=True)
    first_path=Path(before['result']['video_path']);first_bytes=first_path.read_bytes()
    headers['Idempotency-Key']='second'
    second=submit(client,headers,stock='000002').json['id'];assert app.extensions['worker'].once()
    after=app.extensions['store'].get(second,private=True)
    assert first!=second and first_path!=Path(after['result']['video_path'])
    assert first_path.read_bytes()==first_bytes


def test_concurrent_idempotent_creation_has_single_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store=Store(tmp_path)
    request={'stock':'000001','type_id':'debate','model':'m'}
    with ThreadPoolExecutor(max_workers=4) as pool:
        result=list(pool.map(lambda _:store.create('same',request,{}),range(8)))
    assert len({task_id for task_id,created in result})==1
    assert sum(created for task_id,created in result)==1


def test_inline_credentials_rejected_before_database_is_created(tmp_path):
    config=load_config();config['tts'].setdefault('options',{})['api_key']='fixture-secret'
    with pytest.raises(ValueError,match='凭据只能'):
        create_app(config,root=tmp_path/'private',models=lambda:['m'])
    assert not (tmp_path/'private/tasks.sqlite3').exists()


@pytest.mark.parametrize('query', ['type=market-brief', 'unknown=value', 'stock=000001&typo=x'])
def test_unknown_task_filters_rejected(app, query):
    client, headers = browser(app)
    assert submit(client, headers).status_code == 202
    result = client.get('/api/tasks?' + query, base_url=ORIGIN)
    assert result.status_code == 400
    assert result.json['error']['code'] == 'INVALID_INPUT'


@pytest.mark.parametrize('query,count,start', [
    ('limit=20&offset=0', 20, 104),
    ('type_id=market-brief&limit=200&offset=0', 100, 104),
    ('stock=000001&limit=20&offset=20', 20, 84),
])
def test_workbench_task_pagination(app, query, count, start):
    store = app.extensions['store']
    task_ids = [store.create(str(index), {'stock': '000001', 'type_id': 'market-brief', 'model': 'fixture-model'}, {})[0]
                for index in range(105)]
    with store.connection() as db:
        for index, task_id in enumerate(task_ids):
            db.execute('UPDATE task SET created_at=? WHERE id=?', (f'2026-01-01T00:{index // 60:02}:{index % 60:02}Z', task_id))
    client, _ = browser(app)
    result = client.get('/api/tasks?' + query, base_url=ORIGIN)
    assert result.status_code == 200
    tasks = result.json['tasks']
    assert len(tasks) == count
    assert [task['created_at'] for task in tasks] == [
        f'2026-01-01T00:{index // 60:02}:{index % 60:02}Z'
        for index in range(start, start - count, -1)
    ]
