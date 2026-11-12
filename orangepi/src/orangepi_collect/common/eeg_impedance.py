"""Pre-recording impedance state owned by the EEG read/command loop."""
from collections import deque
from dataclasses import asdict
import copy
import math
from pathlib import Path
import time
import uuid
from impedance_estimator import (ALGORITHM, SDK_REFERENCE, WINDOW_SECONDS,
    DATA_TIMEOUT_SECONDS, NORMAL_RECOVERY_MAX_SAMPLES,
    estimate_channel_reference, select_recent_positions,
    series_resistance_kohm)
from impedance_record import append_event, save_preflight, fingerprint
from impedance_quality import assess_impedance, acceptance_policy, quality_allows_recording, review_eligibility

PHASES = ('starting','measuring','restoring','resuming')


class Preflight:
    def __init__(self, board, config, output, emit, required):
        self.board, self.config, self.output, self.emit, self.required = board, config, output, emit, required
        frontend = board.get_config()
        self.mask = frontend.enabled_mask
        self.rows = deque(maxlen=1000)
        self.last_publish = 0
        self.measure_generation = None
        self.window_first_position = None
        self.last_received_at = None
        self.storage_error = None
        self.state = dict(required=required,supported=required,
            impedance_acceptance_policy=acceptance_policy(config),
            phase='not_checked' if required else 'unavailable',can_record=not required,
            active=False,pending=False,error=None,reviewed=False,request_id=None,
            owner_id=uuid.uuid4().hex, measurement_epoch=None,completed_at=None,updated_at=None,
            completed_persisted=False,saved_measurement_epoch=None,saved_owner_id=None,saved_config_fingerprint=None,
            loff_off_confirmed=False if required else None,
            frontend_restored=True if required else None,
            deadline_monotonic=None,
            series_kohm=series_resistance_kohm(frontend) if required else None,
            input_side=frontend.reference, algorithm=ALGORITHM,
            sdk_reference=copy.deepcopy(SDK_REFERENCE),
            provenance='OmniBCI 0.2.0.dev0 supplied SDK; Python source 0b8c342; SDK 843013ba62f1f05098c8639a657585720ce66a72',
            window_seconds=WINDOW_SECONDS, sample_rate_hz=250,
            window_required_positions=1000, window_positions=0,
            window_received_samples=0, window_observed_span=0,
            window_ready=False, window_status='waiting', window_progress=0.0,
            normal_frame_confirmed=False, expected_channel_mask=self.mask, stale=False,
            channels=self._channels())
        if required: board.on_state=self.update
        self.publish()

    def _channels(self):
        return [dict(channel=i+1,label=self.config['labels'][i],enabled=bool(self.mask & (1<<i)),
            value_kohm=None,reason='waiting' if self.mask&(1<<i) else 'disabled',accepted_samples=0)
            for i in range(8)]

    def _clear_window(self, reason='waiting'):
        self.rows.clear()
        self.window_first_position = None
        self.last_received_at = None
        self.last_publish = 0
        channels = self._channels()
        for channel in channels:
            if channel['enabled']:
                channel['reason'] = reason
        self.state.update(channels=channels, window_ready=False,
            window_status=reason, window_progress=0.0, window_positions=0,
            window_received_samples=0, window_observed_span=0,
            window_start_position=None, window_end_position=None,
            window_first_sequence=None, window_last_sequence=None,
            updated_at=None, last_sample_monotonic_ns=None, clipped_mask=0,
            stale=reason in ('stale', 'delivery_outage'))

    def _estimate_window(self):
        """Final four seconds by sample positions, never by received row count."""
        rows = list(self.rows)
        channels = self._channels()
        if not rows:
            return
        last_position = rows[-1][4]
        observed_span = last_position-self.window_first_position+1
        first_position = max(self.window_first_position, last_position-999)
        positions = last_position-first_position+1
        ready = observed_span >= 1000
        clipped = 0
        for i, channel in enumerate(channels):
            if not channel['enabled']:
                continue
            arrays = select_recent_positions([r[0][i] for r in rows], [r[1] for r in rows],
                [r[2] for r in rows], [r[3][i] for r in rows], sample_rate_hz=250)
            result = estimate_channel_reference(arrays[0], arrays[1], sample_rate_hz=250,
                series_kohm=self.state['series_kohm'], valid=arrays[2], raw_counts=arrays[3])
            channel.update(value_kohm=result.value_kohm, reason=result.reason,
                           accepted_samples=result.accepted_samples,
                           fitted_peak_uv=result.amplitude_uv,dc_offset_uv=result.dc_uv)
            if result.reason == 'adc_saturated':
                clipped |= 1 << i
            if not ready and result.value_kohm is not None:
                channel.update(value_kohm=None, preview_value_kohm=result.value_kohm,
                               reason='pending_window')
        self.state.update(channels=channels, updated_at=time.time(),
            measurement_generation=self.measure_generation,
            window_positions=positions, window_received_samples=len(rows),
            window_observed_span=observed_span, window_ready=ready,
            window_status='ready' if ready else 'pending_window',
            window_progress=min(1.0, observed_span/1000.0),
            window_start_position=first_position, window_end_position=last_position,
            window_first_sequence=rows[0][1], window_last_sequence=rows[-1][1],
            clipped_mask=clipped, stale=False)

    def _require_completed_window(self):
        # Recompute the newest data even if the 500 ms UI publish throttle has
        # not expired. A cached earlier valid preview cannot be finalized.
        if self.state['phase'] != 'measuring' or self.last_received_at is None:
            raise RuntimeError('阻抗检测尚未满4秒，请等待完整窗口后结束检测')
        if time.monotonic()-self.last_received_at >= DATA_TIMEOUT_SECONDS:
            self.stale()
            raise RuntimeError('阻抗数据已过期，请等待新的完整4秒检测窗口')
        self._estimate_window()
        if not self.state['window_ready']:
            raise RuntimeError('阻抗检测尚未满4秒，请等待完整窗口后结束检测')

    def _confirm_normal_frame(self, started_at):
        """Find a fresh valid normal frame in the existing device-owner thread.

        This occurs before formal START. It is intentionally not sent to Pump
        or a recording writer, and is never counted a second time.
        """
        deadline = time.monotonic()+3.0
        consumed = 0
        self.state['normal_recovery_samples_consumed'] = 0
        while consumed < NORMAL_RECOVERY_MAX_SAMPLES:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                break
            batch = self.board.read(1, timeout=remaining)
            if batch is None or not len(batch):
                continue
            consumed += len(batch)
            self.state['normal_recovery_samples_consumed'] = consumed
            frontend = self.board.get_config()
            if (len(batch) != 1 or batch.sample_rate_hz != 250
                    or batch.eeg_uv.shape != (1, 8)
                    or batch.valid.shape != (1,) or batch.mode.shape != (1,)
                    or getattr(frontend, 'verified', None) is not True
                    or frontend.reference != self.state['input_side']
                    or frontend.enabled_mask != self.mask
                    or frontend.mode not in ('both_bias', 'eeg', 'no_bias')):
                raise RuntimeError('正常脑电配置或数据形状发生变化，禁止开始采集')
            if (not bool(batch.valid[0])
                    or batch.generation == self.measure_generation
                    or not isinstance(batch.received_at, (int, float))
                    or not math.isfinite(batch.received_at)
                    or batch.received_at < started_at
                    or time.monotonic()-batch.received_at >= DATA_TIMEOUT_SECONDS
                    or int(batch.mode[0]) != ('both_bias', 'eeg', 'no_bias', 'shorted', 'test').index(frontend.mode)
                    or any(not math.isfinite(float(batch.eeg_uv[0, i])) for i in range(8)
                           if self.mask & (1 << i))):
                continue
            self.state.update(normal_frame_confirmed=True,
                normal_frame_generation=int(batch.generation),
                normal_frame_received_at=float(batch.received_at),
                normal_frame_monotonic_ns=time.monotonic_ns(),
                normal_recovery_sample_usage='preview readiness only; before formal START')
            return
        raise RuntimeError('正常脑电新帧未确认，禁止开始采集')

    def update(self, **value):
        # Restoration is not normal acquisition readiness. Keep the health pause
        # bounded until the owner starts and receives a fresh normal-mode frame.
        if value.get('phase')=='restored':
            value.update(phase='resuming',pending=True,deadline_monotonic=time.monotonic()+7)
        self.state.update(value)
        if self.state['phase'] in PHASES or self.state['phase']=='fault': self.state['can_record']=False
        if self.output and self.state.get('measurement_epoch'):
            # A disk failure must never interrupt the hardware's LOFF-off and
            # original-configuration restoration transaction.
            self._event('transition', durable=True, strict=False)
        self.publish()

    def publish(self):
        self._refresh_quality()
        self.emit('impedance',state=copy.deepcopy(self.state))

    def _refresh_quality(self):
        self.state['expected_channel_mask'] = self.mask
        self.state['quality'] = assess_impedance(self.state, expected_mask=self.mask)
        self.state.update(review_eligibility(self.state))
        if self.required and not quality_allows_recording(self.state):
            self.state['can_record'] = False

    def _event(self, event, durable=False, stream='attempts', strict=True):
        self._refresh_quality()
        try:
            append_event(self.output, self.state, event, durable=durable, stream=stream)
        except Exception as exc:
            self.storage_error = str(exc)
            self.state.update(can_record=False, reviewed=False, completed_persisted=False, phase='fault', error=str(exc))
            self.publish()
            if strict:
                raise RuntimeError('impedance evidence write failed: '+str(exc)) from exc

    def _save(self, event='snapshot'):
        if not self.output:
            if event=='completed':raise RuntimeError('阻抗结果无法持久化，不能确认')
            return
        frontend=asdict(self.board.get_config())
        self.state['config_fingerprint']=fingerprint(frontend)
        self._event(event, durable=True)
        try:
            value=save_preflight(self.output,self.state,frontend,
                {'port':self.config.get('port'),
                 'configured_usb_serial':self.config.get('serial'),
                 'firmware_identity_readback_available':False})
        except Exception as exc:
            self.storage_error = str(exc)
            self.state.update(can_record=False,reviewed=False,completed_persisted=False,phase='fault',error=str(exc))
            self.publish()
            raise RuntimeError('impedance evidence write failed: '+str(exc)) from exc
        self.state.update(config_fingerprint=value['config_fingerprint'],
                          schema_version=value['schema_version'],session_id=value['session_id'])
        if event=='completed':
            self.state.update(completed_persisted=True,saved_measurement_epoch=self.state['measurement_epoch'],
                saved_owner_id=self.state['owner_id'],saved_config_fingerprint=value['config_fingerprint'])

    def handle(self, job):
        if not self.required: raise RuntimeError('impedance backend is unavailable')
        command=job.get('command')
        if command=='IMPEDANCE_START':
            if self.state['phase'] in PHASES: raise RuntimeError('measurement transition already in progress')
            if self.state['phase']=='fault': raise RuntimeError('recover the device before another check')
            frontend = self.board.get_config()
            series = series_resistance_kohm(frontend)
            self.mask = frontend.enabled_mask
            self._clear_window()
            self.measure_generation = None
            self.state.update(phase='starting',can_record=False,reviewed=False,error=None,
                channels=self._channels(),request_id=job.get('request_id'),
                measurement_epoch=uuid.uuid4().hex,completed_at=None,
                completed_persisted=False,saved_measurement_epoch=None,saved_owner_id=None,saved_config_fingerprint=None,
                updated_at=None,last_sample_monotonic_ns=None,samples_seen=None,reviewed_at=None,
                measurement_started_at=time.time(),measurement_started_monotonic_ns=time.monotonic_ns(),
                completed_monotonic_ns=None,reviewed_monotonic_ns=None,
                series_kohm=series, input_side=frontend.reference,
                normal_frame_confirmed=False, normal_frame_monotonic_ns=None,
                normal_frame_generation=None, normal_frame_received_at=None,
                last_command_error=None,
                frontend_restored=False,loff_off_confirmed=False)
            try:
                self._save('start')
                self.board.begin_impedance(self.mask)
                if self.storage_error:
                    self.board.end_impedance()
                    self.board.start(timeout=3)
                    raise RuntimeError('impedance evidence write failed: '+self.storage_error)
            except Exception as exc:
                self.state.update(phase='fault',error=str(exc),can_record=False,reviewed=False)
                self.publish()
                try: self._save('fault')
                except RuntimeError: pass
                raise
        elif command=='IMPEDANCE_END':
            if job.get('measurement_epoch')!=self.state.get('measurement_epoch'):
                raise RuntimeError('阻抗测量已变化，请刷新后重试')
            if self.state['phase'] not in PHASES and not self.board.restore_required: return
            self._require_completed_window()
            self.state['last_command_error'] = None
            try:
                self.board.end_impedance()
                self.update(phase='resuming',pending=True,active=False,deadline_monotonic=time.monotonic()+7)
                resume_started_at = time.monotonic()
                self.board.start(timeout=3)
                self._confirm_normal_frame(resume_started_at)
                if self.storage_error:
                    raise RuntimeError('impedance evidence write failed: '+self.storage_error)
                self.state.update(phase='completed',pending=False,deadline_monotonic=None,
                    completed_at=time.time(),completed_monotonic_ns=time.monotonic_ns(),
                    can_record=False,reviewed=False)
                self._save('completed'); self.publish()
            except Exception as exc:
                self.state.update(phase='fault',error=str(exc),can_record=False,reviewed=False)
                self.publish()
                try: self._save('fault')
                except RuntimeError: pass
                raise
        elif command=='IMPEDANCE_REVIEW':
            if job.get('measurement_epoch')!=self.state.get('measurement_epoch'):
                raise RuntimeError('阻抗测量已变化，请刷新后重新确认')
            frontend=asdict(self.board.get_config())
            if (fingerprint(frontend)!=self.state.get('saved_config_fingerprint')
                    or frontend.get('enabled_mask')!=self.mask):
                self.state.update(completed_persisted=False,reviewed=False,can_record=False)
                self.publish()
                raise RuntimeError('脑电配置已变化，请重新检测')
            self._refresh_quality()
            eligibility=review_eligibility(self.state)
            if not eligibility['can_review']:
                self.state.update(reviewed=False, can_record=False)
                self.publish()
                raise RuntimeError('阻抗结果暂不可确认：'+','.join(eligibility['reason_codes']))
            self.state.update(last_command_error=None,reviewed=True,reviewed_at=time.time(),
                reviewed_monotonic_ns=time.monotonic_ns(),can_record=True)
            try: self._save('review')
            except Exception:
                self.state.update(reviewed=False,can_record=False)
                self.publish();raise
            self.publish()
        else: raise ValueError('unknown impedance command')

    def observe(self, batch):
        if self.state['phase']!='measuring': return
        now = time.monotonic()
        received = batch.received_at
        if (not isinstance(received, (int, float)) or not math.isfinite(received)
                or now-received >= DATA_TIMEOUT_SECONDS):
            self.stale()
            return
        if self.measure_generation is not None and batch.generation!=self.measure_generation:
            self._clear_window('generation_changed')
        elif self.last_received_at is not None and received-self.last_received_at >= DATA_TIMEOUT_SECONDS:
            self._clear_window('delivery_outage')
        self.measure_generation=batch.generation
        if (batch.sample_rate_hz!=250 or batch.eeg_uv.shape!=(len(batch),8)
                or batch.raw_counts.shape != (len(batch),8)
                or batch.sequence.shape != (len(batch),)
                or batch.valid.shape != (len(batch),)
                or batch.sample_indices.shape != (len(batch),)):
            raise RuntimeError('impedance profile changed')
        for y,s,ok,raw,position in zip(batch.eeg_uv,batch.sequence,batch.valid,batch.raw_counts,batch.sample_indices):
            position = int(position)
            if position < 0 or (self.rows and position <= self.rows[-1][4]):
                raise RuntimeError('non-forward impedance sample positions')
            if self.window_first_position is None:
                self.window_first_position = position
            self.rows.append((y.copy(),int(s),bool(ok),raw.copy(),position))
        if not self.rows:
            return
        lower = self.rows[-1][4]-999
        while self.rows and self.rows[0][4] < lower:
            self.rows.popleft()
        self.last_received_at = received
        self.state.update(last_sample_monotonic_ns=time.monotonic_ns(),samples_seen=self.board.samples)
        if now-self.last_publish<.5: return
        self.last_publish=now
        self._estimate_window()
        self._event('measurement', stream='samples')
        self.publish()

    def check_completed_binding(self, batch):
        """Invalidate frozen evidence on a changed live owner/configuration generation."""
        if self.state.get('phase')!='completed' or not self.state.get('completed_persisted'):return
        frontend=asdict(self.board.get_config())
        if (fingerprint(frontend)!=self.state.get('saved_config_fingerprint')
                or frontend.get('enabled_mask')!=self.mask
                or getattr(batch,'generation',None)!=self.state.get('normal_frame_generation')):
            self.state.update(completed_persisted=False,reviewed=False,can_record=False,
                phase='interrupted',error='脑电连接或配置已变化，请重新检测')
            self.publish()

    def stale(self):
        if self.state['phase']=='measuring':
            self._clear_window('stale')
            self.publish()

    def finish(self, error=None):
        """Retain aborted checks; never rewrite a proof already used by START."""
        if not self.output or not self.state.get('measurement_epoch'):
            return
        if (Path(self.output)/'start_gate.json').exists():
            self._event('acquisition_error' if error else 'acquisition_closed', durable=True)
            return
        self.state.update(phase='fault' if error else 'cancelled', can_record=False,
                          reviewed=False, error=error or self.state.get('error'))
        self._save('fault' if error else 'cancelled')
        self.publish()
