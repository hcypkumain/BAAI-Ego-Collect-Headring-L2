"""Same-origin mobile/PC reference management routes for the existing dashboard.

Call handle_reference_request before the dashboard's global authorized() check.
It grants library-only access via X-Reference-Key; existing /api/action remains
under the dashboard's original control authorization and revision checks.
"""
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

from reference_catalog import (CatalogError, MAX_INPUT_BYTES, MAX_SCENE_FILE_BYTES,
                               unique_json)
from reference_credentials import authorize_library

GET_PATHS = frozenset(('/api/reference-library', '/api/reference-list', '/api/reference-thumbnail'))
POST_PATHS = frozenset(('/api/reference-library', '/api/reference-upload', '/api/reference-scene'))
MAX_ACTION_BYTES = 512 * 1024


def _send(handler, status, value, content='application/json; charset=utf-8'):
    try:
        handler.send(status, value, content)
    except (OSError, TimeoutError):
        pass


def _parameters(query, allowed):
    values = parse_qs(query, keep_blank_values=True, strict_parsing=True) if query else {}
    if set(values)-set(allowed) or any(len(value) != 1 for value in values.values()):
        raise CatalogError('请求参数无效')
    return {key: value[0] for key, value in values.items()}


def _integer(value, default, maximum=9007199254740991):
    if value is None:
        return default
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdecimal():
        raise CatalogError('页码或版本无效')
    number = int(value)
    if not 0 <= number <= maximum:
        raise CatalogError('页码或版本无效')
    return number


def _length(handler, maximum):
    value = handler.headers.get('Content-Length', '')
    number = _integer(value, None)
    if number is None or number < 1:
        raise CatalogError('请求内容为空')
    if number > maximum:
        raise CatalogError('上传内容过大，请减少图片大小', 'body_too_large', 413)
    return number


def _read_body(handler, length):
    deadline = time.monotonic()+30
    chunks = []
    remaining = length
    while remaining:
        if time.monotonic() >= deadline:
            raise CatalogError('上传超时，请检查网络后刷新图库', 'upload_timeout', 408)
        handler.connection.settimeout(min(5, max(.05, deadline-time.monotonic())))
        chunk = handler.rfile.read(min(65536, remaining))
        if not chunk:
            raise CatalogError('上传未完成，请刷新图库后重试', 'incomplete_upload', 400)
        chunks.append(chunk); remaining -= len(chunk)
    return b''.join(chunks)


def _completed(owner):
    provider = getattr(owner, 'reference_completed_paths', None)
    if callable(provider):
        return provider()
    return ()


def handle_reference_request(handler, owner, method):
    """Return True if this is a library route, including rejected requests."""
    if len(handler.path) > 4096:
        return False
    try:
        url = urlsplit(handler.path)
    except ValueError:
        return False
    if method == 'GET' and url.path in ('/references', '/references/'):
        if url.query:
            _send(handler, 400, {'error': 'invalid_path', 'message': '请从设备上的上传入口打开图库'})
        else:
            page = getattr(owner, 'reference_page', None)
            if page is None:
                try:page = Path(__file__).with_name('references.html').read_bytes()
                except OSError:
                    _send(handler, 503, {'error': 'library_unavailable', 'message': '图库页面暂不可用，请稍后刷新'})
                    return True
            _send(handler, 200, page, 'text/html; charset=utf-8')
        return True
    if url.path not in GET_PATHS | POST_PATHS:
        return False
    try:
        if getattr(owner, 'reference_catalog', None) is None or getattr(owner, 'reference_credentials', None) is None:
            raise CatalogError('图库暂未就绪，请稍后刷新或返回手机采集页面重试', 'library_unavailable', 503)
        seen = set()
        for name, value in handler.headers.items():
            name = name.lower()
            if name in seen or len(value) > 4096:
                raise CatalogError('请求头无效')
            seen.add(name)
        if 'transfer-encoding' in seen:
            raise CatalogError('上传内容格式无效')
        if method == 'POST':
            origin = handler.headers.get('Origin')
            if origin is not None and origin != 'http://'+handler.headers.get('Host', ''):
                raise CatalogError('请在同一个图库页面操作', 'cross_origin_forbidden', 403)
        authorized, limited = authorize_library(handler, owner)
        if not authorized:
            _send(handler, 429 if limited else 401, dict(error='rate_limited' if limited else 'library_code_required',
                message='尝试次数较多，请30秒后重试' if limited else '请输入手机采集页面显示的图库管理码'))
            return True
        store = owner.reference_catalog
        importer=getattr(owner,'usb_task_importer',None)
        if method=='POST' and importer is not None and importer.active:
            raise CatalogError('正在从U盘替换任务库，完成后再编辑列表','usb_import_busy',423)
        if method == 'GET':
            if url.path not in GET_PATHS:
                raise CatalogError('该接口不支持读取', 'method_not_allowed', 405)
            if handler.headers.get('Content-Length', '0') != '0':
                raise CatalogError('读取请求不能带上传内容')
            if url.path == '/api/reference-library':
                args = _parameters(url.query, ('offset', 'limit', 'include_deleted', 'hidden_only'))
                deleted = args.get('include_deleted', '0')
                if deleted not in ('0', '1') or args.get('hidden_only','0') not in ('0','1'):
                    raise CatalogError('回收站参数无效')
                value = store.library_page(_integer(args.get('offset'), 0), _integer(args.get('limit'), 12, 50),
                    completed_paths=_completed(owner), include_deleted=deleted == '1', hidden_only=args.get('hidden_only') == '1')
                _send(handler, 200, value)
            elif url.path == '/api/reference-list':
                args = _parameters(url.query, ('id', 'offset', 'limit', 'include_deleted', 'deleted_only'))
                deleted = args.get('include_deleted', '0')
                only_deleted = args.get('deleted_only', '0')
                if deleted not in ('0', '1') or only_deleted not in ('0', '1') or (only_deleted == '1' and deleted != '1'):
                    raise CatalogError('回收站参数无效')
                value = store.list_page(args.get('id'), _integer(args.get('offset'), 0), _integer(args.get('limit'), 20, 50),
                    completed_paths=_completed(owner), include_deleted=deleted == '1', deleted_only=only_deleted == '1')
                _send(handler, 200, value)
            else:
                args = _parameters(url.query, ('id', 'revision', 'include_deleted'))
                deleted = args.get('include_deleted', '0')
                if deleted not in ('0', '1'):
                    raise CatalogError('回收站参数无效')
                if not store.thumbnail_slots.acquire(blocking=False):
                    raise CatalogError('预览正在加载，请稍后再试', 'thumbnail_busy', 503)
                try:
                    raw = store.thumbnail(args.get('id'), _integer(args.get('revision'), None), include_deleted=deleted == '1')
                finally:store.thumbnail_slots.release()
                _send(handler, 200, raw, 'image/jpeg')
            return True
        if method != 'POST' or url.path not in POST_PATHS:
            raise CatalogError('该接口不支持此操作', 'method_not_allowed', 405)
        if url.path == '/api/reference-scene':
            # One scene file per request, mirroring the single-image upload
            # route: the browser extracts the bundle and posts each member.
            args = _parameters(url.query, ('list_id', 'revision', 'name', 'task'))
            if set(args)-{'task'} != {'list_id', 'revision', 'name'}:
                raise CatalogError('场景参数缺失')
            content = handler.headers.get('Content-Type', '').lower()
            if content not in ('application/octet-stream', 'text/csv', 'text/plain',
                               'text/yaml', 'application/x-yaml'):
                raise CatalogError('场景文件格式无效', 'unsupported_scene', 415)
            length = _length(handler, MAX_SCENE_FILE_BYTES)
            raw = _read_body(handler, length)
            result = store.set_list_scene(args['list_id'], args.get('task'), None,
                                          {args['name']: raw}, _integer(args['revision'], None))
            _send(handler, 201, result)
            return True
        if url.path == '/api/reference-upload':
            args = _parameters(url.query, ('list_id', 'revision', 'name'))
            if set(args) != {'list_id', 'revision', 'name'}:
                raise CatalogError('上传参数缺失')
            content = handler.headers.get('Content-Type', '').lower()
            if content not in ('application/octet-stream', 'image/png', 'image/jpeg', 'image/webp'):
                raise CatalogError('请选择PNG、JPEG或WebP图片', 'unsupported_image', 415)
            length = _length(handler, MAX_INPUT_BYTES)
            if not store.upload_slots.acquire(blocking=False):
                raise CatalogError('已有图片正在上传，请稍后再试', 'upload_busy', 503)
            try:
                raw = _read_body(handler, length)
                result = store.upload(raw, args['name'], args['list_id'], _integer(args['revision'], None))
            finally:
                store.upload_slots.release()
            _send(handler, 201, result)
            return True
        if url.query:
            raise CatalogError('列表操作参数无效')
        if handler.headers.get('Content-Type', '').lower() not in ('application/json', 'application/json; charset=utf-8'):
            raise CatalogError('请使用图库页面提交操作', 'json_required', 415)
        raw = _read_body(handler, _length(handler, MAX_ACTION_BYTES))
        try:
            value = unique_json(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise CatalogError('列表操作格式无效') from error
        if not isinstance(value, dict):
            raise CatalogError('列表操作格式无效')
        action = value.get('action')
        fields = {'create_list': {'action', 'name', 'revision'},
            'set_list_sampling': {'action','id','hidden','enabled','tags','revision'},
            'begin_bundle': {'action','name','manifest','revision'}, 'complete_bundle': {'action','id','revision'},
            'clone_list': {'action', 'source_id', 'name', 'revision'},
            'rename_list': {'action', 'id', 'name', 'revision'},
            'update_list': {'action', 'id', 'name', 'items', 'revision'},
            'move_image': {'action', 'id', 'image_id', 'delta', 'revision'},
            'remove_image': {'action', 'id', 'image_id', 'revision'},
            'delete_list': {'action', 'id', 'revision'}, 'restore_list': {'action', 'id', 'revision'},
            'delete_image': {'action', 'id', 'revision'}, 'restore_image': {'action', 'id', 'revision'}}
        if action not in fields or set(value) != fields[action]:
            raise CatalogError('未知或无效的图库操作')
        revision = value['revision']
        if action == 'create_list':result = store.create_list(value['name'], revision)
        elif action == 'set_list_sampling':result = store.set_list_sampling(value['id'], value['hidden'], value['enabled'], value['tags'], revision)
        elif action == 'begin_bundle':result = store.begin_bundle(value['name'],value['manifest'],revision)
        elif action == 'complete_bundle':result = store.complete_bundle(value['id'],revision)
        elif action == 'clone_list':result = store.clone_list(value['source_id'], value['name'], revision)
        elif action == 'rename_list':result = store.rename_list(value['id'], value['name'], revision)
        elif action == 'update_list':result = store.update_list(value['id'], value['name'], value['items'], revision)
        elif action == 'move_image':result = store.move_image(value['id'], value['image_id'], value['delta'], revision)
        elif action == 'remove_image':result = store.remove_from_list(value['id'], value['image_id'], revision)
        elif action in ('delete_list', 'restore_list'):
            result = store.delete_list(value['id'], revision, restore=action == 'restore_list')
        else:result = store.delete_image(value['id'], revision, restore=action == 'restore_image')
        _send(handler, 200, result)
    except CatalogError as error:
        _send(handler, error.status, dict(error=error.code, message=str(error), result_uncertain=error.committed))
    except (OSError, TimeoutError):
        _send(handler, 503, dict(error='storage_or_network_error', message='上传或保存中断，请刷新图库确认结果后再操作'))
    except (ValueError, TypeError, KeyError, RecursionError):
        _send(handler, 400, dict(error='invalid_request', message='请求格式无效，请刷新页面'))
    return True
