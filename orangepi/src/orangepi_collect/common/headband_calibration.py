"""Archive the local ROS headband's calibration bytes without certifying defaults.

Only an explicitly verified source with matching camera topics and resolutions
is published where the upstream dataset packer finds it. Other complete sources
are retained as candidates. Calibration parameters and sensor data are untouched.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

FILENAMES = ('camera-camchain.yaml', 'camera_imu-camchain-imucam.yaml',
             'camera_imu-imu.yaml')
MAX_FILE_BYTES = 1024 * 1024
CAMERA_SLOTS = ('left', 'right', 'bright', 'bleft')
LABELS = dict(left='前左相机', right='前右相机', bright='后右相机', bleft='后左相机')
# The vendor calibration files still identify cameras by their original ROS
# topic, even though the capture path moved to camd. Keep the slot -> topic map
# here so a calibration snapshot can still be matched to our camera slots.
CALIBRATION_TOPICS = {slot: '/fisheye/%s/image_raw/compressed' % slot
                      for slot in CAMERA_SLOTS}
CALIBRATION_IMU_TOPIC = '/imu_data_raw'


class SnapshotError(ValueError):
    pass


def _signature(value):
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _real_directory(path, create=False):
    if path.is_symlink():
        raise SnapshotError('symlink_directory')
    if create:
        path.mkdir(parents=True, exist_ok=True)
    value = path.stat()
    if not stat.S_ISDIR(value.st_mode):
        raise SnapshotError('not_a_directory')
    return _signature(value)


def _read_file(path, limit=MAX_FILE_BYTES):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
        raise SnapshotError('invalid_source_file:' + path.name)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(str(path), flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _signature(opened) != _signature(before):
            raise SnapshotError('source_changed:' + path.name)
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (not 0 < len(raw) <= limit or len(raw) != before.st_size
            or _signature(after) != _signature(before)
            or _signature(path.lstat()) != _signature(before)):
        raise SnapshotError('source_changed:' + path.name)
    return dict(raw=raw, signature=_signature(before),
                sha256=hashlib.sha256(raw).hexdigest())


def _check_stable(source, directory_signature, records, identities):
    if _real_directory(source) != directory_signature:
        raise SnapshotError('source_directory_changed')
    for name, original in records.items():
        current = _read_file(source / name)
        if (current['signature'] != original['signature']
                or current['sha256'] != original['sha256']):
            raise SnapshotError('source_changed:' + name)
    for path, original in identities:
        current = _read_file(path, 4096)
        if (current['signature'] != original['signature']
                or current['sha256'] != original['sha256']):
            raise SnapshotError('identity_changed:' + path.name)


def _identity(options, status):
    identities = []
    for field, option in (('device_id', 'device_id_file'),
                          ('hardware_model', 'hardware_model_file')):
        value = options.get(option)
        entry = dict(path=value if isinstance(value, str) else None,
                     value=None, sha256=None)
        try:
            if not isinstance(value, str) or not value:
                raise SnapshotError('identity_path_missing')
            path = Path(value)
            record = _read_file(path, 4096)
            text = record['raw'].decode('utf-8').strip()
            if not text or '\n' in text or '\r' in text:
                raise SnapshotError('invalid_identity')
            entry.update(value=text, sha256=record['sha256'])
            identities.append((path, record))
        except (OSError, ValueError, UnicodeError):
            status['reasons'].append(field + '_not_available')
        status['device'][field] = entry
    return identities


def _topic_key(topic):
    # Kalibr names the raw image topic; the recorder may use its ROS compressed
    # transport. This known suffix changes transport, not the camera identity.
    return topic[:-len('/compressed')] if topic.endswith('/compressed') else topic


def _camera_metadata(records, config, status):
    try:
        import yaml
        values = {name: yaml.safe_load(records[name]['raw']) for name in FILENAMES}
    except ImportError:
        raise SnapshotError('yaml_parser_not_available')
    except (ValueError, UnicodeError, RecursionError, yaml.YAMLError):
        raise SnapshotError('invalid_calibration_yaml')
    if any(not isinstance(value, dict) or not value for value in values.values()):
        raise SnapshotError('invalid_calibration_mapping')
    imu = values[FILENAMES[2]].get('imu0')
    if (not isinstance(imu, dict) or not isinstance(imu.get('rostopic'), str)
            or not imu['rostopic']):
        raise SnapshotError('invalid_imu_metadata')
    expected_imu = config.get('board_imu', {})
    expected_imu_topic = (expected_imu.get('topic') if isinstance(expected_imu, dict) else None)
    if not isinstance(expected_imu_topic, str) or not expected_imu_topic:
        expected_imu_topic = CALIBRATION_IMU_TOPIC
    if imu['rostopic'] != expected_imu_topic:
        status['reasons'].append('imu_topic_mismatch')

    expected = {}
    cameras = config.get('cameras', {})
    if not isinstance(cameras, dict):
        cameras = {}
    for slot in CAMERA_SLOTS:
        item = cameras.get(slot, {})
        if not isinstance(item, dict):
            item = {}
        # camd-based configs carry no topic; fall back to the calibration map.
        topic = item.get('topic') or CALIBRATION_TOPICS.get(slot)
        size = [item.get('width'), item.get('height')]
        if (not isinstance(topic, str) or not topic
                or any(type(n) is not int or n <= 0 for n in size)
                or not _topic_key(topic) or _topic_key(topic) in expected):
            status['reasons'].append('expected_camera_configuration_incomplete')
            continue
        expected[_topic_key(topic)] = dict(slot=slot, resolution=size, configured_topic=topic)
    by_file = {}
    for name in FILENAMES[:2]:
        value = values[name]
        entries = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.startswith('cam'):
                continue
            if not isinstance(item, dict):
                raise SnapshotError('invalid_camera_entry:' + name)
            topic, size = item.get('rostopic'), item.get('resolution')
            if (not isinstance(topic, str) or not topic
                    or not isinstance(size, list) or len(size) != 2
                    or any(type(n) is not int or n <= 0 for n in size)):
                raise SnapshotError('invalid_camera_metadata:' + name)
            match = expected.get(_topic_key(topic))
            entries[key] = dict(topic=topic, resolution=size,
                slot=match['slot'] if match else None,
                label=LABELS.get(match['slot']) if match else None,
                configured_topic=match['configured_topic'] if match else None,
                expected_resolution=match['resolution'] if match else None,
                topic_matches=match is not None,
                resolution_matches=match is not None and size == match['resolution'])
        if (set(entries) != {'cam0', 'cam1', 'cam2', 'cam3'}
                or {_topic_key(item['topic']) for item in entries.values()} != set(expected)
                or len({_topic_key(item['topic']) for item in entries.values()}) != 4):
            status['reasons'].append('camera_topic_mismatch:' + name)
        if any(not item['resolution_matches'] for item in entries.values()):
            status['reasons'].append('camera_resolution_mismatch:' + name)
        by_file[name] = entries
    first, second = [by_file[name] for name in FILENAMES[:2]]
    if ({k: (_topic_key(v['topic']), v['resolution']) for k, v in first.items()}
            != {k: (_topic_key(v['topic']), v['resolution']) for k, v in second.items()}):
        status['reasons'].append('camchain_camera_mapping_mismatch')
    status['cameras'] = by_file


def _write_file(path, raw):
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _manifest_bytes(manifest):
    return (json.dumps(manifest, indent=2) + '\n').encode('utf-8')


def _same_snapshot(target, records, manifest):
    _real_directory(target)
    if {p.name for p in target.iterdir()} != set(FILENAMES) | {'manifest.json'}:
        raise SnapshotError('existing_snapshot_conflict')
    for name in FILENAMES:
        if _read_file(target / name)['raw'] != records[name]['raw']:
            raise SnapshotError('existing_snapshot_conflict')
    if _read_file(target / 'manifest.json')['raw'] != _manifest_bytes(manifest):
        raise SnapshotError('existing_snapshot_conflict')


def _publish(parent, target, records, manifest, verify_source):
    other = parent / ('calibration_candidates' if target.name == 'calibrations' else 'calibrations')
    if other.exists() or other.is_symlink():
        raise SnapshotError('existing_snapshot_mode_conflict')
    if target.exists() or target.is_symlink():
        try:
            _same_snapshot(target, records, manifest)
        except (OSError, ValueError) as exc:
            raise SnapshotError('existing_snapshot_conflict') from exc
        verify_source()
        return
    staging = Path(tempfile.mkdtemp(prefix='.headband-calibration-', dir=str(parent)))
    try:
        for name in FILENAMES:
            _write_file(staging / name, records[name]['raw'])
        _write_file(staging / 'manifest.json', _manifest_bytes(manifest))
        _same_snapshot(staging, records, manifest)
        _sync_directory(staging)
        verify_source()
        staging.rename(target)
        _sync_directory(parent)
    finally:
        if staging.exists():
            shutil.rmtree(str(staging))


def _check_frozen_status(parent, target, status, verified, manifest):
    if not target.exists() and not target.is_symlink():
        return
    try:
        prior = json.loads(_read_file(parent / 'calibration_status.json')['raw'])
        expected = {key: status[key] for key in ('source_dir', 'source_verified', 'device', 'files', 'cameras')}
        expected.update(state='verified' if verified else 'candidate', available=True,
                        applicability_verified=verified,
                        snapshot_relative_dir='ego_headband/' + target.name,
                        manifest_sha256=hashlib.sha256(_manifest_bytes(manifest)).hexdigest())
        if not isinstance(prior, dict) or any(prior.get(key) != value for key, value in expected.items()):
            raise SnapshotError('existing_snapshot_identity_conflict')
    except (OSError, ValueError) as exc:
        raise SnapshotError('existing_snapshot_identity_conflict') from exc


def _save_status(parent, status):
    path = parent / 'calibration_status.json'
    if path.is_symlink():
        raise SnapshotError('symlink_status_destination')
    fd, name = tempfile.mkstemp(prefix='.headband-status-', dir=str(parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(status, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, str(path))
        _sync_directory(parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def verify_saved(output, status):
    """Raise SnapshotError if an archived complete set changed after preparation."""
    if not isinstance(status, dict) or status.get('available') is not True:
        return
    relative = status.get('snapshot_relative_dir')
    verified = status.get('applicability_verified') is True
    expected_relative = 'ego_headband/' + ('calibrations' if verified else 'calibration_candidates')
    if relative != expected_relative or status.get('state') != ('verified' if verified else 'candidate'):
        raise SnapshotError('saved_status_mismatch')
    entries = status.get('files')
    if (not isinstance(entries, list) or len(entries) != len(FILENAMES)
            or [entry.get('name') if isinstance(entry, dict) else None for entry in entries] != list(FILENAMES)):
        raise SnapshotError('saved_manifest_mismatch')
    target = Path(output) / expected_relative
    try:
        _real_directory(Path(output))
        _real_directory(Path(output) / 'ego_headband')
        _real_directory(target)
        if {p.name for p in target.iterdir()} != set(FILENAMES) | {'manifest.json'}:
            raise SnapshotError('saved_snapshot_incomplete')
        for entry in entries:
            if (set(entry) != {'name', 'size', 'sha256'} or type(entry['size']) is not int
                    or not 0 < entry['size'] <= MAX_FILE_BYTES or not isinstance(entry['sha256'], str)):
                raise SnapshotError('saved_manifest_mismatch')
            value = _read_file(target / entry['name'])
            if len(value['raw']) != entry['size'] or value['sha256'] != entry['sha256']:
                raise SnapshotError('saved_file_changed:' + entry['name'])
        raw_manifest = _read_file(target / 'manifest.json')['raw']
        if (raw_manifest != _manifest_bytes(dict(version=1, files=entries))
                or hashlib.sha256(raw_manifest).hexdigest() != status.get('manifest_sha256')):
            raise SnapshotError('saved_manifest_mismatch')
    except (OSError, ValueError) as exc:
        if isinstance(exc, SnapshotError):
            raise
        raise SnapshotError('saved_snapshot_not_available') from exc


def snapshot(output, config):
    """Snapshot a full devices config into a session root; failures return warnings.

    ``state`` is verified/candidate/not_available/disabled. ``available`` means a
    complete snapshot was archived; only ``applicability_verified`` permits the
    canonical directory. This function does not start or stop sensor acquisition.
    """
    options = config.get('headband_calibration', {}) if isinstance(config, dict) else {}
    if not isinstance(options, dict):
        options = {}
    status = dict(version=1, state='not_available', available=False,
        applicability_verified=False, conflict=False, created_at=time.time(),
        source_dir=options.get('source_dir') if isinstance(options.get('source_dir'), str) else None,
        source_verified=options.get('source_verified') is True, reasons=[], warnings=[],
        device={}, files=[], cameras={}, snapshot_relative_dir=None, manifest_sha256=None,
        status_relative_path='ego_headband/calibration_status.json')
    if options.get('enabled') is not True:
        status.update(state='disabled', message='未启用头环标定文件快照。')
        return status
    parent = None; lock = None; lock_held = False
    try:
        value = options.get('source_dir')
        if not isinstance(value, str) or not value:
            raise SnapshotError('source_directory_not_configured')
        source = Path(value)
        output = Path(output)
        if source.resolve() in (output.resolve(), *output.resolve().parents):
            raise SnapshotError('output_inside_source_directory')
        _real_directory(output, create=True)
        parent = output / 'ego_headband'
        _real_directory(parent, create=True)
        lock_path = parent / '.calibration.lock'
        lock = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o644)
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise SnapshotError('invalid_snapshot_lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_held = True
        directory_signature = _real_directory(source)
        records = {name: _read_file(source / name) for name in FILENAMES}
        status['files'] = [dict(name=name, size=len(records[name]['raw']), sha256=records[name]['sha256'])
                           for name in FILENAMES]
        identities = _identity(options, status)
        verify_source = lambda: _check_stable(source, directory_signature, records, identities)
        verify_source()
        if options.get('source_verified') is not True:
            status['reasons'].append('source_not_verified')
        if type(options.get('source_verified')) is not bool:
            status['reasons'].append('source_verified_must_be_boolean')
        _camera_metadata(records, config, status)
        status['reasons'] = list(dict.fromkeys(status['reasons']))
        verified = not status['reasons']
        target = parent / ('calibrations' if verified else 'calibration_candidates')
        manifest = dict(version=1, files=status['files'])
        _check_frozen_status(parent, target, status, verified, manifest)
        _publish(parent, target, records, manifest, verify_source)
        status.update(state='verified' if verified else 'candidate', available=True,
                      applicability_verified=verified,
                      snapshot_relative_dir='ego_headband/' + target.name,
                      manifest_sha256=hashlib.sha256(_manifest_bytes(manifest)).hexdigest())
        verify_saved(output, status)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        status.update(state='not_available', available=False, applicability_verified=False)
        status['reasons'].append(str(exc) or type(exc).__name__)
        status['conflict'] = str(exc).startswith('existing_snapshot_')
    if status['state'] == 'verified':
        status['message'] = '已保存已确认来源且图像尺寸、话题匹配的头环标定快照。'
    elif status['state'] == 'candidate':
        status['message'] = '已保存头环候选标定；适用性尚未确认，不作为已认证标定供打包使用。'
    elif status['conflict']:
        status['message'] = '本会话已有不同或不一致的标定快照，不能复用开录；请使用新会话并检查标定来源。'
    else:
        status['message'] = '头环标定快照不可用；本段采集可继续，请查看原因。'
    if not status['applicability_verified']:
        status['warnings'] = list(status['reasons'])
    try:
        if parent is not None and lock_held:
            _real_directory(parent)
            _save_status(parent, status)
    except (OSError, ValueError, TypeError) as exc:
        status['warnings'].append('status_save_failed:' + str(exc))
    finally:
        if lock is not None:
            os.close(lock)
    return status
