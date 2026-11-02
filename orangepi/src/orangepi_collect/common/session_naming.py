"""Collector-prefixed session names matching the supplied L3 reference format.

Python 3.8, stdlib only. The collector ID is operator-provided; this module
never borrows L3-001 from the sample attachment. The caller must validate its
storage volume before reserving a directory. Historical folders are untouched.
"""
import datetime
import math
import os
from pathlib import Path
import re
import time


TIMEZONE_NAME = 'Asia/Shanghai'
LOCAL_TIMEZONE = datetime.timezone(datetime.timedelta(hours=8), TIMEZONE_NAME)
COLLECTOR_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z')
TIMESTAMP_PATTERN = re.compile(r'\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\Z')
NAME_PATTERN = re.compile(
    r'(?:(?P<collector>[A-Za-z0-9][A-Za-z0-9_-]{0,47})-)?'
    r'(?P<session>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\Z')


class AllocationDurabilityError(OSError):
    """The directory exists but syncing its parent failed; retain it for review."""
    def __init__(self, path, identity, cause):
        super().__init__('会话目录已建立，但同步失败；已保留目录，请检查存储')
        self.path = Path(path)
        self.identity = dict(identity)
        self.__cause__ = cause


def collector_id(value):
    if not isinstance(value, str):
        raise ValueError('请先设置采集员编号，例如 L3-001')
    value = value.strip()
    if not COLLECTOR_PATTERN.fullmatch(value):
        raise ValueError('采集员编号须为 1–48 位字母、数字、下划线或连接号，且以字母或数字开头')
    return value


def session_identity(value, epoch=None):
    """Return one identity to freeze in folder name, meta.json and meta.yaml."""
    value = collector_id(value)
    epoch = time.time() if epoch is None else epoch
    if (isinstance(epoch, bool) or not isinstance(epoch, (int, float))
            or not math.isfinite(epoch) or epoch < 0):
        raise ValueError('系统时间无效，请先校准时间')
    try:
        stamp = datetime.datetime.fromtimestamp(epoch, LOCAL_TIMEZONE).strftime('%Y-%m-%d-%H-%M-%S')
    except (ValueError, OverflowError, OSError) as error:
        raise ValueError('系统时间超出可用范围，请先校准时间') from error
    if not TIMESTAMP_PATTERN.fullmatch(stamp):
        raise ValueError('系统日期格式不符合采集目录规则')
    return dict(collector_id=value, session=stamp, session_id=value+'-'+stamp,
                session_timezone=TIMEZONE_NAME, session_created_unix=float(epoch),
                session_name_format='collector_id-YYYY-MM-DD-HH-MM-SS')


def parse_session_name(name):
    """Read new names or historical timestamp-only names without renaming."""
    if not isinstance(name, str):
        raise ValueError('会话名无效')
    match = NAME_PATTERN.fullmatch(name)
    if match is None:
        raise ValueError('会话名不符合采集编号与日期时间规则')
    stamp = match.group('session')
    try:
        datetime.datetime.strptime(stamp, '%Y-%m-%d-%H-%M-%S')
    except ValueError as error:
        raise ValueError('会话日期或时间无效') from error
    return dict(collector_id=match.group('collector'), session=stamp, session_id=name)


def export_name(name, value=None):
    """Avoid doubling a prefix when an exporter receives an already named run."""
    parsed = parse_session_name(name)
    existing = parsed['collector_id']
    requested = collector_id(value) if value is not None else existing
    if existing and requested and existing != requested:
        raise ValueError('目录中的采集员编号与元数据不一致')
    return requested+'-'+parsed['session'] if requested else parsed['session']


def _sync_directory(path):
    if os.name == 'nt':
        return
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def reserve_session_dir(root, value, epoch=None):
    """Atomic reservation; collisions raise FileExistsError, never add a suffix.

    The controller may wait for the next wall-clock second and retry a
    collision. I/O/durability errors must stop preparation, not trigger a retry.
    """
    identity = session_identity(value, epoch)
    root = Path(root)
    if root.is_symlink():
        raise ValueError('会话根目录不能是符号链接')
    try:
        root.mkdir(parents=True, exist_ok=True)
    except FileExistsError as error:
        # Only a collision at the new leaf is retryable. A file in place of
        # the storage root must not send the controller into an endless retry.
        raise ValueError('会话根目录不是可用目录') from error
    target = root/identity['session_id']
    target.mkdir()  # Fail atomically for an existing file or directory.
    try:
        _sync_directory(root)
    except OSError as error:
        raise AllocationDurabilityError(target, identity, error) from error
    return target, identity
