"""Offline HTTP acceptance run; real decodable media, fixture generation/publication."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

from stocktalk.pipeline import load_config
from stocktalk.platform.demo import DemoPipeline, DemoPublisher
from stocktalk.platform.server import create_app


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence-dir',type=Path)
    args=parser.parse_args()
    origin='http://127.0.0.1:8765'
    with tempfile.TemporaryDirectory(prefix='stocktalk-acceptance-') as directory:
        config=load_config();config['video_types_dir']=str(Path(__file__).resolve().parents[1]/'examples/video_types')
        app=create_app(config,root=Path(directory),models=lambda:['fixture-model'],pipeline_factory=DemoPipeline,publisher_factory=DemoPublisher,demo=True)
        client=app.test_client();client.get('/',base_url=origin)
        with client.session_transaction(base_url=origin) as session:csrf=session['csrf']
        headers={'X-CSRF-Token':csrf,'Origin':origin,'Idempotency-Key':'offline-acceptance'}
        submitted=client.post('/api/tasks',base_url=origin,headers=headers,json={'stock':'000001','type_id':'market-brief','model':'fixture-model'})
        assert submitted.status_code==202
        task_id=submitted.json['id']
        assert app.extensions['worker'].once()
        detail=client.get('/api/tasks/'+task_id,base_url=origin).json
        assert detail['status']=='succeeded'
        mp4=next(a for a in detail['artifacts'] if a['kind']=='mp4')
        video=app.extensions['store'].artifact(mp4['id'],task_id)
        streams=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(video)]))
        assert {s['codec_type'] for s in streams['streams']}=={'audio','video'}
        preflight=client.post('/api/preflight',base_url=origin,headers=headers,json={'platforms':['bilibili']})
        assert preflight.status_code==200
        publication=client.post(f'/api/tasks/{task_id}/publications',base_url=origin,headers=headers,json={'platform':'bilibili','reviewed':True,'ai_declaration':True})
        assert publication.status_code==202 and app.extensions['worker'].once()
        report=client.get(f'/api/tasks/{task_id}/publications',base_url=origin).json
        assert report['publications'][0]['status']=='succeeded'
        assert client.get('/desktop',base_url=origin).status_code==200
        evidence={'01-created':submitted.json,'02-generated':detail,'03-preflight':preflight.json,'04-publications':report}
        if args.evidence_dir:
            args.evidence_dir.mkdir(parents=True,exist_ok=True)
            for name,data in evidence.items():(args.evidence_dir/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2))
        print(json.dumps({'result':'PASS','fixture':True,'task_id':task_id,'status':detail['status'],'video_bytes':mp4['size'],
                          'streams':[s['codec_type'] for s in streams['streams']], 'publication':report['publications'][0]['status'],'desktop_http':200},ensure_ascii=False))


if __name__=='__main__':main()
