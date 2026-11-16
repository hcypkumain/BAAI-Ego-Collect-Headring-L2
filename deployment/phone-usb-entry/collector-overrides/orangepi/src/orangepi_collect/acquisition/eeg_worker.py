"""Headless Python 3.11 OmniBCI worker; no ROS/GUI import or second serial reader."""
import argparse
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'common'))
import numpy as np
from eeg_settings import PRESETS, validate
from live_preview import publisher

FIELDS = ('eeg_uv', 'raw_counts', 'sequence', 'valid', 'sample_indices', 'status', 'mode')
BASIS = 'host SDK read gate; sample_indices / 250 Hz; no measured hardware clock alignment'
EMIT_LOCK = threading.Lock()


def atomic_json(path, value):
    path = Path(path); tmp = path.with_suffix('.json.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush(); os.fsync(handle.fileno())
    tmp.replace(path)


def emit(event, **value):
    with EMIT_LOCK:
        print(json.dumps(dict(event=event, **value), ensure_ascii=False), flush=True)


class HostGate:
    """Read the native controller's gate, never substitute pipe delivery time."""
    def __init__(self, root):
        self.root = Path(root)
        self.start = None
        self.stop = None

    def accepts(self, begin, end, received):
        if self.start is None and (self.root/'start_gate.json').exists():
            self.start = json.loads((self.root/'start_gate.json').read_text())
        if self.stop is None and (self.root/'stop_gate.json').exists():
            self.stop = json.loads((self.root/'stop_gate.json').read_text())
        return (self.start is not None and begin >= self.start['start_monotonic_ns'] and
                received >= self.start['start_monotonic_ns'] and
                (self.stop is None or end < self.stop['stop_monotonic_ns']))


class Pump:
    """Only this thread reads/stops the SDK; bounded loss is always an error."""
    def __init__(self, board, preflight=None):
        self.board = board
        self.preflight = preflight
        self.commands = queue.Queue(maxsize=8)
        self.epoch = 0
        self.jobs = queue.Queue(maxsize=64)
        self.stop = threading.Event(); self.done = threading.Event()
        self.error = None; self.unqueued = 0; self.peak = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def publish(self, batch, begin):
        if batch is None or not len(batch): return
        if self.preflight and self.preflight.state['phase']=='measuring':
            self.preflight.observe(batch)
            if self.preflight.last_received_at==batch.received_at:
                publisher().eeg(batch.eeg_uv,batch.valid,self.preflight.config['labels'],'raw',time.time(),
                    sample_indices=batch.sample_indices,generation=int(batch.generation),mode='impedance',adc_counts=batch.raw_counts)
            return
        item = (batch, begin, time.monotonic_ns(), time.time_ns(), self.epoch)
        try: self.jobs.put(item, timeout=.05)
        except queue.Full:
            self.unqueued += len(batch)
            raise RuntimeError('EEG queue full; disk/filter is too slow; unsaved samples='+str(self.unqueued))
        self.peak = max(self.peak, self.jobs.qsize())

    def run(self):
        from omnibci import OmniBCIError, PartialReadError
        try:
            self.board.start(timeout=3)
            latest = time.monotonic()
            while not self.stop.is_set():
                try: job=self.commands.get_nowait()
                except queue.Empty: job=None
                if job:
                    if self.preflight and self.preflight.output and (Path(self.preflight.output)/'start_gate.json').exists():
                        self.preflight.state['last_command_error']='录制中不能执行阻抗操作'
                        self.preflight.publish()
                    else:
                        self.epoch+=1
                        while not self.jobs.empty():
                            try: self.jobs.get_nowait()
                            except queue.Empty: break
                        try: self.preflight.handle(job)
                        except (ValueError,RuntimeError) as exc:
                            if self.preflight.state['phase']=='fault': raise
                            self.preflight.state['last_command_error']=str(exc)
                            self.preflight.publish()
                        latest=time.monotonic()
                begin = time.monotonic_ns()
                try: batch = self.board.read(25, timeout=.25)
                except PartialReadError as exc: batch = exc.partial
                except OmniBCIError as exc:
                    self.publish(getattr(exc, 'partial', None), begin)
                    raise
                self.publish(batch, begin)
                if batch is not None and len(batch): latest = time.monotonic()
                if self.preflight and time.monotonic()-latest>2: self.preflight.stale()
                if time.monotonic()-latest > 3: raise TimeoutError('No EEG samples for 3 seconds')
        except Exception as exc: self.error = str(exc)
        finally:
            try:
                if self.preflight and getattr(self.board,'restore_required',False):
                    self.board.end_impedance()
                self.board.stop(timeout=3)
                # Buffered tails are retained by SDK; drain them with fresh host
                # delivery clocks, so samples delivered after STOP are excluded.
                deadline = time.monotonic()+1
                while time.monotonic() < deadline:
                    begin = time.monotonic_ns()
                    try: batch = self.board.read(timeout=.05)
                    except PartialReadError as exc: batch = exc.partial
                    if batch is None or not len(batch): break
                    self.publish(batch, begin)
            except Exception as exc: self.error = self.error or 'stop/drain: '+str(exc)
            try:
                if self.preflight:
                    self.preflight.finish(self.error)
            except Exception as exc:
                self.error = self.error or 'impedance evidence finalization: '+str(exc)
            self.done.set()


class Writer:
    def __init__(self, path, config, frontend, metadata):
        self.path = Path(path); self.path.mkdir(exist_ok=False)
        self.enabled = np.array([bool(frontend.enabled_mask & (1 << i)) for i in range(8)])
        from eeg_filter import FilterSettings, StreamingFilter
        preset = {k:v for k,v in PRESETS[config['filter_preset']].items() if k != 'label'}
        settings = FilterSettings(**preset)
        self.filter = StreamingFilter(settings, 250, self.enabled)
        self.metadata = dict(config=config, frontend_config=asdict(frontend), device=metadata,
            sample_rate_hz=250, channel_labels=config['labels'], channel_enabled=self.enabled.tolist(),
            filter=dict(settings.metadata(), preset=config['filter_preset'],
                        official_equivalence='not established; official MNE parameters unavailable',
                        sos_coefficients=self.filter.sos.tolist()), timestamp_basis=BASIS,
            gate_policy='whole SDK read batches: read began and SDK delivery at/after START; read completion before STOP; boundary batch may be excluded',
            status='preparing')
        self.parts = []; self.pending = self.count = self.chunks = self.invalid = self.missing = 0
        self.first = self.last = self.generation = None
        atomic_json(self.path/'metadata.json', self.metadata)

    def prepare(self, batch):
        if batch.sample_rate_hz != 250 or batch.eeg_uv.shape != (len(batch), 8):
            raise RuntimeError('Unexpected EEG sample rate/channel count')
        return self.filter.apply(batch.eeg_uv, batch.sample_indices, batch.valid)

    def append(self, batch, filtered, begin, mono, wall):
        idx = batch.sample_indices.astype(np.int64)
        if np.any(np.diff(idx) <= 0) or (self.last is not None and idx[0] <= self.last):
            raise RuntimeError('Non-increasing EEG sample indices')
        if self.generation is not None and self.generation != batch.generation:
            raise RuntimeError('SDK generation changed during recording')
        previous = idx[0]-1 if self.last is None else self.last
        self.missing += int(np.maximum(np.diff(np.r_[previous, idx])-1, 0).sum())
        self.first = int(idx[0]) if self.first is None else self.first
        self.last = int(idx[-1]); self.generation = batch.generation
        n = len(batch)
        arrays = {key:getattr(batch, key) for key in FIELDS}
        arrays.update(eeg_filtered_uv=filtered, read_begin_monotonic_ns=np.full(n, begin, np.int64),
            arrival_monotonic_ns=np.full(n, mono, np.int64), arrival_unix_ns=np.full(n, wall, np.int64),
            sdk_received_monotonic_ns=np.full(n, round(batch.received_at*1e9), np.int64))
        self.parts.append(arrays); self.pending += n; self.count += n
        self.invalid += int((~batch.valid).sum())
        if self.pending >= 500: self.flush()

    def flush(self):
        if not self.parts: return
        arrays = {key:np.concatenate([p[key] for p in self.parts]) for key in self.parts[0]}
        path = self.path/('chunk_%06d.npz' % self.chunks); tmp = path.with_suffix('.tmp')
        with tmp.open('xb') as handle:
            np.savez(handle, **arrays, sample_rate_hz=250, generation=self.generation,
                channel_enabled=self.enabled, channel_labels=np.array(self.metadata['channel_labels']),
                filter_preset=np.array(self.metadata['config']['filter_preset']), timestamp_basis=np.array(BASIS))
            handle.flush(); os.fsync(handle.fileno())
        tmp.replace(path)
        self.parts = []; self.pending = 0; self.chunks += 1
        self.metadata.update(status='recording', statistics=self.statistics())
        atomic_json(self.path/'metadata.json', self.metadata)

    def statistics(self):
        return dict(samples=self.count, chunks=self.chunks, invalid_samples=self.invalid,
            missing_samples=self.missing, first_sample_index=self.first, last_sample_index=self.last,
            generation=self.generation, sample_rate_hz=250, timestamp_basis=BASIS)

    def close(self, status, **details):
        self.flush()
        self.metadata.update(status=status, statistics=self.statistics(), **details)
        atomic_json(self.path/'metadata.json', self.metadata)
        return dict(self.statistics(), port=self.metadata['config']['port'],
            channel_labels=self.metadata['channel_labels'], channel_enabled=self.enabled.tolist(),
            frontend_config=self.metadata['frontend_config'], filter=self.metadata['filter'], **details)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--output', type=Path)
    mode = ap.add_mutually_exclusive_group(); mode.add_argument('--preview', action='store_true'); mode.add_argument('--probe', action='store_true')
    args = ap.parse_args(); config = validate(json.loads(sys.stdin.readline()))
    if not (args.output or args.preview or args.probe): ap.error('Choose output, preview or probe')
    base = Path(__file__).resolve().parents[3]
    (base/'runtime').mkdir(exist_ok=True)
    lock = (base/'runtime/eeg.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Every recorded EEG episode requires its own durable preflight. Preview stays opt-in.
    required=bool(args.output) or os.environ.get('OMNIBCI_ENABLE_IMPEDANCE_PREFLIGHT')=='1'
    official_sdk = (base/'runtime/official-sdk-0.2.0').is_dir()
    if official_sdk:
        sys.path.insert(0, str(base/'runtime/official-sdk-0.2.0'))
        from omnibci import Board
    elif required:
        from serial_v1_impedance import SerialV1Board as Board
    else:
        from omnibci import Board
    from eeg_frontend import enable_all_channels
    writer = pump = None; errors = []; summary = {}
    with Board.connect('serial://'+config['port'], timeout=5) as board:
        info = asdict(board.info) if board.info else {}
        if args.probe:
            board.start(timeout=3)
            try:
                batch = board.read(25, timeout=1)
                if batch.sample_rate_hz != 250 or batch.eeg_uv.shape != (25, 8):
                    raise RuntimeError('Unsupported EEG device profile')
                emit('probe', ready=True, sample_rate_hz=250, device=info,
                     startup_diagnostics=getattr(board,'startup_diagnostics',{}))
            finally: board.stop(timeout=3)
            return 0
        _, frontend = enable_all_channels(board)
        if frontend.enabled_mask != 255 or frontend.mode not in ('eeg', 'both_bias'):
            raise RuntimeError('Require EEG mode and active CH1–8; inspect frontend configuration')
        path = args.output/'eeg' if args.output else None
        if path: writer = Writer(path, config, frontend, info)
        else:
            # Coefficients are prepared before acquisition also in idle preview.
            from eeg_filter import FilterSettings, StreamingFilter
            preset = {k:v for k,v in PRESETS[config['filter_preset']].items() if k != 'label'}
            preview_filter = StreamingFilter(FilterSettings(**preset), 250, [True]*8)
        gate = HostGate(args.output) if args.output else None
        if official_sdk:
            from official_impedance import OfficialPreflight
            preflight=OfficialPreflight(board,config,args.output,emit,required)
        else:
            from eeg_impedance import Preflight
            preflight=Preflight(board,config,args.output,emit,required)
        pump = Pump(board,preflight); parent = os.getppid()
        signal.signal(signal.SIGTERM, lambda *a:pump.stop.set())
        signal.signal(signal.SIGINT, lambda *a:pump.stop.set())
        def commands():
            for line in sys.stdin:
                if line.strip() == 'STOP': break
                try:
                    job=json.loads(line)
                    if (isinstance(job,dict) and job.get('command') in
                            ('IMPEDANCE_START','IMPEDANCE_END','IMPEDANCE_REVIEW')):
                        pump.commands.put(job,timeout=.1)
                except (ValueError,queue.Full): continue
            pump.stop.set()
        threading.Thread(target=commands, daemon=True).start()
        seen = 0; last_heartbeat = 0; impedance_start_validated = False
        pump.thread.start()
        acquisition_begin = time.monotonic(); cpu_begin = time.process_time()
        try:
            while not pump.done.is_set() or not pump.jobs.empty():
                if os.getppid() != parent: pump.stop.set()
                if gate and (args.output/'stop_gate.json').exists(): pump.stop.set()
                try: batch, begin, mono, wall, epoch = pump.jobs.get(timeout=.1)
                except queue.Empty: continue
                if epoch!=pump.epoch: continue
                if batch.sample_rate_hz != 250: raise RuntimeError('EEG rate changed')
                seen += len(batch)
                if getattr(main,'filter_epoch',None)!=epoch:
                    target_filter=writer.filter if writer else preview_filter
                    target_filter.zi=None; target_filter.last_index=None
                    main.filter_epoch=epoch
                filtered = writer.prepare(batch) if writer else preview_filter.apply(batch.eeg_uv, batch.sample_indices, batch.valid)
                publisher().eeg(batch.eeg_uv if config['filter_preset']=='raw' else filtered,
                    batch.valid, config['labels'], config['filter_preset'], wall/1e9,
                    sample_indices=batch.sample_indices,generation=int(batch.generation),
                    raw_values=batch.eeg_uv if config['filter_preset']!='raw' else None,adc_counts=batch.raw_counts)
                if gate and gate.accepts(begin, mono, round(batch.received_at*1e9)):
                    if required:
                        if not preflight.state['can_record']:
                            raise RuntimeError('正式录制前必须完成阻抗检查并确认设备恢复')
                        if not impedance_start_validated:
                            from impedance_record import validate_start_binding
                            validate_start_binding(args.output, gate.start, config)
                            impedance_start_validated=True
                    writer.append(batch, filtered, begin, mono, wall)
                if not last_heartbeat or time.monotonic()-last_heartbeat >= 1:
                    emit('ready' if not last_heartbeat else 'heartbeat', samples_seen=seen,
                         last_sample_monotonic_ns=mono)
                    last_heartbeat = time.monotonic()
            if pump.error: raise RuntimeError(pump.error)
        except Exception as exc:
            errors.append(str(exc)); emit('error', error=str(exc))
        finally:
            pump.stop.set(); pump.thread.join(timeout=8)
            if pump.thread.is_alive(): errors.append('SDK reader did not stop')
            if pump.error and pump.error not in errors: errors.append(pump.error)
            import resource
            elapsed = time.monotonic()-acquisition_begin
            cpu = time.process_time()-cpu_begin
            details = dict(errors=errors, sdk_snapshot=asdict(board.snapshot),
                peak_pending_batches=pump.peak, unsaved_samples=pump.unqueued,
                performance=dict(acquisition_wall_seconds=elapsed, process_cpu_seconds=cpu,
                    cpu_percent_of_one_core=100*cpu/elapsed, max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
            if writer: summary = writer.close('error' if errors else 'closed', **details)
            else: summary = dict(samples_seen=seen, **details)
            publisher().close()
            emit('closed', summary=summary)
    return 1 if errors else 0


if __name__ == '__main__':
    from scan_registry import require_member
    require_member()
    try: sys.exit(main())
    except Exception as exc:
        emit('error', error=type(exc).__name__+': '+str(exc)); sys.exit(1)
