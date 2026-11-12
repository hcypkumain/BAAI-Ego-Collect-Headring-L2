"""One-shot bounded read-only probe, with no collector imports or token output."""
import json, math, os, re, sys, time, subprocess, urllib.request, urllib.error, urllib.parse
from pathlib import Path

def read(path, limit=8388608):
    try:
        p=Path(path)
        if p.is_symlink() or p.stat().st_size>limit: return None
        return json.loads(p.read_text())
    except (OSError, ValueError): return None

def api(base, endpoint, library=False):
    headers={}
    try:
        p=base/'runtime/dashboard_token'
        if not p.is_symlink() and p.stat().st_size<=4096: headers['X-Access-Token']=p.read_text().strip()
    except OSError: pass
    def get(h):
        req=urllib.request.Request('http://127.0.0.1:8765/api/'+endpoint,headers=h)
        with urllib.request.urlopen(req,timeout=3) as response: data=response.read(1048577)
        if len(data)>1048576: raise ValueError('too_large')
        return json.loads(data)
    try: return get(headers),None
    except urllib.error.HTTPError as e:
        if e.code not in (401,403) or not library: return None,'http_%d'%e.code
        try:
            p=base/'runtime/reference_library/access_code'
            if p.is_symlink() or p.stat().st_size>64: return None,'auth_unavailable'
            code=p.read_text().strip()
            if not re.fullmatch('[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{12}',code): return None,'auth_unavailable'
            return get({**headers,'X-Reference-Key':code}),None
        except urllib.error.HTTPError as second: return None,'auth_denied' if second.code in (401,403,429) else 'http_%d'%second.code
        except (OSError,ValueError): return None,'auth_unavailable'
    except (OSError,ValueError): return None,'unreachable_or_invalid'

def request(base,endpoint): return api(base,endpoint)[0]

def reference(fetch, max_pages=30, deadline_seconds=16):
    """Use controller completed flags; visible all/list scope only; pending unavailable."""
    start=time.monotonic(); revision=None; pages=0; reasons=[]; lists=[]; images={}; list_pages=0
    def page(endpoint):
        nonlocal pages,revision
        if pages>=max_pages or time.monotonic()-start>=deadline_seconds:
            if 'page_or_time_limit' not in reasons: reasons.append('page_or_time_limit')
            return None
        value,error=fetch(endpoint); pages+=1
        if error: reasons.append(error); return None
        if not isinstance(value,dict) or 'revision' not in value: reasons.append('invalid_response'); return None
        if revision is None: revision=value['revision']
        elif revision!=value['revision']: reasons.append('revision_changed'); return None
        return value
    offset=0
    while True:
        v=page('reference-library?offset=%d&limit=50'%offset)
        if v is None: break
        rows=v.get('lists')
        if 'next_offset' not in v: reasons.append('invalid_pagination'); break
        if not isinstance(rows,list): reasons.append('invalid_response'); break
        lists.extend(row['id'] for row in rows if isinstance(row,dict) and row.get('deleted') is False and isinstance(row.get('id'),str) and re.fullmatch(r'(?:all|pool|list_[a-f0-9]{32})',row['id']))
        nxt=v.get('next_offset')
        if nxt is None: break
        if type(nxt)!=int or nxt<=offset: reasons.append('invalid_pagination'); break
        offset=nxt
    if not reasons:
        # all is valid even when the controller omits it in single-list mode.
        targets=['all'] if 'all' in lists else [x for x in lists if x!='pool']
        if not targets: reasons.append('no_visible_lists')
        for target in targets:
            offset=0
            while True:
                v=page('reference-list?'+urllib.parse.urlencode({'id':target,'offset':offset,'limit':50}))
                if v is None: break
                rows=v.get('items')
                if 'next_offset' not in v: reasons.append('invalid_pagination'); break
                if not isinstance(rows,list): reasons.append('invalid_response'); break
                list_pages+=1
                for row in rows:
                    if not isinstance(row,dict) or row.get('deleted') is not False: continue
                    ident=row.get('id'); complete=row.get('completed')
                    if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ident) or type(complete)!=bool:
                        reasons.append('invalid_item'); continue
                    old=images.get(ident)
                    if old:
                        old['completed']=old['completed'] or complete
                        if target not in old['lists']: old['lists'].append(target)
                    else:
                        images[ident]={'id':ident,'name':str(row.get('name') or '')[:240],'origin':str(row.get('origin') or '')[:80],'completed':complete,'lists':[target]}
                nxt=v.get('next_offset')
                if nxt is None: break
                if type(nxt)!=int or nxt<=offset: reasons.append('invalid_pagination'); break
                offset=nxt
            if reasons: break
    available=list_pages>0
    return {'source':'controller_reference_library','sampled_at':time.time(),'scope':'visible_all' if 'all' in lists else 'visible_lists_only','partial':True,'reason':','.join(dict.fromkeys(reasons)) if reasons else ('visible_controller_cache_excludes_hidden_or_importing' if 'all' in lists else 'single_list_visible_coverage_only'),'pages':pages,'lists_seen':len(lists),'total_unique':len(images) if available else None,'completed_unique':sum(x['completed'] for x in images.values()) if available else None,'remaining_unique':sum(not x['completed'] for x in images.values()) if available else None,'pending_unique':None,'revision':revision,'items':list(images.values())}

def saved(row):
    return bool(row.get('retained') and row.get('has_data') and row.get('status') not in ('deleted','discarded','cancelled','unconfirmed') and not row.get('read_error') and not row.get('source_missing'))

def unescape(text): return re.sub(r'\\([0-7]{3})',lambda m:chr(int(m.group(1),8)),text)
def volumes_from_mountinfo(lines,uuids,stat_fn=os.stat,statvfs_fn=os.statvfs):
    volumes={}
    for line in lines:
        try:
            left,right=line.split(' - ',1); a=left.split(); b=right.split(); dev=a[2]; mount=unescape(a[4]); fs=b[0]; source=unescape(b[1])
            if not (mount=='/' or any(mount.startswith(p) for p in ('/media/','/mnt/','/run/media/','/run/collector-usb-task-import/'))): continue
            if fs in ('autofs','tmpfs','devtmpfs','overlay','squashfs') or not re.fullmatch(r'\d+:\d+',dev): continue
            major,minor=map(int,dev.split(':'))
            st=stat_fn(mount)
            if (os.major(st.st_dev),os.minor(st.st_dev))!=(major,minor): continue
            v=statvfs_fn(mount)
            if dev in volumes:
                volumes[dev]['aliases'].append(mount); continue
            volumes[dev]={'mount':mount,'aliases':[mount],'source':source,'majmin':dev,'uuid':uuids.get(dev),'fstype':fs,'ro':'ro' in a[5].split(','),'total':v.f_blocks*v.f_frsize,'free':v.f_bfree*v.f_frsize,'available':v.f_bavail*v.f_frsize,'used':(v.f_blocks-v.f_bfree)*v.f_frsize}
        except (OSError,ValueError,IndexError): continue
    return list(volumes.values())

def read_reason(path,limit=8388608):
    try:
        p=Path(path)
        if p.is_symlink(): return None,'unsafepath'
        if p.stat().st_size>limit: return None,'oversized'
        value=json.loads(p.read_text())
        return value,None
    except FileNotFoundError: return None,'absent'
    except PermissionError: return None,'permission'
    except (ValueError,UnicodeError): return None,'invalidjson'
    except OSError: return None,'unreadable'

def profile_details(code,selection,volumes):
    legacy=selection.get('legacy_mode'); active=selection.get('active_mode'); result={}
    if legacy not in ('emg','eeg') or active not in ('emg','eeg'):
        return {m:{'root_status':'invalid_selection'} for m in ('emg','eeg')}
    for mode in ('emg','eeg'):
        base=code if mode==legacy else code.parent/'profiles'/mode/'orangepi'
        cat,error=read_reason(base/'runtime/reference_library/catalog.json')
        ledger,ledger_error=read_reason(base/'runtime/daily_collection/records.json')
        row={'root_status':'resolved','base_exists':base.is_dir(),'active':mode==active,'legacy':mode==legacy,'catalog':{'status':error or 'readable','revision':cat.get('revision') if isinstance(cat,dict) else None,'count':len(cat.get('images',{})) if isinstance(cat,dict) and isinstance(cat.get('images'),dict) else None},'ledger':{'status':ledger_error or 'readable'},'storage':{'status':'unknown'}}
        try:
            import yaml
            path=base/'configs/storage.yaml'
            if path.is_symlink() or path.stat().st_size>262144: raise ValueError()
            cfg=yaml.safe_load(path.read_text())
            mount=cfg.get('mount_point'); directory=cfg.get('directory'); device=cfg.get('device_id'); shift=cfg.get('shift'); profile=cfg.get('capture_profile')
            parts=[directory,device]+(['profiles',profile] if profile else [])+['data',shift]
            if not isinstance(mount,str) or not mount.startswith('/') or '..' in Path(mount).parts or '\x00' in mount or any(not isinstance(x,str) or not x or x in ('.','..') or '/' in x for x in parts): raise ValueError()
            root=str(Path(mount).joinpath(*parts)); actual=next((v for v in volumes if mount in v['aliases']),None)
            uuid=cfg.get('expected_uuid') or cfg.get('volume_uuid')
            root_volume=next((v for v in volumes if '/' in v.get('aliases',[])),None)
            status=('not_mounted' if actual is None else 'root_fallback' if root_volume and actual['majmin']==root_volume['majmin'] else 'wrong_uuid' if uuid and actual['uuid']!=uuid else 'readonly' if actual['ro'] else 'mounted')
            row['storage']={'status':status,'mount':mount,'expected_uuid':uuid,'actual_uuid':actual.get('uuid') if actual else None,'majmin':actual.get('majmin') if actual else None,'data_root':root,'fs_health':'not_checked'}
        except FileNotFoundError: row['storage']={'status':'absent'}
        except PermissionError: row['storage']={'status':'permission'}
        except (ValueError,TypeError,AttributeError,OSError): row['storage']={'status':'invalid_or_unsafe'}
        result[mode]=row
    return result

def catalog_membership(ref,catalog):
    if not isinstance(catalog,dict) or catalog.get('revision')!=ref.get('revision'):
        ref['membership_status']='revision_mismatch_or_unavailable'; return {}
    image_map={x['id']:x for x in ref.get('items',[])}; images=catalog.get('images') or {}; paths={}; lists=[]
    for ident,item in images.items():
        if ident not in image_map or not isinstance(item,dict): continue
        path=item.get('path')
        if isinstance(path,str) and not path.startswith('/') and all(x not in ('','.','..') for x in path.split('/')):
            key='configs/environments/'+path
            if key in paths: paths[key]=None
            else: paths[key]=ident
    rows=catalog.get('lists') or {}; order=catalog.get('list_order') or []
    if not isinstance(rows,dict) or not isinstance(order,list): ref['membership_status']='invalid_catalog'; return paths
    for ident in order[:500]:
        item=rows.get(ident)
        if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ident) or not isinstance(item,dict) or item.get('deleted') is not False: continue
        members=item.get('items')
        if not isinstance(members,list): continue
        count=0
        for image_id in dict.fromkeys(x for x in members if isinstance(x,str)):
            if image_id in image_map:
                image_map[image_id]['lists'].append(ident); count+=1
        lists.append({'id':ident,'name':str(item.get('name') or ident)[:240],'count_visible':count,'hidden':item.get('hidden') is True})
    ref['lists']=lists;ref['membership_status']='catalog_revision_matched_visible_items';return paths

def offline_reference(catalog):
    if not isinstance(catalog,dict) or not isinstance(catalog.get('images'),dict): return None
    images=[]
    for ident,item in catalog['images'].items():
        if not isinstance(ident,str) or not re.fullmatch(r'image_[a-f0-9]{40}',ident) or not isinstance(item,dict) or item.get('deleted') is not False: continue
        images.append({'id':ident,'name':str(item.get('name') or '')[:240],'origin':str(item.get('origin') or '')[:80],'completed':None,'lists':[]})
        if len(images)>=10000: break
    ref={'source':'inactive_catalog_offline','revision':catalog.get('revision'),'items':images,'partial':True,'reason':'offline_completion_unverified'}
    catalog_membership(ref,catalog); return ref

def history_rows(records, paths, active):
    out=[]
    for (mode,key),row in records.items():
        if len(out)>=5000: break
        ident=row.get('id') or key
        if not isinstance(ident,str) or len(ident)>140: continue
        refid=None
        if mode==active:
            asset=row.get('reference_asset_id')
            if isinstance(asset,str) and re.fullmatch(r'image_[a-f0-9]{40}',asset): refid=asset
            if refid is None: refid=paths.get(row.get('reference_path'))
        out.append({'id':ident,'mode':mode,'day':row.get('day') if isinstance(row.get('day'),str) else None,'duration':row.get('duration') if type(row.get('duration')) in (int,float) and math.isfinite(row['duration']) else None,'status':row.get('status') if isinstance(row.get('status'),str) else None,'saved':saved(row),'qualified':row.get('qualified') if type(row.get('qualified')) is bool else None,'qc_done':row.get('qc_done') if type(row.get('qc_done')) is bool else None,'asset_id':refid,'association':'exact_catalog_project_relative_path' if refid and mode==active else None,'source':'daily_collection_cache'})
    return out

def block_inventory(disks,volumes):
    result=[]
    def walk(items,parent_usb=False):
        for item in items:
            usb=parent_usb or item.get('tran')=='usb'
            dev=item.get('maj:min');vol=next((v for v in volumes if v.get('majmin')==dev),None)
            result.append({'name':str(item.get('name') or '')[:80],'type':item.get('type'),'parent':item.get('pkname'),'majmin':dev,'uuid':item.get('uuid'),'fstype':item.get('fstype'),'size':item.get('size'),'ro':item.get('ro'),'removable':item.get('rm'),'transport':item.get('tran'),'usb':usb,'mounted':bool(vol),'aliases':vol.get('aliases',[]) if vol else []})
            walk(item.get('children') or [],usb)
    walk(disks.get('blockdevices',[])); return result

def main():
    expected=sys.argv[1]
    try: identity=Path('/opt/egorecord/DEVICE_ID').read_text().strip()
    except OSError: identity=None
    if identity!=expected:
        print(json.dumps({'identity':identity,'identity_mismatch':True})); return
    roots=[Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi'),Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main/orangepi')]
    service=subprocess.run(['systemctl','show','embodied-pi-collect','-p','ExecStart','--value'],capture_output=True,text=True,timeout=4).stdout
    matches=[p for p in roots if str(p/'scripts/run.sh') in service]
    if len(matches)!=1:
        print(json.dumps({'identity':identity,'identity_mismatch':False,'error':'collector_root_unresolved'})); return
    code=matches[0]
    profiles,profile_error=read_reason(code/'runtime/collection_profile.json',4096)
    profiles=profiles if isinstance(profiles,dict) else {}
    legacy=profiles.get('legacy_mode'); active=profiles.get('active_mode')
    if legacy not in ('emg','eeg') or active not in ('emg','eeg') or profiles.get('version')!=1 or type(profiles.get('generation'))!=int or profiles.get('generation',0)<1:
        print(json.dumps({'identity':identity,'identity_mismatch':False,'error':'profile_selection_'+str(profile_error or 'invalid')})); return
    base=code if active==legacy else code.parent/'profiles'/active/'orangepi'
    gate=request(base,'display-state') or {}; full=request(base,'status?signals=&review=0&cameras=0&display=fleet-maintenance') or {}
    worker=gate.get('worker') or {}; daily=full.get('daily_collection') or {}; release=read(base/'runtime/fleet-release.json') or {}
    legacy=profiles.get('legacy_mode')
    records={}; coverage=[]
    for mode in ('emg','eeg'):
        if legacy not in ('emg','eeg'):
            coverage.append({'mode':mode,'available':False,'reason':'legacy_mode_unknown'}); continue
        p=code if mode==legacy else code.parent/'profiles'/mode/'orangepi'
        data=read(p/'runtime/daily_collection/records.json'); rows=data.get('records') if isinstance(data,dict) else None
        if isinstance(rows,dict):
            for key,row in rows.items():
                if isinstance(row,dict): records[(mode,str(row.get('id') or key))]=row
            coverage.append({'mode':mode,'available':True,'updated_at':data.get('updated_at'),'rows':len(rows)})
        else: coverage.append({'mode':mode,'available':False,'reason':'missing_or_unreadable'})
    sums=None; rows=list(records.values()); saved_rows=[r for r in rows if saved(r)]
    if any(x['available'] for x in coverage):
        known=[r['duration'] for r in saved_rows if type(r.get('duration')) in (int,float) and math.isfinite(r['duration']) and 0<=r['duration']<1e9]
        sums={'saved':len(saved_rows),'qualified':sum(r.get('qualified') is True for r in rows),'pending_qc':sum(r.get('qc_done') is False for r in saved_rows),'unknown_qc':sum(r.get('qc_done') is None for r in saved_rows),'saved_seconds_known':sum(known),'saved_seconds_missing':len(saved_rows)-len(known)}
    try:
        disks=json.loads(subprocess.run(['lsblk','-J','-b','-o','NAME,TYPE,PKNAME,MAJ:MIN,UUID,FSTYPE,SIZE,RO,RM,TRAN,MOUNTPOINT'],capture_output=True,text=True,timeout=4).stdout)
        def walk(items):
            for item in items:
                yield item
                yield from walk(item.get('children') or [])
        uuids={x.get('maj:min'):x.get('uuid') for x in walk(disks.get('blockdevices',[]))}
    except Exception: uuids={}; disks={}
    try: volumes=volumes_from_mountinfo(Path('/proc/self/mountinfo').read_text().splitlines(),uuids)
    except OSError: volumes=[]
    ref=reference(lambda endpoint:api(base,endpoint,library=True))
    cat=read(base/'runtime/reference_library/catalog.json',16777216)
    paths=catalog_membership(ref,cat)
    history=history_rows(records,paths,active)
    by_asset={}
    for row in history:
        if row['asset_id']: by_asset.setdefault(row['asset_id'],[]).append(row)
    for item in ref.get('items',[]): item['history']=by_asset.get(item['id'],[])
    blocks=block_inventory(disks,volumes)
    current,current_error=read_reason(code/'runtime/collection_profile.json',4096)
    if current_error or current!=profiles:
        print(json.dumps({'identity':identity,'identity_mismatch':False,'error':'context_changed'})); return
    profile_rows=profile_details(code,profiles,volumes)
    for mode in ('emg','eeg'):
        storage=profile_rows[mode].get('storage',{})
        if storage.get('status')=='not_mounted' and storage.get('expected_uuid') and blocks:
            storage['status']='attached_unmounted' if any(b.get('uuid')==storage['expected_uuid'] for b in blocks) else 'not_connected_or_uuid_unreadable'
        if mode!=active:
            inactive=code if mode==legacy else code.parent/'profiles'/mode/'orangepi'
            profile_rows[mode]['reference']=offline_reference(read(inactive/'runtime/reference_library/catalog.json',16777216))
    result={'schema':2,'profiles':profile_rows,'profile_context':{'active_mode':active,'legacy_mode':legacy,'generation':profiles.get('generation'),'source':'systemd_execstart_and_selection'},'identity':identity,'identity_mismatch':False,'server_time':time.time(),'timezone':time.tzname,'app_online':bool(gate or full),'state':gate.get('state'),'busy':gate.get('busy'),'session':Path(gate['session']).name if isinstance(gate.get('session'),str) else None,'worker_recording':worker.get('recording_active'),'mode':gate.get('collection_mode') or full.get('collection_mode'),'release':{k:release.get(k) for k in ('commit','version')},'today':{k:daily.get(k) for k in ('day','qualified_count','pass_count','warn_count','total_seconds','ready')},'history_cache':sums,'history_coverage':coverage,'history':{'items':history,'coverage':'daily_collection_records_only','truncated':len(records)>5000},'volumes':volumes,'blocks':blocks,'reference':ref,'raw_bytes':None,'backup':'unknown','cloud':'unknown'}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
if __name__=='__main__': main()
