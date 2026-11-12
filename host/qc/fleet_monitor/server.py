"""Read-only fleet gateway, isolated from receiver jobs and recorder controls."""
import argparse,json,time,threading,queue,subprocess,selectors,socket,os,re,shlex,hashlib,signal,urllib.request,urllib.error,urllib.parse,shutil,io,base64
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from collections import deque
from history import History,day_of,session_time
from agent import normalized,image_id

ROOT=Path(__file__).resolve().parent

def encoded(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()

def route_interface(address=None):
    try:
        target=int.from_bytes(socket.inet_aton(address),'little') if address else 0
        rows=[]
        for line in Path('/proc/net/route').read_text().splitlines()[1:]:
            f=line.split();dest,mask=int(f[1],16),int(f[7],16)
            if not int(f[3],16)&1:continue
            if address and target&mask==dest:rows.append((mask.bit_count(),-int(f[6]),f[0]))
            elif not address and dest==0:rows.append((0,-int(f[6]),f[0]))
        return max(rows)[2] if rows else None
    except (OSError,ValueError):return None

class Meter:
    def __init__(self):self.last={};self.traces={}
    def sample(self,name):
        result={'interface':name,'rx_bps':None,'tx_bps':None}
        if not name:return result
        try:
            p=Path('/sys/class/net')/name/'statistics';now=time.monotonic()
            counters=(now,int((p/'rx_bytes').read_text()),int((p/'tx_bytes').read_text()))
            previous=self.last.get(name)
            if previous and now>previous[0]:
                result.update(rx_bps=max(0,counters[1]-previous[1])/(now-previous[0]),tx_bps=max(0,counters[2]-previous[2])/(now-previous[0]))
                self.traces.setdefault(name,deque(maxlen=30)).append([time.time(),result['rx_bps'],result['tx_bps']])
            self.last[name]=counters
        except OSError:pass
        return result

class Hub:
    @classmethod
    def read_only(cls, read_model):
        """Offline injection: deliberately bypass legacy config/History/start."""
        hub=cls.__new__(cls)
        hub.read_model=read_model
        hub.stop=threading.Event()
        hub.bytes_out=0
        return hub

    def v1_snapshot(self):
        return self.read_model.snapshot()

    def __init__(self,app,state,port,hold=False):
        self.app=Path(app);self.config=json.loads((self.app/'config.json').read_text());self.definitions={d['id']:d for d in self.config['devices']}
        self.hold=hold
        self.state=Path(state);self.state.mkdir(parents=True,exist_ok=True);self.lock=threading.RLock();self.stop=threading.Event();self.watch_until=0
        self.clients=set();self.devices={};self.events=deque(maxlen=100);self.revision=0;self.network={};self.metrics={};self.receiver={};self.meter=Meter()
        self.port=port;self.bytes_out=0;self.remote_bytes=0;self.session_context={};self.images={};self.image_lock=threading.Lock();self.connect_gate=threading.BoundedSemaphore(3)
        if hold:
            from recovery_history import RecoveryHistory
            self.history=RecoveryHistory(app,state)
        else:self.history=History(app,state)
        for d in self.definitions:
            self.devices[d]={'id':d,'host':self.definitions[d]['host'],'online':None,'app_online':False,'state':'connecting','updated_at':None,
                             'receive_enabled':not self.definitions[d].get('historical_only') and self.definitions[d].get('enabled',True),
                             'write_enabled':not hold}
        cache=self.state/'last-status.json'
        try:
            if hold:raise FileNotFoundError
            saved=json.loads(cache.read_text())
            for d,row in saved.items():
                if d in self.devices:
                    self.devices[d].update(row,online=None,app_online=False,state='connecting',image_visible=False)
                    self.devices[d]['host']=self.definitions[d]['host']
                    self.devices[d]['receive_enabled']=not self.definitions[d].get('historical_only') and self.definitions[d].get('enabled',True)
                    self.devices[d]['write_enabled']=not hold
        except (OSError,ValueError):pass

    def active(self):
        with self.lock:return time.monotonic()<self.watch_until

    def touch(self):
        with self.lock:self.watch_until=time.monotonic()+35

    def publish(self,kind,data):
        value={'event':kind,'data':data};body=encoded(value)
        with self.lock:
            self.revision+=1
            for client in tuple(self.clients):
                try:client.put_nowait(value)
                except queue.Full:
                    try:client.get_nowait();client.put_nowait({'event':'reset','data':{'revision':self.revision}})
                    except (queue.Empty,queue.Full):pass

    def update(self,device,data,transport='ssh',verified=True):
        if data.get('id')!=device:raise ValueError('Device identity mismatch')
        if self.hold:
            data=dict(data);data['write_enabled']=False;data['image_visible']=False;data['privacy_required']=True
        now=time.time()
        with self.lock:
            previous=dict(self.devices[device]);row=dict(previous);row.update(data,online=True,updated_at=now,transport=transport,identity_verified=verified)
            row['connection_error']=None
            if transport=='http':row['network']=None
            self.devices[device]=row
            if previous.get('state')!=row.get('state'):
                event={'at':now,'device':device,'state':row.get('state'),'image_name':row.get('image_name')};self.events.appendleft(event)
            session=row.get('session')
            if session and row.get('image_name'):
                self.session_context[(device,session)]={'device':device,'session':session,'mode':row.get('mode'),'task_name':(row.get('task') or {}).get('name'),
                    'image_name':row.get('image_name'),'image_key':row.get('image_name'),'created':session_time(session) or now}
            last=row.get('last_completed') or {};context=self.session_context.get((device,last.get('session')))
        if context and last.get('outcome') in ('success','pending_qc','failed','error','interrupted'):
            self.history.upsert(dict(context,outcome=last['outcome'],saved=last['outcome'] in ('success','pending_qc'),level=last.get('qc_level'),source='live-observation'))
        self.publish('device',{'device':self.public_device(row)})

    def heartbeat(self,device,value):
        with self.lock:
            row=self.devices[device];row.update(updated_at=time.time(),online=True)
            if value.get('network') is not None:row['network']=value['network']
            event={'id':device,'updated_at':row['updated_at'],'network':row.get('network')}
        self.publish('heartbeat',event)

    def disconnected(self,device,error):
        reason='当前地址暂不可达'
        if 'Permission denied' in error or 'Host key' in error or 'verification failed' in error:reason='监看连接需要配置授权'
        if 'IDENTITY_MISMATCH' in error:reason='设备身份与登记不一致'
        with self.lock:
            row=dict(self.devices[device]);row.update(online=False,app_online=False,state='offline',image_visible=False,connection_error=reason);self.devices[device]=row
        self.publish('device',{'device':self.public_device(row)})

    @staticmethod
    def public_device(row):
        fields=('id','host','online','app_online','state','mode','context','recording','session','image_name','image_id','image_visible','privacy_required','recording_since','task','daily','run_saved','sensors','errors','last_completed','version','storage','network','updated_at','transport','identity_verified','receive_enabled','write_enabled','connection_error')
        return {k:row.get(k) for k in fields}

    def snapshot(self):
        with self.lock:
            return {'revision':self.revision,'now':time.time(),'devices':[self.public_device(self.devices[d]) for d in sorted(self.devices)],
                    'network':self.network,'history':self.metrics,'history_ready':self.history.ready,'history_error':self.history.error,'history_last_success':self.history.last_success,'receiver':self.receiver,'events':list(self.events)[:15],
                    'policy':{'readonly':True,'thumbnail_on_demand':True,'no_sensor_streams':True,
                              'write_enabled':False if self.hold else None,
                              'maintenance_history_day':'2026-11-09' if self.hold else None,
                              'today_indexed':False if self.hold else True}}

    def host_loop(self):
        last_sync=last_receiver=last_cache=0
        while not self.stop.is_set():
            now=time.monotonic()
            try:
                lan=route_interface(next(iter(self.definitions.values()))['host']);internet=route_interface()
                values={name:self.meter.sample(name) for name in set((lan,internet)) if name}
                network={'lan':values.get(lan,{'interface':None,'rx_bps':None,'tx_bps':None}),
                         'internet':values.get(internet,{'interface':None,'rx_bps':None,'tx_bps':None}),'shared_interface':lan==internet,'at':time.time()}
                with self.lock:self.network=network
                self.publish('host',{'network':network,'now':time.time()})
                if now-last_sync>=10:
                    self.history.sync();metrics=self.history.summaries()
                    with self.lock:changed=(metrics,self.history.ready,self.history.error,self.history.last_success)!=(self.metrics,getattr(self,'published_history_ready',None),getattr(self,'published_history_error',None),getattr(self,'published_history_success',None));self.metrics=metrics;self.published_history_ready=self.history.ready;self.published_history_error=self.history.error;self.published_history_success=self.history.last_success
                    if changed:self.publish('history',{'history':metrics,'ready':self.history.ready,'error':self.history.error,'last_success':self.history.last_success})
                    last_sync=now
                if now-last_receiver>=5:
                    receiver=self.history.receiver();receiver={k:receiver.get(k) for k in ('control','summary','error')}
                    try:
                        req=urllib.request.Request('http://127.0.0.1:%s/health'%self.config.get('port',8788))
                        with urllib.request.urlopen(req,timeout=2) as r:receiver['storage']=json.load(r).get('storage')
                    except Exception:receiver['storage']={'ready':False,'error':'接收服务暂未响应'}
                    with self.lock:changed=receiver!=self.receiver;self.receiver=receiver
                    if changed:self.publish('receiver',receiver)
                    last_receiver=now
                if now-last_cache>=30:
                    with self.lock:cache=dict(self.devices)
                    target=self.state/'last-status.json';temp=target.with_suffix('.tmp');temp.write_text(json.dumps(cache,ensure_ascii=False));os.replace(temp,target);last_cache=now
            except Exception as e:print('HOST_STATUS_ERROR '+type(e).__name__,flush=True)
            self.stop.wait(2)

    def http_status(self,device,full=False):
        host=self.definitions[device]['host'];endpoint='status?signals=&review=0&cameras=0&display=fleet-monitor' if full else 'display-state'
        request=urllib.request.Request('http://%s:8765/api/%s'%(host,endpoint))
        with urllib.request.urlopen(request,timeout=2) as r:
            raw=r.read(524289)
            if len(raw)>524288:raise ValueError('Status exceeds bounded size')
            self.remote_bytes+=len(raw);return json.loads(raw)

    def http_window(self,device,seconds=60):
        start=time.monotonic();next_full=0;full={};previous=None;key=None
        while self.active() and not self.stop.is_set() and time.monotonic()-start<seconds:
            try:gate=self.http_status(device)
            except urllib.error.HTTPError as e:
                if e.code!=404:raise
                full=self.http_status(device,True);gate=full
            current=(gate.get('state'),gate.get('session'),gate.get('image'),gate.get('collection_context'))
            if current!=key or time.monotonic()>=next_full:
                try:full=self.http_status(device,True)
                except Exception:pass
                next_full=time.monotonic()+20;key=current
            row=normalized(device,gate,full,self.state,True);body=json.dumps(row,sort_keys=True,ensure_ascii=False)
            if body!=previous:self.update(device,row,'http',False);previous=body
            else:self.heartbeat(device,{})
            self.stop.wait(2)

    def device_loop(self,device):
        if self.hold:
            self.hold_device_loop(device);return
        backoff=3;script=(ROOT/'agent.py').read_text()
        command=['ssh','-T','-o','LogLevel=ERROR','-o','ConnectTimeout=4','-F',str(self.app/'ssh/config'),device,
                 'exec nice -n 15 python3 -u -c '+shlex.quote(script)+' '+shlex.quote(device)]
        while not self.stop.is_set():
            if not self.active():self.stop.wait(1);continue
            process=None;error=''
            try:
                with self.connect_gate:
                    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.DEVNULL,start_new_session=True)
                stderr=[]
                def drain(stream=process.stderr,accumulator=stderr):
                    for line in iter(stream.readline,b''):accumulator.append(line.decode(errors='replace')[-300:]);del accumulator[:-4]
                threading.Thread(target=drain,daemon=True).start()
                os.set_blocking(process.stdout.fileno(),False);selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ);buffer=b'';last_message=time.monotonic()
                while self.active() and not self.stop.is_set():
                    if process.poll() is not None:break
                    for ready,_ in selector.select(1):
                        part=os.read(ready.fileobj.fileno(),65536)
                        if not part:break
                        self.remote_bytes+=len(part);buffer+=part
                        if len(buffer)>65536:raise ValueError('Agent response exceeds bounded size')
                        while b'\n' in buffer:
                            line,buffer=buffer.split(b'\n',1);value=json.loads(line);last_message=time.monotonic();backoff=3
                            identity=(value.get('data') or {}).get('id') if value.get('type')=='state' else value.get('id')
                            if identity!=device:raise ValueError('HEADRING_IDENTITY_MISMATCH')
                            if value['type']=='state':self.update(device,value['data'])
                            elif value['type']=='heartbeat':self.heartbeat(device,value)
                            elif value['type']=='records':
                                for row in value.get('rows',[]):
                                    if row.get('device')==device:self.history.upsert(row)
                                self.publish('history-changed',{'id':device})
                    if time.monotonic()-last_message>15:raise TimeoutError('Telemetry heartbeat timeout')
                selector.close();error=''.join(stderr)
            except Exception as e:error=str(e)
            finally:
                if process and process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=4)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
            if not self.active() or self.stop.is_set():continue
            if 'IDENTITY_MISMATCH' not in error:
                try:self.http_window(device);backoff=3;continue
                except Exception:pass
            self.disconnected(device,error);self.stop.wait(backoff);backoff=min(90,backoff*2)

    def hold_device_loop(self,device):
        """Poll only the lightweight display-state endpoint; never SSH or scan captures."""
        backoff=3
        while not self.stop.is_set():
            try:
                with self.connect_gate:gate=self.http_status(device,False)
                remote_id=gate.get('device_id') or gate.get('id')
                if remote_id and remote_id!=device:raise ValueError('HEADRING_IDENTITY_MISMATCH')
                row=normalized(device,gate,{},self.state,True)
                row['image_visible']=False;row['privacy_required']=True;row['write_enabled']=False
                row['network']=None
                self.update(device,row,'http',remote_id==device)
                backoff=3
                self.stop.wait(4)
            except Exception as exc:
                self.disconnected(device,str(exc));self.stop.wait(backoff);backoff=min(60,backoff*2)

    def start(self):
        threading.Thread(target=self.host_loop,daemon=True,name='host-history').start()
        for device in self.definitions:threading.Thread(target=self.device_loop,args=(device,),daemon=True,name='telemetry-'+device).start()

    def thumbnail(self,device,asset):
        with self.lock:row=dict(self.devices[device])
        if not row.get('online') or not row.get('app_online') or not row.get('image_visible') or row.get('recording') or row.get('privacy_required') is not False:raise PermissionError('当前采集阶段不显示图纸')
        if row.get('image_id')!=asset:raise ValueError('图纸已变化，请刷新后查看')
        key=(device,asset)
        with self.image_lock:
            if row.get('transport')=='ssh':
                script=(ROOT/'thumbnail.py').read_text();gate_only=key in self.images
                command=['ssh','-T','-o','LogLevel=ERROR','-o','ConnectTimeout=4','-F',str(self.app/'ssh/config'),device,
                         'exec nice -n 15 /home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi/.venvs/hardware/bin/python -c '+shlex.quote(script)+' '+shlex.quote(device)+' '+shlex.quote(asset)+(' gate' if gate_only else '')]
                process=subprocess.run(command,capture_output=True,text=True,timeout=20)
                if process.returncode:raise ValueError('轻量图纸通道暂不可用')
                value=json.loads(process.stdout)
                if not value.get('ok'):raise PermissionError(value.get('message') or '图纸目前不可显示')
                if gate_only:return self.images[key]
                data=base64.b64decode(value['data'],validate=True)
                if len(data)>160000:raise ValueError('缩略图超过传输预算')
                self.remote_bytes+=len(process.stdout);self.images[key]=data
                if len(self.images)>50:self.images.pop(next(iter(self.images)))
                return data
            gate=self.http_status(device);full=self.http_status(device,True)
            if (gate.get('privacy') or {}).get('required') is not False or (gate.get('privacy') or {}).get('stop_confirmed') is not True or (gate.get('worker') or {}).get('recording_active') is True:raise PermissionError('当前采集阶段不显示图纸')
            if image_id(gate,full)!=asset:raise ValueError('图纸已变化，请刷新后查看')
            if key in self.images:return self.images[key]
            request=urllib.request.Request('http://%s:8765/api/reference'%self.definitions[device]['host'])
            with urllib.request.urlopen(request,timeout=8) as response:
                if int(response.headers.get('Content-Length','0'))>16*1024*1024:raise ValueError('图纸原图过大')
                chunks=[];count=0;start=time.monotonic()
                while True:
                    chunk=response.read(65536)
                    if not chunk:break
                    chunks.append(chunk);count+=len(chunk)
                    if count>16*1024*1024:raise ValueError('图纸原图过大')
                    delay=count/524288-(time.monotonic()-start)
                    if delay>0:time.sleep(min(delay,.15))
                raw=b''.join(chunks)
                self.remote_bytes+=len(raw)
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as image:
                if image.width*image.height>40000000:raise ValueError('图纸尺寸过大')
                image.thumbnail((480,480));target=io.BytesIO();image.convert('RGB').save(target,format='WEBP',quality=75);data=target.getvalue()
            after=self.http_status(device)
            if (after.get('privacy') or {}).get('required') is not False or after.get('image')!=gate.get('image') or after.get('collection_context')!=gate.get('collection_context'):raise PermissionError('图纸显示状态已变化')
            self.images[key]=data
            if len(self.images)>50:self.images.pop(next(iter(self.images)))
            return data

class Handler(BaseHTTPRequestHandler):
    hub=None;origin_port=8788
    def log_message(self,*_):pass
    def cors(self):
        origin=self.headers.get('Origin')
        if origin:
            parsed=urllib.parse.urlsplit(origin);host=urllib.parse.urlsplit('http://'+self.headers.get('Host','')).hostname
            if parsed.scheme not in ('http','https') or parsed.hostname!=host or parsed.port!=self.origin_port:raise PermissionError('不允许跨站读取监看数据')
            self.send_header('Access-Control-Allow-Origin',origin);self.send_header('Access-Control-Expose-Headers','ETag');self.send_header('Vary','Origin')
    def send(self,status,value,content='application/json; charset=utf-8'):
        data=encoded(value) if not isinstance(value,bytes) else value
        self.send_response(status);self.cors();self.send_header('Content-Type',content);self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data);self.hub.bytes_out+=len(data)
    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path in ('/api/v1/fleet','/api/v1/fleet/events'):
            self.v1_get();return
        parsed=urllib.parse.urlsplit(self.path);query=urllib.parse.parse_qs(parsed.query)
        device=query.get('device',[None])[0]
        if device and device not in self.hub.definitions:self.send(404,{'message':'头环未登记'});return
        try:
            if parsed.path=='/health':self.send(200,{'ok':True,'readonly':True});return
            self.hub.touch()
            if parsed.path=='/api/fleet/summary':
                value=self.hub.snapshot();etag='W/"%s"'%value['revision']
                if self.headers.get('If-None-Match')==etag:
                    self.send_response(304);self.cors();self.send_header('ETag',etag);self.end_headers();return
                data=encoded(value);self.send_response(200);self.cors();self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('ETag',etag);self.send_header('Cache-Control','no-cache');self.end_headers();self.wfile.write(data);return
            if parsed.path=='/api/fleet/events':self.events();return
            page=max(1,int(query.get('page',['1'])[0]));limit=max(1,min(50,int(query.get('limit',['20'])[0])))
            period=query.get('period',['all'])[0];mode=query.get('mode',['all'])[0]
            if (period not in ('all','today') and not re.fullmatch(r'20\d\d-\d\d-\d\d',period)) or mode not in ('all','emg','eeg'):raise ValueError('筛选条件无效')
            if parsed.path=='/api/fleet/tasks':self.send(200,self.hub.history.tasks(device,period,mode,page,limit));return
            if parsed.path=='/api/fleet/sessions':self.send(200,self.hub.history.sessions(device,period,mode,page,limit,query.get('task',[None])[0]));return
            if parsed.path=='/api/fleet/receiver':self.send(200,self.hub.history.receiver(page,limit,device));return
            if parsed.path=='/api/fleet/image':
                asset=query.get('asset',[''])[0]
                if not device or not re.fullmatch(r'[a-f0-9]{64}',asset):raise ValueError('图纸标识无效')
                self.send(200,self.hub.thumbnail(device,asset),'image/webp');return
            self.send(404,{'message':'接口不存在'})
        except PermissionError as e:self.send(403,{'message':str(e)})
        except (ValueError,KeyError) as e:self.send(409,{'message':str(e)})
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:self.send(503,{'message':'监看数据暂不可用，请稍后重试'})
    def v1_get(self):
        from snapshot_http import error
        if '?' in self.path:self.send(400,error('INVALID_REQUEST'));return
        model=getattr(self.hub,'read_model',None)
        if model is None:self.send(503,error('SNAPSHOT_UNAVAILABLE'));return
        if self.path=='/api/v1/fleet':self.send(200,self.hub.v1_snapshot());return
        client=model.subscribe(self.headers.get('Last-Event-ID'))
        try:
            self.send_response(200);self.cors();self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-store');self.end_headers()
            while not self.hub.stop.is_set():
                try:
                    value=client.get(timeout=.5)
                    raw=('id: '+value['id']+'\nevent: '+value['event']+'\ndata: ').encode()+encoded(value['data'])+b'\n\n'
                    self.wfile.write(raw);self.wfile.flush()
                except queue.Empty:continue
        except (BrokenPipeError,ConnectionResetError,OSError):pass
        finally:model.unsubscribe(client)

    def do_OPTIONS(self):self.send_response(204);self.cors();self.send_header('Access-Control-Allow-Methods','GET, OPTIONS');self.send_header('Access-Control-Allow-Headers','If-None-Match');self.end_headers()
    def _deny_write(self):self.send(405,{'error':'read_only_fault_hold','message':'维护只读模式拒绝全部写请求'})
    def do_POST(self):self._deny_write()
    def do_PUT(self):self._deny_write()
    def do_PATCH(self):self._deny_write()
    def do_DELETE(self):self._deny_write()
    def events(self):
        client=queue.Queue(maxsize=80)
        with self.hub.lock:
            if len(self.hub.clients)>=16:self.send(503,{'message':'监看连接数量已达上限'});return
            self.hub.clients.add(client)
        self.send_response(200);self.cors();self.send_header('Content-Type','text/event-stream; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Accel-Buffering','no');self.end_headers()
        try:
            self.write_event('snapshot',self.hub.snapshot())
            while not self.hub.stop.is_set():
                self.hub.touch()
                try:value=client.get(timeout=10);self.write_event(value['event'],value['data'])
                except queue.Empty:self.wfile.write(b': keepalive\n\n');self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError,OSError):pass
        finally:
            with self.hub.lock:self.hub.clients.discard(client)
    def write_event(self,event,data):
        raw=b'event: '+event.encode()+b'\ndata: '+encoded(data)+b'\n\n';self.wfile.write(raw);self.wfile.flush();self.hub.bytes_out+=len(raw)

def main():
    p=argparse.ArgumentParser();p.add_argument('--app',required=True);p.add_argument('--state',required=True);p.add_argument('--port',type=int,default=8791);p.add_argument('--origin-port',type=int,default=8788);p.add_argument('--fault-hold',action='store_true');args=p.parse_args()
    hub=Hub(args.app,args.state,args.port,args.fault_hold);Handler.hub=hub;Handler.origin_port=args.origin_port
    server=ThreadingHTTPServer(('0.0.0.0',args.port),Handler);server.daemon_threads=True;hub.start()
    def stop(*_):hub.stop.set();threading.Thread(target=server.shutdown,daemon=True).start()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    print('Fleet monitor listening on %s; readonly'%args.port,flush=True)
    try:server.serve_forever()
    finally:hub.stop.set();server.server_close()

if __name__=='__main__':main()
