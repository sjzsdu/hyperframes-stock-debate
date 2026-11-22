"""Save redacted HTTP error DTOs using injected oneapi errors, without network."""
import argparse
import json
from pathlib import Path
import tempfile

from stocktalk.modules.providers.oneapi import OneAPIRequestError
from stocktalk.platform.server import create_app

parser=argparse.ArgumentParser()
parser.add_argument('--evidence-dir',type=Path,required=True)
args=parser.parse_args();args.evidence_dir.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as root:
    for code in ['CONFIG_MISSING','UPSTREAM_AUTH','UPSTREAM_TIMEOUT','MODEL_UNAVAILABLE','QUOTA_EXCEEDED']:
        def fail():raise OneAPIRequestError(code,outcome_unknown=code=='UPSTREAM_TIMEOUT')
        app=create_app(root=Path(root),models=fail)
        response=app.test_client().get('/api/models',base_url='http://127.0.0.1:8765')
        assert response.status_code==503 and response.json['error']['code']==code
        (args.evidence_dir/(code+'.json')).write_text(json.dumps({'fixture':True,'http':response.status_code,**response.json},ensure_ascii=False,indent=2))
        print(code,response.status_code)
