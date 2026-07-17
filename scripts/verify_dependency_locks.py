#!/usr/bin/env python3
"""Validate Phase 6 hashed dependency locks without network access."""
from __future__ import annotations
import argparse, hashlib, json, re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
MANIFEST=ROOT/'requirements/lock-manifest.json'
PKG_RE=re.compile(r'^([A-Za-z0-9_.-]+)==([^ \\\n]+)')

def sha(path:Path)->str:
 h=hashlib.sha256(); h.update(path.read_bytes()); return h.hexdigest()

def packages(path:Path)->dict[str,str]:
 out={}
 for line in path.read_text(encoding='utf-8').splitlines():
  m=PKG_RE.match(line)
  if m: out[m.group(1).lower().replace('_','-')]=m.group(2)
 return out

def main()->int:
 ap=argparse.ArgumentParser(); ap.add_argument('--json-out',type=Path); a=ap.parse_args()
 data=json.loads(MANIFEST.read_text(encoding='utf-8')); checks=[]
 for item in data['locks']:
  for key in ('input','lock'):
   path=ROOT/item[key]; actual=sha(path); expected=item[f'{key}_sha256']
   checks.append({'name':f"{item['id']}:{key}",'passed':actual==expected,'detail':actual})
  text=(ROOT/item['lock']).read_text(encoding='utf-8')
  pkgs=packages(ROOT/item['lock'])
  checks.append({'name':f"{item['id']}:hashed",'passed':bool(pkgs) and all('--hash=sha256:' in block for block in re.split(r'\n(?=[A-Za-z0-9_.-]+==)',text)[1:]),'detail':f'{len(pkgs)} packages'})
 forbidden={'torch','torchvision','triton','nvidia-cublas','nvidia-cuda-runtime','nvidia-cudnn-cu13'}
 for rel in ('requirements/stage1-runtime.lock','compliance-engine/requirements/runtime.lock'):
  hit=sorted(forbidden & set(packages(ROOT/rel)))
  checks.append({'name':f'{rel}:external-ml-excluded','passed':not hit,'detail':','.join(hit) or 'ok'})
 expected={
  'requirements/stage1-ml-overlay.lock':{'ultralytics':'8.4.72','ultralytics-thop':'2.0.20'},
  'compliance-engine/requirements/ml-overlay.lock':{'sentence-transformers':'3.4.1'},
 }
 for rel,want in expected.items():
  got=packages(ROOT/rel); checks.append({'name':f'{rel}:overlay','passed':got==want,'detail':json.dumps(got,sort_keys=True)})
 payload={'schema_version':'phase6-lock-verification-v1','passed':all(x['passed'] for x in checks),'checks':checks,'target':data['target'],'uv_version':data['uv_version']}
 rendered=json.dumps(payload,indent=2,sort_keys=True); print(rendered)
 if a.json_out:
  p=a.json_out if a.json_out.is_absolute() else ROOT/a.json_out; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(rendered+'\n')
 return 0 if payload['passed'] else 1
if __name__=='__main__': raise SystemExit(main())
