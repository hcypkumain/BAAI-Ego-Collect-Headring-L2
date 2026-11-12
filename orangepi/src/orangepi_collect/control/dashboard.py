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
    if state in ('device_confirm','idle','failed_wait','exhausted'):actions=actions+['collection_mode']+['delete_recording','retain_error_recording']+['qc_mode','usb_task_import']
    if state in ('device_confirm','idle','failed_wait','exhausted','ready'):actions=actions+['select_reference','switch_reference']
    if state in SAFE:actions=actions+['shutdown']
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
        'recording':['stop','discard_recording','volume'], 'confirm_stop':['keep','retry','discard_recording','volume'],
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
    privacy=snapshot.get('privacy',{})
    if (privacy.get('required') is True and privacy.get('stop_confirmed') is not True
            and all(isinstance(privacy.get(key),str) and privacy.get(key)
                    for key in ('instance','session_id','nonce'))
            and 'stop' not in actions):
        # STOP stays reachable through native-error/fault states. The target
        # still has to match this exact committed privacy session at dispatch.
        actions.append('stop')
    if snapshot.get('state')=='device_confirm' and snapshot.get('scan_ok') is not True:
        actions=[action for action in actions if action!='confirm_devices']
    if snapshot.get('collection_mode')=='emg':
        actions=[action for action in actions if action!='eeg_filter' and not action.startswith('impedance_')]
    if snapshot.get('task_list',{}).get('manual_task_switch') and not snapshot.get('task_list',{}).get('can_switch'):
        actions=[x for x in actions if x not in ('select_reference','switch_reference','reference_list')]
    if (snapshot.get('privacy',{}).get('required') or (snapshot.get('worker') or {}).get('recording_active')):
        actions=[action for action in actions if action not in ('abandon','delete_recording','retain_error_recording','select_reference','switch_reference','usb_task_import','collection_mode','shutdown')]
    if snapshot.get('wrist_camera_settings',{}).get('can_change') is False:
        actions=[action for action in actions if action!='wrist_camera_settings']
    if (snapshot.get('state') in ('device_confirm','idle','failed_wait','exhausted')
            and snapshot.get('task_list',{}).get('can_change') is True):
        actions=actions+['reference_list']
    return actions


class Dashboard:
    def __init__(self, base, preview, settings, reference_catalog=None, reference_credentials=None,
                 reference_completed_paths=None, reference_access=None,recording_store=None,reference_states=None,usb_task_importer=None):
        self.base=Path(base);self.preview=Path(preview)
        self.reference_catalog=reference_catalog;self.reference_credentials=reference_credentials
        self.usb_task_importer=usb_task_importer
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
        self.lock=threading.Lock();self.snapshot={'state':'starting','revision':0,'devices':[],'selected_devices':[],'worker':{},'session':None,'image':None};self.pending=None;self.busy=False;self.result=None;self.prompting=False
        self.publish_seq=0;self.published_monotonic=time.monotonic();self.terminal_results=[];self.active_request_id=None;self.active_start_committed=False
        self.cancelled_starts=set();self.start_targets={};self.pending_stop=None
        from diagnostic_events import EventWriter, NullEvents
        try:self.events=EventWriter(self.base/'runtime/diagnostics')
        except Exception:self.events=NullEvents()
        from cloud_uploads import UploadManager
        try:self.cloud_uploads=UploadManager(self.base,self.recording_store,self.display_state) if self.recording_store is not None else None
        except Exception:self.cloud_uploads=None
        from daily_collection import DailyCollection
        self.daily_collection=DailyCollection(self.base,self.recording_store,self.display_state) if self.recording_store is not None else None
        if self.cloud_uploads is not None:self.cloud_uploads.daily_collection=self.daily_collection
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
            def handle(self):
                try:super().handle()
                except (BrokenPipeError,ConnectionResetError,TimeoutError):
                    # Phone disconnects and aborted polls are expected. Leave
                    # capture ownership untouched and close only this socket.
                    self.close_connection=True
            def setup(self):
                super().setup();self.connection.settimeout(5)
            def send(self,status,data,content='application/json; charset=utf-8',extra_headers=None):
                if isinstance(data,dict):
                    data=dict(data,collection_context=owner.snapshot.get('collection_context'),collection_mode=owner.snapshot.get('collection_mode'),mode_recovery_version='20261107.2')
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
                if path=='/api/display-state':
                    query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                    if set(query)-{'client_id','stop_protocol'} or any(len(v)!=1 for v in query.values()):self.send(400,{'error':'无效屏幕状态请求'});return
                    identity=query.get('client_id',[None])[0]
                    if identity is not None:
                        try:owner.privacy.display({'client_id':identity,'stop_protocol':query.get('stop_protocol',[None])[0]})
                        except ValueError as error:self.send(400,{'error':str(error)});return
                    value=owner.display_state()
                    if identity is not None:value['display_registration']={'client_id':identity,'instance':value['privacy']['instance']}
                    self.send(200,value);return
                if path=='/api/status':
                    query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                    if not query:
                        owner.demands.touch('legacy:'+self.client_address[0],SIGNALS,True);self.send(200,owner.status());return
                    if set(query)-{'signals','review','display','cameras','camera'} or any(len(v)!=1 for v in query.values()):self.send(400,{'error':'无效显示请求'});return
                    names=query.get('signals',[''])[0].split(',');names=[n for n in names if n]
                    if any(n not in SIGNALS for n in names) or query.get('review',['0'])[0] not in ('0','1') or query.get('cameras',['0'])[0] not in ('0','1'):self.send(400,{'error':'无效显示请求'});return
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
                current=owner.snapshot.get('collection_context')
                supplied=self.headers.get('X-Collection-Context')
                if current and supplied!=current and urlsplit(self.path).path!='/api/action':
                    self.send(409,{'error':'采集模式已变化，请刷新页面后操作','message':'采集模式已变化，请刷新页面后操作'});return
                if supplied and current and supplied!=current:
                    self.send(409,{'error':'采集模式已变化，请刷新页面后操作'});return
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
        import startup_status
        startup_status.handoff()
        self.server=DashboardHTTPServer((settings.get('host','0.0.0.0'),int(settings.get('port',8765))),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        startup_status.live()

    def publish(self,value):
        value=dict(value);root=value.pop('review_session',None)
        self.demands.state(dict(value,display_blocked=self.privacy.snapshot()['required']))
        with self.lock:
            self.snapshot=copy.deepcopy(value);self.review_root=root
            self.publish_seq+=1;self.published_monotonic=time.monotonic()

    def submit(self,data):
        request_id=data.get('request_id')
        if request_id is not None and (not isinstance(request_id,str) or not re.fullmatch(r'[0-9a-f]{32}',request_id)):
            raise ValueError('无效的请求标识')
        if data.get('action') not in ('privacy_display','privacy_ack'):
            with self.lock:
                current=self.snapshot.get('collection_context')
                if current and data.get('collection_context')!=current:
                    raise ValueError('采集模式已变化，请刷新页面后操作')
        if data.get('action')=='start':
            if not request_id:raise ValueError('页面版本已更新，请刷新后再开始')
            with self.lock:
                if data.get('privacy_instance')!=self.privacy.instance:
                    raise ValueError('控制器已变化，请刷新后再开始')
                if len(self.cancelled_starts)>=256:
                    same_active=self.active_request_id==request_id or (self.pending and self.pending.get('request_id')==request_id)
                    if not same_active:self._terminal_locked(request_id,'start','取消记录已满，本次未开始',False)
                    raise ValueError('取消记录已满，本次未开始，请联系维护人员')
            if data.get('stop_protocol') != 'session-stop-v1':raise ValueError('页面版本已更新，请刷新后再开始')
            self.privacy.require_stop_protocol()
            if not self.camera_checks.approved():raise ValueError('请先完成全部相机同屏检查并确认画面方向')
        # Display ACKs must remain reachable while the main thread waits for them.
        if data.get('action')=='privacy_display':
            self.privacy.display(data.get('value'));return
        if data.get('action')=='privacy_ack':
            self.privacy.acknowledge(data.get('value'));return
        if data.get('action')=='cancel_start':
            if not request_id:raise ValueError('缺少开始请求标识')
            with self.lock:
                if data.get('privacy_instance')!=self.privacy.instance:raise ValueError('控制器已变化，无法确认原请求')
                if self.snapshot.get('collection_context') and data.get('collection_context')!=self.snapshot['collection_context']:
                    raise ValueError('采集模式已变化，请刷新页面后操作')
                target=self.start_targets.get(request_id)
                if target:
                    if self.privacy.snapshot()['phase']=='idle' and self.privacy.target_matches(target,allow_stopped=True):return
                    if not self.privacy.target_matches(target,allow_same_session=True):
                        raise ValueError('该请求的采集会话已变化，不能停止其他采集')
                    self._queue_stop_locked(target,request_id)
                    return
                known_pending=(self.pending and self.pending.get('action')=='start' and self.pending.get('request_id')==request_id)
                known_active=(getattr(self,'active_action',None)=='start' and self.active_request_id==request_id)
                if not known_pending and not known_active and self.privacy.snapshot()['phase']!='idle':
                    raise ValueError('无法确认该请求属于当前采集，不能停止其他会话')
                if request_id not in self.cancelled_starts and len(self.cancelled_starts)>=256 and not (known_pending or known_active):
                    raise ValueError('取消记录已满，状态未确认，请联系维护人员')
                self.cancelled_starts.add(request_id)
                if known_pending:
                    self.pending=None
                    self._terminal_locked(request_id,'start','本次开始已取消',False)
                elif known_active:
                    p=self.privacy.snapshot()
                    if p['phase']=='arming' and p['nonce']:
                        self.privacy.cancel_request({k:p[k] for k in ('instance','session_id','nonce','generation')})
                elif not any(r['request_id']==request_id for r in self.terminal_results):
                    self._terminal_locked(request_id,'start','本次开始已取消',False)
                return
        if data.get('action')=='stop':
            # STOP is session-bound; unrelated status revisions must not make
            # a valid retry unreachable after the native writer faults.
            with self.lock:
                if self.snapshot.get('collection_context') and data.get('collection_context')!=self.snapshot['collection_context']:
                    raise ValueError('采集模式已变化，请刷新页面后操作')
                target=data.get('privacy_target')
                if self.privacy.snapshot()['phase']=='idle' and self.privacy.target_matches(target,allow_stopped=True):return
                if not self.privacy.target_matches(target,allow_same_session=True):
                    raise ValueError('采集会话已变化，请刷新后重试')
                if target.get('generation')==self.privacy.generation and self.privacy.cancel_request(target):return
                self._queue_stop_locked(target)
                return
        with self.lock:
            if self.snapshot.get('collection_context') and data.get('collection_context')!=self.snapshot['collection_context']:
                raise ValueError('采集模式已变化，请刷新页面后操作')
            current_stop=(data.get('action')=='stop'
                and self.privacy.target_matches(data.get('privacy_target'),allow_same_session=True))
            if ((self.busy and not self.prompting) or self.pending is not None or self.pending_stop is not None) and not current_stop:
                raise ValueError('正在执行上一步，请等待')
            if data.get('action') not in ('collection_mode','stop') and data.get('revision')!=self.snapshot.get('revision'):
                raise ValueError('流程已变化，请刷新后操作')
            if data.get('action') not in snapshot_actions(dict(self.snapshot,privacy=self.privacy.snapshot())):raise ValueError('当前步骤不允许这个操作')
            if data.get('action')=='start' and request_id in self.cancelled_starts:raise ValueError('本次开始已取消')
            if request_id and (request_id==self.active_request_id or any(r['request_id']==request_id for r in self.terminal_results)):
                raise ValueError('请求已处理，请查询状态')
            if data['action']=='collection_mode' and data.get('value') not in ('emg','eeg'):
                raise ValueError('请选择脑电或肌电')
            if data['action']=='reference_list':
                value=data.get('value')
                if (not isinstance(value,dict) or set(value)!={'list_id','catalog_revision'}
                        or not valid_id(value.get('list_id'))
                        or type(value.get('catalog_revision')) is not int
                        or not 0<=value['catalog_revision']<=MAX_REVISION):
                    raise ValueError('请选择当前图库版本中的任务列表')
            if data['action']=='usb_task_import':
                value=data.get('value')
                if (not isinstance(value,dict) or set(value)!={'scan_id','zip_ids','catalog_revision','remove_list_ids'}
                        or not valid_id(value.get('scan_id'))
                        or not isinstance(value.get('zip_ids'),list) or len(value['zip_ids'])>50
                        or any(not valid_id(v) for v in value['zip_ids'])
                        or len(set(value['zip_ids']))!=len(value['zip_ids'])
                        or not isinstance(value.get('remove_list_ids'),list)
                        or any(not valid_id(v) for v in value['remove_list_ids'])
                        or len(set(value['remove_list_ids']))!=len(value['remove_list_ids'])
                        or not value['zip_ids'] and not value['remove_list_ids']
                        or type(value.get('catalog_revision')) is not int):
                    raise ValueError('请选择任务包或要移入回收站的旧列表')
            if data['action'] in ('delete_recording','retain_error_recording'):
                value=data.get('value')
                if (not isinstance(value,dict) or set(value)!={'session_id','token'}
                        or not isinstance(value['session_id'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value['session_id'])
                        or not isinstance(value['token'],str) or not re.fullmatch('[0-9a-f]{64}',value['token'])):
                    raise ValueError('请选择当前采集条目后再删除')
            if data['action']=='select_reference':
                value=data.get('value')
                if (not isinstance(value,dict) or set(value) not in ({'image_id','catalog_revision'},{'image_id','catalog_revision','list_id'}) or ('list_id' in value and not valid_id(value['list_id'])) or not valid_id(value.get('image_id'))
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
            if data['action']=='stop':
                target=self.privacy.stopping(data.get('privacy_target'),allow_same_session=True)
                data=dict(data,privacy_target=target)
            self.pending=dict(data);self.result=None
            self.events.emit('action_submitted', action=data['action'] if data['action'] in ('start','stop','impedance_start','impedance_end','impedance_review','rescan') else 'other', request_id=data.get('request_id'), revision=data.get('revision'))

    def set_prompting(self,active):
        with self.lock:self.prompting=bool(active)

    def has_pending(self):
        with self.lock:return self.pending is not None or self.pending_stop is not None

    def _terminal_locked(self,request_id,action,error,committed):
        self.terminal_results.append(dict(request_id=request_id,action=action,ok=not bool(error),error=error,
            publish_seq=self.publish_seq,commit_state='committed' if committed else 'not_committed'))
        self.terminal_results=self.terminal_results[-64:]
        retained={r['request_id'] for r in self.terminal_results}
        self.start_targets={k:v for k,v in self.start_targets.items() if k in retained or k==self.active_request_id}

    def _queue_stop_locked(self,target,request_id=None):
        target=self.privacy.stopping(target,allow_same_session=True)
        if self.pending_stop:
            if not self.privacy.target_matches(self.pending_stop.get('privacy_target'),allow_same_session=True):
                raise ValueError('已有其他停止请求等待执行')
            if request_id and not self.pending_stop.get('request_id'):self.pending_stop['request_id']=request_id
            return
        self.pending_stop=dict(action='stop',revision=self.snapshot.get('revision'),privacy_target=target,request_id=request_id)

    def arm_start(self,session,**owner):
        with self.lock:
            if self.active_request_id in self.cancelled_starts:return False
            self.privacy.arm(session,**owner)
            return True

    def commit_start(self):
        with self.lock:
            if self.active_request_id in self.cancelled_starts:raise ValueError('本次开始已取消')
            proof=self.privacy.commit()
            if self.active_action=='start':
                self.active_start_committed=True
                if self.active_request_id:
                    p=self.privacy.snapshot()
                    self.start_targets[self.active_request_id]={k:p[k] for k in ('instance','session_id','nonce','generation')}
            return proof

    def take(self):
        with self.lock:
            if self.pending_stop is not None:job=self.pending_stop;self.pending_stop=None
            else:job=self.pending;self.pending=None
            if job:
                self.busy=True;self.active_action=job.get('action');self.active_request_id=job.get('request_id');self.active_start_committed=False
                self.events.emit('action_taken', action=self.active_action if self.active_action in ('start','stop','impedance_start','impedance_end','impedance_review','rescan') else 'other', request_id=job.get('request_id'), revision=job.get('revision'))
            return job

    def complete(self,error=None):
        with self.lock:
            self.busy=False;self.result={'ok':not error,'error':error,'time':time.time(),'action':getattr(self,'active_action',None)}
            action=getattr(self,'active_action',None)
            request_id=self.active_request_id
            if request_id:
                self._terminal_locked(request_id,action,error,self.active_start_committed if action=='start' else False)
            self.active_request_id=None
            self.events.emit('action_complete', action=action if action in ('start','stop','impedance_start','impedance_end','impedance_review','rescan') else 'other', request_id=request_id, ok=not bool(error), reason='failed' if error else 'ok')

    def note_start_commit(self):
        with self.lock:
            if self.active_action=='start':self.active_start_committed=True

    def display_state(self):
        """Authoritative display gate without disk, QC, media or impedance parsing."""
        with self.lock:
            value={key:self.snapshot.get(key) for key in ('state','session','preview_id','image','selected_devices','revision','collection_mode','collection_context')}
            value['busy']=self.busy or self.pending is not None or self.pending_stop is not None
            value['result']=copy.deepcopy(self.result)
            value['terminal_results']=copy.deepcopy(self.terminal_results)
            value['publish_seq']=self.publish_seq
            value['publish_age_ms']=max(0,round((time.monotonic()-self.published_monotonic)*1000))
            value['worker']={k:(self.snapshot.get('worker') or {}).get(k) for k in ('recording_active','state')}
        value['privacy']=self.privacy.snapshot()
        return value

    def status(self,include_signals=None,include_review=True):
        with self.lock:
            data=copy.deepcopy(self.snapshot);data.update(busy=self.busy or self.pending is not None or self.pending_stop is not None,result=self.result,
                terminal_results=copy.deepcopy(self.terminal_results),publish_seq=self.publish_seq,
                publish_age_ms=max(0,round((time.monotonic()-self.published_monotonic)*1000)))
            review_root=self.review_root
            data['speaking']=self.prompting
            data['actions']=[] if self.pending is not None or self.pending_stop is not None or (self.busy and not self.prompting) else snapshot_actions(data)
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
        if self.daily_collection is not None:
            data['daily_collection']={k:v for k,v in self.daily_collection.snapshot().items() if k!='rows'}
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
        if self.daily_collection is not None:self.daily_collection.close()
        if self.cloud_uploads is not None:self.cloud_uploads.close()
        self.server.shutdown();self.server.server_close();self.thread.join(2)
