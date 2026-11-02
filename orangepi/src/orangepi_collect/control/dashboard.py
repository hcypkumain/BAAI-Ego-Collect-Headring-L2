"""Dependency-free LAN dashboard. All mutations are queued for the controller."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit
from live_preview import CAMERAS, EMGS, SIGNALS
from eeg_settings import PRESETS
from wrist_calibration import SLOTS
import recording_review
import re
from reference_catalog import valid_id, MAX_REVISION
from reference_library_http import handle_reference_request
from reference_access import handle_reference_access
from phone_http import handle_phone_request

class DashboardHTTPServer(ThreadingHTTPServer):
    # Six preview requests plus polling can exceed TCPServer's default queue of 5.
    request_queue_size = 32


SAFE = ('device_confirm','idle','failed_wait','exhausted','ready')


def impedance_presentation(state, selected_devices, worker, worker_active):
    """Explain preparation separately from the current owner's measured state."""
    if 'eeg' not in (selected_devices or ()):
        return dict(required=False, supported=False, phase='not_selected', can_record=True)
    required = os.environ.get('OMNIBCI_ENABLE_IMPEDANCE_PREFLIGHT') == '1'
    worker = worker or {}
    measured = worker.get('eeg_impedance')
    if worker_active and isinstance(measured, dict) and measured.get('phase'):
        return copy.deepcopy(measured)
    value = dict(required=required, supported=False, can_record=not required,
        active=False, pending=False, error=None, channels=[],
        loff_off_confirmed=False, frontend_restored=False)
    if not required:
        return dict(value, phase='unavailable', reason='feature_disabled',
                    message='当前采集服务未启用阻抗检查。')
    if state in ('failed_wait', 'error'):
        errors = [str(error) for error in worker.get('errors', [])
                  if 'eeg' in str(error).lower() or '脑电' in str(error)]
        error = '；'.join(errors) or None
        description = error or '采集准备未完成'
        normalized = (error or '').lower()
        if ('no data' in normalized or 'no eeg samples' in normalized) and '3' in normalized:
            description = '脑电连续 3 秒未收到数据'
        return dict(value, phase='interrupted', error=error,
            message='上次准备已中断：'+description+'。请重新准备本段，再开始检查。')
    if state == 'preparing':
        return dict(value, phase='initializing',
                    message='正在准备本段并连接脑电设备，请稍候。')
    if worker_active:
        return dict(value, phase='initializing',
                    message='尚未收到脑电检查状态，请等待设备准备完成；当前不能开始录制。')
    return dict(value, phase='awaiting_prepare',
                message='请先点击“准备本段”，完成设备预检后即可开始阻抗检查。')


def impedance_required(value=None):
    value = value or {}
    return value.get('required', os.environ.get('OMNIBCI_ENABLE_IMPEDANCE_PREFLIGHT') == '1') is True


def impedance_allows_recording(value=None):
    value = value or {}
    if not impedance_required(value): return True
    from impedance_quality import quality_allows_recording
    if not quality_allows_recording(value):return False
    return (value.get('supported') is True and value.get('can_record') is True
        and not value.get('active') and not value.get('pending') and not value.get('error')
            and value.get('loff_off_confirmed') is True and value.get('frontend_restored') is True
            and value.get('phase') == 'completed' and value.get('reviewed') is True
            and value.get('window_ready') is True and value.get('normal_frame_confirmed') is True)


def preparation_state(state):
    return state in ('device_confirm','idle','failed_wait','exhausted','preparing','ready','emg_manual','emg_pairing') or state.startswith('wrist_')


def actions_for(state, impedance=None):
    actions=_base_actions_for(state,impedance)
    if preparation_state(state):actions=actions+['abandon']
    if state in ('device_confirm','idle','failed_wait','exhausted'):actions=actions+['delete_recording','retain_error_recording']
    if state in ('device_confirm','idle','failed_wait','exhausted','ready'):actions=actions+['select_reference','switch_reference']
    return actions


def _base_actions_for(state, impedance=None):
    if state == 'emg_pairing': return ['restart_calibration','volume']
    if state == 'wrist_choose': return ['wrist_method','restart_calibration','volume']
    if state == 'wrist_manual': return ['wrist_method','assign_wrist_roles','restart_calibration','volume']
    if state.startswith('wrist_'): return ['measure','restart_calibration','wrist_method','volume']
    if state == 'emg_manual': return ['assign_emg_roles','restart_calibration','volume']
    table = {'device_confirm':['confirm_devices','rescan','calibrate_wrist','calibrate_emg','volume'],
        'idle':['prepare','rescan','calibrate_wrist','calibrate_emg','volume'],
        'ready':['start','repeat','rescan','calibrate_wrist','calibrate_emg','volume'],
        'recording':['stop','volume'], 'confirm_stop':['keep','retry','volume'],
        'scanning':['volume'], 'preparing':['volume'], 'countdown':['volume'], 'finalizing':['volume'],
        'failed_wait':['prepare','rescan','calibrate_wrist','calibrate_emg','volume','repeat'],
        'exhausted':['prepare','rescan','calibrate_wrist','calibrate_emg','volume']}
    actions=table.get(state,[])
    if state in SAFE:actions=actions+['eeg_filter','wrist_camera_settings']
    if state == 'ready' and impedance_required(impedance):
        value = impedance or {}
        if not impedance_allows_recording(value): actions = [a for a in actions if a != 'start']
        if value.get('supported') is True:
            if value.get('active') or value.get('pending'):
                return ['impedance_end', 'repeat', 'volume']
            restored = value.get('loff_off_confirmed') is True and value.get('frontend_restored') is True
            if restored and not value.get('error'):
                actions = actions + ['impedance_start'] if impedance_allows_recording(value) else ['impedance_start'] + actions
                if value.get('completed_at') and not value.get('can_record'):
                    actions = ['impedance_review'] + actions
            elif value.get('phase') == 'not_checked':
                actions = ['impedance_start'] + actions
            else:
                # Ending also requests recovery; never offer a fresh activation
                # while the preceding transaction may still be on the device.
                actions = ['impedance_end', 'repeat', 'volume']
    return actions


def snapshot_actions(snapshot):
    actions=actions_for(snapshot.get('state',''),snapshot.get('eeg_impedance'))
    if (snapshot.get('privacy',{}).get('required') or (snapshot.get('worker') or {}).get('recording_active')):
        actions=[action for action in actions if action not in ('abandon','delete_recording','retain_error_recording','select_reference','switch_reference')]
    if snapshot.get('wrist_camera_settings',{}).get('can_change') is False:
        actions=[action for action in actions if action!='wrist_camera_settings']
    if (snapshot.get('state') in ('device_confirm','idle','failed_wait','exhausted')
            and snapshot.get('task_list',{}).get('can_change') is True):
        actions=actions+['reference_list']
    return actions


class Dashboard:
    def __init__(self, base, preview, settings, reference_catalog=None, reference_credentials=None,
                 reference_completed_paths=None, reference_access=None,recording_store=None,reference_states=None):
        self.base=Path(base);self.preview=Path(preview)
        self.reference_catalog=reference_catalog;self.reference_credentials=reference_credentials
        self.reference_completed_paths=reference_completed_paths or (lambda: ())
        self.reference_access=reference_access
        self.recording_store=recording_store
        self.reference_states=reference_states or (lambda:{})
        from screen_privacy import ScreenPrivacy
        self.privacy=ScreenPrivacy()
        from display_demand import DisplayDemand
        self.preview.mkdir(parents=True,exist_ok=True);self.demands=DisplayDemand(self.preview)
        self.phone_slots=threading.BoundedSemaphore(1)
        self.phone_preview_slots=threading.BoundedSemaphore(1)
        self.phone_stream_slots=threading.BoundedSemaphore(3)
        self.lock=threading.Lock();self.snapshot={};self.pending=None;self.busy=False;self.result=None;self.prompting=False
        from cloud_uploads import UploadManager
        try:self.cloud_uploads=UploadManager(self.base,self.recording_store,self.display_state) if self.recording_store is not None else None
        except Exception:self.cloud_uploads=None
        self.review_root=None
        self._review_cache=(None,0,None)
        self.access_code_required=settings.get('access_code_required',True)
        if type(self.access_code_required) is not bool:raise ValueError('access_code_required must be a boolean')
        path=self.base/'runtime/dashboard_token'
        if path.exists():self.token=path.read_text().strip()
        else:
            self.token=secrets.token_urlsafe(24)
            fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(self.token)
        if len(self.token)<24:raise ValueError('Invalid dashboard token')
        from phone_links import PhoneLinks
        self.phone_links=PhoneLinks(self)
        from device_diagnostics import DeviceDiagnostics
        self.device_diagnostics=DeviceDiagnostics(self)
        from camera_check import CameraChecks
        self.camera_checks=CameraChecks(self)
        self.page=Path(__file__).with_name('dashboard.html').read_bytes()
        if not self.access_code_required:
            self.page=self.page.replace(b'<body>',b'<body data-access-code-required="false">',1)
        try:self.boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:self.boot_id=None
        owner=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            disable_nagle_algorithm=True
            def log_message(self,*args):pass
            def setup(self):
                super().setup();self.connection.settimeout(5)
            def send(self,status,data,content='application/json; charset=utf-8',extra_headers=None):
                if not isinstance(data,bytes):data=json.dumps(data,ensure_ascii=False).encode()
                self.send_response(status);self.send_header('Content-Type',content)
                self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Referrer-Policy','no-referrer')
                for key,value in (extra_headers or {}).items():self.send_header(key,value)
                if content.startswith('text/html'):
                    self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; frame-ancestors 'none'; connect-src 'self'")
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def authorized(self):
                if not owner.access_code_required:return True
                supplied=self.headers.get('X-Access-Token','') or parse_qs(urlsplit(self.path).query).get('token',[''])[0]
                try:return len(supplied)<=511 and hmac.compare_digest(supplied,owner.token)
                except (TypeError,UnicodeError):return False
            def do_GET(self):
                if handle_phone_request(self,owner,'GET'):return
                if handle_reference_access(self,owner):return
                if handle_reference_request(self,owner,'GET'):return
                path=urlsplit(self.path).path
                if path=='/':self.send(200,owner.page,'text/html; charset=utf-8');return
                if not self.authorized():self.send(401,{'error':'请输入此设备的访问码'});return
                if path.startswith('/api/video/'):
                    self.video();return
                if path=='/api/wrist-context':self.send(200,owner.wrist_context());return
                if path=='/api/display-state':self.send(200,owner.display_state());return
                if path=='/api/status':
                    query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                    if not query:
                        owner.demands.touch('legacy:'+self.client_address[0],('eeg','emg_left','emg_right'),True);self.send(200,owner.status());return
                    if set(query)-{'signals','review','display','cameras','camera'} or any(len(v)!=1 for v in query.values()):self.send(400,{'error':'无效显示请求'});return
                    names=query.get('signals',[''])[0].split(',');names=[n for n in names if n]
                    if any(n not in ('eeg','emg_left','emg_right') for n in names) or query.get('review',['0'])[0] not in ('0','1') or query.get('cameras',['0'])[0] not in ('0','1'):self.send(400,{'error':'无效显示请求'});return
                    client=query.get('display',['web'])[0]
                    camera=query.get('camera',[None])[0]
                    if camera is not None and camera not in CAMERAS:self.send(400,{'error':'无效相机显示请求'});return
                    if len(client)>96:self.send(400,{'error':'无效显示身份'});return
                    owner.demands.touch(client,names,query.get('cameras',['0'])[0]=='1',[camera] if camera else None);self.send(200,owner.status(names,query.get('review',['0'])[0]=='1'));return
                if path=='/api/reference':
                    before=owner.display_state();image=before.get('image')
                    if before['privacy'].get('required') or not before['privacy'].get('stop_confirmed'):
                        self.send(423,{'error':'采集期间隐藏参考图'});return
                    try:
                        image=Path(image).resolve();image.relative_to((owner.base.parent/'configs/environments').resolve())
                        if image.stat().st_size>64*1024*1024:raise ValueError()
                        raw=image.read_bytes();after=owner.display_state()
                        if after['privacy'].get('required') or before.get('image')!=after.get('image'):
                            self.send(423,{'error':'参考图上下文已变化'});return
                        self.send(200,raw,'image/png' if raw[:8]==b'\x89PNG\r\n\x1a\n' else 'image/jpeg')
                    except (OSError,ValueError,TypeError):self.send(404,{'error':'未选择参考图像'})
                    return
                name=path.removeprefix('/preview/') if hasattr(str,'removeprefix') else path[len('/preview/'):]
                if path.startswith('/preview/') and name in [n+'.jpg' for n in CAMERAS]:
                    try:
                        p=owner.preview/name
                        if time.time()-p.stat().st_mtime>3:raise OSError('stale')
                        self.send(200,p.read_bytes(),'image/jpeg')
                    except OSError:self.send(404,{'error':'等待实时画面'})
                    return
                self.send(404,{'error':'不存在'})
            def do_HEAD(self):
                if not self.authorized():self.send(401,{});return
                if urlsplit(self.path).path.startswith('/api/video/'):self.video(head=True)
                else:self.send(404,{})
            def video(self,head=False):
                url=urlsplit(self.path)
                name=url.path[len('/api/video/'):]
                session=parse_qs(url.query).get('session',[''])[0]
                with owner.lock:root=owner.review_root
                try:
                    path=recording_review.video_path(root,session,name)
                    handle=path.open('rb');size=os.fstat(handle.fileno()).st_size
                except (ValueError,OSError):self.send(404,{'error':'录像尚未完成收尾或已切换'});return
                with handle:
                    try:start,end,partial=recording_review.byte_range(self.headers.get('Range'),size)
                    except ValueError:
                        self.send_response(416);self.send_header('Content-Range','bytes */'+str(size));self.send_header('Content-Length','0');self.end_headers();return
                    self.send_response(206 if partial else 200)
                    self.send_header('Content-Type','video/mp4');self.send_header('Accept-Ranges','bytes')
                    self.send_header('Content-Length',str(end-start+1));self.send_header('Cache-Control','no-store')
                    self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Content-Type-Options','nosniff')
                    if partial:self.send_header('Content-Range','bytes %d-%d/%d'%(start,end,size))
                    self.end_headers()
                    if head:return
                    handle.seek(start);remaining=end-start+1
                    try:
                        while remaining:
                            chunk=handle.read(min(256*1024,remaining))
                            if not chunk:break
                            self.wfile.write(chunk);remaining-=len(chunk)
                    except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
            def do_POST(self):
                if handle_phone_request(self,owner,'POST'):return
                if handle_reference_request(self,owner,'POST'):return
                if urlsplit(self.path).path!='/api/action':self.send(404,{});return
                # The library capability must never impersonate a control token.
                # Existing anonymous-control configuration remains independent.
                if self.headers.get('X-Reference-Key') and not self.headers.get('X-Access-Token'):
                    self.send(403,{'error':'图库管理码仅用于图库，请在控制端操作采集'});return
                if not self.authorized():self.send(401,{'error':'访问码无效'});return
                origin=self.headers.get('Origin')
                if origin and origin!='http://'+self.headers.get('Host',''):
                    self.send(403,{'error':'不允许跨站操作'});return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=2048:raise ValueError('请求太大或为空')
                    data=json.loads(self.rfile.read(length))
                    if not isinstance(data,dict):raise ValueError('无效操作')
                    owner.submit(data)
                    self.send(202,{'accepted':True})
                except (ValueError,TypeError,json.JSONDecodeError) as exc:self.send(409,{'error':str(exc)})
        self.server=DashboardHTTPServer((settings.get('host','0.0.0.0'),int(settings.get('port',8765))),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def publish(self,value):
        value=dict(value);root=value.pop('review_session',None)
        self.demands.state(dict(value,display_blocked=self.privacy.snapshot()['required']))
        with self.lock:self.snapshot=copy.deepcopy(value);self.review_root=root

    def submit(self,data):
        if data.get('action')=='start' and not self.camera_checks.approved():
            raise ValueError('请先完成全部相机同屏检查并确认画面方向')
        # Display ACKs must remain reachable while the main thread waits for them.
        if data.get('action')=='privacy_display':
            self.privacy.display(data.get('value'));return
        if data.get('action')=='privacy_ack':
            self.privacy.acknowledge(data.get('value'));return
        if data.get('action')=='stop' and self.privacy.cancel_request():return
        with self.lock:
            if (self.busy and not self.prompting) or self.pending is not None:raise ValueError('正在执行上一步，请等待')
            if data.get('revision')!=self.snapshot.get('revision'):raise ValueError('流程已变化，请刷新后操作')
            if data.get('action') not in snapshot_actions(self.snapshot):raise ValueError('当前步骤不允许这个操作')
            if data['action']=='reference_list':
                value=data.get('value')
                if (not isinstance(value,dict) or set(value)!={'list_id','catalog_revision'}
                        or not valid_id(value.get('list_id'))
                        or type(value.get('catalog_revision')) is not int
                        or not 0<=value['catalog_revision']<=MAX_REVISION):
                    raise ValueError('请选择当前图库版本中的任务列表')
            if data['action'] in ('delete_recording','retain_error_recording'):
                value=data.get('value')
                if (not isinstance(value,dict) or set(value)!={'session_id','token'}
                        or not isinstance(value['session_id'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value['session_id'])
                        or not isinstance(value['token'],str) or not re.fullmatch('[0-9a-f]{64}',value['token'])):
                    raise ValueError('请选择当前采集条目后再删除')
            if data['action']=='select_reference':
                value=data.get('value')
                if (not isinstance(value,dict) or set(value)!={'image_id','catalog_revision'} or not valid_id(value.get('image_id'))
                        or type(value.get('catalog_revision')) is not int or not 0<=value['catalog_revision']<=MAX_REVISION):raise ValueError('请选择当前图库中的参考图')
            if data['action']=='volume' and (type(data.get('value')) is not int or not 20<=data['value']<=120):
                raise ValueError('音量应在 20 到 120 之间')
            if data['action']=='eeg_filter' and data.get('value') not in PRESETS:
                raise ValueError('无效的脑电滤波选项')
            if data['action']=='wrist_camera_settings' and ('value' not in data or
                    (data['value'] is not None and type(data['value']) is not bool)):
                raise ValueError('请选择保持当前设置、允许或不允许自动曝光改变帧率')
            if data['action']=='wrist_method' and data.get('value') not in ('manual','occlusion'):
                raise ValueError('无效的腕部标定方式')
            if data['action'] in ('assign_wrist_roles','assign_emg_roles'):
                value=data.get('value')
                slots=SLOTS if data['action']=='assign_wrist_roles' else EMGS
                if (not isinstance(value,dict) or set(value)!={'session_id','preview_id','left_slot'}
                        or value.get('left_slot') not in slots
                        or any(not isinstance(value.get(k),str) or not 0<len(value[k])<=64
                               for k in ('session_id','preview_id'))):
                    raise ValueError('请选择当前预览对应的左右设备')
            self.pending=dict(data);self.result=None
            if data['action']=='stop':self.privacy.stopping()

    def set_prompting(self,active):
        with self.lock:self.prompting=bool(active)

    def has_pending(self):
        with self.lock:return self.pending is not None

    def take(self):
        with self.lock:
            job=self.pending;self.pending=None
            if job:self.busy=True
            return job

    def complete(self,error=None):
        with self.lock:self.busy=False;self.result={'ok':not error,'error':error,'time':time.time()}

    def display_state(self):
        """Authoritative display gate without disk, QC, media or impedance parsing."""
        with self.lock:
            value={key:self.snapshot.get(key) for key in ('state','session','preview_id','image','selected_devices','revision')}
            value['busy']=self.busy or self.pending is not None
            value['worker']={k:(self.snapshot.get('worker') or {}).get(k) for k in ('recording_active','state')}
        value['privacy']=self.privacy.snapshot()
        return value

    def status(self,include_signals=None,include_review=True):
        with self.lock:
            data=copy.deepcopy(self.snapshot);data.update(busy=self.busy or self.pending is not None,result=self.result)
            review_root=self.review_root
            data['speaking']=self.prompting
            data['actions']=[] if self.pending is not None or (self.busy and not self.prompting) else snapshot_actions(data)
        data['can_skip_speech']=data['speaking'] and bool(data['actions'])
        data['privacy']=self.privacy.snapshot()
        data['camera_check']=self.camera_checks.status()
        if isinstance(data.get('eeg_impedance'),dict):
            from impedance_quality import assess_impedance
            data['eeg_impedance']['quality']=assess_impedance(data['eeg_impedance'])
        if data['privacy']['required']:
            data['image']=None
            if data['privacy']['phase']=='arming':data['actions']=['stop']
        if data['privacy']['required'] or not include_review:data['review']=None
        else:
            cached_root,cached_at,cached_value=self._review_cache
            if cached_root!=review_root or time.monotonic()-cached_at>=1:
                cached_value=recording_review.describe(review_root);self._review_cache=(review_root,time.monotonic(),cached_value)
            data['review']=copy.deepcopy(cached_value)
        data['system']={'boot_id':self.boot_id,'controller_pid':os.getpid()}
        data['server_time']=time.time();data['previews']={};data['signals']={}
        for name in (() if data['privacy']['required'] else CAMERAS):
            try:age=time.time()-(self.preview/(name+'.jpg')).stat().st_mtime
            except OSError:age=None
            data['previews'][name]={'live':age is not None and age<3,'age':age}
        for name in (() if data['privacy']['required'] else SIGNALS if include_signals is None else include_signals):
            try:
                value=json.loads((self.preview/(name+'.json')).read_text())
                value['live']=0<=time.time()-value['time']<3;data['signals'][name]=value
            except (OSError,ValueError,TypeError,KeyError):data['signals'][name]={'live':False,'rows':[]}
        data['server_time']=time.time()
        return data

    def wrist_context(self):
        # Same authoritative snapshot and live process identity as status(),
        # without reviewing recordings or reading any preview/signal files.
        with self.lock:
            data=copy.deepcopy({key:self.snapshot.get(key)
                                for key in ('state','preview_id','wrist_assignment')})
        data['system']={'boot_id':self.boot_id,'controller_pid':os.getpid()}
        return data

    def close(self):
        if self.cloud_uploads is not None:self.cloud_uploads.close()
        self.server.shutdown();self.server.server_close();self.thread.join(2)
