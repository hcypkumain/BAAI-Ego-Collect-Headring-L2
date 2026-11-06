#!/usr/bin/env python3
"""Explicit short EEG validation in diagnostics; never writes the task ledger."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(BASE/'src/orangepi_collect/common'))
from project_paths import SRC,devices
import storage_paths


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--seconds',type=float,default=10)
    args=ap.parse_args()
    if not 1<=args.seconds<=60:ap.error('Duration must be 1–60 seconds')
    config=devices()
    if not config['eeg'].get('discovery',{}).get('present'):raise RuntimeError('OmniBCI USB not found')
    root=storage_paths.diagnostics_root()/('eeg-integration-'+time.strftime('%Y%m%d-%H%M%S'))
    storage_paths.validate_output(root);root.mkdir(parents=True,exist_ok=False)
    (root/'device_selection.json').write_text(json.dumps({'selected':['eeg'],'device_config':config}),encoding='utf-8')
    (root/'meta.json').write_text(json.dumps({'test':True,'diagnostic':'EEG integration','selected_devices':['eeg'],'device_config':config}),encoding='utf-8')
    with (root/'worker.log').open('w') as log:
        process=subprocess.Popen([str(BASE/'.venvs/framework/bin/python'),'-u',str(SRC/'framework/framework_recorder.py'),
            '--output',str(root)],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,text=True)
        try:
            deadline=time.monotonic()+25
            while True:
                if process.poll() is not None:raise RuntimeError('Recorder exited during preparation; see '+str(root))
                try:status=json.loads((root/'status.json').read_text())
                except (OSError,ValueError):status={}
                if status.get('state')=='ready':break
                if time.monotonic()>deadline:raise TimeoutError('EEG preparation timeout; see '+str(root))
                time.sleep(.1)
            process.stdin.write('START\n');process.stdin.flush()
            until=time.monotonic()+args.seconds
            while time.monotonic()<until:
                if process.poll() is not None:raise RuntimeError('Recorder exited during acquisition; see '+str(root))
                time.sleep(.1)
            process.stdin.write('STOP\n');process.stdin.flush()
            code=process.wait(timeout=90)
        finally:
            if process.poll() is None:
                try:process.stdin.write('STOP\n');process.stdin.flush();process.wait(timeout=45)
                except (BrokenPipeError,subprocess.TimeoutExpired):process.terminate();process.wait(timeout=8)
            process.stdin.close()
    summary=json.loads((root/'capture_summary.json').read_text())
    result=dict(path=str(root),process_exit_code=code,status=summary['status'],qc_level=summary.get('qc_level'),
        eeg=summary.get('eeg'),errors=summary.get('errors'),no_task_ledger_written=True)
    (root/'eeg_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return 0 if code==0 and summary.get('capture_status',summary['status'])=='success' else 1


if __name__=='__main__':sys.exit(main())
