"""Fixed USB import plans; browser supplies opaque IDs, never paths or commands."""
import base64,hashlib,ipaddress,json,os,re,shlex,subprocess,time,selectors,threading
from pathlib import Path
import online
HERE=Path(__file__).parent
REGISTRY='usb-tasks-v1'
def canonical(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
def store(path,data):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    raw=canonical(data);fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    return hashlib.sha256(raw).hexdigest()
def load(path,limit=8388608):
    if path.is_symlink() or path.stat().st_size>limit:raise ValueError('unsafe_state')
    return json.loads(path.read_bytes())
def remote(config,device,payload,timeout=120,on_event=None):
    if not re.fullmatch(r'cp_\d{4}',device) or device not in config['devices']:raise ValueError('invalid_device')
    ip=str(ipaddress.IPv4Address(config['devices'][device]));ssh=online.SSH(config['ssh_config'],device,ip,config.get('bind_interface'),config.get('bind_address'))
    script=(HERE/'zip_preview.py').read_bytes()+b'\n'+(HERE/'resources/usb_import_adapter.py').read_bytes()
    bootstrap='import base64;exec(compile(base64.b64decode('+repr(base64.b64encode(script).decode())+'),"usb_adapter","exec"))'
    command='python3 -c '+shlex.quote(bootstrap)
    request=canonical(payload)
    if len(request)>8388608:raise ValueError('request_too_large')
    args=ssh.argv[:-1]+['-o','BatchMode=yes','-T',ssh.argv[-1],command]
    if on_event is None:
        proc=subprocess.run(args,input=request,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
        if proc.returncode or len(proc.stdout)>8388608:raise RuntimeError('remote_unavailable')
        result=json.loads(proc.stdout)
    else:
        proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        def writer():
            try:proc.stdin.write(request)
            except OSError:pass
            finally:proc.stdin.close()
        threading.Thread(target=writer,daemon=True).start()
        selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ);pending=b'';result=None;deadline=time.monotonic()+timeout
        try:
            while time.monotonic()<deadline:
                if not selector.select(1):
                    if proc.poll() is not None:break
                    continue
                chunk=os.read(proc.stdout.fileno(),65536)
                if not chunk:break
                pending+=chunk
                if len(pending)>8388608:raise RuntimeError('remote_too_large')
                while b'\n' in pending:
                    raw,pending=pending.split(b'\n',1);value=json.loads(raw)
                    if isinstance(value,dict) and 'event' in value:
                        event=value['event']
                        if isinstance(event,str) and (event=='verifying' or re.fullmatch(r'(building|ready|committing):\d{1,8}:\d{1,8}',event)):on_event(event)
                    else:result=value
            if proc.poll() is None:
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.terminate();proc.wait(timeout=5);raise TimeoutError('remote_timeout')
            if proc.returncode or pending or not isinstance(result,dict):raise RuntimeError('remote_unavailable')
        finally:
            selector.close();proc.stdout.close()
            if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
    if not isinstance(result,dict):raise ValueError('invalid_remote_result')
    return result
def scan(state,config,device):
    result=remote(config,device,{'op':'scan','device':device},timeout=80)
    if result.get('status')!='ok':raise RuntimeError('scan_failed')
    scan=result.get('scan') or {}; sid=scan.get('scan_id');entries=scan.get('files')
    if not re.fullmatch(r'[a-f0-9]{32}',sid or '') or not isinstance(entries,list) or len(entries)>500:raise ValueError('invalid_scan')
    clean=[]
    for e in entries:
        if not isinstance(e,dict) or not re.fullmatch(r'[a-f0-9]{32}',e.get('id','')) or not isinstance(e.get('relative_path'),str) or type(e.get('size_bytes'))!=int:raise ValueError('invalid_scan')
        clean.append({'id':e['id'],'relative_path':e['relative_path'],'name':str(e.get('name') or '')[:240],'size_bytes':e['size_bytes']})
    record={'device':device,'address':config['devices'][device],'mode':result['mode'],'context':result['context'],'scan_id':sid,'revision':scan['catalog_revision'],'created_at':time.time(),'entries':clean}
    store(Path(state)/'imports/scans'/(sid+'.json'),record)
    return {'scan_id':sid,'mode':record['mode'],'context':record['context'],'revision':record['revision'],'files':[{'id':x['id'],'name':x['name'],'size_bytes':x['size_bytes']} for x in clean],'note':'USB 扫描可能触发只读挂载；旧列表默认保留'}
def preview(state,config,device,sid,ids):
    if not re.fullmatch(r'[a-f0-9]{32}',sid or '') or not isinstance(ids,list) or not 1<=len(ids)<=50 or any(not isinstance(x,str) or not re.fullmatch(r'[a-f0-9]{32}',x) for x in ids) or len(set(ids))!=len(ids):raise ValueError('invalid_selection')
    scan=load(Path(state)/'imports/scans'/(sid+'.json'))
    if scan['device']!=device or scan['address']!=config['devices'].get(device) or time.time()-scan['created_at']>600:raise ValueError('scan_expired')
    byid={x['id']:x for x in scan['entries']};entries=[byid[x] for x in ids if x in byid]
    if len(entries)!=len(ids):raise ValueError('unknown_zip_id')
    payload={'op':'preview','device':device,'scan':sid,'mode':scan['mode'],'context':scan['context'],'revision':scan['revision'],'entries':entries}
    info=remote(config,device,payload,timeout=300)
    if info.get('status')!='preview':raise ValueError(info.get('reason','preview_failed'))
    plan={'schema':1,'operation':'import_tasks','device':device,'address':scan['address'],'created_at':time.time(),'scan':sid,'mode':scan['mode'],'context':scan['context'],'revision':scan['revision'],'entries':entries,'expected':info,'remove_list_ids':[]}
    raw=canonical(plan);digest=hashlib.sha256(raw).hexdigest();path=Path(state)/'imports/plans'/(digest+'.json')
    try:store(path,plan)
    except FileExistsError:
        if path.read_bytes()!=raw:raise ValueError('plan_collision')
    return {'plan_sha256':digest,'device':device,'address':scan['address'],'mode':scan['mode'],'context':scan['context'],'revision':scan['revision'],'created_at':plan['created_at'],'old_lists_preserved':True,'new_unique':info['new_unique'],'reused_unique':info['reused_unique'],'name_conflicts':info['name_conflicts'],'deleted_conflicts':info.get('deleted_conflicts',0),'existing_lists':info['existing_lists'],'available_bytes':info['available_bytes'],'estimated_bytes':info['estimated_bytes'],'volume_majmin':info['volume_majmin'],'sources':[{'id':z['id'],'sha256':z['sha256'],'size':z['size'],'images':len(z['images'])} for z in info['sources']],'coverage':'USB ZIP 内容逐项验证；真实提交仍需控制器回读'}
def execute(state,config,device,digest,progress=lambda *_:None):
    if not re.fullmatch(r'[a-f0-9]{64}',digest or ''):raise ValueError('invalid_plan')
    path=Path(state)/'imports/plans'/(digest+'.json');plan=load(path)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=digest or plan.get('schema')!=1 or plan.get('operation')!='import_tasks' or plan.get('device')!=device or plan.get('address')!=config['devices'].get(device) or plan.get('remove_list_ids')!=[] or time.time()-plan.get('created_at',0)>600:raise ValueError('plan_expired_or_scope')
    if plan.get('expected',{}).get('deleted_conflicts',0):raise ValueError('deleted_asset_conflict')
    progress('preflight')
    marker=Path(state)/'imports/started'/(digest+'.json');marker.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    # Durable one-shot marker is written before issuing the one remote action.
    store(marker,{'device':device,'plan_sha256':digest,'state':'started','created_at':time.time()})
    progress('applying')
    payload={'op':'apply','device':device,'scan':plan['scan'],'mode':plan['mode'],'context':plan['context'],'revision':plan['revision'],'entries':plan['entries'],'expected':plan['expected']}
    try:result=remote(config,device,payload,timeout=1320,on_event=progress)
    except Exception:return {'status':'interrupted_reconcile','reason':'transport_unknown'}
    if result.get('status') not in ('succeeded','failed','partial_failed','interrupted_reconcile'):return {'status':'interrupted_reconcile','reason':'invalid_remote_result'}
    return result
