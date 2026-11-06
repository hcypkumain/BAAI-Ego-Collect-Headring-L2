#!/usr/bin/env python3
"""Run pending Pi QC on a Linux host; return verified reports and derived arrays."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'common'))
import project_paths
import argparse
import json
import os
import shutil
import time
import traceback
import yaml
from qc_deferred import atomic,digest,apply_result

def run_one(path,output,workers):
    request=json.loads((path/'qc_pending.json').read_text())
    if digest(path/'native_capture_summary.json')!=request['native_sha256']:raise ValueError('Native capture fingerprint mismatch')
    if json.loads((path/'status.json').read_text()).get('recording_active'):raise ValueError('Recording is active')
    os.environ['ORANGEPI_DEVICE_SELECTION']=str(path/'device_selection.json')
    os.environ['ORANGEPI_HOST_QC']='1';os.environ['ORANGEPI_HOST_QC_WORKERS']=str(workers)
    import framework_qc as qc
    qc.HEADBAND_RATE=qc._headband_rate()
    # Frozen per-session camera rate is also applied to the checker class.
    from embodied_brain_collect.checkers.checks import ts_checks,FrameCountMatch,BlackFrame,Freeze
    qc.PiCameraChecker.checks=[ts_checks('frames',expected_rate=qc.HEADBAND_RATE),FrameCountMatch(video='frames.mp4'),BlackFrame(video='frames.mp4'),Freeze(video='frames.mp4')]
    destination=output/path.name
    destination.mkdir(parents=True,exist_ok=True)
    result_path=destination/'result.json'
    if result_path.exists():
        previous=json.loads(result_path.read_text())
        if previous.get('request_id')==request['request_id'] and all((destination/'artifacts'/rel).is_file() and digest(destination/'artifacts'/rel)==sha for rel,sha in previous['artifacts'].items()):return previous
    native=json.loads((path/'native_capture_summary.json').read_text())
    atomic(path/'capture_summary.json',native)
    started=time.monotonic()
    def progress(value):
        atomic(output/'progress.json',dict(session_id=path.name,stage='checking',**value))
        print(json.dumps(dict(session_id=path.name,**value),ensure_ascii=False),flush=True)
    atomic(output/'progress.json',dict(session_id=path.name,stage='standardizing',started_at=time.time()))
    qc.standardize(path);report=qc.check_session(path,html=True,progress=progress)
    artifacts={}
    names=['qc_report.json','qc_raw_report.json','qc_policy.json','qc_timing.json','qc.html']
    names += [str(p.relative_to(path)) for p in path.glob('*/*.npz') if not p.name.startswith('chunk_')]
    names += [str(p.relative_to(path)) for p in path.glob('*/timestamp_rebuild.json')]
    for rel in names:
        source=path/rel
        if not source.exists():continue
        target=destination/'artifacts'/rel;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target);artifacts[rel]=digest(target)
    metadata=yaml.safe_load((path/'meta.yaml').read_text(encoding='utf-8')) or {}
    for key in ['session_dir','status','retention_state','retained_at_unix','reference_state','upload_state']:metadata.pop(key,None)
    result=dict(version=1,session_id=path.name,request_id=request['request_id'],native_sha256=request['native_sha256'],
                level=report.level,raw_level=getattr(report,'qc_raw_level',report.level),passed=report.level!='ERROR',
                elapsed_seconds=time.monotonic()-started,metadata=metadata,artifacts=artifacts)
    atomic(result_path,result);return result

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run');run.add_argument('sessions',type=Path);run.add_argument('--results',type=Path,required=True);run.add_argument('--workers',type=int,default=min(8,max(1,(os.cpu_count() or 2)//2)))
    apply=sub.add_parser('apply');apply.add_argument('results',type=Path);apply.add_argument('--sessions',type=Path,required=True)
    args=parser.parse_args();rows=[];errors=[]
    if args.command=='run':
        if not 1<=args.workers<=16:parser.error('--workers must be 1..16')
        args.results.mkdir(parents=True,exist_ok=True)
        paths=[args.sessions] if (args.sessions/'qc_pending.json').exists() else sorted(p.parent for p in args.sessions.glob('*/qc_pending.json'))
        for path in paths:
            if (path/'qc_applied.json').exists():continue
            try:rows.append(run_one(path.resolve(),args.results.resolve(),args.workers))
            except Exception as exc:
                errors.append(dict(session_id=path.name,error=str(exc)));traceback.print_exc()
        summary=dict(checked=len(rows),passed=sum(row['passed'] for row in rows),failed=sum(not row['passed'] for row in rows),execution_errors=errors,
                     results=[dict(session_id=row['session_id'],level=row['level'],passed=row['passed']) for row in rows])
        atomic(args.results/'batch-summary.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))
    else:
        from project_paths import BASE
        for path in sorted(args.results.glob('*/result.json')):
            try:rows.append(apply_result(path.parent,args.sessions,BASE))
            except Exception as exc:errors.append(dict(session_id=path.parent.name,error=str(exc)))
        print(json.dumps(dict(applied=rows,errors=errors),ensure_ascii=False,indent=2))
    return 1 if errors else 0

if __name__=='__main__':sys.exit(main())
