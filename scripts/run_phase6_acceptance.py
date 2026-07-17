#!/usr/bin/env python3
"""Phase 6 daemon-free release acceptance."""
from __future__ import annotations
import argparse,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def run(name,args):
 p=subprocess.run(args,cwd=ROOT,text=True,capture_output=True,check=False,timeout=180); return {'name':name,'passed':p.returncode==0,'returncode':p.returncode,'stdout':p.stdout[-2000:],'stderr':p.stderr[-2000:]}
def main()->int:
 ap=argparse.ArgumentParser(); ap.add_argument('--out',type=Path,default=ROOT/'release/local/phase6/acceptance_result.json'); a=ap.parse_args()
 checks=[run('dependency-locks',[sys.executable,'scripts/verify_dependency_locks.py']),run('container-contracts',[sys.executable,'scripts/validate_container_contracts.py']),run('code-preflight',[sys.executable,'scripts/preflight.py','--mode','code','--skip-runtime-imports','--allow-environment-blockers']),run('sbom',[sys.executable,'scripts/generate_sbom.py'])]
 payload={'schema_version':'phase6-acceptance-v1','passed':all(x['passed'] for x in checks),'checks':checks}
 out=a.out if a.out.is_absolute() else ROOT/a.out; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n'); print(json.dumps(payload,indent=2,sort_keys=True)); return 0 if payload['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
