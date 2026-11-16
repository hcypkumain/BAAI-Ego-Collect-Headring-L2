"""Single-owner, one-shot official OmniBCI 0.2.0 impedance preflight."""
from dataclasses import asdict
import math
import time
import uuid
import omnibci
from eeg_impedance import Preflight
from impedance_estimator import series_resistance_kohm

SDK_REVISION = '843013ba62f1f05098c8639a657585720ce66a72'
ALGORITHM = 'omnibci-official-measure-0.2.0'

class OfficialPreflight(Preflight):
    def __init__(self, board, config, output, emit, required):
        super().__init__(board, config, output, emit, required)
        self.state.update(algorithm=ALGORITHM, sdk_revision=getattr(omnibci, 'SDK_REVISION', None),
                          sdk_reference=None, provenance='OmniBCI official Board.measure_impedance',
                          sdk_measurement_completed=False)
        self.publish()

    def _confirm_official_normal_frame(self, original, started):
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            batch = self.board.read(1, timeout=max(.01, deadline-time.monotonic()))
            if batch is None or len(batch) != 1:
                continue
            config = self.board.get_config()
            if asdict(config) != asdict(original) or not config.verified:
                raise RuntimeError('正常脑电配置恢复未确认')
            if (batch.sample_rate_hz == 250 and batch.eeg_uv.shape == (1, 8)
                    and batch.valid.shape == (1,) and bool(batch.valid[0])
                    and batch.mode.shape == (1,)
                    and int(batch.mode[0]) == ('both_bias','eeg','no_bias','shorted','test').index(config.mode)
                    and isinstance(batch.received_at, (int,float))
                    and math.isfinite(batch.received_at) and started <= batch.received_at <= time.monotonic()
                    and time.monotonic()-batch.received_at < 2
                    and all(math.isfinite(float(batch.eeg_uv[0,i])) for i in range(8) if self.mask & (1<<i))):
                self.state.update(normal_frame_confirmed=True, normal_frame_monotonic_ns=time.monotonic_ns(),
                    normal_frame_received_at=float(batch.received_at), normal_frame_generation=int(batch.generation),
                    normal_recovery_samples_consumed=1, normal_recovery_sample_usage='preview readiness only; before formal START')
                return
        raise RuntimeError('正常脑电新帧未确认，禁止开始采集')

    def handle(self, job):
        command = job.get('command')
        if command != 'IMPEDANCE_START':
            if command == 'IMPEDANCE_END' and self.state['phase'] == 'completed':
                return
            return super().handle(job)
        if not self.required or self.state['phase'] in ('starting','measuring','restoring','resuming','fault'):
            raise RuntimeError('当前不能开始阻抗检测')
        original = self.board.get_config()
        if getattr(omnibci, 'SDK_REVISION', None) != SDK_REVISION:
            raise RuntimeError('不支持的 OmniBCI SDK 版本，禁止开始检测')
        series = series_resistance_kohm(original)
        self.mask = original.enabled_mask
        self._clear_window()
        self.state.update(phase='starting', active=False, pending=True, deadline_monotonic=time.monotonic()+85,
            can_record=False, reviewed=False, error=None, request_id=job.get('request_id'),
            measurement_epoch=uuid.uuid4().hex, measurement_started_at=time.time(),
            measurement_started_monotonic_ns=time.monotonic_ns(), completed_at=None, completed_monotonic_ns=None,
            reviewed_at=None, reviewed_monotonic_ns=None, normal_frame_confirmed=False,
            frontend_restored=False, loff_off_confirmed=False, sdk_measurement_completed=False,
            sdk_result_mask=None, series_kohm=series, input_side=original.reference, last_command_error=None)
        self.publish()
        try:
            self._save('start')
            self.state.update(phase='measuring', active=True)
            self.publish()
            result = self.board.measure_impedance(mask=self.mask, window_seconds=4.0, timeout=15.0)
            if result.mask != self.mask or result.series_kohm != series or len(result.values_kohm) != 8:
                raise RuntimeError('官方 SDK 返回的检测配置不一致')
            restored = self.board.get_config()
            if not restored.verified or asdict(restored) != asdict(original):
                raise RuntimeError('检测后前端配置恢复未确认')
            rows = self._channels()
            for i,row in enumerate(rows):
                row['accepted_samples'] = None
                if not row['enabled']:
                    continue
                number = result.values_kohm[i]
                clipped = bool(result.clipped_mask & (1<<i))
                if number is None:
                    row.update(value_kohm=None, reason='adc_saturated' if clipped else 'invalid')
                elif clipped or not isinstance(number,(int,float)) or not math.isfinite(number) or number < 0:
                    raise RuntimeError('官方 SDK 返回的阻抗结果无效')
                else:
                    row.update(value_kohm=float(number), reason='ok')
            self.state.update(channels=rows, clipped_mask=result.clipped_mask, sdk_result_mask=result.mask,
                sdk_measurement_completed=True, window_ready=True, window_status='ready', window_progress=1.0,
                window_positions=None, window_received_samples=None, window_observed_span=None,
                updated_at=time.time(), stale=False, phase='resuming', active=False,
                loff_off_confirmed=True, frontend_restored=True)
            self.publish()
            started=time.monotonic()
            self.board.start(timeout=3)
            self._confirm_official_normal_frame(original, started)
            self.state.update(phase='completed', pending=False, deadline_monotonic=None,
                completed_at=time.time(), completed_monotonic_ns=time.monotonic_ns())
            self._save('completed'); self.publish()
        except Exception as exc:
            self.state.update(phase='fault', active=False, pending=False, deadline_monotonic=None,
                error=str(exc), can_record=False, reviewed=False)
            self.publish()
            try: self._save('fault')
            except Exception: pass
            raise
