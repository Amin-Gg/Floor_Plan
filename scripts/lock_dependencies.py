#!/usr/bin/env python3
"""Regenerate Phase 6 Python 3.11/linux-amd64 hashed locks with uv 0.8.4."""
from __future__ import annotations
import argparse, hashlib, json, shutil, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
UV='0.8.4'; PLATFORM='x86_64-manylinux_2_31'; PY='3.11'
SPECS=[
 ('stage1-runtime','requirements/stage1-runtime.in','requirements/stage1-runtime.lock',False),
 ('stage1-ml-overlay','requirements/stage1-ml-overlay.in','requirements/stage1-ml-overlay.lock',True),
 ('baseline','requirements/baseline.in','requirements/baseline.lock',False),
 ('engine-runtime','compliance-engine/requirements/runtime.in','compliance-engine/requirements/runtime.lock',False),
 ('engine-ml-overlay','compliance-engine/requirements/ml-overlay.in','compliance-engine/requirements/ml-overlay.lock',True),
]
def sha(p:Path)->str:
 h=hashlib.sha256(); h.update(p.read_bytes()); return h.hexdigest()
def compile_one(src:Path,out:Path,no_deps:bool)->None:
 cmd=['uv','pip','compile',str(src),'--python-version',PY,'--python-platform',PLATFORM,'--generate-hashes','--output-file',str(out),'--custom-compile-command','python scripts/lock_dependencies.py','--quiet']
 if no_deps: cmd.insert(4,'--no-deps')
 subprocess.run(cmd,cwd=ROOT,check=True)
def manifest()->dict:
 return {'schema_version':'phase6-dependency-locks-v1','uv_version':UV,'target':{'python':PY,'platform':PLATFORM},'locks':[{'id':i,'input':s,'lock':o,'input_sha256':sha(ROOT/s),'lock_sha256':sha(ROOT/o),'no_deps_overlay':n} for i,s,o,n in SPECS]}
def main()->int:
 ap=argparse.ArgumentParser(); ap.add_argument('--check',action='store_true'); a=ap.parse_args()
 if shutil.which('uv') is None: raise SystemExit('uv is required: python -m pip install uv==0.8.4')
 if a.check:
  with tempfile.TemporaryDirectory() as td:
   for i,s,o,n in SPECS:
    tmp=Path(td)/(Path(o).name); compile_one(ROOT/s,tmp,n)
    if tmp.read_bytes()!=(ROOT/o).read_bytes(): raise SystemExit(f'lock drift: {o}')
 else:
  for i,s,o,n in SPECS: compile_one(ROOT/s,ROOT/o,n)
 data=manifest(); (ROOT/'requirements/lock-manifest.json').write_text(json.dumps(data,indent=2,sort_keys=True)+'\n')
 print(json.dumps({'locks':len(SPECS),'check':a.check},sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
