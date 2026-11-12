"""Ephemeral read-only telemetry; never imports the collector or opens sensors."""
import json,sys,time,os,signal,urllib.request,urllib.error,hashlib,socket,fcntl,struct,re
from pathlib import Path

STOP=False
def stop(*_):
    global STOP
    STOP=True

def read_json(path,default=None,limit=262144):
    try:
        p=Path(path)
        if p.is_symlink() or p.stat().st_size>limit:return default
        return json.loads(p.read_text())
    except (OSError,ValueError):return default

def scalar_config(path):
    out={}
    try:
        p=Path(path)
        if p.is_symlink() or p.stat().st_size>262144:return out
        for line in p.read_text().splitlines():
            if not line or line[0].isspace() or ':' not in line:continue
            k,v=line.split(':',1);v=v.strip()
            if not v or v in ('null','~'):out[k]=None;continue
            if v[0:1]=='"':
                try:v=json.loads(v)
                except ValueError:pass
            elif v[0:1]=="'" and v[-1:]=="'":v=v[1:-1].replace("''","'")
            out[k]=v
    except OSError:pass
    return out

def base():
    candidates=[Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi'),Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main/orangepi')]
    return next((p for p in candidates if (p/'runtime/dashboard_token').is_file()),candidates[0])

def request(code_base,endpoint,limit=524288):
    token=(code_base/'runtime/dashboard_token').read_text().strip()
    req=urllib.request.Request('http://127.0.0.1:8765/api/'+endpoint,headers={'X-Access-Token':token})
    with urllib.request.urlopen(req,timeout=3) as r:
        data=r.read(limit+1)
        if len(data)>limit:raise ValueError('Status too large')
        return json.loads(data)

def short(value,n=160):return str(value or '')[:n]

def image_id(gate,full):
    key=[gate.get('collection_context'),gate.get('image'),full.get('image_name')]
    if not key[1]:return None
    return hashlib.sha256(json.dumps(key,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def net_interface():
    server_ip=os.environ.get('SSH_CONNECTION','').split()
    server_ip=server_ip[2] if len(server_ip)>2 else None
    names=[]
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        name=line.split(':',1)[0].strip()
        if name!='lo':names.append(name)
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
        for name in names:
            try:
                info=fcntl.ioctl(s.fileno(),0x8915,struct.pack('256s',name[:15].encode()))
                if socket.inet_ntoa(info[20:24])==server_ip:return name
            except OSError:pass
    return next((n for n in names if (Path('/sys/class/net')/n/'device').exists()),names[0] if names else None)

class Net:
    def __init__(self):self.interface=net_interface();self.last=None
    def sample(self):
        result={'interface':self.interface,'rx_bps':None,'tx_bps':None}
        try:
            p=Path('/sys/class/net')/self.interface/'statistics'
            current=(time.monotonic(),int((p/'rx_bytes').read_text()),int((p/'tx_bytes').read_text()))
            if self.last and current[0]>self.last[0]:
                result['rx_bps']=max(0,current[1]-self.last[1])/(current[0]-self.last[0])
                result['tx_bps']=max(0,current[2]-self.last[2])/(current[0]-self.last[0])
            self.last=current
        except (OSError,TypeError):pass
        return result

def normalized(device,gate,full,code_base,api_ok):
    worker=gate.get('worker') or {};privacy=gate.get('privacy') or {}
    flag=worker.get('recording_active')
    recording=flag if isinstance(flag,bool) else gate.get('state')=='recording'
    task=full.get('task_list') or {};daily=full.get('daily_collection') or {}
    release=read_json(code_base/'runtime/fleet-release.json',{}) or {}
    sensors=[]
    selected=full.get('selected_devices') or []
    for row in full.get('devices') or []:
        sensors.append({'name':short(row.get('name'),60),'label':short(row.get('label'),70),'connected':row.get('connected') is True,
                        'status':short(row.get('status'),40),'selected':row.get('name') in selected})
    impedance=(full.get('worker') or {}).get('eeg_impedance') or full.get('eeg_impedance') or {}
    quality=impedance.get('quality') or {}
    image_name=full.get('image_name') or (Path(gate['image']).stem if gate.get('image') else '')
    if not gate.get('image') and privacy.get('required') is False and not recording:image_name=''
    errors=[short(x) for x in (full.get('errors') or [])[-3:]]
    if quality.get('passed') is False and impedance.get('required'):
        errors.append('脑电预检有 %s 路需关注，按采集界面提示处理。'%len(quality.get('failed_channels') or []))
    return {'id':device,'app_online':api_ok,'state':short(gate.get('state') or 'unavailable',50),
            'mode':short(gate.get('collection_mode') or full.get('collection_mode'),20),
            'context':short(gate.get('collection_context'),100),'recording':recording,
            'session':Path(gate['session']).name if gate.get('session') else None,
            'image_name':short(image_name,240),'image_id':image_id(gate,full),
            'image_visible':bool(api_ok and gate.get('image') and not recording and privacy.get('required') is False and privacy.get('stop_confirmed') is True),
            'privacy_required':privacy.get('required',True),'recording_since':full.get('recording_since') if recording else None,
            'task':{k:task.get(k) for k in ('id','name','total','remaining','position','block_size','block_completed','selection_mode','block_task_id')},
            'daily':{k:daily.get(k) for k in ('day','qualified_count','pass_count','warn_count','total_seconds','ready')},
            'run_saved':full.get('success_count'),'sensors':sensors,'errors':errors,
            'last_completed':{k:(full.get('last_completed') or {}).get(k) for k in ('session','outcome','qc_level')},
            'version':short(release.get('version'),80),'storage':full.get('storage') or {}}

def roots(code_base):
    profiles=read_json(code_base/'runtime/collection_profile.json',{}) or {}
    result=[]
    for mode in ('emg','eeg'):
        p=code_base if mode==profiles.get('legacy_mode','emg') else code_base.parent/'profiles'/mode/'orangepi'
        c=scalar_config(p/'configs/storage.yaml')
        if not all(c.get(k) for k in ('mount_point','directory','device_id','shift')):continue
        volume=Path(c['mount_point'])/c['directory']/c['device_id']
        if c.get('capture_profile'):volume=volume/'profiles'/c['capture_profile']
        result.append((mode,volume/'data'/c['shift']))
    return result

def record_meta(device,mode,path):
    m=read_json(path/'meta.json',{}) or {};y=scalar_config(path/'meta.yaml')
    outcome=y.get('status')
    if outcome not in ('success','pending_qc','qc_error','failed','error','interrupted'):return None
    if not y.get('retention_state') and not y.get('retained_at_unix'):return None
    selection=m.get('reference_selection') or {};asset=selection.get('asset') or {}
    lists=asset.get('source_lists') or []
    task=(lists[0].get('name') if lists else None) or (m.get('reference_bundle') or {}).get('task') or y.get('task_name') or ''
    name=asset.get('name') or (Path(m['reference_image']).stem if m.get('reference_image') else '')
    created=m.get('session_created_unix')
    if not created:
        match=re.search(r'(20\d\d-\d\d-\d\d-\d\d-\d\d-\d\d)',path.name)
        if match:
            import datetime
            created=datetime.datetime.strptime(match.group(1),'%Y-%m-%d-%H-%M-%S').replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()
    return {'device':device,'session':path.name,'mode':m.get('collection_mode') or mode,'task_name':short(task,240),
            'image_name':short(name,240),'image_key':short(asset.get('id') or m.get('reference_sha256') or name,160),
            'created':created,'outcome':outcome,'saved':outcome in ('success','pending_qc','qc_error'),
            'level':None,'test':m.get('test') is True}

class TodayIndex:
    def __init__(self):self.cache={};self.pending=[];self.next_scan=0
    def scan(self,device,code_base,gate):
        out=[]
        if gate.get('state') not in ('device_confirm','idle','failed_wait','exhausted'):return out
        if (gate.get('worker') or {}).get('recording_active'):return out
        day=time.strftime('%Y-%m-%d',time.gmtime(time.time()+8*3600))
        if time.monotonic()>=self.next_scan:
            self.pending=[]
            for mode,parent in roots(code_base):
                try:
                    for entry in os.scandir(parent):
                        if day not in entry.name or not entry.is_dir(follow_symlinks=False) or not re.fullmatch(r'L[23]-[A-Za-z0-9_.-]+',entry.name):continue
                        path=Path(entry.path)
                        if gate.get('session') and str(path)==gate['session']:continue
                        try:stamp=(path/'meta.yaml').stat().st_mtime_ns
                        except OSError:continue
                        if self.cache.get(str(path))!=stamp:self.pending.append((mode,path,stamp))
                except OSError:pass
            self.next_scan=time.monotonic()+30
        for _ in range(min(5,len(self.pending))):
            mode,path,stamp=self.pending.pop(0)
            row=record_meta(device,mode,path)
            if row:out.append(row);self.cache[str(path)]=stamp
        return out

def emit(row):
    try:print(json.dumps(row,ensure_ascii=False,separators=(',',':'),allow_nan=False),flush=True)
    except (BrokenPipeError,OSError):stop()

def run():
    for sig in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT):signal.signal(sig,stop)
    device=Path('/opt/egorecord/DEVICE_ID').read_text().strip()
    if len(sys.argv)>1 and device!=sys.argv[1]:raise RuntimeError('HEADRING_IDENTITY_MISMATCH')
    code_base=base()
    network=Net();index=TodayIndex();full={};last_gate_key=None;last_state=None;next_full=next_heartbeat=0
    while not STOP:
        started=time.monotonic();api_ok=True
        try:
            try:gate=request(code_base,'display-state')
            except urllib.error.HTTPError as e:
                if e.code!=404:raise
                full=request(code_base,'status?signals=&review=0&cameras=0&display=fleet-monitor')
                gate=full
            key=(gate.get('state'),gate.get('session'),gate.get('image'),gate.get('collection_context'))
            if key!=last_gate_key or started>=next_full:
                try:full=request(code_base,'status?signals=&review=0&cameras=0&display=fleet-monitor');next_full=started+15
                except Exception:pass
            last_gate_key=key
        except Exception:
            api_ok=False;gate={'state':'unavailable','worker':{},'privacy':{'required':True}}
        state=normalized(device,gate,full,code_base,api_ok)
        encoded=json.dumps(state,ensure_ascii=False,sort_keys=True,allow_nan=False)
        if encoded!=last_state:
            emit({'type':'state','sampled_at':time.time(),'data':state});last_state=encoded
        if started>=next_heartbeat:
            emit({'type':'heartbeat','id':device,'sampled_at':time.time(),'app_online':api_ok,'network':network.sample()});next_heartbeat=started+5
        rows=index.scan(device,code_base,gate) if api_ok else []
        if rows:emit({'type':'records','id':device,'rows':rows})
        time.sleep(max(.1,2-(time.monotonic()-started)))

if __name__=='__main__':run()
