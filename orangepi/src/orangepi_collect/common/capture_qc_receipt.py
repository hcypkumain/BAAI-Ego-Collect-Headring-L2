"""Bind a completed full QC verdict to its frozen capture and saved inputs."""
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from required_device_contract import resolve_selection, qualification_errors, data_warnings

RECEIPT_NAME='capture_qc_receipt.json'
RECEIPT_VERSION=1
MAX_JSON_BYTES=16*1024*1024


def _reject_constant(value):
    raise ValueError('non-finite JSON value: '+value)


def _read_json(path,limit=MAX_JSON_BYTES):
    path=Path(path);info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size>limit:
        raise ValueError('invalid regular evidence file: '+path.name)
    value=json.loads(path.read_text(encoding='utf-8'),parse_constant=_reject_constant)
    if not isinstance(value,dict):raise ValueError('evidence JSON is not an object: '+path.name)
    return value


def _hash_file(path,limit=MAX_JSON_BYTES):
    path=Path(path);info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size>limit:
        raise ValueError('invalid regular evidence file: '+path.name)
    h=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def _signature(path):
    info=Path(path).lstat()
    if stat.S_ISLNK(info.st_mode):raise ValueError('symlink in QC input tree: '+str(path))
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ValueError('non-regular QC input: '+str(path))
    return dict(mode=stat.S_IFMT(info.st_mode),device=info.st_dev,inode=info.st_ino,
        size=info.st_size,mtime_ns=info.st_mtime_ns,ctime_ns=info.st_ctime_ns)


def _contract(path,contract=None):
    if contract is not None:return contract
    selection=_read_json(Path(path)/'device_selection.json')
    result,errors,_legacy=resolve_selection(selection.get('device_config'),selection)
    if errors:raise ValueError('invalid frozen device selection: '+'; '.join(errors))
    return result


def _input_rows(path,contract,require_marker=False):
    """Return stable path/stat evidence for every required stream and gate."""
    root=Path(path);rows=[]
    fixed=('device_selection.json','meta.json','start_gate.json','stop_gate.json',
           'native_capture_summary.json')
    selection=_read_json(root/'device_selection.json')
    legacy='required_device_contract' not in selection
    for name in fixed:
        candidate=root/name
        if not candidate.exists():
            if name in ('meta.json','native_capture_summary.json') or (legacy and name in ('start_gate.json','stop_gate.json')):continue
            raise ValueError('missing frozen QC input: '+name)
        rows.append((name,_signature(candidate)))
    names=sorted(set(contract.get('required_devices',())))
    marker=root/'marker'
    if marker.exists():names.append('marker')
    elif require_marker:raise ValueError('missing marker QC input')
    else:rows.append(('marker',None))
    for name in names:
        directory=root/name
        info=directory.lstat()
        if not stat.S_ISDIR(info.st_mode):raise ValueError('QC input is not a real directory: '+name)
        rows.append((name,_signature(directory)))
        def onerror(error):raise error
        for current,dirs,files in os.walk(str(directory),topdown=True,followlinks=False,onerror=onerror):
            current=Path(current)
            dirs.sort();files.sort()
            for child in dirs:
                candidate=current/child
                rows.append((candidate.relative_to(root).as_posix(),_signature(candidate)))
            for child in files:
                candidate=current/child
                rows.append((candidate.relative_to(root).as_posix(),_signature(candidate)))
    return rows


def input_rows(path,contract=None,require_marker=False):
    """Expose the local stat snapshot used by input_signature for journals."""
    return _input_rows(path,_contract(path,contract),require_marker=require_marker)


def input_signature(path,contract=None,require_marker=False):
    """Compact signature used to bind deferred QC to pre-host source inputs."""
    raw=json.dumps(input_rows(path,contract,require_marker=require_marker),sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _content_hash(path):
    path=Path(path);before=path.lstat()
    if not stat.S_ISREG(before.st_mode):raise ValueError('non-regular portable QC input: '+str(path))
    h=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):h.update(block)
    after=path.lstat()
    if (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)!=(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns):
        raise ValueError('QC input changed while hashing: '+str(path))
    return h.hexdigest()


def portable_input_signature(path,contract=None,require_marker=False):
    """Content-bound signature that remains stable across a copied host session."""
    root=Path(path);contract=_contract(root,contract);rows=[]
    selection=_read_json(root/'device_selection.json');legacy='required_device_contract' not in selection
    fixed=('device_selection.json','meta.json','start_gate.json','stop_gate.json','native_capture_summary.json')
    for name in fixed:
        candidate=root/name
        if not candidate.exists():
            if name in ('meta.json','native_capture_summary.json') or (legacy and name in ('start_gate.json','stop_gate.json')):continue
            raise ValueError('missing frozen QC input: '+name)
        rows.append((name,candidate.stat().st_size,_content_hash(candidate)))
    names=sorted(set(contract.get('required_devices',())))
    marker=root/'marker'
    if marker.exists():names.append('marker')
    elif require_marker:raise ValueError('missing marker QC input')
    else:rows.append(('marker',None))
    for name in names:
        directory=root/name;info=directory.lstat()
        if not stat.S_ISDIR(info.st_mode):raise ValueError('QC input is not a real directory: '+name)
        rows.append((name,'directory'))
        def onerror(error):raise error
        for current,dirs,files in os.walk(str(directory),topdown=True,followlinks=False,onerror=onerror):
            current=Path(current);dirs.sort();files.sort()
            for child in dirs:
                candidate=current/child
                if candidate.is_symlink():raise ValueError('symlink in QC input tree: '+str(candidate))
                rows.append((candidate.relative_to(root).as_posix(),'directory'))
            for child in files:
                candidate=current/child
                if candidate.is_symlink():raise ValueError('symlink in QC input tree: '+str(candidate))
                rows.append((candidate.relative_to(root).as_posix(),candidate.stat().st_size,_content_hash(candidate)))
    raw=json.dumps(rows,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def verify_input_signature(path,expected,contract=None,require_marker=False):
    if not isinstance(expected,str) or len(expected)!=64:return False
    try:return input_signature(path,contract,require_marker=require_marker)==expected
    except (OSError,ValueError,TypeError,KeyError,RecursionError):return False


def verify_portable_input_signature(path,expected,contract=None,require_marker=False):
    if not isinstance(expected,str) or len(expected)!=64:return False
    try:return portable_input_signature(path,contract,require_marker=require_marker)==expected
    except (OSError,ValueError,TypeError,KeyError,RecursionError,OverflowError):return False


def _atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temporary=tempfile.mkstemp(prefix='.'+path.name+'-',dir=str(path.parent))
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as handle:
            json.dump(value,handle,ensure_ascii=False,indent=2,allow_nan=False)
            handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,str(path))
        if os.name!='nt':
            directory=os.open(str(path.parent),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        try:os.unlink(temporary)
        except FileNotFoundError:pass


def write(path,expected_input_signature=None):
    """Issue a receipt only after this real full report passes local evidence."""
    root=Path(path)
    selection=_read_json(root/'device_selection.json')
    summary=_read_json(root/'capture_summary.json')
    report=_read_json(root/'qc_report.json')
    contract,errors,legacy=resolve_selection(selection.get('device_config'),selection)
    if errors:raise ValueError('invalid frozen device contract: '+'; '.join(errors))
    errors=qualification_errors(root,summary,contract,report,legacy=legacy)
    if errors:raise ValueError('full QC qualification failed: '+'; '.join(errors[:12]))
    signed_inputs=input_signature(root,contract,require_marker=True)
    if expected_input_signature is not None and signed_inputs!=expected_input_signature:
        raise ValueError('QC inputs changed between check and receipt')
    required=contract.get('required_devices',[])
    native=root/'native_capture_summary.json'
    warnings=data_warnings(summary,legacy=legacy)
    value=dict(version=RECEIPT_VERSION,scope='full',session_id=root.name,
        legacy_source=legacy,
        contract_version=None if legacy else contract['version'],mode=contract['mode'],required_devices=required,
        selected_devices=contract['selected_devices'],selection_sha256=_hash_file(root/'device_selection.json'),
        summary_sha256=_hash_file(root/'capture_summary.json'),report_sha256=_hash_file(root/'qc_report.json'),
        native_sha256=_hash_file(native) if native.exists() else None,
        warnings=warnings,
        input_signature=signed_inputs,
        portable_input_signature=portable_input_signature(root,contract,require_marker=True),
        created_at_unix=__import__('time').time())
    _atomic_json(root/RECEIPT_NAME,value)
    return value


def verify(path):
    """Verify an existing receipt without issuing or upgrading one."""
    root=Path(path);errors=[]
    try:
        receipt=_read_json(root/RECEIPT_NAME,1024*1024)
        selection=_read_json(root/'device_selection.json')
        summary=_read_json(root/'capture_summary.json')
        report=_read_json(root/'qc_report.json')
        contract,contract_errors,legacy=resolve_selection(selection.get('device_config'),selection)
        if contract_errors:return ['冻结设备合同无效：'+'；'.join(contract_errors)]
        legacy_binding=(receipt.get('legacy_source') is True and receipt.get('contract_version') is None
            if legacy else receipt.get('legacy_source') in (None,False)
            and receipt.get('contract_version')==contract['version'])
        if (receipt.get('version')!=RECEIPT_VERSION or receipt.get('scope')!='full'
                or receipt.get('session_id')!=root.name
                or not legacy_binding
                or receipt.get('mode')!=contract['mode']
                or receipt.get('required_devices')!=contract['required_devices']
                or receipt.get('selected_devices')!=contract['selected_devices']
                or (legacy and receipt.get('warnings')!=data_warnings(summary,legacy=True))):
            errors.append('QC回执与本段冻结设备合同不一致')
        checks=(('selection_sha256',root/'device_selection.json'),
                ('summary_sha256',root/'capture_summary.json'),('report_sha256',root/'qc_report.json'))
        for key,file_path in checks:
            if receipt.get(key)!=_hash_file(file_path):errors.append('QC后文件已变化：'+file_path.name)
        native=root/'native_capture_summary.json'
        expected_native=receipt.get('native_sha256')
        if native.exists():
            if expected_native!=_hash_file(native):errors.append('QC后原始采集摘要已变化')
        elif expected_native is not None:errors.append('QC回执要求的原始采集摘要缺失')
        if receipt.get('input_signature')!=input_signature(root,contract,require_marker=True):
            errors.append('QC后必需设备数据或门限凭据已变化')
        errors.extend(qualification_errors(root,summary,contract,report,legacy=legacy))
    except (OSError,ValueError,TypeError,KeyError,RecursionError,OverflowError) as error:
        return ['完整QC回执缺失或无效：'+str(error)[:240]]
    return list(dict.fromkeys(errors))


def verify_portable(path):
    """Verify a completed receipt after a custody copy changed local inodes."""
    root=Path(path)
    try:
        receipt=_read_json(root/RECEIPT_NAME,1024*1024)
        selection=_read_json(root/'device_selection.json')
        summary=_read_json(root/'capture_summary.json')
        report=_read_json(root/'qc_report.json')
        contract,contract_errors,legacy=resolve_selection(selection.get('device_config'),selection)
        if contract_errors:return ['冻结设备合同无效：'+'；'.join(contract_errors)]
        legacy_binding=(receipt.get('legacy_source') is True and receipt.get('contract_version') is None
            if legacy else receipt.get('legacy_source') in (None,False)
            and receipt.get('contract_version')==contract['version'])
        if (receipt.get('version')!=RECEIPT_VERSION or receipt.get('scope')!='full'
                or receipt.get('session_id')!=root.name
                or not legacy_binding
                or receipt.get('mode')!=contract['mode']
                or receipt.get('required_devices')!=contract['required_devices']
                or receipt.get('selected_devices')!=contract['selected_devices']
                or (legacy and receipt.get('warnings')!=data_warnings(summary,legacy=True))):
            return ['QC回执与本段冻结设备合同不一致']
        for key,file_path in (('selection_sha256',root/'device_selection.json'),
                ('summary_sha256',root/'capture_summary.json'),('report_sha256',root/'qc_report.json')):
            if receipt.get(key)!=_hash_file(file_path):return ['QC回执绑定文件内容不一致：'+file_path.name]
        native=root/'native_capture_summary.json'
        expected=receipt.get('native_sha256')
        if native.exists() and expected!=_hash_file(native):return ['QC回执绑定原始摘要不一致']
        if (receipt.get('portable_input_signature')!=portable_input_signature(root,contract,require_marker=True)):
            return ['QC回执绑定的可移植采集输入不一致']
        return qualification_errors(root,summary,contract,report,legacy=legacy)
    except (OSError,ValueError,TypeError,KeyError,RecursionError,OverflowError) as error:
        return ['可移植完整QC回执缺失或无效：'+str(error)[:240]]
