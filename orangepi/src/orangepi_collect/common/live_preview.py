"""Bounded previews; recording callbacks never wait on the preview lock or IO.

Lock contention skips only a preview update. Recording buffers and the common
START/STOP gate are owned by the acquisition code and are not changed here.
"""
import collections
import json
import math
import os
from pathlib import Path
import threading
import time
from display_demand import DemandReader
try:
    from wrist_preview_transport import Producer as WristPreviewProducer, DualProducer
except ImportError:
    WristPreviewProducer = None
    DualProducer = None

CAMERAS = ('cam_left_wrist', 'cam_right_wrist', 'cam_ros_left', 'cam_ros_right', 'cam_ros_bleft', 'cam_ros_bright')
# Upper bound on published frames per camera, not the intended rate: the
# producer sets the pace. At 0.33 s this gate allowed only ~3 fps, and at 0.125 s
# it still quantised against the preview worker's 0.12 s rounds -- a round
# arriving a few milliseconds early was rejected and the next accepted frame came
# a whole round later, halving the delivered rate to ~4.2 fps. Keeping the gate
# well below the producer's period makes it a safety valve again.
#
# Headband frames are produced by the preview worker's timed rounds, so their
# gate only needs to stay out of the way. Wrist frames arrive from a 30 Hz UVC
# callback and reach the viewer through the owner lease transport, so their file
# copy is a fallback and is kept at a modest rate instead of writing every frame.
# Recording keeps priority either way: a contended lock skips the preview update
# instead of waiting.
HEADBAND_PUBLISH_INTERVAL = .02
WRIST_PUBLISH_INTERVAL = .125
CAMERA_PUBLISH_INTERVAL = HEADBAND_PUBLISH_INTERVAL
# How often the writer thread flushes pending frames to the publisher directory.
# This is the real ceiling on what the viewer sees: at 0.2 s every camera, of
# either kind, updated at exactly 4.8 fps regardless of how fast it was sampled.
# camd serialises SNAPSHOT at ~33 ms per camera, so four headband cameras cannot
# exceed ~8.8 fps however this is set; 0.1 s sits under that ceiling and leaves
# the per-camera gates above as the binding limits.
FILE_PUBLISH_PERIOD = .025
SIGNAL_PUBLISH_PERIOD = .1


def camera_publish_interval(name):
    return (WRIST_PUBLISH_INTERVAL if name in ('cam_left_wrist', 'cam_right_wrist')
            else HEADBAND_PUBLISH_INTERVAL)
EMGS = ('emg_left', 'emg_right')
EEGS = ('eeg',)
SIGNALS = EMGS + EEGS
STREAMS = CAMERAS + SIGNALS


class Publisher:
    def __init__(self, directory=None):
        directory = directory or os.environ.get('ORANGEPI_PREVIEW_DIR')
        self.directory = Path(directory) if directory else None
        self.demand=DemandReader(self.directory) if self.directory else None
        self.pending = {}; self.last_camera = {}; self.signals = {}
        self.last_signal_publish = 0.
        self.emg_context = {}
        self.skipped_updates = collections.Counter()
        self.lock = threading.Lock(); self.stop = threading.Event()
        self.wrist_tap = None
        self.dual_wrist_tap = None
        self.wrist_tap_errors = 0
        if self.directory:
            if WristPreviewProducer is not None:
                # Scheduling headroom for a 30 Hz UVC source avoids dropping a
                # frame just because consecutive callbacks jitter around 33 ms.
                try:self.wrist_tap = WristPreviewProducer(max_fps=40)
                except Exception:self.wrist_tap_errors += 1
            if DualProducer is not None:
                try:self.dual_wrist_tap = DualProducer(max_fps=40)
                except Exception:self.wrist_tap_errors += 1
            self.directory.mkdir(parents=True, exist_ok=True)
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def bind_emg_sources(self, preview_id, sources):
        # Bound before serial readers start; never infer body side from a port.
        with self.lock:
            self.emg_context = {slot: dict(preview_id=preview_id, source_path=path)
                                for slot, path in sources.items() if slot in EMGS}

    def wants_signal(self,name):
        return bool(self.directory and (self.demand is None or name in self.demand.signals))

    def wants_cameras(self):
        return bool(self.directory and (self.demand is None or self.demand.cameras))

    def wants_camera(self,name):
        return bool(self.directory and (self.demand is None or name in self.demand.camera_names))

    def wants_full_camera(self,name):
        if not self.wants_camera(name):return False
        if self.demand is None:return False
        heads=self.demand.camera_names.intersection(('cam_ros_left','cam_ros_right','cam_ros_bleft','cam_ros_bright'))
        return name in self.demand.camera_detail_names or len(heads)==1

    def bind_wrist_sources(self, preview_id, sources):
        # Idle owner calls this before opening UVC sources. Recording never binds
        # this separate calibration tap and cannot supply calibration evidence.
        if self.dual_wrist_tap is not None:
            try:self.dual_wrist_tap.bind(preview_id, sources)
            except (ValueError, TypeError):pass

    def camera(self, name, payload):
        if not self.directory or name not in CAMERAS: return
        # Optional viewer receives the existing UVC owner's latest JPEG before
        # legacy throttling. This path never opens a camera or performs IO.
        if self.wrist_tap is not None and name in ('cam_left_wrist','cam_right_wrist'):
            try:self.wrist_tap.offer(name,payload)
            except Exception:self.wrist_tap_errors += 1
        if self.dual_wrist_tap is not None and name in ('cam_left_wrist','cam_right_wrist'):
            try:self.dual_wrist_tap.offer(name,payload)
            except Exception:self.wrist_tap_errors += 1
        if not self.wants_camera(name):return
        now = time.monotonic();interval = camera_publish_interval(name)
        if now-self.last_camera.get(name, 0) < interval: return
        image = bytes(payload)
        if not self.lock.acquire(False):
            self.skipped_updates[name] += 1
            return
        try:
            if now-self.last_camera.get(name, 0) < interval: return
            self.last_camera[name] = now
            self.pending[name+'.jpg'] = image
        finally:
            self.lock.release()

    def emg(self, name, frame):
        if not self.directory or name not in EMGS or len(frame) != 29 or frame[3] != 0xaa: return
        if self.demand is not None and name not in self.demand.signals:return
        values = [int.from_bytes(frame[i:i+3], 'big', signed=True) for i in range(5,29,3)]
        now = time.time()
        if not self.lock.acquire(False):
            self.skipped_updates[name] += 1
            return
        try:
            item = self.signals.setdefault(name, dict(rows=collections.deque(maxlen=100), bucket=None, count=0, updated=now))
            if now-item['updated']>1:item['rows'].clear();item['bucket']=None
            item['count'] += 1; item['updated'] = now
            bucket = item['bucket']
            if bucket is None or now-bucket[0] >= .02:
                if bucket is not None: item['rows'].append(bucket)
                item['bucket'] = [now, [[x,x] for x in values]]
            else:
                for bounds, value in zip(bucket[1], values):
                    bounds[0] = min(bounds[0], value); bounds[1] = max(bounds[1], value)
        finally:
            self.lock.release()

    def _snapshot(self, include_signals=True):
        # Completed envelope rows are no longer modified by producers. Only
        # copy their bounded list here; encode JSON after releasing the lock.
        with self.lock:
            pending, self.pending = self.pending, {}
            values = {}
            if not include_signals:return pending,values
            for name, item in self.signals.items():
                if self.demand is not None and name not in self.demand.signals:continue
                value = dict(time=item['updated'], count=item['count'], rows=list(item['rows']),
                    units='raw signed 24-bit ADC counts', envelope_ms=20,
                    preview_skipped_updates=self.skipped_updates[name])
                value.update(item.get('metadata', {}))
                if 'trace' in item:value['trace']=list(item['trace'])
                value.update(self.emg_context.get(name, {}))
                values[name] = value
        return pending, values

    def _publish_once(self):
        now=time.monotonic();include_signals=now-self.last_signal_publish>=SIGNAL_PUBLISH_PERIOD
        pending, values = self._snapshot(include_signals)
        if include_signals:self.last_signal_publish=now
        for name, value in values.items():
            pending[name+'.json'] = json.dumps(value, separators=(',',':'), allow_nan=False).encode()
        for name, data in pending.items():
            try:
                path = self.directory/name; tmp = self.directory/(name+'.'+str(os.getpid())+'.tmp')
                tmp.write_bytes(data); os.replace(str(tmp), str(path))
            except OSError:
                pass  # A preview failure must not interrupt the recorder.

    def _run(self):
        legacy_at = time.monotonic() + FILE_PUBLISH_PERIOD
        while True:
            now = time.monotonic()
            if self.demand is not None:self.demand.refresh(now)
            delay = max(.001,legacy_at-now)
            if self.wrist_tap is not None:
                delay = min(delay,self.wrist_tap.poll_interval)
            if self.dual_wrist_tap is not None:
                delay = min(delay,self.dual_wrist_tap.poll_interval)
            if self.stop.wait(delay):break
            now = time.monotonic()
            if self.wrist_tap is not None:
                try:self.wrist_tap.tick(now)
                except Exception:self.wrist_tap_errors += 1
            if self.dual_wrist_tap is not None:
                try:self.dual_wrist_tap.tick(now)
                except Exception:self.wrist_tap_errors += 1
            if now >= legacy_at:
                self._publish_once()
                legacy_at = time.monotonic() + FILE_PUBLISH_PERIOD

    def eeg(self, values, valid, labels, preset, wall, *, sample_indices=None,
            generation=None, raw_values=None, mode='eeg', adc_counts=None):
        if not self.directory: return
        if self.demand is not None and 'eeg' not in self.demand.signals:return
        bounds = []
        for ch in range(8):
            column = [float(row[ch]) for row, ok in zip(values, valid) if ok and math.isfinite(float(row[ch]))]
            bounds.append([min(column), max(column)] if column else [None, None])
        # A bounded, derived 250Hz trace makes the 31.25Hz test excitation
        # visible. Never alter the raw recorder or the impedance estimator.
        trace=[];count=len(values);start=max(0,count-500)
        adc_bounds=[]
        if adc_counts is not None:
            for ch in range(8):
                column=[int(row[ch]) for row,ok in zip(adc_counts,valid) if ok and math.isfinite(float(row[ch]))]
                adc_bounds.append([min(column),max(column)] if column else [None,None])
        for i in range(start,count):
            def channel_values(source):
                return [round(float(source[i][ch]),3) if bool(valid[i]) and math.isfinite(float(source[i][ch])) else None for ch in range(8)]
            trace.append([round(wall-(count-1-i)/250.,6),channel_values(values),
                channel_values(raw_values) if raw_values is not None else None,
                int(sample_indices[i]) if sample_indices is not None else None])
        if not self.lock.acquire(False):
            self.skipped_updates['eeg'] += 1
            return
        try:
            item = self.signals.setdefault('eeg', dict(rows=collections.deque(maxlen=20), count=0, updated=wall))
            previous=item.get('metadata',{})
            if (previous.get('mode'),previous.get('generation'),previous.get('filter_preset'))!=(mode,generation,preset):
                item['rows'].clear();item['trace']=collections.deque(maxlen=500)
            item.setdefault('trace',collections.deque(maxlen=500)).extend(trace)
            item['count'] += len(values); item['updated'] = wall
            item['rows'].append([wall, bounds])
            item['metadata'] = dict(units='uV', envelope_ms=100, channel_labels=list(labels),
                physical_channels=list(range(1, 9)), filter_preset=preset, disabled_channels=[],
                mode=mode,generation=generation,sample_rate_hz=250,
                adc_count_bounds=adc_bounds or None,adc_bounds_scope='latest_preview_batch',
                trace_format=['unix_seconds','display_uv','raw_uv_or_null','sample_index_or_null'])
        finally:
            self.lock.release()

    def close(self):
        self.stop.set()
        if self.directory: self.thread.join(timeout=2)
        if self.wrist_tap is not None:
            try:self.wrist_tap.close()
            except Exception:self.wrist_tap_errors += 1
        if self.dual_wrist_tap is not None:
            try:self.dual_wrist_tap.close()
            except Exception:self.wrist_tap_errors += 1


_publisher = None
_instance_lock = threading.Lock()

def publisher():
    global _publisher
    if _publisher is None:
        with _instance_lock:
            if _publisher is None: _publisher = Publisher()
    return _publisher
