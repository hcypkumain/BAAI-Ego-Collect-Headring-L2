#!/usr/bin/env python3
"""Bounded hardware + original QC test on SD; never modifies used.yaml."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src/orangepi_collect/common'))
from project_paths import BASE, SRC
import storage_paths


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=10)
    parser.add_argument('--slots',nargs='+',required=True)
    args=parser.parse_args()
    if not 1<=args.seconds<=300:parser.error('seconds must be within 1..300')
    root=storage_paths.diagnostics_root()/('wrist-'+datetime.now().strftime('%Y-%m-%d-%H-%M-%S'))
    storage_paths.validate_output(root)
    root.mkdir(parents=True,exist_ok=False)
    (root/'device_selection.json').write_text(json.dumps({'selected':args.slots}))
    (root/'meta.json').write_text(json.dumps({'test':True,'purpose':'wrist integration; no ledger update'}))
    print(str(root),flush=True)
    with (root/'worker.log').open('w') as log:
        process=subprocess.Popen([str(BASE/'.venvs/framework/bin/python'),str(SRC/'framework/framework_recorder.py'),
                                  '--output',str(root)],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,text=True)
        try:
            deadline=time.monotonic()+35
            while time.monotonic()<deadline:
                status=root/'status.json'
                state=json.loads(status.read_text()) if status.exists() else {}
                if state.get('state')=='ready':break
                if process.poll() is not None:raise RuntimeError('worker exited during preparation')
                time.sleep(.2)
            else:raise RuntimeError('preparation timeout')
            process.stdin.write('START\n');process.stdin.flush()
            end=time.monotonic()+args.seconds
            while time.monotonic()<end and process.poll() is None:
                time.sleep(.1)
            if process.poll() is None:
                try:process.stdin.write('STOP\n');process.stdin.flush()
                except BrokenPipeError:pass  # Capture failure is reported by its saved summary.
            code=process.wait(timeout=600)
            summary=json.loads((root/'capture_summary.json').read_text())
            print(json.dumps({'session':str(root),'exit':code,'summary':summary},ensure_ascii=False),flush=True)
            return code
        finally:
            if process.poll() is None:
                try:
                    process.stdin.write('STOP\n');process.stdin.flush();process.wait(timeout=45)
                except (BrokenPipeError,subprocess.TimeoutExpired):
                    process.terminate()
                    try:process.wait(timeout=10)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
            try:process.stdin.close()
            except BrokenPipeError:pass


if __name__=='__main__':sys.exit(main())
