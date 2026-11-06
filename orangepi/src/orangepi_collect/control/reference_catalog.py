"""Durable reference library; image bytes are immutable and deletion is reversible.

All image decode/hash work runs outside the catalog mutex. The controller may
pin a single asset before preparing an episode; deletion and pin use that same
mutex. No controller/dashboard callback is invoked while holding it.
"""
import copy
from contextlib import contextmanager
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
import warnings
import zlib

TOKEN = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
LEGACY_PNG = re.compile(r'(?:_\d+|\d+_combo\d+_r\d+)$')
MAX_REVISION = 9007199254740991
MAX_IMAGES = 5000
MAX_LISTS = 1000
MAX_INPUT_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_DIMENSION = 16384
MAX_NORMALIZED_BYTES = 64 * 1024 * 1024
MIN_FREE_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
# A task bundle contributes at most a config.yaml and a placements.csv.
MAX_SCENE_FILES = 4
MAX_SCENE_FILE_BYTES = 2 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 256 * 1024


class CatalogError(ValueError):
    def __init__(self, message, code='invalid_request', status=400, committed=False):
        super().__init__(message)
        self.code, self.status, self.committed = code, status, committed


def valid_id(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def display_name(value, default='未命名'):
    if not isinstance(value, str):
        raise CatalogError('名称必须是文字')
    value = ''.join(c if c.isprintable() else ' ' for c in value).strip()
    if not value:
        value = default
    raw = value.encode('utf-8')[:384]
    while True:
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            raw = raw[:-1]


def task_tags(value):
    if not isinstance(value, list) or len(value) > 16 or any(not isinstance(tag, str) or not tag.strip() for tag in value):
        raise CatalogError('标签应为最多 16 个非空文字')
    return list(dict.fromkeys(display_name(tag) for tag in value))


def unique_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('invalid number')))


def _encoded(value):
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'), sort_keys=True) + '\n').encode('utf-8')
    if len(raw) > MAX_METADATA_BYTES:
        raise CatalogError('图库信息过大，请减少列表或图片数量', 'catalog_full', 413)
    return raw


def _sync_directory(path):
    if os.name == 'nt':
        return
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable(path, raw, mode=0o600):
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    committed = False
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(raw)
            output.flush()
            if hasattr(os, 'fchmod'):os.fchmod(output.fileno(), mode)
            else:os.chmod(temporary, mode)
            os.fsync(output.fileno())
        os.replace(temporary, str(path))
        committed = True
        _sync_directory(path.parent)
    except OSError as error:
        raise CatalogError('保存失败，请刷新后检查磁盘空间', 'storage_error', 503, committed) from error
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _revision(value):
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise CatalogError('图库版本无效，请刷新后再试', 'invalid_revision')
    return value


class ReferenceCatalog:
    def __init__(self, base, project=None, library=None):
        self.base = Path(base).resolve()
        from task_block_sampling import policy
        self.task_block_policy = policy(self.base)
        policy = self.base/'configs/reference_policy.json'
        self.single_list_mode = False
        if policy.exists():
            if policy.is_symlink() or not policy.is_file() or policy.stat().st_size>4096:
                raise ValueError('任务抽取设置无效')
            settings=unique_json(policy.read_bytes())
            if settings!={'version':1,'mode':'single_list_random'}:
                raise ValueError('任务抽取设置无效')
            self.single_list_mode=True
        self.project = Path(project).resolve() if project is not None else self.base.parent
        self.library = Path(library).resolve() if library is not None else self.project/'configs/environments'
        self.library.relative_to(self.project)
        self.directory = self.base/'runtime/reference_library'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory/'catalog.json'
        self.lock_path = self.directory/'catalog.lock'
        self.mutex = threading.RLock()
        self.pins = {}
        self._cache = None
        self._stamp = None
        self._thumbnail_cache = {}
        self._thumbnail_lock = threading.Lock()
        self.decode_slots = threading.BoundedSemaphore(2)
        self.upload_slots = self.decode_slots
        self.thumbnail_slots = self.decode_slots
        self._load()

    @staticmethod
    def _empty():
        return dict(version=1, revision=0, images={}, lists={}, image_order=[], list_order=[])

    def _load(self):
        with self.mutex:
            if not self.path.exists():
                if self._cache is None:
                    self._cache = self._empty()
                return self._cache
            if self.path.is_symlink():
                raise CatalogError('图库索引路径无效', 'invalid_storage', 503)
            info = self.path.stat()
            stamp = (info.st_mtime_ns, info.st_size)
            if stamp == self._stamp:
                return self._cache
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_BYTES:
                raise CatalogError('图库索引格式错误', 'invalid_storage', 503)
            try:
                value = unique_json(self.path.read_bytes())
                self._validate(value)
            except (ValueError, TypeError, UnicodeError, RecursionError) as error:
                raise CatalogError('图库索引损坏，请联系维护人员恢复备份', 'invalid_storage', 503) from error
            self._cache, self._stamp = value, stamp
            return value

    @staticmethod
    def _validate(value):
        if not isinstance(value, dict) or type(value.get('version')) is not int or value.get('version') != 1:
            raise ValueError('unsupported catalog')
        _revision(value.get('revision'))
        images, lists = value.get('images'), value.get('lists')
        if not isinstance(images, dict) or not isinstance(lists, dict) or len(images) > MAX_IMAGES or len(lists) > MAX_LISTS:
            raise ValueError('invalid inventory')
        for collection, order in ((images, value.get('image_order')), (lists, value.get('list_order'))):
            if not isinstance(order, list) or len(order) != len(set(order)) or set(order) != set(collection):
                raise ValueError('invalid ordering')
        for identifier, item in images.items():
            if not valid_id(identifier) or not isinstance(item, dict) or item.get('id') != identifier:
                raise ValueError('invalid image identity')
            if item.get('origin') not in ('legacy', 'upload') or type(item.get('deleted')) is not bool:
                raise ValueError('invalid image state')
            ReferenceCatalog._relative(item.get('path'))
            if not isinstance(item.get('name'), str) or len(item['name'].encode('utf-8')) > 384:
                raise ValueError('invalid image name')
        for identifier, item in lists.items():
            if not valid_id(identifier) or not isinstance(item, dict) or item.get('id') != identifier:
                raise ValueError('invalid list identity')
            if not isinstance(item.get('name'), str) or len(item['name'].encode('utf-8')) > 384:
                raise ValueError('invalid list name')
            ids = item.get('items')
            if (not isinstance(ids, list) or len(ids) > MAX_IMAGES or len(ids) != len(set(ids))
                    or any(identifier not in images for identifier in ids)
                    or type(item.get('deleted')) is not bool or type(item.get('readonly')) is not bool):
                raise ValueError('invalid list items')
            ReferenceCatalog._validate_scene(item.get('scene'))
            if any(type(item.get(key, default)) is not bool for key, default in (('hidden', False), ('sampling_enabled', True))):
                raise ValueError('invalid sampling state')
            task_tags(item.get('tags', []))
            aliases=item.get('original_filenames',{})
            if not isinstance(aliases,dict) or any(key not in images or not isinstance(name,str) or not 0<len(name.encode('utf-8'))<=384 for key,name in aliases.items()):
                raise ValueError('invalid image aliases')
            bundle=item.get('bundle')
            if bundle is not None:
                if (not isinstance(bundle,dict) or set(bundle)!={'state','manifest_sha256','image_names','scene_names','image_map','manifest'}
                        or bundle['state'] not in ('importing','complete') or not isinstance(bundle['manifest_sha256'],str)
                        or len(bundle['manifest_sha256'])!=64 or not isinstance(bundle['image_names'],list)
                        or not isinstance(bundle['scene_names'],list) or not isinstance(bundle['image_map'],dict) or not isinstance(bundle['manifest'],dict)
                        or any(name not in bundle['image_names'] or identifier not in images for name,identifier in bundle['image_map'].items())):
                    raise ValueError('invalid bundle import state')

    @staticmethod
    def _validate_scene(scene):
        """Optional per-list scene metadata imported from a task bundle.

        Holds the bundle's config.yaml / placements.csv so the scene notes and
        prop placements that produced the reference images stay with the list.
        """
        if scene is None:
            return
        if not isinstance(scene, dict) or set(scene) - {'task', 'notes', 'files', 'imported_at'}:
            raise ValueError('invalid list scene')
        for key in ('task', 'notes'):
            value = scene.get(key)
            if value is not None and (not isinstance(value, str)
                                      or len(value.encode('utf-8')) > 4096):
                raise ValueError('invalid list scene text')
        files = scene.get('files')
        if files is not None:
            if not isinstance(files, dict) or len(files) > MAX_SCENE_FILES:
                raise ValueError('invalid list scene files')
            for name, entry in files.items():
                if (not isinstance(name, str) or not name or len(name.encode('utf-8')) > 384
                        or '/' in name or '\\' in name or name.startswith('.')):
                    raise ValueError('invalid list scene file name')
                if (not isinstance(entry, dict)
                        or set(entry) - {'sha256', 'size_bytes', 'path'}
                        or not isinstance(entry.get('sha256'), str)
                        or type(entry.get('size_bytes')) is not int):
                    raise ValueError('invalid list scene file entry')
                ReferenceCatalog._relative(entry.get('path'))

    @staticmethod
    def _relative(value):
        if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 2048 or '\\' in value:
            raise CatalogError('图片路径无效')
        path = Path(value)
        if path.is_absolute() or '..' in path.parts or ':' in value or '\0' in value:
            raise CatalogError('图片路径无效')
        return path

    def _resolve(self, relative):
        path = self.library/self._relative(relative)
        try:
            path.resolve().relative_to(self.library)
        except (ValueError, OSError) as error:
            raise CatalogError('图片不在图库目录内') from error
        current = self.library
        for part in path.relative_to(self.library).parts:
            current = current/part
            if current.is_symlink():
                raise CatalogError('图片路径不能使用链接')
        return path

    @contextmanager
    def _transaction(self, expected_revision):
        expected_revision = _revision(expected_revision)
        with self.mutex:
            descriptor = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                if os.name != 'nt':
                    import fcntl
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as error:
                        raise CatalogError('图库正在更新，请稍后再试', 'catalog_busy', 409) from error
                current = self._load()
                if current['revision'] != expected_revision:
                    raise CatalogError('图库已更新，请刷新后再试', 'revision_conflict', 409)
                value = copy.deepcopy(current)
                yield value
                if value == current:
                    return
                if value['revision'] >= MAX_REVISION:
                    raise CatalogError('图库版本号已满', 'catalog_full', 503)
                value['revision'] += 1
                self._validate(value)
                raw = _encoded(value)
                try:
                    _durable(self.path, raw)
                except CatalogError as error:
                    if error.committed:
                        self._cache, self._stamp = value, None
                    raise
                self._cache, self._stamp = value, None
            finally:
                os.close(descriptor)

    @property
    def revision(self):
        # All in-process mutations update this cache. Controller heartbeat may
        # inspect the published immutable document without waiting for a slow
        # metadata fsync. CPython publishes the new dict by one reference swap;
        # transactions never mutate this cached dict in place.
        return self._cache['revision']

    def import_legacy(self, paths=None):
        """Enumerate filenames/stat only. Hash only the image chosen for prepare."""
        if self._load().get('legacy_import_disabled') is True:return 0
        if paths is None:
            paths = sorted(p for p in self.library.rglob('*.png')
                           if LEGACY_PNG.search(p.stem) and not {'_uploads', '_uploaded'}.intersection(p.relative_to(self.library).parts))
        found = []
        for path in paths:
            path = Path(path)
            relative = path.resolve().relative_to(self.library).as_posix()
            bound = self._resolve(relative)
            info = bound.stat()
            if not stat.S_ISREG(info.st_mode):
                continue
            identifier = 'legacy_' + hashlib.sha256(relative.encode('utf-8')).hexdigest()[:40]
            found.append(dict(id=identifier, name=display_name(path.stem), path=relative,
                origin='legacy', deleted=False, original_filename=display_name(path.name),
                size_bytes=info.st_size, imported_mtime_ns=info.st_mtime_ns))
        if len(found) > MAX_IMAGES:
            raise CatalogError('现有图库超过图片数量上限', 'catalog_full', 413)
        revision = self.revision
        with self._transaction(revision) as value:
            for image in found:
                if image['id'] not in value['images']:
                    value['images'][image['id']] = image
                    value['image_order'].append(image['id'])
            groups = {}
            for image in found:
                parent = Path(image['path']).parent.as_posix()
                groups.setdefault(parent, []).append(image['id'])
            for group, identifiers in sorted(groups.items()):
                identifier = 'group_' + hashlib.sha256(group.encode('utf-8')).hexdigest()[:40]
                if identifier not in value['lists']:
                    value['lists'][identifier] = dict(id=identifier, name=display_name(group if group != '.' else '原有参考图'),
                        items=identifiers, readonly=True, deleted=False)
                    value['list_order'].append(identifier)
                else:
                    old = value['lists'][identifier]
                    old['items'] = list(dict.fromkeys(old['items'] + identifiers))
        return dict(revision=value['revision'], imported=len(found))

    def _image(self, value, identifier, include_deleted=False):
        if not valid_id(identifier) or identifier not in value['images']:
            raise CatalogError('找不到这张参考图', 'not_found', 404)
        image = value['images'][identifier]
        if image['deleted'] and not include_deleted:
            raise CatalogError('这张参考图已移到回收站', 'deleted', 409)
        return image

    def _list(self, value, identifier, include_deleted=False):
        if identifier == 'pool':
            ids = list(dict.fromkeys(image_id for row in self._sampling_lists(value) for image_id in row['items']))
            return dict(id='pool', name='混合任务池', items=ids, readonly=True, deleted=False)
        if identifier == 'all':
            return dict(id='all', name='全部参考图', items=list(value['image_order']), readonly=True, deleted=False)
        if not valid_id(identifier) or identifier not in value['lists']:
            raise CatalogError('找不到这个任务列表', 'not_found', 404)
        item = value['lists'][identifier]
        if item['deleted'] and not include_deleted:
            raise CatalogError('这个任务列表已移到回收站', 'deleted', 409)
        return item

    def _sampling_lists(self, value):
        return [value['lists'][identifier] for identifier in value['list_order']
                if self.task_block_policy is None or identifier in self.task_block_policy['list_ids']
                if not value['lists'][identifier]['deleted'] and not value['lists'][identifier].get('hidden', False)
                and value['lists'][identifier].get('sampling_enabled', True)
                and (value['lists'][identifier].get('bundle') or {}).get('state') != 'importing']

    def set_list_sampling(self, identifier, hidden, enabled, tags, expected_revision):
        if type(hidden) is not bool or type(enabled) is not bool:
            raise CatalogError('任务池设置无效')
        tags = task_tags(tags)
        with self._transaction(expected_revision) as value:
            item = self._list(value, identifier)
            if identifier in ('all', 'pool'):
                raise CatalogError('请操作具体列表')
            if any(self.pins.get(image_id) for image_id in item['items']):
                raise CatalogError('列表正在准备或采集中，请结束本段后再修改', 'in_use', 409)
            item.update(hidden=hidden, sampling_enabled=enabled and not hidden, tags=tags)
        return dict(id=identifier, revision=value['revision'])

    def _active_items(self, value, item):
        pending=set();available=set()
        if item['id']=='all':
            for row in value['lists'].values():
                if row['deleted'] or row.get('hidden', False):continue
                (pending if (row.get('bundle') or {}).get('state')=='importing' else available).update(row['items'])
        aliases=item.get('original_filenames') or {}
        result=[]
        for identifier in item['items']:
            if item['id'] == 'all' and identifier not in available:continue
            if value['images'][identifier]['deleted'] or identifier in pending-available:continue
            image=value['images'][identifier]
            if str(image.get('original_filename',image['name'])).lower().endswith('_placements_overview.png') or image['name'].lower().endswith('_placements_overview'):
                continue
            if identifier in aliases:image=dict(image,name=display_name(Path(aliases[identifier]).stem),original_filename=aliases[identifier])
            result.append(image)
        return result

    def _completed(self, image, completed_paths):
        absolute = self.library/self._relative(image['path'])
        relative = absolute.relative_to(self.project).as_posix()
        return (image['id'] in completed_paths or image['path'] in completed_paths or relative in completed_paths
                or str(absolute) in completed_paths or absolute in completed_paths)

    @staticmethod
    def _page(offset, limit):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise CatalogError('页码无效')
        return offset, limit

    def library_page(self, offset=0, limit=12, completed_paths=(), include_deleted=False, hidden_only=False):
        offset, limit = self._page(offset, limit)
        completed_paths = set(completed_paths)
        with self.mutex:
            value = self._load()
            identifiers = ([] if hidden_only or self.single_list_mode else ['pool', 'all']) + [identifier for identifier in value['list_order']
                if (include_deleted or not value['lists'][identifier]['deleted'])
                and (include_deleted or bool(value['lists'][identifier].get('hidden', False)) == hidden_only)]
            rows = []
            for identifier in identifiers[offset:offset+limit]:
                item = self._list(value, identifier, include_deleted=True)
                images = self._active_items(value, item)
                rows.append(dict(id=identifier, name=item['name'], count=len(images),
                    remaining=sum(not self._completed(image, completed_paths) for image in images),
                    readonly=item['readonly'], deleted=item['deleted'], hidden=item.get('hidden', False),
                    sampling_enabled=item.get('sampling_enabled', True) and not item.get('hidden', False), tags=item.get('tags', []),
                    import_pending=(item.get('bundle') or {}).get('state')=='importing'))
            return dict(version=1, revision=value['revision'], total=len(identifiers), offset=offset,
                        next_offset=offset+limit if offset+limit < len(identifiers) else None, lists=rows)

    def list_page(self, list_id, offset=0, limit=20, completed_paths=(), include_deleted=False, deleted_only=False):
        offset, limit = self._page(offset, limit)
        completed_paths = set(completed_paths)
        with self.mutex:
            value = self._load(); item = self._list(value, list_id, include_deleted)
            images = ([value['images'][identifier] for identifier in item['items'] if not deleted_only or value['images'][identifier]['deleted']]
                      if include_deleted else self._active_items(value,item))
            active = [image for image in images if not image['deleted']]
            brief = dict(id=item['id'], name=item['name'], count=len(active),
                remaining=sum(not self._completed(image, completed_paths) for image in active),
                readonly=item['readonly'], deleted=item['deleted'],import_pending=(item.get('bundle') or {}).get('state')=='importing')
            brief.update(hidden=item.get('hidden', False), sampling_enabled=item.get('sampling_enabled', True) and not item.get('hidden', False), tags=item.get('tags', []))
            scene = item.get('scene')
            if scene:
                brief['scene'] = dict(task=scene.get('task'), notes=scene.get('notes'),
                                      files=sorted(scene.get('files') or {}))
            rows = [dict(id=image['id'], name=image['name'], origin=image['origin'],
                         completed=self._completed(image, completed_paths), deleted=image['deleted'])
                    for image in images[offset:offset+limit]]
            return dict(version=1, revision=value['revision'], list=brief, total=len(images),
                offset=offset, next_offset=offset+limit if offset+limit < len(images) else None, items=rows)

    def create_list(self, name, expected_revision):
        identifier = 'list_' + uuid.uuid4().hex
        with self._transaction(expected_revision) as value:
            if len(value['lists']) >= MAX_LISTS:
                raise CatalogError('任务列表数量已满', 'catalog_full', 413)
            value['lists'][identifier] = dict(id=identifier, name=display_name(name), items=[], readonly=False, deleted=False)
            value['list_order'].append(identifier)
        return dict(id=identifier, revision=value['revision'])

    def begin_bundle(self,name,manifest,expected_revision):
        if not isinstance(manifest,list) or not 1<=len(manifest)<=MAX_IMAGES+MAX_SCENE_FILES:
            raise CatalogError('任务包清单无效')
        names=set();images=[];scenes=[]
        for entry in manifest:
            if (not isinstance(entry,dict) or set(entry)!={'name','size','crc','kind'} or entry['kind'] not in ('image','scene')
                    or not isinstance(entry['name'],str) or '/' in entry['name'] or '\\' in entry['name'] or entry['name'].startswith('.')
                    or not 0<len(entry['name'].encode('utf-8'))<=384 or type(entry['crc']) is not int or not 0<=entry['crc']<=0xffffffff
                    or type(entry['size']) is not int or not 0<entry['size']<=(MAX_INPUT_BYTES if entry['kind']=='image' else MAX_SCENE_FILE_BYTES)
                    or entry['name'] in names):raise CatalogError('任务包成员清单无效')
            names.add(entry['name']);(images if entry['kind']=='image' else scenes).append(entry['name'])
        if not images or len(images)>MAX_IMAGES or len(scenes)>MAX_SCENE_FILES:raise CatalogError('任务包数量无效')
        digest=hashlib.sha256(_encoded(manifest)).hexdigest();identifier=None
        with self._transaction(expected_revision) as value:
            for item in value['lists'].values():
                if not item['deleted'] and (item.get('bundle') or {}).get('state')=='importing' and item['name']==display_name(name) and item['bundle']['manifest_sha256']==digest:
                    identifier=item['id'];break
            if identifier is None:
                if len(value['lists'])>=MAX_LISTS:raise CatalogError('列表数量已满','catalog_full',413)
                identifier='list_'+uuid.uuid4().hex
                value['lists'][identifier]=dict(id=identifier,name=display_name(name),items=[],readonly=False,deleted=False,
                    bundle=dict(state='importing',manifest_sha256=digest,image_names=images,scene_names=scenes,image_map={},manifest={entry['name']:entry for entry in manifest}))
                value['list_order'].append(identifier)
        return dict(id=identifier,revision=value['revision'])

    def complete_bundle(self,identifier,expected_revision):
        with self._transaction(expected_revision) as value:
            item=self._list(value,identifier);bundle=item.get('bundle') or {}
            if bundle.get('state') not in ('importing','complete'):raise CatalogError('不是任务包列表')
            if set(bundle.get('image_map',{}))!=set(bundle['image_names']) or not set(bundle['scene_names'])<=set((item.get('scene') or {}).get('files',{})):
                raise CatalogError('任务包尚未完整落盘，请继续导入','bundle_incomplete',409)
            for image_id in bundle['image_map'].values():self._image(value,image_id)
            if bundle['state']=='importing':
                item['items']=list(dict.fromkeys(bundle['image_map'][name] for name in bundle['image_names']))
                item['original_filenames']={bundle['image_map'][name]:name for name in reversed(bundle['image_names'])}
            bundle['state']='complete'
        return dict(id=identifier,revision=value['revision'],images=len(item['items']))

    def set_list_scene(self, identifier, task, notes, files, expected_revision):
        """Attach a task bundle's scene metadata (config.yaml / placements.csv).

        `files` maps a plain file name to its bytes. Contents are written beside
        the uploaded images under _scenes/<list_id>/ and recorded with their
        digest, so the scene that produced a list stays recoverable.
        """
        if not isinstance(files, dict) or not files or len(files) > MAX_SCENE_FILES:
            raise CatalogError('场景文件数量无效')
        for name, raw in files.items():
            if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_SCENE_FILE_BYTES:
                raise CatalogError('场景文件过大或为空', 'scene_too_large', 413)
            if (not isinstance(name, str) or not name or '/' in name or '\\' in name
                    or name.startswith('.') or len(name.encode('utf-8')) > 384):
                raise CatalogError('场景文件名无效')
        _revision(expected_revision)
        with self.mutex:
            initial = self._load()
            if initial['revision'] != expected_revision:
                raise CatalogError('图库已更新，请刷新后重试', 'revision_conflict', 409)
            item = self._list(initial, identifier)
            bundle=item.get('bundle') or {}
            if bundle.get('state')=='importing':
                for name,raw in files.items():
                    expected=bundle['manifest'].get(name)
                    if not expected or expected['kind']!='scene' or len(raw)!=expected['size'] or zlib.crc32(raw)!=expected['crc']:
                        raise CatalogError('辅助文件与 ZIP 清单不一致','bundle_mismatch',409)
            if item['readonly']:
                raise CatalogError('系统列表不能附加场景信息', 'readonly', 409)
        directory = self.library/'_scenes'/identifier
        directory.mkdir(parents=True, exist_ok=True)
        # Write and fsync outside the catalog lock; only the commit is locked.
        recorded = {}
        for name, raw in files.items():
            relative = '_scenes/%s/%s' % (identifier, name)
            destination = self._resolve(relative)
            descriptor, temporary = tempfile.mkstemp(prefix='.scene-', dir=str(destination.parent))
            try:
                with os.fdopen(descriptor, 'wb') as target:
                    target.write(raw); target.flush()
                    if hasattr(os, 'fchmod'):os.fchmod(target.fileno(), 0o600)
                    else:os.chmod(temporary, 0o600)
                    os.fsync(target.fileno())
                os.replace(temporary, str(destination))
            except OSError as error:
                try:os.unlink(temporary)
                except FileNotFoundError:pass
                raise CatalogError('场景文件保存失败，请检查磁盘空间', 'storage_error', 503) from error
            recorded[name] = dict(sha256=hashlib.sha256(raw).hexdigest(),
                                  size_bytes=len(raw), path=relative)
        _sync_directory(directory)
        with self._transaction(expected_revision) as value:
            item = self._list(value, identifier)
            if item['readonly']:
                raise CatalogError('系统列表不能附加场景信息', 'readonly', 409)
            # Accumulate: a bundle's files arrive in separate requests, so keep
            # the ones already recorded instead of replacing the whole set.
            previous = item.get('scene') if isinstance(item.get('scene'), dict) else {}
            merged = dict(previous.get('files') or {})
            merged.update(recorded)
            if len(merged) > MAX_SCENE_FILES:
                raise CatalogError('场景文件数量已满', 'scene_full', 413)
            scene = dict(imported_at=time.time())
            task = task or previous.get('task')
            notes = notes or previous.get('notes')
            if task:scene['task'] = display_name(task, default='')[:1024]
            if notes:scene['notes'] = str(notes)[:1024]
            scene['files'] = merged
            item['scene'] = scene
        return dict(id=identifier, revision=value['revision'], files=sorted(merged))

    def update_list(self, identifier, name, image_ids, expected_revision):
        if (not isinstance(image_ids, list) or len(image_ids) > MAX_IMAGES
                or any(not valid_id(item) for item in image_ids) or len(image_ids) != len(set(image_ids))):
            raise CatalogError('列表顺序无效，不能重复添加同一张图')
        with self._transaction(expected_revision) as value:
            item = self._list(value, identifier)
            if item['readonly']:
                raise CatalogError('原有系统列表不可改顺序，请新建列表后添加图片', 'readonly', 409)
            for image_id in image_ids:
                self._image(value, image_id)
            item.update(name=display_name(name), items=list(image_ids))
        return dict(id=identifier, revision=value['revision'])

    def clone_list(self, source_id, name, expected_revision):
        identifier = 'list_' + uuid.uuid4().hex
        with self._transaction(expected_revision) as value:
            if len(value['lists']) >= MAX_LISTS:
                raise CatalogError('任务列表数量已满', 'catalog_full', 413)
            source = self._list(value, source_id)
            if (source.get('bundle') or {}).get('state')=='importing':raise CatalogError('请先完成任务包导入','bundle_incomplete',409)
            value['lists'][identifier] = dict(id=identifier, name=display_name(name),
                items=[image['id'] for image in self._active_items(value, source)], readonly=False, deleted=False)
            value['list_order'].append(identifier)
            for key in ('scene','original_filenames'):
                if key in source:value['lists'][identifier][key]=copy.deepcopy(source[key])
        return dict(id=identifier, revision=value['revision'])

    def rename_list(self, identifier, name, expected_revision):
        with self._transaction(expected_revision) as value:
            item = self._list(value, identifier)
            if item['readonly']:
                raise CatalogError('系统列表不能改名，请复制为自定义列表', 'readonly', 409)
            item['name'] = display_name(name)
        return dict(id=identifier, revision=value['revision'])

    def move_image(self, list_id, image_id, delta, expected_revision):
        if type(delta) is not int or delta not in (-1, 1):
            raise CatalogError('移动方向无效')
        with self._transaction(expected_revision) as value:
            item = self._list(value, list_id)
            if (item.get('bundle') or {}).get('state')=='importing':raise CatalogError('任务包导入未完成，请先继续导入','bundle_incomplete',409)
            if item['readonly']:
                raise CatalogError('请先复制为自定义列表，再调整顺序', 'readonly', 409)
            self._image(value, image_id)
            if image_id not in item['items']:
                raise CatalogError('图片不在这个列表中', 'not_found', 404)
            index = item['items'].index(image_id)
            target = index+delta
            # Hidden deleted entries do not consume visible ordering steps.
            while 0 <= target < len(item['items']) and value['images'][item['items'][target]]['deleted']:
                target += delta
            if 0 <= target < len(item['items']):
                item['items'][index], item['items'][target] = item['items'][target], item['items'][index]
        return dict(id=list_id, revision=value['revision'])

    def remove_from_list(self, list_id, image_id, expected_revision):
        with self._transaction(expected_revision) as value:
            item = self._list(value, list_id)
            if item['readonly']:
                raise CatalogError('请先复制为自定义列表，再移除图片', 'readonly', 409)
            self._image(value, image_id, include_deleted=True)
            if image_id not in item['items']:
                raise CatalogError('图片不在这个列表中', 'not_found', 404)
            item['items'].remove(image_id)
        return dict(id=list_id, revision=value['revision'])

    def delete_list(self, identifier, expected_revision, restore=False):
        with self._transaction(expected_revision) as value:
            item = self._list(value, identifier, include_deleted=True)
            # A readonly system list may be retired to the recycle bin, but only
            # once its images are reachable through an editable list, so retiring
            # it never hides reference images that exist nowhere else. Retiring
            # only hides the list: image files and collected data are untouched,
            # and import_legacy keeps the retired flag across restarts.
            if item['readonly'] and not restore:
                remaining = {image['id'] for image in self._active_items(value, item)}
                for other in value['lists'].values():
                    if other['id'] == item['id'] or other['deleted'] or other['readonly']:
                        continue
                    remaining -= set(other['items'])
                if remaining:
                    raise CatalogError('系统列表需要先复制为自定义列表，再移入回收站', 'readonly', 409)
            if not restore and any(self.pins.get(image_id) for image_id in item['items']):
                raise CatalogError('列表中有正在准备或采集的图片，请结束后再删除', 'in_use', 409)
            item['deleted'] = not restore
        return dict(id=identifier, revision=value['revision'])

    def delete_image(self, identifier, expected_revision, restore=False):
        with self._transaction(expected_revision) as value:
            image = self._image(value, identifier, include_deleted=True)
            if not restore and self.pins.get(identifier):
                raise CatalogError('图片正在准备或采集中，请结束后再删除', 'in_use', 409)
            image['deleted'] = not restore
        # Original bytes and original path are retained for historical episodes.
        return dict(id=identifier, revision=value['revision'])

    def asset_path(self, identifier, include_deleted=False):
        with self.mutex:
            image = self._image(self._load(), identifier, include_deleted)
            relative = image['path']
        return self._resolve(relative)

    def pin(self, identifier, owner_token):
        if not valid_id(owner_token):
            raise CatalogError('任务锁标识无效')
        with self.mutex:
            image = self._image(self._load(), identifier)
            self.pins.setdefault(identifier, set()).add(owner_token)
            return copy.deepcopy(image)

    def unpin(self, owner_token):
        with self.mutex:
            for identifier in list(self.pins):
                self.pins[identifier].discard(owner_token)
                if not self.pins[identifier]:
                    del self.pins[identifier]

    def freeze_list(self, list_id, expected_revision, completed_paths=(), include_completed=False):
        _revision(expected_revision); completed_paths = set(completed_paths)
        if self.single_list_mode and list_id in ('pool','all'):
            raise CatalogError('请选定一个具体任务列表；本机不使用混合任务池','single_list_required',409)
        with self.mutex:
            value = self._load()
            if value['revision'] != expected_revision:
                raise CatalogError('图库已更新，请刷新列表后再选择', 'revision_conflict', 409)
            if self.task_block_policy and list_id not in ['all','pool']+self.task_block_policy['list_ids']:
                raise CatalogError('此列表不在本机采集任务范围内','sampling_scope',409)
            item = self._list(value, list_id)
            if item.get('hidden', False):
                raise CatalogError('隐藏列表不参与采集，请先在隐藏列表页恢复', 'hidden_list', 409)
            if list_id == 'all':item = self._list(value, 'pool')
            if (item.get('bundle') or {}).get('state')=='importing':raise CatalogError('任务包导入未完成，请先继续导入','bundle_incomplete',409)
            images = [image for image in self._active_items(value, item)
                if include_completed or not self._completed(image, completed_paths)]
            items = [dict(id=image['id'], name=image['name'], origin=image['origin'],
                relative_path=(self.library/self._relative(image['path'])).relative_to(self.project).as_posix(),
                original_filename=image.get('original_filename', image['name'])) for image in images]
            sources = self._sampling_lists(value) if item['id'] == 'pool' else [item]
            scenes = {}
            for asset in items:
                rows = [row for row in sources if asset['id'] in row['items']]
                if not rows:
                    continue
                asset['source_lists'] = [dict(id=row['id'], name=row['name']) for row in rows]
                asset['tags'] = list(dict.fromkeys(tag for row in rows for tag in ([row['name']] + row.get('tags', []))))[:16]
                primary = rows[0]
                alias = primary.get('original_filenames', {}).get(asset['id'])
                if alias:
                    asset.update(name=display_name(Path(alias).stem), original_filename=alias)
                if primary.get('scene'):
                    scenes[primary['id']] = copy.deepcopy(primary['scene'])
                    asset['_scene_id'] = primary['id']
            snapshot = dict(version=1, catalog_revision=value['revision'], list_id=item['id'], list_name=item['name'],
                            total=len(items), items=items)
            if item.get('scene'):snapshot['scene']=copy.deepcopy(item['scene'])
            if scenes:snapshot['scenes']=scenes
            snapshot['queue_sha256'] = hashlib.sha256(_encoded(snapshot)).hexdigest()
            return snapshot

    def per_item_selection(self, snapshot, index, owner_token=None):
        if (not isinstance(snapshot, dict) or type(snapshot.get('version')) is not int or snapshot.get('version') != 1 or type(index) is not int
                or not isinstance(snapshot.get('items'), list) or not 0 <= index < len(snapshot['items'])
                or snapshot.get('total') != len(snapshot['items'])):
            raise CatalogError('任务列表快照无效')
        expected = dict(snapshot); digest = expected.pop('queue_sha256', None)
        if digest != hashlib.sha256(_encoded(expected)).hexdigest():
            raise CatalogError('任务列表快照校验失败', 'invalid_snapshot', 409)
        asset = copy.deepcopy(snapshot['items'][index]); identifier = asset['id']
        asset.pop('_scene_id', None)
        with self.mutex:
            image = self._image(self._load(), identifier)
            if owner_token is not None and owner_token not in self.pins.get(identifier, ()):
                raise CatalogError('参考图尚未锁定', 'not_pinned', 409)
            path = self._resolve(image['path'])
            if path.relative_to(self.project).as_posix() != asset['relative_path']:
                raise CatalogError('参考图来源改变，请重新选择列表', 'source_changed', 409)
        before = path.stat(); hasher = hashlib.sha256()
        with path.open('rb') as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise CatalogError('参考图在准备时发生改变，请重试', 'source_changed', 409)
        asset['sha256'] = hasher.hexdigest()
        if image.get('sha256') and image['sha256'] != asset['sha256']:
            raise CatalogError('参考图内容校验失败，请重新上传', 'source_changed', 409)
        return dict(version=1, catalog_revision=snapshot['catalog_revision'], list_id=snapshot['list_id'],
            list_name=snapshot['list_name'], queue_sha256=digest, position=index+1, total=len(snapshot['items']), asset=asset)

    def snapshot_scene(self,snapshot,destination,index=None):
        scene=(snapshot.get('scenes',{}).get(snapshot['items'][index].get('_scene_id'),snapshot.get('scene'))
               if index is not None else snapshot.get('scene'))
        if not scene:return None
        check=dict(snapshot);digest=check.pop('queue_sha256',None)
        if hashlib.sha256(_encoded(check)).hexdigest()!=digest:raise CatalogError('场景快照校验失败')
        self._validate_scene(scene);directory=Path(destination)/'reference_bundle';directory.mkdir(exist_ok=True)
        recorded={}
        for name,entry in (scene.get('files') or {}).items():
            source=self._resolve(entry['path'])
            if source.stat().st_size>MAX_SCENE_FILE_BYTES:raise CatalogError('场景文件过大','source_changed',409)
            with source.open('rb') as handle:raw=handle.read(MAX_SCENE_FILE_BYTES+1)
            if len(raw)!=entry['size_bytes'] or len(raw)>MAX_SCENE_FILE_BYTES or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
                raise CatalogError('任务包场景文件已变化，请重新准备','source_changed',409)
            fd,tmp=tempfile.mkstemp(prefix='.bundle-',dir=str(directory))
            try:
                with os.fdopen(fd,'wb') as handle:handle.write(raw);handle.flush();os.fsync(handle.fileno())
                os.replace(tmp,str(directory/name))
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
            recorded[name]=dict(path='reference_bundle/'+name,sha256=entry['sha256'],size_bytes=len(raw))
        _sync_directory(directory);_sync_directory(Path(destination))
        return dict(task=scene.get('task'),notes=scene.get('notes'),files=recorded)

    def upload(self, raw, filename, list_id, expected_revision):
        """Call in an HTTP/background worker, not the acquisition/controller loop."""
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_INPUT_BYTES:
            raise CatalogError('每张图片最多12MiB', 'image_too_large', 413)
        _revision(expected_revision)
        with self.mutex:
            initial = self._load()
            if initial['revision'] != expected_revision:
                raise CatalogError('图库已更新，请刷新后再上传', 'revision_conflict', 409)
            if self._list(initial, list_id)['readonly']:
                raise CatalogError('请先新建一个任务列表，再上传图片', 'readonly', 409)
            bundle=self._list(initial,list_id).get('bundle') or {}
            if bundle.get('state')=='importing':
                expected=bundle['manifest'].get(str(filename).replace('\\','/').rsplit('/',1)[-1])
                if not expected or expected['kind']!='image' or len(raw)!=expected['size'] or zlib.crc32(raw)!=expected['crc']:
                    raise CatalogError('图片与 ZIP 清单不一致','bundle_mismatch',409)
        self.library.mkdir(parents=True, exist_ok=True)
        from PIL import Image, ImageOps
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as source:
                    if source.format not in ('PNG', 'JPEG', 'WEBP') or getattr(source, 'n_frames', 1) != 1:
                        raise CatalogError('请选择静态PNG、JPEG或WebP图片', 'unsupported_image', 415)
                    if (source.width <= 0 or source.height <= 0 or max(source.size) > MAX_DIMENSION
                            or source.width*source.height > MAX_PIXELS):
                        raise CatalogError('图片最多2000万像素，单边不超过16384', 'image_too_large', 413)
                    required = max(len(raw)*2, source.width*source.height*4)
                    if shutil.disk_usage(self.library).free < MIN_FREE_BYTES+required:
                        raise CatalogError('磁盘剩余空间不足，请先导出采集数据', 'disk_full', 507)
                    source.load(); image = ImageOps.exif_transpose(source)
                    if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                        rgba = image.convert('RGBA'); background = Image.new('RGBA', rgba.size, 'white')
                        background.alpha_composite(rgba); image = background.convert('RGB')
                    else:
                        image = image.convert('RGB')
                    width, height = image.size
                    # Orientation has already been applied to pixels. Do not
                    # copy phone GPS/Artist/timestamps or other source metadata
                    # into the canonical content-addressed PNG.
                    image.info.clear()
                    output = io.BytesIO(); image.save(output, 'PNG', optimize=False)
                    normalized = output.getvalue()
        except CatalogError:
            raise
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise CatalogError('图片无法解码，请换一张图片', 'invalid_image', 415) from error
        if len(normalized) > MAX_NORMALIZED_BYTES:
            raise CatalogError('转换后的图片过大，请降低图片分辨率', 'image_too_large', 413)
        digest = hashlib.sha256(normalized).hexdigest(); source_digest = hashlib.sha256(raw).hexdigest()
        identifier = 'image_' + digest[:40]
        relative = '_uploads/'+identifier+'_1.png'; destination = self._resolve(relative)
        destination.parent.mkdir(exist_ok=True); _sync_directory(destination.parent.parent)
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise CatalogError('同名图片内容不一致，请联系维护人员', 'source_changed', 503)
        # The slow PNG write/fsync is outside the mutex. Only the final link and
        # compact catalog commit share the pin/delete lock.
        descriptor, temporary = tempfile.mkstemp(prefix='.upload-', dir=str(destination.parent))
        linked_inode = None
        deduplicated = False
        try:
            with os.fdopen(descriptor, 'wb') as target:
                target.write(normalized); target.flush()
                if hasattr(os, 'fchmod'):os.fchmod(target.fileno(), 0o600)
                else:os.chmod(temporary, 0o600)
                os.fsync(target.fileno())
            with self._transaction(expected_revision) as value:
                item = self._list(value, list_id)
                bundle=item.get('bundle') or {}
                clean_filename=str(filename).replace('\\','/').rsplit('/',1)[-1]
                if bundle.get('state')=='importing' and clean_filename not in bundle['image_names']:
                    raise CatalogError('该图片不在任务包清单中')
                if item['readonly']:
                    raise CatalogError('请先新建一个任务列表，再上传图片', 'readonly', 409)
                existing = value['images'].get(identifier)
                deduplicated = existing is not None
                if existing is not None and existing['deleted']:
                    raise CatalogError('同一图片在回收站中，请先恢复', 'deleted', 409)
                if existing is None and len(value['images']) >= MAX_IMAGES:
                    raise CatalogError('图片数量已满', 'catalog_full', 413)
                try:
                    os.link(temporary, str(destination)); linked_inode = destination.stat().st_ino
                    _sync_directory(destination.parent)
                except FileExistsError:
                    pass  # Same canonical digest; verified before the transaction.
                if existing is None:
                    clean_filename = str(filename).replace('\\', '/').rsplit('/', 1)[-1]
                    value['images'][identifier] = dict(id=identifier, name=display_name(Path(clean_filename).stem), path=relative,
                        origin='upload', deleted=False, original_filename=display_name(clean_filename),
                        source_sha256=source_digest, sha256=digest,
                        size_bytes=len(normalized), width=width, height=height, created_at=time.time())
                    value['image_order'].append(identifier)
                if identifier not in item['items']:
                    item['items'].append(identifier)
                if bundle.get('state')=='importing':bundle['image_map'][clean_filename]=identifier
        except (CatalogError, OSError) as error:
            # Never erase a file that committed successfully or was adopted by
            # another uploader. Every uploader links its own fully synced temp
            # inside the final lock before adding a metadata reference.
            if linked_inode is not None:
                with self.mutex:
                    current = self._load()
                    if identifier not in current['images'] and destination.exists() and destination.stat().st_ino == linked_inode:
                        destination.unlink(); _sync_directory(destination.parent)
            if isinstance(error, CatalogError):raise
            raise CatalogError('图片保存失败，请检查磁盘空间', 'storage_error', 503) from error
        finally:
            try:os.unlink(temporary)
            except FileNotFoundError:pass
        return dict(id=identifier, revision=value['revision'], width=width, height=height, sha256=digest, deduplicated=deduplicated)

    def thumbnail(self, identifier, expected_revision=None, include_deleted=False):
        if expected_revision is not None and _revision(expected_revision) != self.revision:
            raise CatalogError('图库已更新，请刷新后预览', 'revision_conflict', 409)
        path = self.asset_path(identifier, include_deleted=include_deleted)
        info = path.stat(); key = (identifier, info.st_mtime_ns, info.st_size)
        with self._thumbnail_lock:
            cached = self._thumbnail_cache.get(key)
        if cached is not None:
            return cached
        from PIL import Image, ImageOps
        try:
            with Image.open(path) as source:
                if source.width*source.height > MAX_PIXELS or max(source.size) > MAX_DIMENSION:
                    raise CatalogError('图片尺寸过大，无法预览', 'image_too_large', 413)
                source.load(); image = ImageOps.exif_transpose(source).convert('RGB')
                image.thumbnail((760, 360), getattr(Image, 'Resampling', Image).LANCZOS)
                for quality in (86, 74, 60):
                    output = io.BytesIO(); image.save(output, 'JPEG', quality=quality, subsampling=0)
                    raw = output.getvalue()
                    if len(raw) <= MAX_THUMBNAIL_BYTES:
                        break
                else:
                    raise CatalogError('缩略图过大', 'image_too_large', 413)
        except CatalogError:raise
        except (OSError, ValueError) as error:
            raise CatalogError('无法加载这张图片', 'invalid_image', 415) from error
        with self._thumbnail_lock:
            if len(self._thumbnail_cache) >= 8:
                self._thumbnail_cache.pop(next(iter(self._thumbnail_cache)))
            self._thumbnail_cache[key] = raw
        return raw
