"""Persistent wrist exposure policy and verified V4L2 control writes.

None preserves the device's current control value. A boolean is an explicit
request which must be read back before the camera stream is opened. This
module is shared by the Python 3.8 capture and Python 3.11 control processes.
"""
import copy
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


KEY = 'allow_auto_exposure_fps_change'
CONTROL = 'exposure_auto_priority'
PREFERENCES_FILE = 'wrist_camera_preferences.json'


class WristCameraSettingsError(ValueError):
    def __init__(self, message, report=None, reports=None):
        super().__init__(message)
        self.report = report
        self.reports = reports


def _value(value):
    if value is not None and type(value) is not bool:
        raise ValueError(KEY + ' must be null or a boolean')
    return value


def load(base):
    path = Path(base) / 'runtime' / PREFERENCES_FILE
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {KEY: None}
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or KEY not in data:
            raise ValueError('expected an object containing ' + KEY)
        return {KEY: _value(data[KEY])}
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid wrist camera preferences in %s: %s' % (path, exc)) from exc


def write_report(path, report):
    """Atomically publish a report (also used for durable preferences)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                dir=str(path.parent), prefix=path.name + '.', suffix='.tmp',
                delete=False) as file:
            tmp = file.name
            json.dump(report, file, indent=2, ensure_ascii=False, allow_nan=False)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, str(path))
        tmp = None
        try:
            fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # Replacement has committed. Some filesystems do not support
            # directory fsync; reporting a save failure here would falsely
            # imply that callers can roll hardware back to the old preference.
            pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def save(base, value):
    value = _value(value)
    write_report(Path(base) / 'runtime' / PREFERENCES_FILE, {KEY: value})
    return {KEY: value}


def apply(config, base):
    result = copy.deepcopy(config)
    settings = load(base)
    result['wrist_camera_settings'] = settings
    for item in result.get('wrist_cameras', {}).values():
        # Always overwrite a selection snapshot, including the preserve case.
        item[KEY] = settings[KEY]
    return result


@contextmanager
def _lock(base, name, busy_message):
    path = Path(base) / 'runtime' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ValueError(busy_message) from exc
            raise
        try:
            yield file
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def preference_lock(base):
    return _lock(base, 'wrist_camera_settings.lock', 'Wrist camera settings are being changed')


def capture_guard(base):
    return _lock(base, 'capture.lock', 'Cannot change wrist camera settings while capture is active')


def capture_available(base):
    try:
        with capture_guard(base):
            return True
    except (ValueError, OSError):
        return False


def _report(config, requested):
    return dict(device=config.get('device'), requested=requested,
                actual_allow_auto_exposure_fps_change=None,
                previous_actual_allow_auto_exposure_fps_change=None, supported=None,
                applied=False, error=None, time=time.time())


def error_report(config, error):
    """Retain a verified control result when later stream startup fails."""
    report = copy.deepcopy(getattr(error, 'report', None))
    if not isinstance(report, dict):
        report = _report(config, config.get(KEY))
    report['error'] = str(error)
    report['time'] = time.time()
    return report


def unavailable_report(config, requested=None):
    report = _report(config, _value(requested))
    report.update(pending=True, error='Wrist camera is not connected; setting is pending')
    return report


def _command(device, option):
    return subprocess.run(['v4l2-ctl', '--device', str(device), option],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=4)


def _command_error(result):
    detail = (result.stderr or result.stdout or '').strip()
    return detail or 'v4l2-ctl exited with status %s' % result.returncode


def _query(config, requested):
    report = _report(config, requested)
    if not config.get('device'):
        report['error'] = 'No wrist camera device configured'
        return report
    try:
        result = _command(config['device'], '--get-ctrl=' + CONTROL)
    except (OSError, subprocess.SubprocessError) as exc:
        report['error'] = 'Cannot query %s: %s' % (CONTROL, exc)
        return report
    if result.returncode:
        detail = _command_error(result)
        report['error'] = 'Cannot query %s: %s' % (CONTROL, detail)
        if re.search(r'unknown control|unsupported|not supported|invalid argument|'
                     r'does not exist|unrecognized control', detail, re.I):
            report['supported'] = False
        return report
    match = re.search(r'(?:^|\n)\s*' + CONTROL + r'\s*:\s*([01])\s*(?:\n|$)', result.stdout)
    if not match:
        report['error'] = 'Invalid %s readback: %s' % (CONTROL, result.stdout.strip())
        return report
    report['supported'] = True
    report['actual_allow_auto_exposure_fps_change'] = bool(int(match.group(1)))
    report['previous_actual_allow_auto_exposure_fps_change'] = report['actual_allow_auto_exposure_fps_change']
    return report


def _raise(report, reports=None):
    raise WristCameraSettingsError(report.get('error') or 'Wrist camera control verification failed',
                                   report=report, reports=reports)


def _set_and_verify(config, report):
    requested = report['requested']
    report['set_attempted'] = True
    set_error = None
    try:
        result = _command(config['device'], '--set-ctrl=%s=%d' % (CONTROL, int(requested)))
        if result.returncode:
            set_error = 'Cannot set %s: %s' % (CONTROL, _command_error(result))
    except (OSError, subprocess.SubprocessError) as exc:
        set_error = 'Cannot set %s: %s' % (CONTROL, exc)
    # Query even after a failed command: the device may have changed before it
    # reported an error. Never substitute the request for a measured value.
    readback = _query(config, requested)
    report.update(actual_allow_auto_exposure_fps_change=readback['actual_allow_auto_exposure_fps_change'],
                  supported=readback['supported'], time=readback['time'])
    if set_error:
        report['error'] = set_error
        if readback['error']:
            report['error'] += '; ' + readback['error']
        _raise(report)
    if readback['error']:
        report['error'] = readback['error']
        _raise(report)
    if readback['actual_allow_auto_exposure_fps_change'] is not requested:
        report['error'] = '%s readback mismatch: requested %d, actual %d' % (
            CONTROL, int(requested), int(readback['actual_allow_auto_exposure_fps_change']))
        _raise(report)
    report.update(applied=True, error=None)
    return report


def configure_device(config):
    requested = _value(config.get(KEY))
    report = _query(config, requested)
    if requested is None:
        # Preserving a device requires no writable control support. An
        # unavailable readback is visible in the report but performs no write.
        return report
    if report['supported'] is not True or report['error']:
        _raise(report)
    return _set_and_verify(config, report)


def _present(config):
    device = config.get('device')
    return (config.get('discovery', {}).get('present') is not False and
            bool(device) and Path(device).exists())


def restore_devices(reports):
    """Restore observed values after preference persistence failed.

    A None preference is not a device value. Only boolean values measured
    before the transaction are restored, and every restore is read back.
    The caller still holds the preference and capture locks.
    """
    restored = {}
    failures = []
    for name, original in reports.items():
        previous = original.get('previous_actual_allow_auto_exposure_fps_change')
        config = {'device': original.get('device'), KEY: previous}
        if (previous is None or original.get('pending') or
                (original.get('requested') is None and not original.get('set_attempted'))):
            report = _report(config, None)
            report.update(rollback_skipped=True, pending=original.get('pending', False))
            restored[name] = report
            continue
        try:
            _value(previous)
            restored[name] = configure_device(config)
        except ValueError as exc:
            report = error_report(config, exc)
            restored[name] = report
            failures.append((name, report))
    if failures:
        message = 'Failed to restore wrist camera controls: ' + '; '.join(
            name + ': ' + report['error'] for name, report in failures)
        raise WristCameraSettingsError(message, report=failures[0][1], reports=restored)
    return restored


def configure_devices(config, value):
    """Validate all connected wrists before writing; roll back a failed batch.

    The caller owns preference/capture locks and idle-preview release. Missing
    devices are pending, and preferences are saved by the caller only after a
    successful return. The transaction never changes exposure_auto mode.
    """
    requested = _value(value)
    reports = {}
    present = []
    for name, item in config.get('wrist_cameras', {}).items():
        if not _present(item):
            reports[name] = unavailable_report(item, requested)
            continue
        report = _query(item, requested)
        reports[name] = report
        present.append((name, item))
    if requested is None:
        return reports
    for name, item in present:
        if reports[name]['supported'] is not True or reports[name]['error']:
            _raise(reports[name], reports)
    previous = {name: reports[name]['actual_allow_auto_exposure_fps_change'] for name, item in present}
    attempted = []
    try:
        for name, item in present:
            attempted.append((name, item))
            _set_and_verify(item, reports[name])
    except WristCameraSettingsError as exc:
        for name, item in reversed(attempted):
            try:
                rollback = configure_device(dict(item, **{KEY: previous[name]}))
            except WristCameraSettingsError as rollback_error:
                rollback = rollback_error.report
            original = reports[name]
            original['rollback'] = rollback
            original['rolled_back'] = rollback['applied']
            original['actual_allow_auto_exposure_fps_change'] = rollback['actual_allow_auto_exposure_fps_change']
            original['applied'] = False
            if not rollback['applied']:
                original['rollback_error'] = rollback['error']
            original['time'] = time.time()
        exc.reports = reports
        raise
    return reports
