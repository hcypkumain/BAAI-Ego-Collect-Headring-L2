"""Per-boot EMG roles from guided insertion, with explicit legacy compatibility.

Ordered roles require an observed empty baseline, separate kernel connection
events and fresh current preview frames. USB/tty sorting never establishes sides.
This module does not open devices or alter acquisition parameters.
"""
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid
from wrist_calibration import CalibrationError, boot_id
import emg_insertion

SLOTS = ('emg_left', 'emg_right')
LABELS = ('左臂肌电', '右臂肌电')
METHOD = 'manual_emg_preview'
VERSION = 2
PROMPTS = {
    'manual': '请观察网页的肌电 A、B 实时波形，分别握拳确认对应关系，选择实际左臂或右臂后点击确认左右。点击重新开始可返回配对流程。',
    'done': '肌电左右已确认。同次开机、连接和佩戴位置不变时可继续采集；更换佩戴位置后请重新确认。点击准备本段，查看任务图像。',
    'changed': '肌电连接发生变化，旧的左右确认已失效。请检查连接，重新确认设备。',
    'pair': '肌电左右确认需要两套可区分的设备。请只连接两套支持的肌电接收端，并检查实时波形后重新扫描。',
    'after_wrist': '腕部相机左右确认完成。接下来请按屏幕提示确认肌电左右。',
}


def current_stamp(config):
    from device_discovery import inventory
    # Include unexpected extras so adding a third unit invalidates old proof.
    return sorted([[x['path'], x['vendor'], x['product'], x['serial'], x['generation']]
                   for x in inventory(config.get('auto_discovery', {})) if x['kind'] == 'emg'])


def _valid_pair(boot, stamp):
    if not isinstance(boot, str) or not boot or not isinstance(stamp, list) or len(stamp) != 2:
        return False
    for row in stamp:
        if (not isinstance(row, list) or len(row) != 5
                or any(not isinstance(value, str) or not value.strip() for value in row[:4])
                or type(row[4]) is not int or row[4] <= 0):
            return False
    return len({row[0] for row in stamp}) == 2 and len({row[3] for row in stamp}) == 2


def valid(proof, boot, stamp):
    if isinstance(proof, dict) and proof.get('method') == emg_insertion.METHOD:
        return emg_insertion.valid(proof, boot, stamp)
    if (not isinstance(proof, dict) or proof.get('version') != VERSION
            or proof.get('method') != METHOD or proof.get('manual_confirmed') is not True
            or not _valid_pair(boot, stamp)):
        return False
    roles, sources = proof.get('roles'), proof.get('sources')
    if any(not isinstance(value, dict) or set(value) != set(SLOTS) for value in (roles, sources)):
        return False
    if any(not isinstance(path, str) or not path for value in (roles, sources) for path in value.values()):
        return False
    paths = {row[0] for row in stamp}
    completed = proof.get('completed_at')
    return (proof.get('boot_id') == boot and proof.get('stamp') == stamp
            and len(set(roles.values())) == 2 and set(roles.values()) == paths
            and len(set(sources.values())) == 2 and set(sources.values()) == paths
            and isinstance(proof.get('calibration_id'), str) and bool(proof['calibration_id'])
            and type(completed) in (int, float) and math.isfinite(completed) and completed > 0)


def load(base):
    try:
        value = json.loads((Path(base)/'runtime/emg_calibration.json').read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save(base, proof):
    path = Path(base)/'runtime/emg_calibration.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.emg-calibration-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(proof, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.flush(); os.fsync(out.fileno())
        os.chmod(tmp, 0o644); os.replace(tmp, str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def invalidate(base):
    proof = load(base)
    if proof:
        proof['boot_id'] = None; proof['invalidated'] = True
        save(base, proof)


def assignment_method(config):
    value = config.get('emg_calibration', {}).get('assignment_method', emg_insertion.METHOD)
    if value not in (emg_insertion.METHOD, METHOD):
        raise ValueError('unknown_emg_assignment_method')
    return value


def apply(config, base):
    if not config.get('emg_calibration', {}).get('enabled', False):
        return config
    config = copy.deepcopy(config)
    proof = load(base); boot = boot_id(); stamp = current_stamp(config)
    sources = {x.get('port'): x for x in config.get('emg', {}).values()
               if x.get('discovery', {}).get('present', False)}
    method = assignment_method(config)
    ok = (valid(proof, boot, stamp) and proof.get('method') == method
          and set(proof['roles'].values()) <= set(sources))
    if ok and method == emg_insertion.METHOD:
        physical = emg_insertion.usb_units(config.get('auto_discovery', {}))
        ok = (len(physical) == 2 and len({u.get('serial') for u in physical}) == 2
              and {u.get('serial') for u in physical} == {row[3] for row in stamp})
    if ok:
        config['emg'] = {slot: dict(copy.deepcopy(sources[proof['roles'][slot]]), label=label)
                         for slot, label in zip(SLOTS, LABELS)}
    for i, slot in enumerate(SLOTS):
        item = config.get('emg', {}).get(slot)
        if item is None: continue
        item.setdefault('discovery', {})['role_confirmed'] = ok
        if not ok:
            note = '待按插入顺序确认左右' if method == emg_insertion.METHOD else '待人工确认左右'
            item['label'] = '肌电设备 ' + chr(65+i) + '（' + note + '）'
    # Freeze full evidence with device_config, not just a mutable runtime ID.
    config['emg_calibration_status'] = dict(valid=ok, boot_id=boot, scope='boot',
        calibration_id=proof.get('calibration_id') if ok else None,
        method=proof.get('method') if ok else None, required_method=method,
        completed_at=proof.get('completed_at') if ok else None,
        proof=copy.deepcopy(proof) if ok else None)
    return config


def manual_proof(boot, stamp, sources, left_slot):
    """Bind the explicit preview selection; the controller validates its token.

    No muscle-strength or movement statistic is required or consulted here.
    """
    if (not isinstance(sources, dict) or set(sources) != set(SLOTS)
            or left_slot not in SLOTS or not _valid_pair(boot, stamp)):
        raise CalibrationError('invalid_assignment')
    right_slot = next(slot for slot in SLOTS if slot != left_slot)
    proof = dict(version=VERSION, method=METHOD, manual_confirmed=True,
                 calibration_id=uuid.uuid4().hex, boot_id=boot,
                 stamp=copy.deepcopy(stamp), sources=copy.deepcopy(sources), completed_at=time.time(),
                 roles={'emg_left': sources[left_slot], 'emg_right': sources[right_slot]})
    if not valid(proof, boot, stamp):
        raise CalibrationError('invalid_assignment')
    return proof


class Wizard:
    """Identity-only manual assignment session; never evaluates activity."""
    def __init__(self, config, boot, stamp):
        self.sources = {slot: config.get('emg', {}).get(slot, {}).get('port') for slot in SLOTS}
        if (not _valid_pair(boot, stamp)
                or any(not isinstance(value, str) or not value for value in self.sources.values())
                or len(set(self.sources.values())) != 2
                or set(self.sources.values()) != {row[0] for row in stamp}
                or any(not config.get('emg', {}).get(slot, {}).get('discovery', {}).get('present', False)
                       for slot in SLOTS)):
            raise CalibrationError('two_unique_receivers_required')
        self.config = copy.deepcopy(config)
        self.paths = set(self.sources.values())
        self.boot = boot; self.stamp = copy.deepcopy(stamp)
        self.session_id = uuid.uuid4().hex
        self.phase = 'manual'

    def unchanged(self, boot, stamp):
        return self.boot == boot and self.stamp == stamp

    def accept(self, stats):
        raise CalibrationError('manual_confirmation_required')

    def proof(self):
        raise CalibrationError('manual_confirmation_required')
