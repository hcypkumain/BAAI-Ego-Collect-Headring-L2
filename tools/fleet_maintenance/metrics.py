import json, time, subprocess, re, ipaddress, os
from pathlib import Path
import online
PROBE=Path(__file__).parent/'resources/metrics_probe.py'
def collect(config,device):
    if not re.fullmatch(r'cp_\d{4}',device) or device not in config['devices']: raise ValueError('invalid device')
    address=str(ipaddress.IPv4Address(config['devices'][device]))
    ssh=online.SSH(config['ssh_config'],device,address,config.get('bind_interface'),config.get('bind_address'))
    argv=ssh.argv[:-1]+['-o','BatchMode=yes','-T',ssh.argv[-1],'python3 - '+device]
    p=subprocess.run(argv,input=PROBE.read_bytes(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45)
    if p.returncode: raise RuntimeError('SSH unavailable or authentication failed')
    result=json.loads(p.stdout)
    if result.get('error'): raise RuntimeError('remote probe context unavailable')
    if result.get('identity_mismatch') or result.get('identity')!=device: return {'identity_mismatch':True,'address':address,'checked_at':time.time()}
    now=time.time()
    result.update(address=address,checked_at=now,last_seen=now,online=True)
    return result

def deep_check(config,device):
    """Explicitly hash reviewed source manifest and aggregate protected metadata only."""
    import hashlib, fleet
    if not re.fullmatch(r'cp_\d{4}',device) or device not in config['devices']: raise ValueError('invalid device')
    address=str(ipaddress.IPv4Address(config['devices'][device]))
    ssh=online.SSH(config['ssh_config'],device,address,config.get('bind_interface'),config.get('bind_address'))
    if ssh.run('cat /opt/egorecord/DEVICE_ID').decode().strip()!=device: raise RuntimeError('identity mismatch')
    raw=(fleet.RESOURCES/'manifest.json').read_bytes()
    if fleet.sha(raw)!=fleet.MANIFEST_SHA256: raise RuntimeError('manifest invalid')
    manifest=json.loads(raw)
    if manifest.get('commit')!=fleet.PIN or len(manifest.get('files',[]))!=236: raise RuntimeError('manifest invalid')
    live=online.read_live(ssh,manifest)
    if live.get('device_id')!=device or live.get('project')!=online.PROJECT: raise RuntimeError('identity mismatch')
    files=live.get('files') or {}
    matched=sum(files.get(item['path'],{}).get('sha256')==item['sha256'] for item in manifest['files'])
    protected=live.get('protected')
    if not isinstance(protected,dict): protected={}
    # Paths and contents, including credentials, never leave this process through the API.
    calibration=[v for k,v in protected.items() if ('calibration' in k.lower()) and isinstance(v,dict)]
    encoded=json.dumps(protected,sort_keys=True,separators=(',',':')).encode()
    return {'checked_at':time.time(),'identity_verified':True,'source':'read_live manifest hash comparison','target_commit':fleet.PIN,'manifest_sha256':fleet.MANIFEST_SHA256,'matched_files':matched,'total_files':len(manifest['files']),'code_matches_pin':matched==len(manifest['files']) and not live.get('errors'),'errors_present':bool(live.get('errors')),'calibration':{'protected_entries':len(calibration),'regular_files':sum(v.get('type')=='regular' for v in calibration),'snapshot_entries':len(protected),'aggregate_sha256':hashlib.sha256(encoded).hexdigest() if protected else None,'previous_deployment_match':None},'component_timer':{k:(live.get('component_timer') or {}).get(k) for k in ('is-enabled','is-active')},'component_full_verification':'unknown_requires_terminal_sudo','hardware':'unknown'}

def create_plan(config,device,state):
    """Build reviewed local artifacts and plan; no staging, sudo or remote mutation."""
    import fleet
    if not re.fullmatch(r'cp_\d{4}',device) or device not in config['devices']: raise ValueError('invalid device')
    address=str(ipaddress.IPv4Address(config['devices'][device]))
    ssh=online.SSH(config['ssh_config'],device,address,config.get('bind_interface'),config.get('bind_address'))
    if ssh.run('cat /opt/egorecord/DEVICE_ID').decode().strip()!=device: raise RuntimeError('identity mismatch')
    bundle=fleet.build(Path(state)/'bundles')
    manifest=json.loads((fleet.RESOURCES/'manifest.json').read_bytes())
    live=online.read_live(ssh,manifest)
    plan=online.prepare_plan(live,manifest,device,bundle)
    digest,_=online.write_plan(Path(state),plan)
    return {'device':device,'address':address,'status':'review_required','plan_sha256':digest,'commit':fleet.PIN,'manifest_sha256':bundle['manifest_sha256'],'code_changes':len(plan['diff']),'protected_entries':len(plan['protected']),'component_changes':sum(x['status']!='same' for x in plan['component_diff']),'component_status':[{'name':Path(x['source']).name,'status':x['status']} for x in plan['component_diff']],'code_diff':[{'path':x['path'],'before_sha256':x['before'],'after_sha256':x['after'],'known_git_history':x['known_git_history']} for x in plan['diff']],'created_at':time.time(),'scope':'read-only remote; local plan and artifacts','apply':'not_available_in_web'}

def fetch_thumbnails(config,device,mode,revision,ids,state):
    import base64, hashlib, io
    from PIL import Image
    if not re.fullmatch(r'cp_\d{4}',device) or device not in config['devices'] or mode not in ('emg','eeg') or type(revision)!=int or not 0<=revision<10**12 or not 1<=len(ids)<=6 or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',x) for x in ids): raise ValueError('invalid asset request')
    address=str(ipaddress.IPv4Address(config['devices'][device]))
    ssh=online.SSH(config['ssh_config'],device,address,config.get('bind_interface'),config.get('bind_address'))
    argv=ssh.argv[:-1]+['-o','BatchMode=yes','-T',ssh.argv[-1],'python3 - '+' '.join([device,mode,str(revision),*ids])]
    proc=subprocess.run(argv,input=(Path(__file__).parent/'resources/thumbnail_probe.py').read_bytes(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=35)
    if proc.returncode: raise RuntimeError('thumbnail SSH failed')
    data=json.loads(proc.stdout)
    if data.get('error'): raise RuntimeError('privacy gate or API unavailable')
    assets=Path(state)/'assets'; assets.mkdir(mode=0o700,exist_ok=True)
    results={}
    for item in data.get('items',[]):
        ident=item.get('id')
        if ident not in ids: continue
        try:
            raw=base64.b64decode(item['data'],validate=True)
            if len(raw)>2097152: raise ValueError()
            with Image.open(io.BytesIO(raw)) as im:
                if im.format not in ('JPEG','PNG') or im.width*im.height>12000000: raise ValueError()
                width,height=im.size; typ=im.format; im.verify()
            digest=hashlib.sha256(raw).hexdigest()
            key=hashlib.sha256((device+'|'+mode+'|'+str(revision)+'|'+ident+'|'+digest).encode()).hexdigest()
            fd=os.open(assets/key,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'wb') as f: f.write(raw)
            results[ident]={'key':key,'sha256':digest,'width':width,'height':height,'format':typ,'kind':'thumbnail'}
        except Exception: results[ident]={'error':'invalid_or_unavailable_image'}
    return results

def fetch_original(config,device,mode,revision,ident,state):
    import base64,hashlib,io
    from PIL import Image
    if not re.fullmatch(r'cp_\d{4}',device) or device not in config['devices'] or mode not in ('emg','eeg') or type(revision)!=int or not 0<=revision<10**12 or not isinstance(ident,str) or not re.fullmatch(r'image_[a-f0-9]{40}',ident): raise ValueError('invalid asset')
    address=str(ipaddress.IPv4Address(config['devices'][device])); ssh=online.SSH(config['ssh_config'],device,address,config.get('bind_interface'),config.get('bind_address'))
    argv=ssh.argv[:-1]+['-o','BatchMode=yes','-T',ssh.argv[-1],'python3 - '+' '.join([device,mode,str(revision),ident])]
    proc=subprocess.run(argv,input=(Path(__file__).parent/'resources/original_probe.py').read_bytes(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45)
    if proc.returncode or len(proc.stdout)>12000000: raise RuntimeError('image unavailable')
    value=json.loads(proc.stdout)
    if value.get('id')!=ident: raise RuntimeError('image unavailable')
    raw=base64.b64decode(value.get('data',''),validate=True)
    digest=hashlib.sha256(raw).hexdigest()
    if digest!=value.get('sha256') or len(raw)>8388608: raise ValueError('image integrity')
    with Image.open(io.BytesIO(raw)) as im:
        if im.format not in ('JPEG','PNG') or im.width*im.height>40000000: raise ValueError('invalid image')
        width,height=im.size; fmt=im.format; im.verify()
    key=hashlib.sha256((device+'|'+mode+'|'+str(revision)+'|'+ident+'|original|'+digest).encode()).hexdigest()
    assets=Path(state)/'assets'; assets.mkdir(mode=0o700,exist_ok=True)
    fd=os.open(assets/key,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'wb') as f: f.write(raw)
    return {'key':key,'sha256':digest,'width':width,'height':height,'format':fmt,'kind':'original'}

def audit_history(config,device):
    import audit_recordings
    if not re.fullmatch(r'cp_\d{4}',device) or device not in config['devices']: raise ValueError('invalid device')
    address=str(ipaddress.IPv4Address(config['devices'][device]))
    ssh=online.SSH(config['ssh_config'],device,address,config.get('bind_interface'),config.get('bind_address'))
    data=audit_recordings.audit(ssh,device,[])
    rows=[]
    for x in data.get('sessions',[]):
        session=x.get('session'); path=x.get('path','')
        if not isinstance(session,str) or not re.fullmatch(r'L2-[A-Za-z0-9-]{5,100}',session): continue
        is_test='/TEST-cp0018/' in path
        rows.append({'session':session,'category':'nonstandard_test' if is_test else 'configured_root_metadata','metadata_files':[i.get('name') for i in x.get('metadata',[]) if i.get('name') in ('native_capture_summary.json','capture_summary.json','meta.json','status.json','manifest.json','raw_manifest.json')],'backup':'unknown','evidence':'metadata_only'})
    return {'sampled_at':time.time(),'source':'bounded_metadata_audit','sessions':rows,'truncated':bool(data.get('truncated')),'errors_count':len(data.get('errors',[])),'raw_backup':'unknown','coverage':'configured_roots_and_known_cp0018_test_roots'}
