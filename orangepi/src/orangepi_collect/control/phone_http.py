"""Small same-origin phone UI boundary; never owns a recorder or device.

Optional reads use one bounded slot and a fixed loopback destination. Control
actions continue through Dashboard.submit, including the screen privacy gate.
"""
from http.client import HTTPConnection, HTTPException
import time
import json
from pathlib import Path
import threading
import re
import html
import struct
from urllib.parse import parse_qs, urlsplit

ASSETS = {'/phone-app.js': ('phone_app.js', 'text/javascript; charset=utf-8'),
          '/reference-bundles.js': ('reference_bundles.js','text/javascript; charset=utf-8'),
          '/phone-references.js': ('phone_references.js', 'text/javascript; charset=utf-8'),
          '/phone-diagnostics.js': ('phone_diagnostics.js', 'text/javascript; charset=utf-8'),
          '/phone-recordings.js': ('phone_recordings.js', 'text/javascript; charset=utf-8'),
          '/phone-uploads.js': ('phone_uploads.js','text/javascript; charset=utf-8'),
          '/signal-quality.js': ('signal_quality.js', 'text/javascript; charset=utf-8'),
          '/waveform-plot.js': ('waveform_plot.js','text/javascript; charset=utf-8'),
          '/phone-transport.js':('phone_transport.js','text/javascript; charset=utf-8'),
          '/phone-shell.js': ('phone_shell.js', 'text/javascript; charset=utf-8'),
          '/phone-connections.js': ('phone_connections.js', 'text/javascript; charset=utf-8'),
          '/phone-headphones.js': ('phone_headphones.js', 'text/javascript; charset=utf-8'),
          '/phone-capture.js': ('phone_capture.js', 'text/javascript; charset=utf-8'),
          '/phone-recording.js': ('phone_recording.js', 'text/javascript; charset=utf-8'),
          '/phone-app.css': ('phone_app.css', 'text/css; charset=utf-8')}
MANIFEST = dict(id='/phone', name='Pi 采集终端', short_name='采集终端',
                lang='zh-CN', start_url='/phone', scope='/', display='standalone',
                background_color='#000000', theme_color='#006e78',
                icons=[dict(src='/phone-icon.svg', sizes='any', type='image/svg+xml', purpose='any')])
ICON = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192"><rect width="192" height="192" rx="42" fill="#006e78"/><path d="M38 66h116M63 66v67m63-67v67" stroke="white" stroke-width="15" stroke-linecap="round"/></svg>'
ROUTES = frozenset(('/reference.jpg', '/headphones', '/replay/sessions',
                    '/replay/session', '/replay/frame.jpg', '/replay/signal', '/replay/release','/replay/qc'))
SAFE_REPLAY = frozenset(('device_confirm', 'idle', 'failed_wait', 'exhausted', 'confirm_stop'))
MAX_REPLY = 1024 * 1024
PREVIEW_CAMERAS = frozenset(('cam_left_wrist','cam_right_wrist','cam_ros_left',
                            'cam_ros_right','cam_ros_bleft','cam_ros_bright'))
PREVIEW_STATES = SAFE_REPLAY | frozenset(('ready','wrist_choose','wrist_manual',
    'wrist_baseline','wrist_left','wrist_right','wrist_restore','emg_manual','emg_pairing'))


def preview_allowed(snapshot):
    privacy = snapshot.get('privacy') or {}
    return (snapshot.get('state') in PREVIEW_STATES
            and privacy.get('required') is False
            and privacy.get('stop_confirmed') is True
            and privacy.get('phase') == 'idle'
            and (snapshot.get('worker') or {}).get('recording_active') is not True)


def handle_preview_stream(handler,owner,url,method):
    """One HTTP response carries latest JPEGs; no Wi-Fi roundtrip per frame."""
    name=url.path[len('/api/phone/stream/'):];query=parse_qs(url.query,keep_blank_values=True)
    if method!='GET' or name not in {n+'.jpg' for n in PREVIEW_CAMERAS}:
        handler.send(404,{'error':'不存在'});return
    quality=query.get('quality',['fast'])[0]
    if set(query)-{'quality'} or any(len(v)!=1 for v in query.values()) or quality not in ('fast','balanced','source'):
        handler.send(400,{'error':'预览请求无效'});return
    relay_frames(handler,owner,'/phone/stream/'+name+'?quality='+quality,preview_allowed)


def relay_frames(handler,owner,target,allowed):
    from frame_stream import read_frame,packet,begin
    before=owner.display_state()
    if not allowed(before):handler.send(423,{'error':'采集期间暂停附加显示'});return
    if not owner.phone_stream_slots.acquire(False):handler.send(429,{'error':'显示连接繁忙'});return
    connection=HTTPConnection('127.0.0.1',8766,timeout=8 if target.startswith('/replay/') else 2)
    lease='phone-stream:%x'%id(handler)
    cameras=target.startswith('/phone/stream/')
    selected=[target.split('/')[-1].split('?')[0][:-4]] if cameras else None
    try:
        owner.demands.touch(lease,(),cameras,selected)
        connection.request('GET',target,headers={'X-Access-Token':owner.token});reply=connection.getresponse()
        if reply.status!=200:handler.send(reply.status,reply.read(MAX_REPLY),'application/json');return
        handler.connection.settimeout(2);begin(handler);deadline=time.monotonic()+22
        while time.monotonic()<deadline:
            owner.demands.touch(lease,(),cameras,selected)
            item=read_frame(reply)
            if item is None:break
            meta,raw=item;current=owner.display_state()
            if not allowed(current) or any(before.get(k)!=current.get(k) for k in ('session','preview_id')):break
            handler.wfile.write(packet(meta,raw))
    except (OSError,ValueError,HTTPException):pass
    finally:connection.close();owner.demands.release(lease);owner.phone_stream_slots.release()


def handle_camera_stream(handler,owner,url):
    from frame_stream import packet,begin
    from camera_check import CameraCheckError
    query=parse_qs(url.query,keep_blank_values=True)
    try:
        if set(query)-{'columns'} or any(len(v)!=1 for v in query.values()):raise ValueError()
        columns=int(query.get('columns',['3'])[0]);raw,meta=owner.camera_checks.sheet(columns)
    except CameraCheckError as error:handler.send(error.status,{'error':str(error)});return
    except ValueError:handler.send(400,{'error':'无效检查布局'});return
    if not owner.phone_stream_slots.acquire(False):handler.send(429,{'error':'显示连接繁忙'});return
    lease='camera-check:%x'%id(handler)
    try:
        owner.demands.touch(lease,(),True)
        handler.connection.settimeout(2);begin(handler);deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            started=time.monotonic()
            if not preview_allowed(owner.display_state()):break
            owner.demands.touch(lease,(),True)
            handler.wfile.write(packet(meta,raw));time.sleep(max(0,.4-(time.monotonic()-started)))
            raw,meta=owner.camera_checks.sheet(columns)
    except (OSError,ValueError):pass
    finally:owner.demands.release(lease);owner.phone_stream_slots.release()


def handle_wrist_stream(handler,owner,url):
    from frame_stream import packet,begin
    query=parse_qs(url.query,keep_blank_values=True);before=owner.display_state()
    if set(query)!={'preview_id'} or len(query['preview_id'])!=1 or query['preview_id'][0]!=before.get('preview_id') or not str(before.get('state','')).startswith('wrist_') or not preview_allowed(before):handler.send(423,{'error':'腕部标定画面已变化'});return
    if not owner.phone_stream_slots.acquire(False):handler.send(429,{'error':'显示连接繁忙'});return
    lease='wrist-stream:%x'%id(handler)
    try:
        owner.demands.touch(lease,(),True)
        begin(handler);last={};deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            state=owner.display_state()
            if not preview_allowed(state) or not str(state.get('state','')).startswith('wrist_') or state.get('preview_id')!=before.get('preview_id'):break
            owner.demands.touch(lease,(),True)
            for name in ('cam_left_wrist','cam_right_wrist'):
                path=owner.preview/(name+'.jpg')
                try:
                    info=path.stat();key=(info.st_ino,info.st_mtime_ns,info.st_size)
                    if path.is_symlink() or key==last.get(name) or not 0<info.st_size<=2*1024*1024 or not 0<=time.time()-info.st_mtime<3:continue
                    raw=path.read_bytes()
                    current=owner.display_state()
                    if not preview_allowed(current) or current.get('preview_id')!=before.get('preview_id'):return
                    handler.wfile.write(packet(dict(camera=name,preview_id=before['preview_id']),raw));last[name]=key
                except FileNotFoundError:continue
            time.sleep(.5)
    except (OSError,ValueError):pass
    finally:owner.demands.release(lease);owner.phone_stream_slots.release()


def handle_preview(handler, owner, url, method):
    """One latest source JPEG at a time; control and STOP never share this slot."""
    name = url.path[len('/api/phone/preview/'):]
    release = name == 'release'
    if method != 'GET' or (not release and name not in {n+'.jpg' for n in PREVIEW_CAMERAS}):
        handler.send(404, {'error':'不存在'}); return
    query = parse_qs(url.query, keep_blank_values=True)
    try:
        if set(query)-{'after','wait_ms','quality'} or any(len(v)!=1 for v in query.values()):raise ValueError()
        if query.get('quality',['fast'])[0] not in ('fast','balanced','source'):raise ValueError()
        after = query.get('after',[''])[0]
        wait_ms = int(query.get('wait_ms',['150'])[0])
        if (not 0 <= wait_ms <= 150 or len(after)>70 or
                (after and not re.fullmatch(r'[A-Za-z0-9_-]{1,48}:[0-9]{1,20}',after))):raise ValueError()
        if release and query:raise ValueError()
    except ValueError:
        handler.send(400,{'error':'预览请求无效'}); return
    started=time.perf_counter();before=owner.display_state();gate_ms=(time.perf_counter()-started)*1000
    if not release and not preview_allowed(before):
        handler.send(423,{'error':'采集期间暂停画面'}); return
    if not release:owner.demands.touch('phone-frame:'+handler.client_address[0],(),True,[name[:-4]])
    if not owner.phone_preview_slots.acquire(False):
        handler.send(429,{'error':'预览正在读取'}); return
    connection = HTTPConnection('127.0.0.1',8766,timeout=2)
    try:
        target = '/phone/preview/'+name+('?' + url.query if url.query else '')
        connection.request('GET',target,headers={'X-Access-Token':owner.token})
        reply = connection.getresponse()
        raw = reply.read(2*1024*1024+1)
        sidecar_ms=(time.perf_counter()-started)*1000-gate_ms
        if len(raw)>2*1024*1024:raise ValueError()
        after=owner.display_state()
        if not release and (not preview_allowed(after) or any(before.get(key)!=after.get(key) for key in ('session','preview_id'))):
            handler.send(423,{'error':'画面已暂停'}); return
        frame_id = reply.getheader('X-Preview-Frame','')
        if frame_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,48}:[0-9]{1,20}',frame_id):raise ValueError()
        content = 'image/jpeg' if reply.status==200 and not release else 'application/json'
        headers={'Server-Timing':'gate;dur=%.2f, sidecar;dur=%.2f'%(gate_ms,sidecar_ms)}
        if frame_id:headers['X-Preview-Frame']=frame_id
        handler.send(reply.status,raw,content,extra_headers=headers)
    except (OSError,ValueError,HTTPException):
        handler.send(503,{'error':'等待相机画面'})
    finally:
        connection.close(); owner.phone_preview_slots.release()


def optional_read_allowed(snapshot, replay=False):
    privacy = snapshot.get('privacy') or {}
    worker = snapshot.get('worker') or {}
    if (privacy.get('required') is not False or privacy.get('stop_confirmed') is not True
            or privacy.get('phase') != 'idle' or worker.get('recording_active') is True):
        return False
    return snapshot.get('state') in (SAFE_REPLAY if replay else SAFE_REPLAY | {'ready'})


def handle_phone_request(handler, owner, method):
    url = urlsplit(handler.path)
    path = url.path
    if method == 'GET':
        if path in ('/','/phone', '/phone/'):
            handler.send(200,owner.page,'text/html; charset=utf-8'); return True
        if path in ASSETS:
            filename, content = ASSETS[path]
            handler.send(200, Path(__file__).with_name(filename).read_bytes(), content); return True
        if path == '/app.webmanifest':
            handler.send(200, json.dumps(MANIFEST, ensure_ascii=False).encode(), 'application/manifest+json'); return True
        if path == '/phone-icon.svg':
            handler.send(200, ICON, 'image/svg+xml'); return True
    if not path.startswith('/api/phone/'):
        return False
    if not handler.authorized():
        handler.send(401, {'error': '请输入此设备的访问码'}); return True
    origin = handler.headers.get('Origin')
    if origin and origin != 'http://' + handler.headers.get('Host', ''):
        handler.send(403, {'error': '不允许跨站访问'}); return True
    if len(handler.path) > 1024 or handler.headers.get('Transfer-Encoding'):
        handler.send(400, {'error': '请求无效'}); return True
    if path.startswith('/api/phone/cloud/'):
        from cloud_http import handle_cloud
        return handle_cloud(handler,owner,method)
    if path=='/api/phone/camera-check/stream' and method=='GET':
        handle_camera_stream(handler,owner,url);return True
    if path=='/api/phone/wrist-stream' and method=='GET':
        handle_wrist_stream(handler,owner,url);return True
    if path=='/api/phone/replay-stream' and method=='GET':
        query=parse_qs(url.query,keep_blank_values=True)
        if set(query)-{'session','stream','start','limit'} or any(len(v)!=1 for v in query.values()):handler.send(400,{'error':'无效回看请求'});return True
        relay_frames(handler,owner,'/replay/stream?'+url.query,lambda s:optional_read_allowed(s,replay=True));return True
    if path=='/api/phone/camera-check.jpg' and method=='GET':
        from camera_check import CameraCheckError
        try:
            query=parse_qs(url.query,keep_blank_values=True)
            if set(query)-{'columns','after_unix_ms','request_id'} or any(len(v)!=1 for v in query.values()):raise CameraCheckError('无效检查请求',400)
            after=int(query['after_unix_ms'][0]) if 'after_unix_ms' in query else None
            if after is not None and not time.time()*1000-30000<=after<=time.time()*1000+8000:raise ValueError()
            request_id=query.get('request_id',[None])[0]
            if (after is None)!=(request_id is None):raise ValueError()
            raw,meta=owner.camera_checks.sheet(int(query.get('columns',['3'])[0]),after_unix_ms=after,request_id=request_id)
            handler.send(200,raw,'image/jpeg',extra_headers={'X-Camera-Check-Meta':json.dumps(meta,separators=(',',':'))})
        except CameraCheckError as error:handler.send(error.status,{'error':str(error)})
        except ValueError:handler.send(400,{'error':'无效检查布局'})
        return True
    if path=='/api/phone/camera-check/confirm' and method=='POST':
        from camera_check import CameraCheckError
        try:
            length=int(handler.headers.get('Content-Length','0'))
            if not 1<=length<=256:raise ValueError()
            value=json.loads(handler.rfile.read(length))
            if not isinstance(value,dict) or set(value)!={'check_id'} or not isinstance(value['check_id'],str):raise ValueError()
            handler.send(200,owner.camera_checks.confirm(value['check_id']))
        except CameraCheckError as error:handler.send(error.status,{'error':str(error)})
        except (ValueError,TypeError):handler.send(400,{'error':'检查确认无效'})
        return True
    if path=='/api/phone/links' and method=='GET':
        handler.send(200,owner.phone_links.snapshot('http://'+handler.headers.get('Host','')));return True
    if path.startswith('/api/phone/preview/'):
        handle_preview(handler,owner,url,method); return True
    if path.startswith('/api/phone/stream/'):
        handle_preview_stream(handler,owner,url,method); return True
    snapshot = owner.display_state()
    if path in ('/api/phone/usb-task-import/scan','/api/phone/usb-task-import/status'):
        scanning=path.endswith('/scan')
        if url.query or (scanning and method!='POST') or (not scanning and (method!='GET' or handler.headers.get('Content-Length','0')!='0')):
            handler.send(400,{'error':'U盘导入请求无效'});return True
        manager=getattr(owner,'usb_task_importer',None)
        if manager is None:handler.send(503,{'error':'U盘导入功能暂不可用'});return True
        from usb_task_import import allowed
        if scanning and not allowed(snapshot):
            handler.send(423,{'error':'请先结束或放弃当前准备/采集，再扫描U盘'});return True
        if scanning:
            try:
                length=int(handler.headers.get('Content-Length','0'))
                if not 1<=length<=64 or handler.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError()
                value=json.loads(handler.rfile.read(length))
                if not isinstance(value,dict) or set(value)!={'scan'} or value['scan'] is not True:raise ValueError()
                if handler.headers.get('X-Reference-Key') and not handler.headers.get('X-Access-Token'):
                    handler.send(403,{'error':'请从采集控制页面扫描U盘'});return True
            except (ValueError,TypeError):handler.send(400,{'error':'U盘扫描请求无效'});return True
        try:handler.send(200,manager.scan() if scanning else manager.status())
        except (ValueError,OSError,TimeoutError) as error:handler.send(getattr(error,'status',503),{'error':str(error)})
        return True
    if path=='/api/phone/reference-options' and method=='GET':
        if not optional_read_allowed(snapshot):handler.send(423,{'error':'当前步骤不可选择参考图'});return True
        try:
            query=parse_qs(url.query,keep_blank_values=True)
            if set(query)-{'list_id','offset'} or any(len(v)!=1 for v in query.values()):raise ValueError()
            values=owner.reference_catalog.list_page(query.get('list_id',['all'])[0],int(query.get('offset',['0'])[0]),50,
                completed_paths=owner.reference_completed_paths())
            histories=owner.reference_states()
            for item in values['items']:
                relative=owner.reference_catalog.asset_path(item['id']).relative_to(owner.base.parent).as_posix()
                item['history']=histories.get(relative,{'state':'unattempted','attempt_count':0,'collected':False,'ever_recorded':False})
            handler.send(200,values)
        except (ValueError,OSError,TypeError):handler.send(400,{'error':'参考图列表已变化，请刷新后选择'})
        return True
    if path in ('/api/phone/recordings','/api/phone/upload-manifest'):
        if method!='GET' or handler.headers.get('Content-Length','0')!='0' or not optional_read_allowed(snapshot,replay=True):
            handler.send(423,{'error':'采集优先，请结束或放弃本段后管理采集条目'});return True
        store=getattr(owner,'recording_store',None)
        if store is None:handler.send(503,{'error':'采集条目管理暂未就绪'});return True
        try:
            query=parse_qs(url.query,keep_blank_values=True)
            if set(query)-{'day','offset'} or any(len(v)!=1 for v in query.values()):raise ValueError()
            day=query.get('day',['today'])[0];offset=int(query.get('offset',['0'])[0])
            value=store.upload_manifest(day) if path.endswith('upload-manifest') else store.page(day,offset)
            if not optional_read_allowed(owner.display_state(),replay=True):handler.send(423,{'error':'状态已变化，请稍后刷新'})
            elif path.endswith('upload-manifest'):
                handler.send(200,json.dumps(value,ensure_ascii=False,indent=2).encode(),'application/json; charset=utf-8',extra_headers={'Content-Disposition':'attachment; filename="retained-recordings-'+value['day']+'.json"'})
            else:handler.send(200,value)
        except (ValueError,OSError,TypeError):handler.send(400,{'error':'无法读取采集条目，请检查日期与存储卡'})
        return True
    if path in ('/api/phone/diagnostics','/api/phone/diagnostics.zip'):
        if method!='GET' or url.query or handler.headers.get('Content-Length','0')!='0':
            handler.send(400,{'error':'诊断请求无效'});return True
        if not optional_read_allowed(snapshot,replay=True):
            handler.send(423,{'error':'采集优先，请在结束本段后查看或导出诊断'});return True
        diagnostics=getattr(owner,'device_diagnostics',None)
        if diagnostics is None:
            handler.send(503,{'error':'诊断服务暂未就绪'});return True
        if not diagnostics.slots.acquire(False):
            handler.send(429,{'error':'诊断正在生成，请稍候再试'});return True
        try:
            value=diagnostics.snapshot()
            payload=diagnostics.bundle(value) if path.endswith('.zip') else value
            after=owner.display_state()
            if (not optional_read_allowed(after,replay=True) or snapshot.get('session')!=after.get('session')
                    or any(snapshot['privacy'].get(k)!=after['privacy'].get(k) for k in ('instance','generation'))):
                handler.send(423,{'error':'采集状态已变化，本次诊断已丢弃，请稍后刷新'})
            elif path.endswith('.zip'):
                handler.send(200,payload,'application/zip',extra_headers={'Content-Disposition':
                    'attachment; filename="pi-diagnostics-'+time.strftime('%Y%m%d-%H%M%S')+'.zip"'})
            else:handler.send(200,payload)
        except (OSError,ValueError,TypeError):handler.send(503,{'error':'诊断信息暂不可读取，请稍后重试'})
        finally:diagnostics.slots.release()
        return True
    if path == '/api/phone/reference-access' and method == 'GET':
        if url.query or handler.headers.get('Content-Length', '0') != '0':
            handler.send(400, {'error': '图库请求无效'}); return True
        importing=(snapshot.get('state') in ('usb_importing','usb_import_recovery')
                   and not snapshot.get('privacy',{}).get('required')
                   and snapshot.get('privacy',{}).get('stop_confirmed') is True)
        if not optional_read_allowed(snapshot) and not importing:
            handler.send(423, {'error': '当前步骤暂停图库管理，请结束本段后再操作'}); return True
        credentials = getattr(owner, 'reference_credentials', None)
        if credentials is None or getattr(owner, 'reference_catalog', None) is None:
            handler.send(503, {'error': '参考图库暂未就绪'}); return True
        # The authenticated collection UI may manage images. Independent
        # library-only clients retain their original separate access code.
        handler.send(200, {'access_code': credentials.code()}); return True
    if path == '/api/phone/tasks' and method == 'GET':
        if not optional_read_allowed(snapshot) or owner.reference_catalog is None:
            handler.send(423, {'error': '当前步骤不可读取任务列表'}); return True
        try:
            query = parse_qs(url.query, keep_blank_values=True)
            if set(query) - {'offset'} or any(len(v) != 1 for v in query.values()): raise ValueError()
            offset = int(query.get('offset', ['0'])[0])
            if not 0 <= offset <= 1000000: raise ValueError()
            value = owner.reference_catalog.library_page(offset, 12,
                        completed_paths=owner.reference_completed_paths(), include_deleted=False)
            handler.send(200, value)
        except (ValueError, OSError): handler.send(400, {'error': '无法读取任务列表'})
        return True
    prefix = '/api/phone/sidecar'
    route = path[len(prefix):] if path.startswith(prefix + '/') else ''
    if route not in ROUTES or (method != 'GET' and not (method == 'POST' and route == '/headphones')):
        handler.send(404, {'error': '不存在'}); return True
    release = route == '/replay/release'
    if not release and not optional_read_allowed(snapshot, replay=route.startswith('/replay/')):
        handler.send(423, {'error': '采集优先，当前步骤暂停附加显示'}); return True
    # This limit belongs only to optional UI work. STOP and display ACK use
    # separate HTTP requests and never wait for this slot or its network I/O.
    if not owner.phone_slots.acquire(False):
        handler.send(429, {'error': '显示请求繁忙，请稍后重试'}); return True
    connection = HTTPConnection('127.0.0.1', 8766, timeout=2)
    try:
        length = int(handler.headers.get('Content-Length', '0'))
        if not 0 <= length <= (256 if method == 'POST' else 0): raise ValueError()
        body = handler.rfile.read(length) if length else None
        headers = {'X-Access-Token': owner.token}
        if method == 'POST': headers['Content-Type'] = 'application/json'
        target = route + ('?' + url.query if url.query else '')
        connection.request(method, target, body=body, headers=headers)
        reply = connection.getresponse()
        raw = reply.read(MAX_REPLY + 1)
        if len(raw) > MAX_REPLY: raise ValueError()
        content = 'image/jpeg' if route.endswith('.jpg') and reply.status == 200 else 'application/json; charset=utf-8'
        # A privacy transition during decode must discard the late image.
        if not release and not (route=='/headphones' and method=='POST') and not optional_read_allowed(owner.display_state(), replay=route.startswith('/replay/')):
            handler.send(423, {'error': '显示已暂停'})
        else: handler.send(reply.status, raw, content)
    except (OSError, ValueError, HTTPException):
        handler.send(503, {'error': '附加显示服务暂不可用'})
    finally:
        connection.close()
        owner.phone_slots.release()
    return True
