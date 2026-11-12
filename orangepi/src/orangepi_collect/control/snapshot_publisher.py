"""Explicit offline publisher; no samplers, timers, workers or lease renewal."""
from dataclasses import dataclass
import json
import threading
import time
from uuid import UUID, uuid4
from snapshot_contract import normalize, loads, aged


def boottime_ms():
    return int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000)


@dataclass(frozen=True)
class Published:
    body: bytes
    etag: str
    published_ms: int


class SnapshotPublisher:
    def __init__(self, device_id, boot_id, controller_instance=None, clock=boottime_ms):
        self.clock = clock
        self.lock = threading.Lock()
        self.reset(device_id, boot_id, controller_instance or str(uuid4()))

    def reset(self, device_id, boot_id, controller_instance=None):
        instance = controller_instance or str(uuid4())
        UUID(boot_id); UUID(instance)
        normalize(device_id, boot_id=boot_id, controller_instance=instance)
        with self.lock:
            if getattr(self,'controller_instance',None)==instance:
                raise ValueError('lifecycle must allocate a new controller instance')
            self.device_id, self.boot_id, self.controller_instance = device_id, boot_id, instance
            self.seq = 0
            self.pointer = None  # No old owner/measurement can survive a lifecycle.

    def publish(self, evidence=None, *, origin_instance=None):
        with self.lock:
            if origin_instance is not None and origin_instance != self.controller_instance:
                raise ValueError('retired controller event')
            now = self.clock()
            value = normalize(self.device_id, evidence, self.boot_id, self.controller_instance)
            if (value['device_id'], value['boot_id'], value['controller_instance']) != (self.device_id, self.boot_id, self.controller_instance):
                raise ValueError('publisher identity mismatch')
            # Measurement-bearing events require an explicitly bound origin.
            if value['health']['impedance']['measurement_id'] is not None and origin_instance != self.controller_instance:
                raise ValueError('unbound measurement')
            value['seq'] = self.seq + 1
            value['time']['monotonic_ms'] = now
            def timestamp_groups(obj):
                if not isinstance(obj,dict):return
                if 'sampled_mono_ms' in obj and obj['sampled_mono_ms'] is not None and obj['boot_id']==self.boot_id:
                    if obj['sampled_mono_ms']>now:raise ValueError('future cached event')
                    obj['age_ms']=now-obj['sampled_mono_ms']
                    if obj['quality']=='known' and obj['age_ms']>obj['stale_after_ms']:
                        obj.update(quality='stale',reason='SAMPLE_EXPIRED')
                for child in obj.values():timestamp_groups(child)
            timestamp_groups(value)
            value['sample'].update(boot_id=self.boot_id, sampled_mono_ms=now, age_ms=0, quality='known', reason=None)
            from snapshot_contract import validate
            validate(value)
            body = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
            if len(body) > 65536: raise ValueError('snapshot size budget')
            item = Published(body, '"%s:%d"' % (self.controller_instance, value['seq']), now)
            self.seq = value['seq']
            self.pointer = item  # One immutable atomic pointer replacement.
            return item

    def read(self):
        return self.pointer

    def observation(self):
        item = self.read()
        return None if item is None else aged(loads(item.body), self.clock() - item.published_ms)
