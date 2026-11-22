"""Recreate the verified optional SAU snapshot from tracked Git history."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

SOURCE = 'e9cddcc'
EXPECTED = '251ac4f445ad9a5d627fda2fe03c3a44cd518544d508861ace39ee6a26692811'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    destination=args.destination.expanduser().resolve()
    if destination.is_relative_to(root) or (destination.exists() and any(destination.iterdir())):
        parser.error('destination must be an empty directory outside this checkout')
    destination.mkdir(parents=True,mode=0o700,exist_ok=True)
    archive=destination/'sau-source.tar'
    with archive.open('wb') as output:
        subprocess.run(['git','archive','--format=tar','--mtime=@1795321023',SOURCE+':third_party/social-auto-upload'],cwd=root,stdout=output,check=True)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest!=EXPECTED:
        raise RuntimeError('archive checksum does not match reviewed source')
    with tarfile.open(archive) as source:
        source.extractall(destination,filter='data')
    sha=subprocess.check_output(['git','rev-parse',SOURCE],cwd=root,text=True).strip()
    (destination/'STOCKTALK_SOURCE.json').write_text(json.dumps({'source_sha':sha,'archive_sha256':digest,'license':'MIT','extra_requirements':'runtime/sau-requirements.txt'},indent=2))
    print(f'Exported verified SAU snapshot: sha256={digest}')
    print('Next: uv sync --locked --project <destination>; then install runtime/sau-requirements.txt into its .venv')


if __name__=='__main__':main()
