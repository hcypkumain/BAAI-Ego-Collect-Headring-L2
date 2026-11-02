"""Per-boot wrist roles, confirmed by preview selection or alternating occlusion.

USB identifies a stream, never the hand wearing it. A completed proof survives
controller restarts and multiple recordings, but not a new kernel boot or replug.
No image decoder or hardware is imported by the state/identity logic.
"""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

SLOTS = ('cam_left_wrist', 'cam_right_wrist')
LABELS = ('左腕相机', '右腕相机')
PROMPTS = {
    'choose': '请选择腕部相机标定方式。网页可看实时画面指定左右，或使用遮挡标定。点击遮挡标定即可进入该流程。',
    'manual': '请观察网页的两路腕部实时画面，指定实际左腕或右腕，然后点击确认左右。点击重新选择可更换确认方式。',
    'baseline': '本次开机需要确认腕部相机左右。请松开两个镜头，点击检测本步；需要时点击重新开始。',
    'left': '请只遮住左腕镜头，右腕不要遮挡。遮好后点击检测本步，并保持到下一条提示。',
    'right': '请松开左腕镜头，只遮住右腕镜头。遮好后点击检测本步，并保持到下一条提示。',
    'restore': '请松开两个镜头，点击检测本步完成确认。',
    'done': '左右标定完成。本次开机连续采集无需重复标定。点击准备本段，查看任务图像并准备录制。',
    'retry': '画面变化不明确，或没有收到足够图像。本步未通过，请按提示重试；点击重新开始可从头确认。',
    'changed': '相机连接发生变化，旧标定已失效。请检查连接，重新确认设备。',
    'pair': '腕部左右标定需要两台相机都有画面。请连接两台相机后点击重新扫描。',
}


class CalibrationError(RuntimeError):
    pass


def boot_id():
    # Never fall back to a constant: that would reuse roles after a power cycle.
    value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not value:
        raise CalibrationError('missing_boot_id')
    return value


def current_stamp(config):
    from device_discovery import inventory
    # Full wrist inventory (including unexpected extras), independent of slot order.
    return sorted([[c['path'], c['vendor'], c['product'], c['serial'], c['generation']]
                   for c in inventory(config.get('auto_discovery', {})) if c['kind'] == 'wrist'])


def valid(proof, boot, stamp):
    if not isinstance(proof, dict) or proof.get('version') != 1:
        return False
    roles = proof.get('roles', {})
    if (not isinstance(roles, dict) or not all(isinstance(v, str) for v in roles.values())
            or not isinstance(stamp, list) or any(not isinstance(row, list) or not row or not isinstance(row[0], str) for row in stamp)):
        return False
    return (proof.get('boot_id') == boot and proof.get('stamp') == stamp and len(stamp) == 2
            and set(roles) == set(SLOTS) and len(set(roles.values())) == 2
            and set(roles.values()) == {row[0] for row in stamp}
            and bool(proof.get('calibration_id')))


def load(base):
    try:
        value = json.loads((Path(base)/'runtime/wrist_calibration.json').read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save(base, proof):
    path = Path(base)/'runtime/wrist_calibration.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.wrist-calibration-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(proof, out, ensure_ascii=False, indent=2)
            out.flush(); os.fsync(out.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def invalidate(base):
    path = Path(base)/'runtime/wrist_calibration.json'
    if path.exists():
        # Preserve last evidence; an explicit retry must not resurrect old roles.
        value = load(base); value['invalidated'] = True
        value['boot_id'] = None
        save(base, value)


def apply(config, base):
    if not config.get('wrist_calibration', {}).get('enabled', False):
        return config
    config = copy.deepcopy(config)
    proof = load(base); boot = boot_id(); stamp = current_stamp(config)
    sources = {item.get('device'): item for item in config.get('wrist_cameras', {}).values()
               if item.get('discovery', {}).get('present', False)}
    ok = valid(proof, boot, stamp) and set(proof.get('roles', {}).values()) <= set(sources)
    if ok:
        config['wrist_cameras'] = {
            slot: dict(copy.deepcopy(sources[proof['roles'][slot]]), label=label)
            for slot, label in zip(SLOTS, LABELS)}
    for i, slot in enumerate(SLOTS):
        item = config.get('wrist_cameras', {}).get(slot)
        if item is None: continue
        item.setdefault('discovery', {})['role_confirmed'] = ok
        if not ok: item['label'] = '腕部相机 ' + chr(65+i) + '（待本次开机标定）'
    config['wrist_calibration_status'] = dict(valid=ok, boot_id=boot,
        calibration_id=proof.get('calibration_id') if ok else None,
        method=proof.get('method', 'alternating_occlusion') if ok else None,
        completed_at=proof.get('completed_at') if ok else None)
    return config


def manual_proof(boot, stamp, sources, left_slot):
    """Resolve a selected preview slot to device paths; use the shared proof format."""
    if (not isinstance(sources, dict) or set(sources) != set(SLOTS)
            or left_slot not in SLOTS or not isinstance(boot, str) or not boot):
        raise CalibrationError('invalid_assignment')
    right_slot = next(slot for slot in SLOTS if slot != left_slot)
    proof = dict(version=1, calibration_id=uuid.uuid4().hex, boot_id=boot,
                 stamp=copy.deepcopy(stamp), completed_at=time.time(),
                 method='manual_preview',
                 roles={'cam_left_wrist': sources[left_slot],
                        'cam_right_wrist': sources[right_slot]})
    if not valid(proof, boot, stamp):
        raise CalibrationError('invalid_assignment')
    return proof


class Wizard:
    def __init__(self, config, boot, stamp):
        cameras = config.get('wrist_cameras', {})
        self.paths = {cameras[k]['device'] for k in SLOTS if k in cameras}
        if len(self.paths) != 2 or len(stamp) != 2:
            raise CalibrationError('two_cameras_required')
        self.config = copy.deepcopy(config)
        self.session_id = uuid.uuid4().hex
        self.boot = boot; self.stamp = copy.deepcopy(stamp)
        self.phase = 'baseline'; self.history = {}; self.roles = {}

    def unchanged(self, boot, stamp):
        return self.boot == boot and self.stamp == stamp

    def accept(self, stats):
        if set(stats) != self.paths or any(x.get('frames', 0) < 12 for x in stats.values()):
            raise CalibrationError('insufficient_frames')
        if any(not (0 <= x['mean'] <= 255 and 0 <= x['dark'] <= 1) for x in stats.values()):
            raise CalibrationError('invalid_statistics')
        if self.phase == 'baseline':
            if any(x['mean'] < 40 or x['dark'] > .7 for x in stats.values()):
                raise CalibrationError('baseline_too_dark')
            next_phase = 'left'
        else:
            baseline = self.history['baseline']
            covered = [p for p, x in stats.items() if x['mean'] < min(30, baseline[p]['mean']*.3) and x['dark'] >= .85]
            visible = [p for p, x in stats.items() if x['mean'] >= max(25, baseline[p]['mean']*.5) and x['dark'] < .8]
            if self.phase in ('left', 'right'):
                if len(covered) != 1 or len(visible) != 1 or covered[0] == visible[0]:
                    raise CalibrationError('occlusion_ambiguous')
                if self.phase == 'right' and covered[0] == self.roles['cam_left_wrist']:
                    raise CalibrationError('same_camera_twice')
                self.roles['cam_'+self.phase+'_wrist'] = covered[0]
                next_phase = 'right' if self.phase == 'left' else 'restore'
            elif self.phase == 'restore':
                if len(visible) != 2: raise CalibrationError('uncover_both')
                next_phase = 'done'
            else:
                raise CalibrationError('already_completed')
        self.history[self.phase] = copy.deepcopy(stats)
        self.phase = next_phase

    def proof(self):
        if self.phase != 'done': raise CalibrationError('incomplete_calibration')
        return dict(version=1, calibration_id=uuid.uuid4().hex, boot_id=self.boot,
                    stamp=self.stamp, roles=self.roles.copy(), completed_at=time.time(),
                    method='alternating_occlusion', statistics=copy.deepcopy(self.history))
