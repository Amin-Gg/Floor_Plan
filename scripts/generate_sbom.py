#!/usr/bin/env python3
"""Generate deterministic CycloneDX 1.5 SBOMs from final release locks and artifacts."""
from __future__ import annotations
import argparse, hashlib, json, re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PKG=re.compile(r'^([A-Za-z0-9_.-]+)==([^ \\\n]+)')
LOCKS={'stage1':ROOT/'requirements/stage1-runtime.lock','stage1-ml-overlay':ROOT/'requirements/stage1-ml-overlay.lock','compliance-engine':ROOT/'compliance-engine/requirements/runtime.lock','compliance-engine-ml-overlay':ROOT/'compliance-engine/requirements/ml-overlay.lock'}
VERSIONS={'stage1':'2.8.0','stage1-ml-overlay':'2.8.0','compliance-engine':'1.4.0','compliance-engine-ml-overlay':'1.4.0','floorplan-external-artifacts':'2.8.0'}
def sha(p:Path)->str:return hashlib.sha256(p.read_bytes()).hexdigest()
def parse(path:Path):
 lines=path.read_text().splitlines(); out=[]
 for i,line in enumerate(lines):
  m=PKG.match(line)
  if not m: continue
  hashes=[]; j=i
  while j<len(lines) and (j==i or lines[j].startswith(' ') or lines[j].startswith('\\')):
   hashes += re.findall(r'--hash=sha256:([0-9a-f]{64})',lines[j]); j+=1
  name=m.group(1); ver=m.group(2)
  comp={'type':'library','name':name,'version':ver,'purl':f'pkg:pypi/{name.lower().replace("_","-")}@{ver}','bom-ref':f'pkg:pypi/{name.lower().replace("_","-")}@{ver}'}
  if hashes: comp['hashes']=[{'alg':'SHA-256','content':x} for x in sorted(set(hashes))]
  out.append(comp)
 return out
def bom(name:str,components:list,seed:str):
 serial=hashlib.sha256(seed.encode()).hexdigest()
 return {'bomFormat':'CycloneDX','specVersion':'1.5','serialNumber':f'urn:uuid:{serial[:8]}-{serial[8:12]}-{serial[12:16]}-{serial[16:20]}-{serial[20:32]}','version':1,'metadata':{'component':{'type':'application','name':name,'version':VERSIONS[name]}},'components':components}
def main()->int:
 ap=argparse.ArgumentParser(); ap.add_argument('--out-dir',type=Path,default=ROOT/'sbom'); a=ap.parse_args(); out=a.out_dir if a.out_dir.is_absolute() else ROOT/a.out_dir; out.mkdir(parents=True,exist_ok=True); summary={}
 for name,path in LOCKS.items():
  components=parse(path); target=out/f'{name}.cdx.json'; target.write_text(json.dumps(bom(name,components,sha(path)),indent=2,sort_keys=True)+'\n'); summary[name]={'components':len(components),'file':str(target.relative_to(ROOT)),'sha256':sha(target)}
 artifacts=json.loads((ROOT/'artifacts-manifest.json').read_text()); comps=[]
 for item in artifacts['artifacts']:
  comp={'type':'file','name':item['id'],'version':item.get('version','unknown'),'properties':[{'name':'path','value':item['path']},{'name':'status','value':item.get('status','unknown')}]}
  if item.get('sha256'):comp['hashes']=[{'alg':'SHA-256','content':item['sha256']}]
  comps.append(comp)
 target=out/'external-artifacts.cdx.json'; target.write_text(json.dumps(bom('floorplan-external-artifacts',comps,sha(ROOT/'artifacts-manifest.json')),indent=2,sort_keys=True)+'\n'); summary['external-artifacts']={'components':len(comps),'file':str(target.relative_to(ROOT)),'sha256':sha(target)}
 print(json.dumps(summary,indent=2,sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
