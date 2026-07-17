#!/usr/bin/env python3
"""Verify sealed external build/runtime artifacts against artifacts-manifest.json."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

IGNORED = {'.gitkeep', 'README.md'}

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(8*1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def tree_digest(path: Path) -> tuple[int,int,str]:
    files=[]
    for p in sorted(path.rglob('*')):
        if p.is_file() and p.name not in IGNORED:
            rel=p.relative_to(path).as_posix(); files.append((rel,p.stat().st_size,sha256_file(p)))
    h=hashlib.sha256()
    for rel,size,digest in files:
        h.update(f'{rel}\0{size}\0{digest}\n'.encode())
    return len(files), sum(x[1] for x in files), h.hexdigest()

def verify(root: Path, item: dict) -> None:
    p=root/item['path']; kind=item.get('kind','')
    if kind.endswith('directory'):
        if not p.is_dir(): raise SystemExit(f"missing directory artifact: {item['id']} ({p})")
        count,size,digest=tree_digest(p)
        if count == 0: raise SystemExit(f"empty directory artifact: {item['id']} ({p})")
        if item.get('file_count') != count or item.get('size_bytes') != size or item.get('sha256') != digest:
            raise SystemExit(f"directory artifact mismatch/unsealed: {item['id']}")
    else:
        if not p.is_file(): raise SystemExit(f"missing file artifact: {item['id']} ({p})")
        if item.get('size_bytes') != p.stat().st_size or item.get('sha256') != sha256_file(p):
            raise SystemExit(f"file artifact mismatch/unsealed: {item['id']}")

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument('--root',type=Path,default=Path('.')); ap.add_argument('--manifest',type=Path,required=True); ap.add_argument('--artifact',action='append',required=True)
    a=ap.parse_args(); data=json.loads(a.manifest.read_text()); by={x['id']:x for x in data['artifacts']}
    for artifact_id in a.artifact:
        if artifact_id not in by: raise SystemExit(f'unknown artifact: {artifact_id}')
        verify(a.root.resolve(),by[artifact_id])
    print(json.dumps({'verified':a.artifact},sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
