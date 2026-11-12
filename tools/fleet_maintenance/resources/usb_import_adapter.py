"""Fixed USB scanner/preview/commit adapter. ZIP helpers prepended by caller."""
import base64,json,os,re,sys,time,urllib.request,urllib.error,hashlib,stat
from pathlib import Path

def encoded(v):return (json.dumps(v,ensure_ascii=False,allow_nan=False,separators=(',',':'),sort_keys=True)+'\n').encode()
def bounded(p,limit=16777216):
    p=Path(p)
    if p.is_symlink() or p.stat().st_size>limit:raise ValueError('unsafe_or_large_metadata')
    return json.loads(p.read_text())
def snapshot(project,base=None,code=None):
    paths=[]
    fixed=[Path('/opt/egorecord/DEVICE_ID'),Path('/opt/egorecord/HARDWARE_MODEL'),Path('/opt/egorecord/camd/camd.conf')]
    if base is not None:
        fixed.extend(base/'runtime'/name for name in ('wrist_calibration.json','emg_calibration.json','device_bindings.json','dashboard_token'))
        fixed.extend(base/'configs'/name for name in ('devices.yaml','storage.yaml'))
        fixed.append(base/'runtime/reference_library/access_code')
    if code is not None:fixed.append(code/'runtime/collection_profile.json')
    for item in fixed:
        try:
            info=item.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size>8388608:raise ValueError('unsafe_protected_file')
            paths.append((str(item),hashlib.sha256(item.read_bytes()).hexdigest()))
        except FileNotFoundError:paths.append((str(item),'absent'))
    for root,pattern in ((project/'configs/environments','used.yaml'),(Path('/opt/egorecord/calibrations'),'*')):
        if root.is_symlink():raise ValueError('unsafe_protected_root')
        if root.exists():
            for p in root.rglob(pattern):
                if p.is_symlink():raise ValueError('unsafe_protected_path')
                if p.is_file():
                    if p.stat().st_size>8388608 or len(paths)>=10000:raise ValueError('protected_limit')
                    paths.append((str(p),hashlib.sha256(p.read_bytes()).hexdigest()))
    return hashlib.sha256(encoded(sorted(paths))).hexdigest()
def mount_roots():
    result=[]; lines=Path('/proc/self/mountinfo').read_text().splitlines();writable=set()
    for line in lines:
        try:
            a=line.split(' - ',1)[0].split()
            if 'rw' in a[5].split(','):writable.add(a[2])
        except IndexError:continue
    for line in lines:
        try:
            left,right=line.split(' - ',1);a=left.split();fs=right.split()[0];mount=re.sub(r'\\([0-7]{3})',lambda m:chr(int(m.group(1),8)),a[4]);dev=a[2]
            if fs in ('autofs','tmpfs','devtmpfs','overlay') or 'ro' not in a[5].split(',') or dev in writable or not mount.startswith(('/run/collector-usb-task-import/','/media/','/mnt/','/run/media/')):continue
            st=os.stat(mount)
            if '%s:%s'%(os.major(st.st_dev),os.minor(st.st_dev))!=dev:continue
            result.append(Path(mount))
        except (OSError,ValueError,IndexError):continue
    return result

def source_file(entry):
    rel=entry.get('relative_path'); ident=entry.get('id')
    if not isinstance(rel,str) or '\x00' in rel or rel.startswith('/') or any(x in ('','.','..') for x in rel.split('/')) or not re.fullmatch(r'[a-f0-9]{32}',ident or ''):raise ValueError('invalid_scan_entry')
    matches=[]
    for root in mount_roots():
        p=root.joinpath(*rel.split('/'))
        try:
            current=root
            for part in rel.split('/'):
                current=current/part
                if current.is_symlink():raise ValueError('symlink')
            s=p.lstat()
            if not stat.S_ISREG(s.st_mode):continue
            identity=[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]
            token=hashlib.sha256(encoded([str(p),identity])).hexdigest()[:32]
            if token==ident:matches.append((p,identity))
        except (OSError,ValueError):continue
    if len(matches)!=1:raise ValueError('usb_unmounted_or_identity_changed')
    return matches[0]

def catalog_diff(cat,previews):
    images=[x for x in (cat.get('images') or {}).values() if isinstance(x,dict)]
    known={x.get('sha256') for x in images if x.get('deleted') is False}
    deleted={x.get('sha256') for x in images if x.get('deleted') is True}
    existing_names={x.get('original_filename'):x.get('sha256') for x in images}
    hashes=[i['sha256'] for z in previews for i in z['images']];local_names={};conflicts=0
    for z in previews:
        for image in z['images']:
            previous=local_names.get(image['name'])
            if previous and previous!=image['sha256']:conflicts+=1
            if image['name'] in existing_names and existing_names[image['name']]!=image['sha256']:conflicts+=1
            local_names[image['name']]=image['sha256']
    return {'new_unique':len(set(hashes)-known),'reused_unique':len(set(hashes)&known),'deleted_conflicts':len(set(hashes)&deleted),'name_conflicts':conflicts}

def main():
    try:
        p=json.load(sys.stdin);device=p['device'];op=p['op']
        if not re.fullmatch(r'cp_\d{4}',device) or Path('/opt/egorecord/DEVICE_ID').read_text().strip()!=device:raise ValueError('identity')
        code=Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi')
        import subprocess
        service=subprocess.run(['systemctl','show','embodied-pi-collect','-p','ExecStart','--value'],capture_output=True,text=True,timeout=4).stdout
        if str(code/'scripts/run.sh') not in service:raise ValueError('code_root')
        selection=bounded(code/'runtime/collection_profile.json',4096);mode=selection.get('active_mode');legacy=selection.get('legacy_mode');generation=selection.get('generation')
        if mode not in ('emg','eeg') or legacy not in ('emg','eeg') or type(generation)!=int or generation<1:raise ValueError('profile')
        context=mode+':'+str(generation);base=code if mode==legacy else code.parent/'profiles'/mode/'orangepi';project=base.parent
        token=(base/'runtime/dashboard_token').read_text().strip()
        def request(endpoint,data=None):
            headers={'X-Access-Token':token,'X-Collection-Context':context,'Origin':'http://127.0.0.1:8765'}
            if data is not None:headers['Content-Type']='application/json';data=json.dumps(data,separators=(',',':')).encode()
            req=urllib.request.Request('http://127.0.0.1:8765/api/'+endpoint,data=data,headers=headers)
            with urllib.request.urlopen(req,timeout=40) as r:
                raw=r.read(2097153)
            if len(raw)>2097152:raise ValueError('api_response_too_large')
            return json.loads(raw)
        def gate():
            g=request('display-state');privacy=g.get('privacy') or {};worker=g.get('worker') or {}
            if g.get('collection_context')!=context or g.get('collection_mode')!=mode or g.get('state') not in ('device_confirm','idle','failed_wait','exhausted') or g.get('busy') is not False or g.get('session') is not None or worker.get('recording_active') is True or privacy.get('required') is not False or privacy.get('phase')!='idle' or privacy.get('stop_confirmed') is not True:raise ValueError('busy_privacy_or_context')
            if bounded(code/'runtime/collection_profile.json',4096)!=selection:raise ValueError('context_changed')
            return g
        gate()
        if op=='scan':
            result=request('phone/usb-task-import/scan',{'scan':True})
            if bounded(code/'runtime/collection_profile.json',4096)!=selection:raise ValueError('context_changed')
            print(json.dumps({'status':'ok','mode':mode,'context':context,'scan':result},ensure_ascii=False));return
        scan=p['scan']; entries=p['entries']
        if p['mode']!=mode or p['context']!=context or not re.fullmatch(r'[a-f0-9]{32}',scan) or not isinstance(entries,list) or not 1<=len(entries)<=50:raise ValueError('invalid_plan')
        cat=bounded(base/'runtime/reference_library/catalog.json')
        if type(p['revision'])!=int or cat.get('revision')!=p['revision']:raise ValueError('catalog_changed')
        previews=[]; seen=set();total=0
        for entry in entries:
            if entry['id'] in seen:raise ValueError('duplicate_zip')
            seen.add(entry['id']);path,identity=source_file(entry)
            fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            with os.fdopen(fd,'rb') as stream:
                before=os.fstat(stream.fileno());result=preview_file(stream);after=os.fstat(stream.fileno())
            if [before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns]!=identity or [after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns]!=identity:raise ValueError('zip_changed')
            total+=result['size'];previews.append({'id':entry['id'],**result})
        if total>4*1024**3 or sum(len(x['images']) for x in previews)>5000:raise ValueError('batch_limit')
        library=project/'configs/environments';runtime=base/'runtime/usb_task_import'
        if library.is_symlink() or runtime.is_symlink() or not library.is_dir() or not runtime.is_dir():raise ValueError('unsafe_storage')
        libst=os.stat(library);runst=os.stat(runtime);volume='%s:%s'%(os.major(libst.st_dev),os.minor(libst.st_dev));runvolume='%s:%s'%(os.major(runst.st_dev),os.minor(runst.st_dev))
        libv=os.statvfs(library);runv=os.statvfs(runtime);available=libv.f_bavail*libv.f_frsize;runfree=runv.f_bavail*runv.f_frsize
        expanded=sum(x['expanded_bytes'] for x in previews);reserve=32*1024*1024+2*(base/'runtime/reference_library/catalog.json').stat().st_size
        estimate=expanded*3+reserve
        if (volume==runvolume and available<estimate) or (volume!=runvolume and (available<expanded+reserve or runfree<expanded*2+reserve)):raise ValueError('insufficient_space')
        hashes=[i['sha256'] for z in previews for i in z['images']]
        diff=catalog_diff(cat,previews)
        digest=snapshot(project,base,code); info={'status':'preview','mode':mode,'context':context,'catalog_revision':cat['revision'],'sources':previews,'existing_lists':len([x for x in (cat.get('lists') or {}).values() if isinstance(x,dict) and not x.get('deleted')]),**diff,'available_bytes':available,'estimated_bytes':estimate,'volume_majmin':volume,'staging_majmin':runvolume,'staging_available_bytes':runfree,'protected_sha256':digest}
        if op=='preview':print(json.dumps(info,ensure_ascii=False));return
        expected=p.get('expected') or {}
        if info['deleted_conflicts']:raise ValueError('deleted_asset_conflict')
        if op!='apply' or {k:v for k,v in expected.items() if k not in ('available_bytes','staging_available_bytes')}!={k:v for k,v in info.items() if k not in ('available_bytes','staging_available_bytes')}:raise ValueError('preview_drift')
        old_status=request('phone/usb-task-import/status');stamp=old_status.get('updated_at');g=gate()
        action={'action':'usb_task_import','value':{'scan_id':scan,'zip_ids':[x['id'] for x in entries],'catalog_revision':cat['revision'],'remove_list_ids':[]},'collection_context':context,'revision':g['revision']}
        # A lost response is ambiguous; do not retry this POST.
        started=False
        try:
            started=True;request('action',action)
            deadline=time.monotonic()+1200;changed=False
            while time.monotonic()<deadline:
                time.sleep(2);status=request('phone/usb-task-import/status');state=status.get('state');changed=changed or status.get('updated_at')!=stamp
                if changed and state in ('building','ready','committing'):
                    print(json.dumps({'event':'%s:%s:%s'%(state,int(status.get('done') or 0),int(status.get('total') or 0))}),flush=True)
                if changed and state in ('failed','needs_recovery'):print(json.dumps({'status':'partial_failed','reason':'controller_'+state}));return
                if changed and state=='complete':
                    print(json.dumps({'event':'verifying'}),flush=True)
                    new=bounded(base/'runtime/reference_library/catalog.json'); old_images=cat.get('images') or {};new_images=new.get('images') or {};old_lists=cat.get('lists') or {};new_lists=new.get('lists') or {}
                    preservation=all(new_images.get(k)==v for k,v in old_images.items()) and all(new_lists.get(k)==v for k,v in old_lists.items())
                    from collections import Counter
                    added=set(new_lists)-set(old_lists)
                    expected_sets=Counter(frozenset(i['sha256'] for i in item['images']) for item in previews)
                    observed_sets=Counter(frozenset((new_images.get(i) or {}).get('sha256') for i in (new_lists[k].get('items') or [])) for k in added)
                    if len(added)!=len(previews) or expected_sets!=observed_sets:raise ValueError('list_membership_mismatch')
                    contents={v.get('sha256') for v in new_images.values() if isinstance(v,dict)}
                    for target_sha in set(hashes):
                        candidates=[v for v in new_images.values() if isinstance(v,dict) and v.get('sha256')==target_sha and v.get('deleted') is False]
                        verified=False
                        for candidate in candidates:
                            rel=candidate.get('path')
                            if not isinstance(rel,str) or rel.startswith('/') or any(x in ('','.','..') for x in rel.split('/')):continue
                            path=project/'configs/environments';unsafe=False
                            for part in rel.split('/'):
                                path=path/part
                                if path.is_symlink():unsafe=True;break
                            if unsafe:continue
                            try:
                                fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0));st=os.fstat(fd)
                                with os.fdopen(fd,'rb') as asset:
                                    if not stat.S_ISREG(st.st_mode) or st.st_size>MAX_IMAGE:continue
                                    actual=hashlib.sha256(asset.read(MAX_IMAGE+1)).hexdigest()
                                if actual==target_sha:verified=True;break
                            except OSError:continue
                        if not verified:raise ValueError('asset_hash_mismatch')
                    if new.get('revision')!=cat['revision']+1 or not preservation or not set(hashes).issubset(contents) or snapshot(project,base,code)!=digest or bounded(code/'runtime/collection_profile.json',4096)!=selection:
                        print(json.dumps({'status':'partial_failed','reason':'readback_or_protected_mismatch'}));return
                    print(json.dumps({'status':'succeeded','revision':new['revision'],'verified_unique':len(set(hashes)),'old_lists_preserved':True,'protected_unchanged':True}));return
            print(json.dumps({'status':'interrupted_reconcile','reason':'controller_timeout'}));return
        except Exception:
            print(json.dumps({'status':'interrupted_reconcile' if started else 'failed','reason':'controller_response_unknown'}));return
    except Exception as exc:
        print(json.dumps({'status':'failed','reason':str(exc) if str(exc) in ('identity','code_root','profile','context_changed','invalid_plan','catalog_changed','invalid_scan_entry','usb_unmounted_or_identity_changed','duplicate_zip','zip_changed','batch_limit','insufficient_space','preview_drift','busy_privacy_or_context','zip_size','unsafe_path','unsafe_member','expansion_limit','duplicate_path','duplicate_basename','member_size','pixel_limit','image_or_scene_count') else 'probe_failed'}))
if __name__=='__main__':main()
