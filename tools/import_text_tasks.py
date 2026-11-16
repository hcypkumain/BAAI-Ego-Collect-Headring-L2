#!/usr/bin/env python3
"""Plan or append one verified text-task manifest to an EEG profile catalog.

The caller must hold the collector's capture lease for --apply. This script
never selects the pool or changes the active profile/preferences.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('plan','apply'));p.add_argument('--code-base',type=Path,required=True);p.add_argument('--eeg-base',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--sha256',required=True);p.add_argument('--expect-catalog-absent',action='store_true');a=p.parse_args()
    if not re.fullmatch(r'[0-9a-f]{64}',a.sha256):p.error('invalid digest')
    raw=a.manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=a.sha256:raise ValueError('manifest hash mismatch')
    code=a.code_base.resolve(strict=True);base=a.eeg_base.resolve(strict=True)
    if any(x.is_symlink() for x in (a.code_base,a.eeg_base)) or not (base/'runtime/reference_library').is_dir():raise ValueError('unsafe or uninitialized EEG profile')
    for folder in ('common','control','storage','framework','acquisition'):sys.path.insert(0,str(code/'src/orangepi_collect'/folder))
    from reference_catalog import ReferenceCatalog
    from text_task_import import _load_manifest,append
    data=_load_manifest(raw);catalog_path=base/'runtime/reference_library/catalog.json'
    if a.expect_catalog_absent and (catalog_path.exists() or catalog_path.is_symlink()):raise ValueError('EEG catalog no longer absent')
    catalog=ReferenceCatalog(base,project=base.parent);old=catalog._load()
    print(json.dumps({'action':a.action,'profile':'eeg','namespace':data['namespace'],'tasks':data['task_count'],'occurrences':data['total_target'],'existing_lists':len(old['lists']),'existing_items':len(old['images']),'selected_preferences_changed':False},ensure_ascii=False),flush=True)
    if a.action=='apply':print(json.dumps(append(catalog,raw,catalog.revision,require_catalog_absent=a.expect_catalog_absent),ensure_ascii=False),flush=True)
if __name__=='__main__':main()
