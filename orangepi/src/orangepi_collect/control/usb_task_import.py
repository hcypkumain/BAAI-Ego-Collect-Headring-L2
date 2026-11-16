"""Selected USB ZIPs -> staged catalog -> controller-owned merge commit.

No recorder, calibration, network setting, or recording ledger is modified.
Image bytes remain immutable; old catalog/preferences are backed up for recovery.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import threading
import time
import uuid
import zipfile

from reference_catalog import (ReferenceCatalog, CatalogError, _durable, _encoded,
    _sync_directory, unique_json, valid_id, MAX_IMAGES, MAX_LISTS, MAX_INPUT_BYTES,
    MAX_SCENE_FILES, MAX_SCENE_FILE_BYTES)

SAFE_STATES = frozenset(('device_confirm', 'idle', 'failed_wait', 'exhausted'))
ACTIVE = frozenset(('building', 'ready', 'committing', 'needs_recovery'))
HELPER = '/usr/local/lib/collector-usb-task-import/mount-readonly'
MAX_ZIPS = 50
MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_BATCH_BYTES = 4 * 1024 ** 3


def allowed(snapshot):
    privacy = snapshot.get('privacy') or {}
    return (snapshot.get('state') in SAFE_STATES and not snapshot.get('busy')
            and not (snapshot.get('worker') or {}).get('recording_active')
            and privacy.get('required') is False
            and privacy.get('stop_confirmed') is True and privacy.get('phase') == 'idle')


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    _durable(path, _encoded(value))


def _preferences(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'collector_id', 'selected_list_id'}
            or value['version'] != 1 or not valid_id(value['selected_list_id'])
            or not isinstance(value['collector_id'], str) or not value['collector_id']):
        raise ValueError('任务库恢复设置无效')
    return value


def recover_replace(base, project):
    """Resolve a power loss between atomic catalog and preferences commits."""
    base, project = Path(base), Path(project)
    journal = base/'runtime/usb_task_import/pending.json'
    if not journal.exists():return False
    if journal.is_symlink() or journal.stat().st_size > 16384:raise ValueError('任务库恢复记录无效')
    value = unique_json(journal.read_bytes())
    catalog = base/'runtime/reference_library/catalog.json'
    digest = hashlib.sha256(catalog.read_bytes()).hexdigest() if catalog.exists() else None
    if digest == value['new_sha256']:prefs = value['new_preferences']
    elif digest == value['old_sha256']:prefs = value['old_preferences']
    else:raise ValueError('任务库版本与恢复记录不符，请联系管理员；未覆盖任何数据')
    _json(base/'configs/reference_control.json', _preferences(prefs))
    journal.unlink();_sync_directory(journal.parent)
    return True


def mounted_usb_volumes():
    """An optional narrow root helper mounts USB partitions read-only."""
    if Path(HELPER).is_file():
        r = subprocess.run(['sudo', '-n', HELPER], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=25)
        if r.returncode:raise CatalogError('无法挂载 U 盘，请检查连接和挂载权限', 'usb_mount_failed', 503)
        result = unique_json(r.stdout)
        if not isinstance(result, list):raise ValueError('U盘挂载返回值无效')
        return result
    r = subprocess.run(['lsblk', '-J', '-o', 'PATH,TYPE,FSTYPE,MOUNTPOINT'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=True)
    found = []
    def walk(rows):
        for row in rows:
            name = Path(row.get('path') or '').name
            parts = (Path('/sys/class/block')/name).resolve().parts
            mount = row.get('mountpoint')
            if mount and any(re.fullmatch(r'usb\d+', p) for p in parts) and mount.startswith(('/media/', '/mnt/', '/run/media/')):
                found.append(dict(mountpoint=mount, device=row['path'], label=name))
            walk(row.get('children') or [])
    walk(unique_json(r.stdout).get('blockdevices', []))
    return found


def _identity(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode):raise ValueError('ZIP 不是普通文件或是符号链接')
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]


def _member_name(info):
    name = info.filename
    if not info.flag_bits & 0x800:
        raw = name.encode('cp437')
        try:name = raw.decode('utf-8')
        except UnicodeDecodeError:name = raw.decode('gb18030')
    name = name.replace('\\', '/')
    if (not name or name.startswith('/') or re.match(r'^[A-Za-z]:', name)
            or any(ord(c) < 32 or ord(c) == 127 for c in name) or '..' in name.split('/')):
        raise ValueError('ZIP 包含不安全路径')
    return name


def _check_directory(stream, size):
    stream.seek(max(0, size-65557));tail = stream.read(65557)
    end = tail.rfind(b'PK\x05\x06')
    if end < 0 or len(tail)-end < 22:raise ValueError('ZIP 目录不完整')
    _, disk, start, here, count, length, offset, comment = struct.unpack('<4s4H2LH', tail[end:end+22])
    if (end+22+comment != len(tail) or disk or start or here != count
            or count >= 65535 or count > 10000 or length > 4*1024*1024
            or offset+length > size-len(tail)+end):
        raise ValueError('不支持分卷、ZIP64 或过大的 ZIP 目录')
    stream.seek(0)


def plan_zip(archive):
    images, scenes, names = [], [], set()
    for info in archive.infolist():
        name = _member_name(info);base = name.rsplit('/', 1)[-1]
        if info.is_dir() or any(p.startswith('.') or p == '__MACOSX' for p in name.split('/')):continue
        lower = base.lower()
        kind = ('scene' if lower.endswith(('_placements_overview.png', '.yaml', '.yml', '.csv'))
                else 'image' if lower.endswith(('.png', '.jpg', '.jpeg', '.webp')) else None)
        if kind is None:continue
        mode = (info.external_attr >> 16) & 0o170000
        if info.flag_bits & 1 or info.compress_type not in (0, 8) or mode == stat.S_IFLNK:
            raise ValueError('ZIP 含加密成员、链接或不支持的压缩方式')
        limit = MAX_INPUT_BYTES if kind == 'image' else MAX_SCENE_FILE_BYTES
        if not 0 < info.file_size <= limit or len(base.encode('utf-8')) > 384:
            raise ValueError('ZIP 成员过大、为空或名称过长：'+base)
        if base in names:raise ValueError('ZIP 有同名成员：'+base)
        names.add(base)
        (images if kind == 'image' else scenes).append((base, info, kind))
    if not images or len(images) > MAX_IMAGES or len(scenes) > MAX_SCENE_FILES:
        raise ValueError('任务包必须有图片，最多5000张图片和4个辅助文件')
    def natural(row):return [int(s) if s.isdecimal() else s for s in re.split(r'(\d+)', row[0])]
    images.sort(key=natural)
    return images+scenes


class UsbTaskImporter:
    def __init__(self, base, project, catalog, volumes=None, operation_path=None):
        self.base, self.project, self.catalog = Path(base), Path(project), catalog
        self.root = self.base/'runtime/usb_task_import';self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock();self.volumes = volumes or mounted_usb_volumes
        from usb_operation_lock import LOCK
        self.operation_path = Path(operation_path) if operation_path is not None else LOCK
        self._bootstrap_operation = volumes is None
        self.cancel = threading.Event();self.scan_id = None;self.files = {};self.thread = None
        self.value = dict(state='idle', done=0, total=0, message='请选择U盘中的 ZIP')
        self.ready = None
        saved = self.root/'status.json'
        if saved.exists():
            try:
                self.value = unique_json(saved.read_bytes())
                if self.value.get('state') in ACTIVE:self.value.update(state='failed', message='上次导入被中断，请重新扫描；旧库或已提交库已按恢复记录核对')
            except (ValueError, OSError):pass

    @property
    def active(self):
        with self.lock:return self.value['state'] in ACTIVE

    def status(self):
        with self.lock:return copy.deepcopy(self.value)

    def update(self, **values):
        with self.lock:
            self.value.update(values);self.value['updated_at'] = time.time()
            # Progress is in memory; phase changes are durable, not an fsync per frame.
            if 'state' in values:_json(self.root/'status.json', self.value)

    def scan(self):
        from usb_operation_lock import operation_lock
        # The privileged helper creates the shared lock before the user opens it.
        if self._bootstrap_operation and not self.operation_path.exists(): self.volumes()
        with operation_lock(self.operation_path): return self._scan_locked()

    def _scan_locked(self):
        if self.active:raise CatalogError('正在导入，请等待完成', 'usb_import_busy', 409)
        rows, choices = [], {}
        for volume in self.volumes()[:8]:
            mount = Path(volume['mountpoint'])
            if not mount.is_dir():continue
            root = mount.resolve();visited = 0
            for directory, folders, files in os.walk(root, followlinks=False):
                rel = Path(directory).relative_to(root)
                folders[:] = [n for n in sorted(folders) if not n.startswith('.') and n not in ('System Volume Information', '$RECYCLE.BIN') and not (Path(directory)/n).is_symlink() and len(rel.parts)<3]
                for name in sorted(files):
                    visited += 1
                    if visited > 10000:raise CatalogError('U盘文件过多，请将任务包放在单独目录', 'usb_too_many_files')
                    if name.startswith('.') or not name.lower().endswith('.zip'):continue
                    path = Path(directory)/name
                    if path.is_symlink():continue
                    identity = _identity(path)
                    token = hashlib.sha256(_encoded([str(path), identity])).hexdigest()[:32]
                    choices[token] = dict(path=str(path), mount=str(root), identity=identity)
                    rows.append(dict(id=token, name=name, relative_path=str(path.relative_to(root)),
                                     size_bytes=identity[2], volume=str(volume.get('label') or volume.get('device') or root.name)[:128]))
                    if len(rows)>500:raise CatalogError('最多显示500个 ZIP，请整理 U 盘目录')
        with self.lock:
            self.scan_id = uuid.uuid4().hex;self.files = choices
        current = self.catalog._load()
        return dict(scan_id=self.scan_id, files=rows, catalog_revision=current['revision'],
                    old_lists=[dict(id=row['id'],name=row['name'],readonly=row['readonly'],deleted=row['deleted'])
                               for row in current['lists'].values() if not row['deleted']])

    def start(self, value):
        with self.lock:
            if self.active:raise ValueError('正在导入，请勿重复提交')
            if (not isinstance(value, dict) or set(value)!={'scan_id','zip_ids','catalog_revision','remove_list_ids'}
                    or value['scan_id'] != self.scan_id):
                raise ValueError('请重新扫描U盘并选择要导入的任务包')
            ids = value['zip_ids']
            if (not isinstance(ids, list) or len(ids)>MAX_ZIPS or any(not isinstance(i,str) or i not in self.files for i in ids) or len(set(ids))!=len(ids)):
                raise ValueError('请选择最多50个当前U盘 ZIP')
            current = self.catalog._load()
            if value['catalog_revision']!=current['revision']:raise ValueError('图库已更新，请重新扫描后选择')
            remove_ids=value['remove_list_ids']
            if (not isinstance(remove_ids,list) or len(set(remove_ids))!=len(remove_ids)
                    or any(not isinstance(i,str) or i not in current['lists'] or current['lists'][i]['deleted'] for i in remove_ids)):
                raise ValueError('请选择当前图库中的有效列表')
            if not ids and not remove_ids:raise ValueError('请选择要导入的任务包或要移入回收站的旧列表')
            selected = [copy.deepcopy(self.files[i]) for i in ids]
            if sum(v['identity'][2] for v in selected)>MAX_BATCH_BYTES:raise ValueError('选中 ZIP 总大小最多4 GiB')
            from usb_operation_lock import operation_lock
            lease = operation_lock(self.operation_path);lease.__enter__()
            self.ready = None;self.cancel.clear()
            try:self.update(state='building', done=0, total=0, selected=len(ids), message='正在校验选中的 ZIP，旧列表暂不改变', error=None)
            except BaseException:
                lease.__exit__(None,None,None);raise
            def build_with_lease():
                try:self._build(selected,current['revision'],remove_ids)
                finally:lease.__exit__(None,None,None)
            self.thread = threading.Thread(target=build_with_lease, daemon=True)
            try:self.thread.start()
            except BaseException:
                lease.__exit__(None,None,None);raise

    def _build(self, selected, revision, remove_ids):
        job = uuid.uuid4().hex;stage = self.root/'staging'/job
        try:
            stage.mkdir(parents=True);stage_base = stage/'orangepi';(stage_base/'configs').mkdir(parents=True)
            store = ReferenceCatalog(stage_base, project=stage)
            total = 0;plans = []
            for entry in selected:
                path = Path(entry['path']);path.resolve().relative_to(Path(entry['mount']))
                if _identity(path)!=entry['identity']:raise ValueError('U盘或 ZIP 已变化，请重新扫描')
                if not 22<=entry['identity'][2]<=MAX_ZIP_BYTES:raise ValueError('每个 ZIP 必须有效且不超过256 MiB')
                with path.open('rb') as f:
                    _check_directory(f, entry['identity'][2])
                    with zipfile.ZipFile(f) as z:members = plan_zip(z)
                total += sum(kind=='image' for _,_,kind in members)
                if total>MAX_IMAGES:raise ValueError('选中任务包合计最多5000张任务图')
                plans.append((entry,members))
            self.update(total=total, message='正在本机导入选中任务包')
            done = 0
            for entry, members in plans:
                if self.cancel.is_set():raise ValueError('导入已取消')
                path=Path(entry['path']);name=path.stem
                manifest=[dict(name=n,size=i.file_size,crc=i.CRC,kind=k) for n,i,k in members]
                created=store.begin_bundle(name,manifest,store.revision);identifier=created['id']
                with path.open('rb') as f:
                    with zipfile.ZipFile(f) as z:
                        for filename,info,kind in members:
                            if self.cancel.is_set():raise ValueError('导入已取消')
                            with z.open(info) as member:raw=member.read(info.file_size+1)
                            if len(raw)!=info.file_size:raise ValueError('ZIP 解压长度不符')
                            if kind=='image':store.upload(raw,filename,identifier,store.revision);done+=1
                            else:store.set_list_scene(identifier,name,None,{filename:raw},store.revision)
                            self.update(done=done,message=name+'：'+filename)
                if _identity(path)!=entry['identity']:raise ValueError('读取过程中 ZIP 已变化，未替换旧库')
                store.complete_bundle(identifier,store.revision)
            imported=copy.deepcopy(store._load())
            old=copy.deepcopy(self.catalog._load())
            if old['revision']!=revision:raise ValueError('导入期间图库已变化，旧列表未被替换，请重新操作')
            new=copy.deepcopy(old)
            if len(set(new['images'])|set(imported['images']))>MAX_IMAGES or len(new['lists'])+len(imported['lists'])>MAX_LISTS:
                raise ValueError('合并后图库超过容量限制，请减少本批任务包或联系管理员整理图库')
            for identifier,image in imported['images'].items():
                if identifier in new['images']:
                    if new['images'][identifier]['deleted']:
                        raise ValueError('任务包包含已在回收站的图片，请先恢复该图片再导入')
                    continue
                new['images'][identifier]=image;new['image_order'].append(identifier)
            for identifier,item in imported['lists'].items():
                if identifier in new['lists']:raise ValueError('任务列表标识冲突，请重新扫描任务包')
                new['lists'][identifier]=item;new['list_order'].append(identifier)
            active=[row for row in new['lists'].values() if not row['deleted']]
            for identifier in remove_ids:
                item=new['lists'][identifier]
                if item['readonly']:
                    remaining={image['id'] for image in self.catalog._active_items(new,item)}
                    for other in active:
                        if other['id']==identifier or other['id'] in remove_ids or other['readonly']:continue
                        remaining-=set(other['items'])
                    if remaining:raise ValueError('系统列表需先复制为自定义列表，并确保剩余旧列表仍包含全部参考图')
                item['deleted']=True
            self.catalog._validate(new)
            # Install immutable assets before publication. No existing file is replaced.
            for source in store.library.rglob('*'):
                if not source.is_file():continue
                relative=source.relative_to(store.library);target=self.catalog._resolve(relative.as_posix())
                target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).digest()!=hashlib.sha256(source.read_bytes()).digest():raise ValueError('现有参考图内容校验失败，请联系管理员')
                else:
                    try:os.link(source,target)
                    except OSError:
                        _durable(target,source.read_bytes())
                _sync_directory(target.parent)
            if self.cancel.is_set():raise ValueError('导入已取消')
            backup=self.root/'backups'/job;backup.mkdir(parents=True)
            prefs=_preferences(unique_json((self.base/'configs/reference_control.json').read_bytes()))
            old=copy.deepcopy(self.catalog._load())
            if old['revision']!=revision:raise ValueError('导入期间图库已变化，旧库未被替换，请重新操作')
            _json(backup/'catalog.json',old);_json(backup/'reference_control.json',prefs)
            self.ready=dict(job=job,new=new,old=old,preferences=prefs,backup=backup)
            self.update(state='ready',message='任务包已校验，正在合并到现有图库')
        except Exception as error:
            self.update(state='failed',error=str(error),message='导入失败，旧列表未替换：'+str(error))
        finally:
            if stage.exists():shutil.rmtree(stage)

    def commit(self):
        """Called only by the controller in an idle, unpinned state."""
        if self.status()['state']!='ready':return None
        item=self.ready;catalog=self.catalog
        with catalog.mutex:
            if any(catalog.pins.values()):raise ValueError('参考图仍被本段采集锁定')
            current=catalog._load()
            if current!=item['old']:raise ValueError('图库在导入期间发生变化，未替换旧库')
            prefs=_preferences(unique_json((self.base/'configs/reference_control.json').read_bytes()))
            if prefs!=item['preferences']:raise ValueError('采集列表设置已变化，未替换旧库')
            new=copy.deepcopy(item['new']);new['revision']=current['revision']+1
            selected=prefs['selected_list_id']
            if selected not in ('pool','all') and (selected not in new['lists'] or new['lists'][selected]['deleted']):
                selected=next((identifier for identifier in new['list_order']
                    if not new['lists'][identifier]['deleted'] and not new['lists'][identifier].get('hidden',False)
                    and new['lists'][identifier].get('sampling_enabled',True)
                    and (new['lists'][identifier].get('bundle') or {}).get('state')!='importing'),None)
            if self.catalog.single_list_mode and (selected in ('pool','all') or selected is None):
                raise ValueError('单列表采集模式需要至少保留一个可用任务列表')
            if selected is None:selected='pool'
            new_prefs=dict(prefs,selected_list_id=selected)
            old_digest=hashlib.sha256(catalog.path.read_bytes()).hexdigest() if catalog.path.exists() else None
            journal=dict(old_sha256=old_digest,new_sha256=hashlib.sha256(_encoded(new)).hexdigest(),
                         old_preferences=prefs,new_preferences=new_prefs)
            pending=self.root/'pending.json'
            self.update(state='committing',message='正在切换任务列表')
            try:
                with catalog._transaction(current['revision']) as target:
                    _json(pending,journal)
                    target.clear();target.update(new);target['revision']=current['revision']
                _json(self.base/'configs/reference_control.json',new_prefs)
                pending.unlink();_sync_directory(pending.parent)
            except Exception:
                if pending.exists():recover_replace(self.base,self.project)
                if hashlib.sha256(catalog.path.read_bytes()).hexdigest()!=journal['new_sha256']:raise
            self.update(state='complete',message='已将选中任务包加入图库；未勾选删除的旧列表已保留，历史录制与标定未改动',
                        backup=str(item['backup'].relative_to(self.base)),lists=len(new['lists']),images=len(new['images']))
            return new_prefs

    def close(self):self.cancel.set()
