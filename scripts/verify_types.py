"""Validate the exact directory contract consumed by CLI/Web (no keys required)."""
import argparse
import json
from stocktalk.video_types import TypeCatalog

parser=argparse.ArgumentParser()
parser.add_argument('--types-dir')
args=parser.parse_args()
catalog=TypeCatalog(args.types_dir)
print(json.dumps({'types':[t.public() for t in catalog.types.values()], 'diagnostics':catalog.diagnostics},ensure_ascii=False,indent=2))
raise SystemExit(1 if catalog.diagnostics else 0)
