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
import tempfile
import time
import traceback
import yaml
from qc_deferred import atomic,digest,apply_result

_QC_MUTABLE_FILES=frozenset(('capture_summary.json','meta.yaml','status.json','qc.html'))


def _session_work_copy(source):
    """Create a private same-volume tree; large immutable streams are hardlinked."""
    source=Path(source).resolve()
    for current,dirs,files in os.walk(str(source),topdown=True,followlinks=False):
        for name in dirs+files:
            if (Path(current)/name).is_symlink():raise ValueError('Symlink in pending QC session')
    parent=Path(tempfile.mkdtemp(prefix='.'+source.name+'-qc-',dir=str(source.parent)))
    destination=parent/source.name
    def copy_input(src,dst):
        if Path(src).name in _QC_MUTABLE_FILES:return shutil.copy2(src,dst)
        try:
            os.link(src,dst);return dst
        except OSError:
            return shutil.copy2(src,dst)
    try:
        shutil.copytree(str(source),str(destination),copy_function=copy_input,symlinks=False)
        return parent,destination
    except Exception:
        shutil.rmtree(str(parent),ignore_errors=True)
        raise

def run_one(path,output,workers):
    source_path=Path(path).resolve()
    request=json.loads((source_path/'qc_pending.json').read_text())
    if request.get('session_id')!=source_path.name:raise ValueError('Pending QC request belongs to a different session')
    if digest(source_path/'native_capture_summary.json')!=request['native_sha256']:raise ValueError('Native capture fingerprint mismatch')
    if json.loads((source_path/'status.json').read_text()).get('recording_active'):raise ValueError('Recording is active')
    from required_device_contract import resolve_selection
    selection=json.loads((source_path/'device_selection.json').read_text())
    contract,contract_errors,legacy=resolve_selection(selection.get('device_config'),selection)
    if contract_errors or contract is None:
        raise ValueError('Invalid frozen device selection: '+'; '.join(contract_errors))
    if type(request.get('legacy_source')) is not bool or request['legacy_source']!=legacy:
        raise ValueError('QC_HOST_UPGRADE_REQUIRED: pending request lacks the current legacy-source binding')
    from capture_qc_receipt import verify_portable_input_signature,portable_input_signature
    if not verify_portable_input_signature(source_path,request.get('input_signature'),contract,require_marker=False):
        raise ValueError('Frozen capture inputs changed before host QC')
    raw_meta=json.loads((source_path/'meta.json').read_text(encoding='utf-8'))
    if request.get('reference_sha256')!=raw_meta.get('reference_sha256'):
        raise ValueError('Pending QC reference identity mismatch')
    os.environ['ORANGEPI_DEVICE_SELECTION']=str(source_path/'device_selection.json')
    os.environ['ORANGEPI_HOST_QC']='1';os.environ['ORANGEPI_HOST_QC_WORKERS']=str(workers)
    import framework_qc as qc
    qc.HEADBAND_RATE=qc._headband_rate()
    # Frozen per-session camera rate is also applied to the checker class.
    from embodied_brain_collect.checkers.checks import ts_checks,FrameCountMatch,BlackFrame,Freeze
    qc.PiCameraChecker.checks=[ts_checks('frames',expected_rate=qc.HEADBAND_RATE),FrameCountMatch(video='frames.mp4'),BlackFrame(video='frames.mp4'),Freeze(video='frames.mp4')]
    destination=Path(output)/source_path.name
    destination.mkdir(parents=True,exist_ok=True)
    result_path=destination/'result.json'
    if result_path.exists():
        previous=json.loads(result_path.read_text())
        if (previous.get('request_id')==request['request_id']
                and previous.get('input_signature')==request.get('input_signature')
                and all((destination/'artifacts'/rel).is_file() and digest(destination/'artifacts'/rel)==sha for rel,sha in previous['artifacts'].items())):return previous
    native=json.loads((source_path/'native_capture_summary.json').read_text())
    work_parent,work_path=_session_work_copy(source_path)
    try:
        source_chunks={};archived_before={}
        for name in ('emg_left','emg_right','eeg'):
            directory=work_path/name
            source_chunks[name]=sorted(p.name for p in directory.glob('chunk_*.npz')) if directory.is_dir() else []
            archived_before[name]=sorted(p.name for p in (directory/'raw_chunks').glob('chunk_*.npz')) if (directory/'raw_chunks').is_dir() else []
        atomic(work_path/'capture_summary.json',native)
        started=time.monotonic()
        def progress(value):
            atomic(Path(output)/'progress.json',dict(session_id=source_path.name,stage='checking',**value))
            print(json.dumps(dict(session_id=source_path.name,**value),ensure_ascii=False),flush=True)
        atomic(Path(output)/'progress.json',dict(session_id=source_path.name,stage='standardizing',started_at=time.time()))
        standardized_summary=qc.standardize(work_path)
        atomic(work_path/'capture_summary.json',standardized_summary)
        report=qc.check_session(work_path,html=True,progress=progress)
        artifacts={}
        names=['qc_report.json','qc_raw_report.json','qc_policy.json','qc_timing.json','qc.html']
        names += [str(p.relative_to(work_path)) for p in work_path.glob('*/*.npz') if not p.name.startswith('chunk_')]
        names += [str(p.relative_to(work_path)) for p in work_path.glob('*/timestamp_rebuild.json')]
        names += [str(p.relative_to(work_path)) for pattern in ('*/gate_finalization.json','*/eeg_summary.json','*/metadata.json') for p in work_path.glob(pattern)]
        for rel in names:
            source=work_path/rel
            if not source.exists():continue
            target=destination/'artifacts'/rel;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target);artifacts[rel]=digest(target)
        metadata=yaml.safe_load((work_path/'meta.yaml').read_text(encoding='utf-8')) or {}
        for key in ['session_dir','status','retention_state','retained_at_unix','reference_state','upload_state']:metadata.pop(key,None)
        derived_counts={}
        if isinstance(standardized_summary.get('eeg'),dict):
            derived_counts['eeg_samples']=standardized_summary['eeg'].get('samples')
        archived_chunks={}
        for name in ('emg_left','emg_right','eeg'):
            directory=work_path/name
            current=set(p.name for p in (directory/'raw_chunks').glob('chunk_*.npz')) if (directory/'raw_chunks').is_dir() else set()
            previous=set(archived_before.get(name,[]))
            archived_chunks[name]=sorted((set(source_chunks.get(name,[]))&current)-previous)
        result=dict(version=1,session_id=source_path.name,request_id=request['request_id'],native_sha256=request['native_sha256'],
                    reference_sha256=request['reference_sha256'],input_signature=request['input_signature'],
                    local_input_signature=request.get('local_input_signature'),
                    legacy_source=legacy,
                    checked_input_signature=portable_input_signature(work_path,contract,require_marker=True),
                    level=report.level,raw_level=getattr(report,'qc_raw_level',report.level),passed=report.level!='ERROR',
                    elapsed_seconds=time.monotonic()-started,metadata=metadata,
                    derived_counts=derived_counts,archived_chunks=archived_chunks,artifacts=artifacts)
        atomic(result_path,result);return result
    finally:
        shutil.rmtree(str(work_parent),ignore_errors=True)

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
