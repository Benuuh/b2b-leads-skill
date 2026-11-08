"""Verify a baseline reference manifest and its current retained artifacts."""
import argparse
import json
from pathlib import Path

from capture_baseline_refs import verify

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('manifest', type=Path)
args = parser.parse_args()
result = verify(json.loads(args.manifest.read_text(encoding='utf-8-sig')))
print(json.dumps(result, ensure_ascii=False))
if result['errors']:
    raise SystemExit(1)
