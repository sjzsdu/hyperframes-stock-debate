"""AC-01/10: a third type changes declaration files only, through core media seams."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import wave

import pytest
import yaml

from stocktalk.modules.dialogue_generator import create_dialogue_generator, DialogueGenerationError
from stocktalk.modules.hyperframes_builder import HyperFramesBuilder
from stocktalk.modules.tts_agent import TTSAgent
from stocktalk.pipeline import Pipeline, PipelineError, load_config
from stocktalk.video_types import TypeCatalog, TypeContractError, bind_type
from test_monologue import _monologue_response

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/video_types"


def configuration(root=EXAMPLE):
    config = load_config()
    config["video_types_dir"] = str(root)
    config["dialogue"].update(type_id="market-brief", opening_gate=False)
    return config


def test_host_from_declaration_reaches_prompt_script_voice_subtitles_and_project(tmp_path):
    config = configuration()
    config["output"]["dir"] = str(tmp_path)
    class LLM:
        def chat(self, system, message, **kw):
            assert 'host' in message
            assert '行情主持人' in system
            assert 'narrator' not in message
            return _monologue_response().replace('narrator', 'host')
    gen = create_dialogue_generator(config, llm=LLM())
    stock = {"code": "000001", "quote": {"name": "平安银行", "price": 12.5}}
    script = gen.generate(stock)
    assert script["type_id"] == "market-brief"
    assert {t["speaker"] for t in script["turns"]} == {"host"}
    assert gen.config.min_duration_seconds == 30
    assert gen.config.max_duration_seconds == 60
    assert gen.config.arc == "deep_dive"
    assert script["arc"]["id"] == "deep_dive"
    jobs = []
    class TTS:
        supports_line_prosody = False
        def check_ready(self): pass
        def synthesize_lines(self, lines):
            jobs.extend(lines)
            for j in lines:
                with wave.open(str(j.out_path), 'wb') as f:
                    f.setnchannels(1); f.setsampwidth(2); f.setframerate(24000)
                    f.writeframes(b'\0\0' * 24000)
    audio = TTSAgent(config, tts_provider=TTS()).synthesize(script)
    assert jobs and all(j.voice == config["characters"]["narrator"]["voice_id"] for j in jobs)
    assert {s["character"] for s in audio["segments"]} == {"host"}
    assert Path(audio["srt_path"]).is_file()
    builder = HyperFramesBuilder(config)
    assert builder._cast() == ("host",)
    project = builder.build(stock, script, audio)
    assert '行情主持人' in (project.directory / "index.html").read_text()
    assert 'host' in (project.directory / "index.html").read_text()


@pytest.mark.parametrize("mutation,field", [
    (lambda d: d.update(schema_version=2), 'schema_version'),
    (lambda d: d.update(required_data=['earnings']), 'required_data'),
    (lambda d: d.update(sample='../../sample.json'), 'sample'),
    (lambda d: d['duration'].update(min_seconds=90), 'duration'),
    (lambda d: d['visual'].update(scene='../../secret'), 'visual'),
    (lambda d: d['compliance_defaults'].update(tags='not a list'), 'tags'),
    (lambda d: d['script'].update(arc='deep-dive'), 'script'),
])
def test_invalid_declaration_is_isolated(tmp_path, mutation, field):
    shutil.copytree(EXAMPLE / 'market-brief', tmp_path / 'market-brief')
    path = tmp_path / 'market-brief/type.yaml'
    d = yaml.safe_load(path.read_text()); mutation(d); path.write_text(yaml.safe_dump(d))
    catalog = TypeCatalog(tmp_path)
    assert field in catalog.diagnostics['market-brief']
    assert catalog.get('debate')
    with pytest.raises(TypeContractError): catalog.get('market-brief')


def test_duplicate_disabled_unknown_and_voice_errors(tmp_path):
    shutil.copytree(EXAMPLE / 'market-brief', tmp_path / 'market-brief')
    path = tmp_path / 'market-brief/type.yaml'
    d = yaml.safe_load(path.read_text()); d['status'] = 'disabled'; path.write_text(yaml.safe_dump(d))
    with pytest.raises(TypeContractError): TypeCatalog(tmp_path).get('market-brief')
    with pytest.raises(TypeContractError): TypeCatalog().get('missing')
    config = configuration(); config['dialogue']['type_id']='missing'
    with pytest.raises(TypeContractError): create_dialogue_generator(config)
    shutil.copytree(Path(__file__).resolve().parents[1] / 'stocktalk/video_types/debate', tmp_path / 'debate')
    assert 'duplicate' in TypeCatalog(tmp_path).diagnostics['debate']
    config = configuration(); d['status']='enabled'; d['cast'][0]['voice_ref']='absent'; path.write_text(yaml.safe_dump(d)); config['video_types_dir']=str(tmp_path)
    with pytest.raises(TypeContractError, match='voice_ref'): bind_type(config)


def test_foreign_speaker_is_not_silently_changed():
    config=configuration()
    class BadLLM:
        def chat(self,*args,**kw): return _monologue_response().replace('narrator','imposter')
    with pytest.raises(DialogueGenerationError, match='unknown speaker'):
        create_dialogue_generator(config,llm=BadLLM()).generate({'code':'000001','quote':{'name':'平安银行'}})


def test_required_data_stops_before_dialogue(tmp_path):
    config=configuration(); config['output']['dir']=str(tmp_path)
    pipe=Pipeline(config); pipe.stock_client.get_stock_data=lambda _: {'quote':{}}
    pipe.dialogue_gen.generate=lambda *args: pytest.fail('dialogue called without required data')
    with pytest.raises(PipelineError,match='required data'): pipe.run('000001')


def test_local_assets_are_sandboxed_snapshotted_and_hashed(tmp_path):
    shutil.copytree(EXAMPLE/'market-brief',tmp_path/'market-brief')
    path=tmp_path/'market-brief/type.yaml';d=yaml.safe_load(path.read_text())
    asset=path.parent/'scene.html.j2';asset.write_text('<div data-composition-id="brief">{{ name }}</div>')
    d['visual']['scene']='scene.html.j2';path.write_text(yaml.safe_dump(d))
    catalog=TypeCatalog(tmp_path);t=catalog.get('market-brief')
    from stocktalk.video_types.templates import template
    assert '&lt;script&gt;' in template(t.declaration['_assets']['scene']).render(name='<script>')
    asset.write_text('{{ name }} changed')
    assert TypeCatalog(tmp_path).get('market-brief').digest!=t.digest
    asset.write_text('{{ cycler.__init__.__globals__ }}')
    with pytest.raises(Exception):template(asset.read_text()).render()
    asset.write_text('{% include "../../secret" %}')
    assert 'market-brief' in TypeCatalog(tmp_path).diagnostics
    asset.unlink();asset.symlink_to('/etc/hosts')
    assert 'market-brief' in TypeCatalog(tmp_path).diagnostics


def test_two_custom_roles_bind_semantic_strategy_slots(tmp_path):
    source=Path(__file__).resolve().parents[1]/'stocktalk/video_types/debate'
    shutil.copytree(source,tmp_path/'custom-debate')
    path=tmp_path/'custom-debate/type.yaml';d=yaml.safe_load(path.read_text());d['id']='custom-debate'
    d['cast'][0].update(speaker_id='optimist',name='乐观分析师');d['cast'][1].update(speaker_id='auditor',name='核验分析师')
    path.write_text(yaml.safe_dump(d));(path.parent/'sample.json').write_text(json.dumps({'turns':[{'speaker':'optimist','line':'观察业务。'},{'speaker':'auditor','line':'核对风险。'}]}))
    config=load_config();config['video_types_dir']=str(tmp_path);config['dialogue'].update(type_id='custom-debate',opening_gate=False)
    class LLM:
        def chat(self,system,message,**kw):
            assert 'optimist' in message and 'auditor' in message
            return json.dumps({'title':'平安银行（000001）：观察业务','turns':[{'speaker':'optimist','line':'平安银行的生意是赚取存贷款息差，利率变化影响这门生意。'},{'speaker':'auditor','line':'但低成本存款能不能守住，需要继续核对公开报表里的变化。'}]})
    script=create_dialogue_generator(config,llm=LLM()).generate({'code':'000001','quote':{'name':'平安银行'}})
    assert {t['speaker'] for t in script['turns']}=={'optimist','auditor'}
    assert HyperFramesBuilder(config)._cast()==('optimist','auditor')
