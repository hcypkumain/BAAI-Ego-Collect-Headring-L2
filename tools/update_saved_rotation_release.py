#!/usr/bin/env python3
"""Freeze reviewed collector inputs for the default deployment builder."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
BASELINE='083155214907c55dc61d78fb34d65b29d12f2b82'
sha=lambda data:hashlib.sha256(data).hexdigest()

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--baseline-tree',type=Path,
     help='Offline exported baseline for development; production normally reads pinned Git objects')
 args=parser.parse_args()
 prefix='orangepi/src/orangepi_collect'
 if args.baseline_tree:
  old={p.relative_to(args.baseline_tree).as_posix():p.read_bytes() for p in (args.baseline_tree/prefix).rglob('*')
       if p.is_file() and p.suffix in ('.py','.js','.html','.css')}
 else:
  paths=subprocess.check_output(['git','-C',str(ROOT),'ls-tree','-r','--name-only',BASELINE,prefix],text=True).splitlines()
  old={p:subprocess.check_output(['git','-C',str(ROOT),'show',BASELINE+':'+p]) for p in paths if p.endswith(('.py','.js','.html','.css'))}
 current={p.relative_to(ROOT).as_posix():p.read_bytes() for p in (ROOT/prefix).rglob('*')
          if p.is_file() and p.suffix in ('.py','.js','.html','.css')}
 if len(old)!=123 or set(old)!=set(current):raise SystemExit('Collector membership differs from pinned 123-file baseline')
 directory=ROOT/'deployment/phone-usb-entry';overrides={}
 for path in sorted(old):
  if old[path]==current[path]:continue
  target=directory/'collector-overrides'/path;target.parent.mkdir(parents=True,exist_ok=True)
  target.write_bytes(current[path])
  overrides[path]=dict(pre_sha256=sha(old[path]),post_sha256=sha(current[path]),size=len(current[path]))
 profile=dict(schema=1,baseline_commit=BASELINE,
              rotation=dict(block_size=5,count_basis='saved',progress_version=4,preserve_existing_n=True),
              overrides=overrides)
 (directory/'collector-release.json').write_text(json.dumps(profile,sort_keys=True,indent=2)+'\n')
 print(json.dumps({'collector_files':len(current),'frozen_overrides':len(overrides),'profile':str(directory/'collector-release.json')}))

if __name__=='__main__':main()
