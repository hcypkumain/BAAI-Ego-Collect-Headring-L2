"""Audited retrospective EMG role swap. Signal payloads are never rewritten."""
import copy,datetime,hashlib,json,os,shutil,time,uuid
from pathlib import Path
import yaml

TZ=datetime.timezone(datetime.timedelta(hours=8))
SLOTS=('emg_left','emg_right')
ROOT_METADATA=('meta.json','meta.yaml','device_selection.json','capture_summary.json',
    'native_capture_summary.json','status.json','qc_raw_report.json','qc_report.json','qc_policy.json','qc_timing.json','storage_sync.json','qc.html')
FROZEN=('emg_calibration','emg_calibration_status')
LABELS={'左臂肌电':'右臂肌电','右臂肌电':'左臂肌电','左臂惯性':'右臂惯性','右臂惯性':'左臂惯性'}

def sha(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()

def atomic(path,raw):
    path=Path(path);temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temporary.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)
    sync_directory(path.parent)

def sync_directory(path):
    fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)

def json_bytes(value):return (json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8')
def write_json(path,value):atomic(path,json_bytes(value))

def flip_text(value):
    if '__EMG_SWAP_SENTINEL__' in value:raise ValueError('reserved swap sentinel in original metadata')
    result=value.replace('emg_left','__EMG_SWAP_SENTINEL__').replace('emg_right','emg_left').replace('__EMG_SWAP_SENTINEL__','emg_right')
    if result in LABELS:return LABELS[result]
    for label,new in LABELS.items():
        if result.startswith(label+'：'):return new+result[len(label):]
    return result

def flip(value):
    if isinstance(value,dict):
        return {flip_text(key):copy.deepcopy(item) if key in FROZEN or key.startswith('reference_') else flip(item) for key,item in value.items()}
    if isinstance(value,list):return [flip(item) for item in value]
    return flip_text(value) if isinstance(value,str) else value

def signal_manifest(root):
    result={}
    for slot in SLOTS:
        directory=root/slot
        if directory.is_symlink() or not directory.is_dir():raise ValueError('Both original EMG streams must be regular directories')
        for path in directory.rglob('*'):
            if path.is_symlink():raise ValueError('Symbolic links are not allowed in an EMG take')
            if path.is_file():result[path.relative_to(root).as_posix()]=dict(sha256=sha(path),bytes=path.stat().st_size)
    return result

def wrist_manifest(root):
    return {path.relative_to(root).as_posix():sha(path) for name in ('cam_left_wrist','cam_right_wrist')
            for path in (root/name).rglob('*') if path.is_file()}

def other_stats(root):
    result={}
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name in SLOTS:continue
        for path in folder.rglob('*'):
            if path.is_file():
                info=path.stat();result[path.relative_to(root).as_posix()]=(info.st_size,info.st_mtime_ns)
    return result

def capture_day(root):
    summary=json.loads((root/'capture_summary.json').read_text(encoding='utf-8'))
    start=summary.get('start_unix_ns')
    if not start and (root/'start_gate.json').is_file():start=json.loads((root/'start_gate.json').read_text())['start_unix_ns']
    if type(start) is not int or start<=0:raise ValueError('No positive capture start timestamp')
    return datetime.datetime.fromtimestamp(start/1e9,TZ).date().isoformat()

def recover_incomplete(backup):
    """Restore an isolated take after process/power loss, without deleting data."""
    backup=Path(backup);journal=json.loads((backup/'journal.json').read_text());root=Path(journal['take']);working=backup/'working-take'
    if journal.get('state') in ('complete','rolled_back'):return
    before=json.loads((backup/'before-signal-manifest.json').read_text())
    if root.exists() and not working.exists():
        marker=root/'emg_role_correction.json'
        if marker.is_file() and json.loads(marker.read_text()).get('state')=='complete':
            journal['state']='complete';write_json(backup/'journal.json',journal);return
        if signal_manifest(root)!=before:raise ValueError('Unfinished correction has an unexpected visible take')
        journal['state']='rolled_back';write_json(backup/'journal.json',journal);return
    if root.exists() or not working.is_dir():raise ValueError('Ambiguous correction recovery paths')
    failed=backup/'recovered-partial';failed.mkdir(exist_ok=True)
    for slot in SLOTS:
        original=backup/slot
        if original.exists():
            if (working/slot).exists():(working/slot).rename(failed/(slot+'-'+uuid.uuid4().hex))
            original.rename(working/slot)
    for path in (backup/'metadata').iterdir():atomic(working/path.name,path.read_bytes())
    for name in ('emg_role_correction.json','emg_role_original_metadata.json'):
        path=working/name
        if path.exists():path.rename(failed/(name+'-'+uuid.uuid4().hex))
    if signal_manifest(working)!=before:raise ValueError('Original payloads were not fully recovered')
    working.rename(root);sync_directory(root.parent)
    journal['state']='rolled_back';write_json(backup/'journal.json',journal)

def annotate_configs(value,brief):
    if isinstance(value,dict):
        if isinstance(value.get('emg'),dict) and set(SLOTS)<=set(value['emg']):
            value['emg_physical_role_correction']=copy.deepcopy(brief)
        for key,item in value.items():
            if key not in FROZEN and key!='emg_physical_role_correction':annotate_configs(item,brief)
    elif isinstance(value,list):
        for item in value:annotate_configs(item,brief)

def correct_session(root,backup_root,date,syncer=None):
    root=Path(root);backup_root=Path(backup_root)
    if root.is_symlink() or not root.is_dir():raise ValueError('Invalid take directory')
    marker=root/'emg_role_correction.json'
    if marker.is_file():
        proof=json.loads(marker.read_text())
        if proof.get('state')=='complete' and proof.get('date')==date and proof.get('operation')=='swap_emg_left_right':
            return dict(session=root.name,already_corrected=True)
        raise ValueError('Existing correction requires audit before proceeding')
    if capture_day(root)!=date:raise ValueError('Take is outside the operator-authorized capture date')
    backup=backup_root/root.name
    if backup.exists():raise ValueError('An existing backup/journal requires inspection before retry')
    backup.mkdir(parents=True)
    if root.stat().st_dev!=backup.stat().st_dev:raise ValueError('Correction staging and takes must share a storage volume')
    before=signal_manifest(root);wrists=wrist_manifest(root);untouched=other_stats(root)
    originals={}
    for name in ROOT_METADATA:
        path=root/name
        if not path.is_file():continue
        raw=path.read_bytes();originals[name]=dict(sha256=hashlib.sha256(raw).hexdigest(),text=raw.decode('utf-8'))
    original_qc=json.loads(originals['capture_summary.json']['text']).get('qc_level')
    original_status=json.loads(originals['capture_summary.json']['text']).get('status')
    correction_id=uuid.uuid4().hex
    brief=dict(schema_version=1,correction_id=correction_id,date=date,timezone='Asia/Shanghai',
        operation='swap_emg_left_right',physical_role_basis='operator_retrospective_declaration',
        operator_statement='Both receivers were removed, then the actual RIGHT was inserted first and LEFT second; all '+date+' EMG sides were reversed.',
        effective_to_recorded_role={'emg_left':'emg_right','emg_right':'emg_left'},
        original_insertion_proof_preserved=True,timestamps_or_signal_values_changed=False,
        accompanying_emg_imu_roles_swapped=True,wrist_camera_roles_changed=False)
    original_evidence=dict(schema_version=1,warning='Original acquisition labels were retrospectively declared reversed; use current canonical streams and emg_role_correction.json.',files=originals)
    metadata_backup=backup/'metadata';metadata_backup.mkdir()
    for name,value in originals.items():atomic(metadata_backup/name,value['text'].encode('utf-8'))
    write_json(backup/'before-signal-manifest.json',before);write_json(backup/'before-wrist-manifest.json',wrists)
    stage=backup/'prepared';stage.mkdir();planned={};warnings=[]
    for old in SLOTS:
        new=flip_text(old);directory=stage/new;shutil.copytree(root/old,directory)
        for path in sorted(directory.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            if not path.is_file():continue
            if path.suffix in ('.log','.json','.txt'):
                raw=path.read_bytes();text=raw.decode('utf-8');updated=flip_text(text)
                # Names inside original scalar log JSON are not a new signal.
                if updated!=text:atomic(path,updated.encode('utf-8'))
            if flip_text(path.name)!=path.name:path.rename(path.with_name(flip_text(path.name)))
    for name,value in originals.items():
        if name=='qc.html':
            text=value['text']
            text=flip_text(text)
            for left,right in (('左臂肌电','右臂肌电'),('左臂惯性','右臂惯性')):
                text=text.replace(left,'__LABEL_SWAP__').replace(right,left).replace('__LABEL_SWAP__',right)
            planned[name]=text.encode('utf-8');continue
        parsed=yaml.safe_load(value['text']) if name.endswith('.yaml') else json.loads(value['text'])
        if name=='meta.yaml' and parsed is None:
            warnings.append('original_empty_meta_yaml_preserved');planned[name]=value['text'].encode('utf-8');continue
        updated=flip(parsed)
        if not isinstance(updated,dict):raise ValueError('Root metadata must be objects: '+name)
        updated['emg_role_correction']=copy.deepcopy(brief);annotate_configs(updated,brief)
        planned[name]=(yaml.safe_dump(updated,allow_unicode=True,sort_keys=False).encode('utf-8') if name.endswith('.yaml') else json_bytes(updated))
    if 'qc_raw_report.json' in planned:
        digest=hashlib.sha256(planned['qc_raw_report.json']).hexdigest()
        for name in ('qc_policy.json','qc_report.json'):
            if name not in planned:continue
            value=json.loads(planned[name])
            if name=='qc_policy.json':value['raw_report_sha256']=digest
            elif isinstance(value.get('pi_qc_policy'),dict):value['pi_qc_policy']['raw_report_sha256']=digest
            planned[name]=json_bytes(value)
    for name,raw in planned.items():atomic(stage/name,raw)
    atomic(stage/'emg_role_original_metadata.json',json_bytes(original_evidence))
    expected={flip_text(name):row for name,row in before.items()}
    staged=signal_manifest(stage)
    for name,record in expected.items():
        if Path(name).suffix in ('.npz','.npy','.csv','.bin'):
            if staged[name]!=record:raise ValueError('A signal payload changed during preparation: '+name)
    journal=dict(schema_version=1,correction_id=correction_id,session=root.name,state='prepared',take=str(root),backup=str(backup))
    write_json(backup/'journal.json',journal)
    original_root=root;working=backup/'working-take'
    root.rename(working);root=working;sync_directory(original_root.parent)
    journal['state']='take_isolated';write_json(backup/'journal.json',journal)
    moved=[];installed=[]
    try:
        for slot in SLOTS:(root/slot).rename(backup/slot);moved.append(slot)
        journal['state']='original_streams_backed_up';write_json(backup/'journal.json',journal)
        for slot in SLOTS:(stage/slot).rename(root/slot);installed.append(slot)
        for name in planned:atomic(root/name,(stage/name).read_bytes())
        atomic(root/'emg_role_original_metadata.json',(stage/'emg_role_original_metadata.json').read_bytes())
        after=signal_manifest(root)
        for name,record in expected.items():
            if Path(name).suffix in ('.npz','.npy','.csv','.bin') and after[name]!=record:
                raise ValueError('Corrected signal differs from its original opposite-side source')
        if wrists!=wrist_manifest(root) or untouched!=other_stats(root):raise ValueError('A non-EMG stream changed')
        summary=json.loads((root/'capture_summary.json').read_text())
        if summary.get('status')!=original_status or summary.get('qc_level')!=original_qc:raise ValueError('Correction changed capture/QC validity')
        proof=dict(brief,state='complete',corrected_at_unix=time.time(),backup=str(backup),
            before_signal_manifest=before,after_signal_manifest=after,wrist_file_hashes=wrists,
            preserved_capture_status=original_status,preserved_qc_level=original_qc,
            qc_findings_reassigned_not_retested=True,warnings=warnings)
        write_json(root/'emg_role_correction.json',proof)
        if syncer:
            durability=syncer(root)
            for name in ('capture_summary.json','meta.yaml'):
                path=root/name
                if not path.exists():continue
                data=yaml.safe_load(path.read_text(encoding='utf-8')) if name.endswith('.yaml') else json.loads(path.read_text())
                if data is None:continue
                data['storage_durability']=durability
                atomic(path,yaml.safe_dump(data,allow_unicode=True,sort_keys=False).encode('utf-8') if name.endswith('.yaml') else json_bytes(data))
        root.rename(original_root);root=original_root;sync_directory(root.parent)
        journal.update(state='complete',completed_at_unix=time.time());write_json(backup/'journal.json',journal)
        return dict(session=original_root.name,corrected=True,payload_files_verified=sum(Path(name).suffix in ('.npz','.npy','.csv','.bin') for name in after),
            wrist_files_unchanged=len(wrists),status=original_status,qc=original_qc,backup=str(backup),warnings=warnings)
    except BaseException:
        # Originals stay available on the same volume; rollback never deletes
        # signal data, including any partially installed corrected copies.
        failed=backup/'failed-install';failed.mkdir(exist_ok=True)
        for slot in installed:
            if (root/slot).exists():(root/slot).rename(failed/slot)
        for slot in moved:
            if (backup/slot).exists():(backup/slot).rename(root/slot)
        for name,value in originals.items():atomic(root/name,value['text'].encode('utf-8'))
        for name in ('emg_role_correction.json','emg_role_original_metadata.json'):
            path=root/name
            if path.exists():path.rename(failed/name)
        if root!=original_root:root.rename(original_root);sync_directory(original_root.parent)
        journal['state']='rolled_back';write_json(backup/'journal.json',journal)
        raise
