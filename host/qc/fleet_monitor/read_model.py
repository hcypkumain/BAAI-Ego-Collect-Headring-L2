"""The existing Hub's in-memory v1 projection. Explicit events only.

No production poller, persistence, legacy fallback, or receiver Store access.
Observation tickets are allocated by this host BEFORE a request; never accept
a generation/order supplied by a device. A transport must authenticate identity.
"""
import copy
import json
import queue
import re
import threading
from uuid import uuid4
from snapshot_publisher import boottime_ms
from snapshot_contract import aged, validate
from snapshot_adapter import device_snapshot, restore_stale


class ReadModel:
    def __init__(self, devices, host_id='qc-host', clock=boottime_ms, restored=None):
        if len(set(devices)) != len(devices) or len(devices) > 64 or any(not re.fullmatch('cp_[0-9]{4}', d) for d in devices): raise ValueError('registered devices')
        self.clock, self.host_id = clock, host_id
        self.host_instance = str(uuid4())
        self.revision = 0
        self.lock = threading.RLock()
        self.clients = set()
        self.issued = {d: 0 for d in devices}
        self.tickets = set()
        self.order = {d: 0 for d in devices}
        self.retired = {d: set() for d in devices}
        self.received = {}
        self.rows = {d: dict(device_id=d, identity_verified=None, connectivity='unknown', last_seen_utc=None,
                             last_seen_age_ms=None, reason='NOT_OBSERVED', snapshot=None) for d in devices}
        self.pipeline = dict(source='host_receiver_projection', observed_at=None, age_ms=None, quality='unknown',
                             receiver_pending=None, qc_pending=None, normalized_pending=None, cloud_pending=None,
                             failed_attempts=None, cloud_receipt_verified=None, cloud_live_verified_at=None,
                             day_basis='unknown', reason='NOT_OBSERVED')
        self.storage = dict(volume_id=None, state='unknown', disk_available_bytes=None, age_ms=None,
                            independent_copy_verified=None, reason='NOT_OBSERVED')
        if restored:
            validate(restored, 'host')
            for row in restored['devices']:
                d = row['device_id']
                if d in self.rows:
                    self.rows[d] = dict(copy.deepcopy(row), connectivity='unknown', last_seen_age_ms=None, reason='HOST_RESTART',
                                        snapshot=restore_stale(row['snapshot']) if row['snapshot'] else None)

    def begin_observation(self, device):
        with self.lock:
            # Phase4 transport must enforce one in-flight per device.
            self.issued[device] += 1
            ticket = (self.host_instance, device, self.issued[device])
            if len(self.tickets) >= 128: raise ValueError('inflight budget')
            self.tickets.add(ticket)
            return ticket

    def accept(self, ticket, raw, *, identity_verified, received_utc=None, snapshot_age_ms=None):
        device = ticket[1]
        value = device_snapshot(raw, device)
        if snapshot_age_ms is None:
            value = restore_stale(value)
        elif type(snapshot_age_ms) is not int or snapshot_age_ms < 0:
            raise ValueError('snapshot response age')
        else:
            value = aged(value, snapshot_age_ms)
        with self.lock:
            if ticket not in self.tickets: return False
            self.tickets.remove(ticket)
            if identity_verified is not True: return False
            order = ticket[2]
            previous = self.rows[device]['snapshot']
            instance = (value['boot_id'], value['controller_instance'])
            old = (previous['boot_id'], previous['controller_instance']) if previous else None
            if old and old[1] == instance[1] and old[0] != instance[0]:
                raise ValueError('controller instance reused across boot')
            if instance in self.retired[device]: return False
            if old == instance:
                if value['seq'] <= previous['seq']: return False
            elif order <= self.order[device]: return False
            if old and old != instance:
                # Bound retirement memory: refuse further new instances at budget,
                # rather than forgetting one and accepting a late retired response.
                if len(self.retired[device]) >= 128: raise ValueError('instance budget')
                self.retired[device].add(old)
            self.order[device] = max(self.order[device], order)
            self.received[device] = self.clock()
            self.rows[device] = dict(device_id=device, identity_verified=True, connectivity='reachable',
                last_seen_utc=received_utc, last_seen_age_ms=0, reason='INSTANCE_CHANGED' if old and old != instance else None, snapshot=value)
            self._changed()
            return True

    def unavailable(self, device):
        with self.lock:
            self.rows[device].update(connectivity='unreachable', reason='SOURCE_UNAVAILABLE')
            self._changed()  # Never change recording_active or lifecycle.

    def source_unavailable(self):
        with self.lock:
            self.pipeline.update(quality='error', reason='SOURCE_UNAVAILABLE')
            self._changed()  # No raw scan fallback.

    def _changed(self):
        self.revision += 1
        for client in tuple(self.clients):
            event = dict(event='revision', id=self.event_id, data=dict(host_instance=self.host_instance, revision=self.revision))
            try: client.put_nowait(event)
            except queue.Full:
                while True:
                    try: client.get_nowait()
                    except queue.Empty: break
                client.put_nowait(dict(event='reset', id=self.event_id, data=event['data']))

    @property
    def event_id(self): return '%s:%d' % (self.host_instance, self.revision)

    def subscribe(self, last_event_id=None):
        with self.lock:
            if len(self.clients) >= 16: raise ValueError('subscriber budget')
            client = queue.Queue(maxsize=8)
            self.clients.add(client)
            client.put_nowait(dict(event='reset', id=self.event_id, data=dict(host_instance=self.host_instance, revision=self.revision)))
            return client

    def unsubscribe(self, client):
        with self.lock: self.clients.discard(client)

    def snapshot(self):
        with self.lock:
            now = self.clock()
            rows = copy.deepcopy(list(self.rows.values()))
            for row in rows:
                received = self.received.get(row['device_id'])
                if received is not None:
                    elapsed = max(0, now - received)
                    row['last_seen_age_ms'] = elapsed
                    row['snapshot'] = aged(row['snapshot'], elapsed)
                    if elapsed > 10000 and row['connectivity'] == 'reachable':
                        row.update(connectivity='unknown', reason='OBSERVATION_EXPIRED')
            return dict(schema_version='1.0', host_id=self.host_id, host_instance=self.host_instance, revision=self.revision,
                        time=dict(utc=None, utc_trusted=None, monotonic_ms=now), devices=rows,
                        pipeline=copy.deepcopy(self.pipeline), storage=copy.deepcopy(self.storage),
                        policy=dict(read_only=True, device_deletion_authorized=False, host_deletion_authorized=False))
