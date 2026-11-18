#!/usr/bin/env python3
"""BaseRecorder-compatible composite adapter: native ROS worker plus framework QC."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0,str(_Path(__file__).resolve().parents[1]/'common'))
import project_paths
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from project_paths import BASE, PROJECT, SRC, devices
sys.path.insert(0,str(BASE.parent/'src'))
from loguru import logger
from embodied_brain_collect.recorders.base import BaseRecorder,BaseRecorderConfig
from framework_qc import atomic_json,standardize,check_session,performance


def impedance_request(line):
    """Bounded control envelope; measurement END is never the global STOP."""
    if not isinstance(line,str) or not line.startswith('{') or len(line)>512:return None
    try:value=json.loads(line)
    except ValueError:return None
    if (not isinstance(value,dict) or set(value)-{'command','request_id'}
            or value.get('command') not in ('IMPEDANCE_START','IMPEDANCE_END','IMPEDANCE_REVIEW')
            or not isinstance(value.get('request_id'),str) or not 0<len(value['request_id'])<=64):return None
    return json.dumps(value)

def publish_capture_completion(path, process, summary):
    """Publish only after this session's native recorder has fully exited."""
    code=process.poll()
    if (type(code) is not int or code < 0 or summary.get('shutdown_complete') is not True
            or (code == 0 and summary.get('status') != 'success')
            or (code != 0 and summary.get('status') != 'error')):return False
    try:
        receipt=json.loads((path/'stop_gate.json').read_text())
        if receipt.get('worker_pid') != process.pid:return False
        if receipt.get('stop_monotonic_ns') != summary.get('stop_monotonic_ns'):return False
        receipt.update(native_reaped=True, native_exit_code=code, shutdown_complete=True,
            capture_finalized=True, summary_status=summary['status'],
            finalized_monotonic_ns=time.monotonic_ns())
        atomic_json(path/'capture_complete.json',receipt)
        return True
    except (OSError,ValueError,TypeError):
        return False  # Absence of a receipt keeps the display locked.

@dataclass
class PiBundleConfig(BaseRecorderConfig):
    hardware_python: str = str(BASE/'.venvs/hardware/bin/python')
    stdin_control: bool = False
    wait_for_save: bool = False

class PiBundleRecorder(BaseRecorder):
    """Implements the existing run/_open/_record/_close and stop_event contract.

    The native worker owns hardware. The adapter forwards stdin START/STOP as
    the explicit Pi controller protocol; request_commit and stop_event also work.
    No existing session launcher or stimulus implementation is loaded.
    """
    name='pi_ros_emg_imu'
    output_dir='.'

    def _init_logging(self):
        self.logger=logger.bind(name=self.name)
        self._log_sink_id=None
        if self.config.session_dir:
            root=Path(self.config.session_dir);root.mkdir(parents=True,exist_ok=True)
            self._log_sink_id=logger.add(root/'embodied_recorder.log',
                filter=lambda record:record['extra'].get('name')==self.name,encoding='utf-8')

    def _open(self):
        self.process=None
        path=Path(self.config.session_dir)
        # Own pipe makes inherited BaseRecorder.request_commit/stop_event usable,
        # while the keyboard controller can send the same two commands.
        self.process=subprocess.Popen([self.config.hardware_python,'-u',str(SRC/'acquisition/capture.py'),
            '--output',str(path),'--selection',str(path/'device_selection.json')],stdin=subprocess.PIPE,text=True,
            env=dict(os.environ,ORANGEPI_DEVICE_SELECTION=str(path/'device_selection.json')))
        return True

    def _recording_signal(self):
        path=Path(self.config.session_dir)/'status.json'
        try:
            state=json.loads(path.read_text())
            return int(state.get('samples_seen',0))
        except (OSError,ValueError):
            return 0

    def _close(self):
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.stdin.write('STOP\n');self.process.stdin.flush()
                self.process.wait(timeout=45)
            except (BrokenPipeError,subprocess.TimeoutExpired):
                self.process.terminate()
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        if self.process is not None and self.process.stdin:
            self.process.stdin.close()

    def _record(self):
        import queue,threading
        commands=queue.Queue()
        def read_commands():
            for line in sys.stdin:commands.put(line.strip())
            commands.put('CANCEL')
        if self.config.stdin_control:
            threading.Thread(target=read_commands,daemon=True).start()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM,lambda *a:self.stop_event.set())
            signal.signal(signal.SIGINT,lambda *a:self.stop_event.set())
        committed=False
        commit_at=0
        sent_stop=False
        decision=None
        while self.process.poll() is None:
            try:command=commands.get(timeout=.05)
            except queue.Empty:command=None
            if self.stop_event.is_set() and not sent_stop:command='STOP'
            if committed and self.config.duration>0 and time.monotonic()-commit_at>=self.config.duration and not sent_stop:command='STOP'
            if self._commit_evt.is_set() and not committed:command='START'
            if command in ('DISCARD','CANCEL'):
                decision=command
                command='STOP' if not sent_stop else None
            elif command=='KEEP':
                if sent_stop:decision='KEEP'
                command=None
            if command in ('START','STOP'):
                self.process.stdin.write(command+'\n');self.process.stdin.flush()
                if command=='START':
                    committed=True;self._committed=True;commit_at=time.monotonic()
                if command=='STOP':sent_stop=True
            elif not committed and not sent_stop:
                request=impedance_request(command)
                if request is not None:
                    self.process.stdin.write(request+'\n');self.process.stdin.flush()
        code=self.process.returncode
        path=Path(self.config.session_dir)
        summary_path=path/'capture_summary.json'
        summary=json.loads(summary_path.read_text()) if summary_path.is_file() else dict(status='error',errors=['Native capture ended without a summary'])
        atomic_json(path/'native_capture_summary.json',summary)
        # The stop gate only stops accepting new samples. Publish a separate
        # receipt after the native process has exited and its writers have
        # closed successfully; bind it to the original privacy proof.
        completion_published=(publish_capture_completion(path,self.process,summary)
                              if summary_path.is_file() else False)
        if not code and summary.get('status')=='success' and not completion_published:
            summary['status']='error'
            summary.setdefault('errors',[]).append('Native STOP/reap completion proof is missing or invalid')
            atomic_json(summary_path,summary)
            atomic_json(path/'status.json',dict(state='error',pid=os.getpid(),time=time.time(),
                recording_active=False,errors=summary['errors']))
            raise RuntimeError('Native STOP/reap completion proof is missing or invalid')
        # Native STOP closes all readers/encoders. Keep the adapter alive while
        # the operator decides, without starting standardization or QC.
        if self.config.wait_for_save and not code and summary.get('status')!='cancelled':
            if not completion_published:
                summary['status']='error'
                summary.setdefault('errors',[]).append('Native STOP/reap completion proof is missing; save decision is unavailable')
                atomic_json(summary_path,summary)
                atomic_json(path/'status.json',dict(state='error',pid=os.getpid(),time=time.time(),
                    recording_active=False,errors=summary['errors']))
                raise RuntimeError('Native STOP/reap completion proof is missing; save decision is unavailable')
            atomic_json(path/'status.json',dict(state='awaiting_decision',pid=os.getpid(),time=time.time(),recording_active=False))
            while decision is None:
                if (path/'discard_request.json').is_file():decision='DISCARD';break
                if self.stop_event.is_set():decision='CANCEL';break
                try:command=commands.get(timeout=.1)
                except queue.Empty:continue
                if command in ('KEEP','DISCARD','CANCEL'):decision=command
        # A durable discard marker also covers a decision made as the native
        # process exits, or before its queued command is consumed.
        request_path=path/'discard_request.json'
        if request_path.is_file():
            request=json.loads(request_path.read_text())
            if request.get('session_id')!=path.name or request.get('directory')!=str(path.resolve()):
                raise RuntimeError('Discard request belongs to a different segment')
            decision='DISCARD'
        if decision in ('DISCARD','CANCEL'):
            summary.update(capture_status=summary.get('status'),status='discarded' if decision=='DISCARD' else 'cancelled',
                qc_skipped=True,decision=decision,recording_active=False)
            atomic_json(summary_path,summary)
            atomic_json(path/'status.json',dict(state=summary['status'],pid=os.getpid(),time=time.time(),recording_active=False,qc_skipped=True))
            return
        if code:raise RuntimeError('Native acquisition failed; see capture_summary.json')
        if summary['status']=='cancelled':return
        from qc_deferred import save_pending
        mode=json.loads((path/'meta.json').read_text(encoding='utf-8')).get('qc_mode','immediate')
        if mode in ('deferred','emg_only'):
            partial=None;eeg_partial=None
            if mode=='emg_only':
                from framework_qc import standardize_emg,standardize_marker,check_emg,check_eeg,standardize_eeg,atomic_npz
                began=time.time()
                atomic_json(path/'status.json',dict(state='postprocessing',pid=os.getpid(),time=time.time(),processing_started=began,qc_scope='emg_only',recording_active=False))
                summary=standardize_emg(path)
                selected=json.loads((path/'device_selection.json').read_text())['selected']
                if 'eeg' in selected:standardize_eeg(path,summary,atomic_npz)
                standardize_marker(path,summary)
                def emg_progress(value):
                    atomic_json(path/'status.json',dict(state='checking',pid=os.getpid(),time=time.time(),processing_started=began,progress=value,qc_scope='emg_only',recording_active=False))
                if any(name in selected for name in ('emg_left','emg_right')):partial=check_emg(path,emg_progress)
                if 'eeg' in selected:eeg_partial=check_eeg(path,emg_progress)
                summary['emg_qc_elapsed_seconds']=time.time()-began
            save_pending(path,summary,BASE,PROJECT,partial,eeg_partial)
            return
        # Acquisition exited: bounded parallel QC, with CPU/memory headroom.
        allowed=os.sched_getaffinity(0)
        settings=performance()
        os.sched_setaffinity(0,set(settings['qc_cpu_ids']) & allowed or {max(allowed)})
        os.nice(5)
        qc_started=time.monotonic()
        import resource
        limit=int(settings['qc_address_space_mb'])*1024*1024
        resource.setrlimit(resource.RLIMIT_AS,(limit,limit))
        processing_started=time.time()
        atomic_json(path/'status.json',dict(state='postprocessing',pid=os.getpid(),time=time.time(),processing_started=processing_started))
        summary=standardize(path)
        summary['standardize_elapsed_seconds']=time.monotonic()-qc_started
        # QC reads this file; commit STOP-boundary-adjusted counts before validation.
        atomic_json(path/'capture_summary.json',summary)
        # Runs after all serial/video handles have been closed.
        def progress(value):
            atomic_json(path/'status.json',dict(state='checking',pid=os.getpid(),time=time.time(),processing_started=processing_started,progress=value))
        progress({'completed':0,'total':0,'active':[]})
        report=check_session(path,progress=progress)
        summary['qc_elapsed_seconds']=time.monotonic()-qc_started
        summary['capture_status']='success'
        summary['qc_level']=report.level
        summary['qc_raw_level']=getattr(report,'qc_raw_level',report.level)
        summary['qc_policy']=getattr(report,'qc_policy','original')
        summary['framework']='BaseRecorder + original timestamp_rebuild + original qc_session/checkers'
        summary['status']='qc_error' if report.level=='ERROR' else 'success'
        if report.level=='ERROR':
            summary['errors'].append('质量检验发现 ERROR；请在录制结果中查看具体原因，确认保留未成功数据或重采。')
        atomic_json(path/'capture_summary.json',summary)
        meta_path=path/'meta.yaml'
        import yaml
        # Reference image names are Chinese, so meta.yaml must be read and written
        # as UTF-8 explicitly. Without encoding= these calls follow the service
        # locale, which the unit file does not pin; a C/POSIX locale would mangle
        # the task name. framework_qc.py already writes this file as UTF-8.
        meta=yaml.safe_load(meta_path.read_text(encoding='utf-8'))
        meta.update(status=summary['status'],qc_level=report.level,
                    qc_raw_level=summary['qc_raw_level'],qc_policy=summary['qc_policy'])
        from orangepi_collect.storage.ledger import atomic_yaml
        from orangepi_collect.storage.durability import sync_session
        atomic_yaml(meta_path,meta)
        proof=sync_session(path)
        summary['storage_durability']=proof;meta['storage_durability']=proof
        atomic_json(path/'capture_summary.json',summary);atomic_yaml(meta_path,meta)
        atomic_json(path/'status.json',dict(state=summary['status'],pid=os.getpid(),time=time.time(),
                errors=summary['errors'],qc_level=report.level,
                qc_raw_level=summary['qc_raw_level'],qc_policy=summary['qc_policy'],storage_durability=proof))
        if summary['status']=='success':
            try:
                from capture_qc_receipt import write as write_qc_receipt
                write_qc_receipt(path,expected_input_signature=getattr(report,'pi_qc_input_signature',None))
            except (OSError,ValueError,TypeError,KeyError,RecursionError) as exc:
                summary['status']='qc_error'
                summary.setdefault('errors',[]).append('本机完整质检回执未能生成：'+str(exc)[:240])
                meta['status']='qc_error';meta['qc_level']='ERROR'
                atomic_json(path/'capture_summary.json',summary);atomic_yaml(meta_path,meta)
                atomic_json(path/'status.json',dict(state='qc_error',pid=os.getpid(),time=time.time(),
                    errors=summary['errors'],qc_level='ERROR',qc_policy=summary.get('qc_policy'),storage_durability=proof))

    def run(self):
        result=super().run()
        self._close()
        if result:
            path=Path(self.config.session_dir)
            summary_path=path/'capture_summary.json'
            summary=json.loads(summary_path.read_text()) if summary_path.exists() else {'errors':[]}
            if summary.get('status')=='success' or not summary_path.exists():
                summary['status']='error'
                summary.setdefault('errors',[]).append('Framework adapter failed; inspect embodied_recorder.log')
                atomic_json(summary_path,summary)
            atomic_json(path/'status.json',dict(state='error',pid=os.getpid(),time=time.time(),
                errors=summary.get('errors',[])))
        return result

if __name__=='__main__':
    from scan_registry import require_member
    require_member()
    ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--wait-for-save',action='store_true');args=ap.parse_args()
    rec=PiBundleRecorder(PiBundleConfig(session_dir=str(Path(args.output).resolve()),stdin_control=True,wait_for_save=args.wait_for_save))
    sys.exit(rec.run())
