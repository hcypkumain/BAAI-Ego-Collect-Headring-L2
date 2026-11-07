"""Bind local signal QC to the closed capture, exact inputs and effective rules."""
import ast,hashlib,json,os
from pathlib import Path

SCOPES={'emg':('emg_left','emg_right'),'eeg':('eeg',)}
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=True,allow_nan=False,separators=(',',':')).encode()
def window_key(value):
    return {k:v for k,v in value.items() if k!='file'} if isinstance(value,dict) else value
def marker_digest(path):
    import numpy as np
    h=hashlib.sha256();size=0
    with np.load(path,allow_pickle=False) as arrays:
        for name in sorted(arrays.files):
            a=np.asarray(arrays[name]);h.update(canonical(dict(name=name,dtype=a.dtype.str,shape=list(a.shape))));h.update(a.tobytes(order='C'));size+=a.nbytes
    return dict(format='npz-arrays-v1',bytes=size,sha256=h.hexdigest())
class NoDocs(ast.NodeTransformer):
    def strip(self,node):
        self.generic_visit(node)
        if node.body and isinstance(node.body[0],ast.Expr) and isinstance(node.body[0].value,ast.Constant) and isinstance(node.body[0].value.value,str):node.body=node.body[1:]
        return node
    visit_Module=visit_ClassDef=visit_FunctionDef=strip
def code(path,names=None):
    tree=ast.parse(Path(path).read_text(encoding='utf-8'))
    if names is not None:tree.body=[n for n in tree.body if getattr(n,'name',None) in names]
    return ast.dump(NoDocs().visit(tree),include_attributes=False)
def rules(scope):
    import project_paths as pp,yaml
    root=getattr(pp,'CODE_PROJECT',pp.PROJECT);fw=Path(__file__).parents[1]/'framework';common=Path(__file__).parent
    checks=root/'src/embodied_brain_collect/checkers'
    result=dict(schema=1,scope=scope,checker=yaml.safe_load((root/'configs/checker.yaml').read_text()),
        policy=json.loads((pp.BASE/'configs/qc_policy.json').read_text()),base=code(checks/'base.py'),policy_code=code(fw/'qc_policy.py'))
    if scope=='emg':
        result.update(emg=code(checks/'emg.py'),checks=code(checks/'checks.py'),
            rebuild=code(root/'src/embodied_brain_collect/recorders/emg/timestamp_rebuild.py'),recovery=code(fw/'emg_recoverability.py'),
            preparation=code(fw/'framework_qc.py',{'standardize_emg','check_emg'}))
    else:
        result.update(eeg=code(fw/'eeg_qc.py'),settings=code(common/'eeg_settings.py'),impedance=code(common/'impedance_record.py'))
    return hashlib.sha256(canonical(result)).hexdigest()
def inputs(root,names):
    root=Path(root);paths=[]
    for name in ['native_capture_summary.json','start_gate.json','stop_gate.json','device_selection.json','meta.json']:
        p=root/name
        if not p.is_file() or p.is_symlink():raise ValueError('Missing regular capture evidence: '+name)
        paths.append(p)
    marker=root/'marker'
    if marker.is_dir():paths.extend(p for p in marker.glob('*.npz'))
    for name in names:
        folder=root/name
        if folder.is_symlink() or not folder.is_dir():raise ValueError('Invalid signal directory')
        paths.extend(p for p in folder.iterdir() if p.is_file())
        if not (folder/(name+'.npz')).is_file():raise ValueError('Canonical signal array missing')
    result={}
    for p in sorted(paths):
        if p.is_symlink():raise ValueError('Signal input is a symlink')
        p.resolve().relative_to(root.resolve())
        result[p.relative_to(root).as_posix()]=marker_digest(p) if p.parent.name=='marker' and p.suffix=='.npz' else dict(bytes=p.stat().st_size,sha256=sha(p))
    return result
def write(root,scope):
    from qc_deferred import atomic
    root=Path(root);p=root/(scope+'_qc_report.json');report=json.loads(p.read_text());names=sorted(report.get('streams',{}))
    if scope not in SCOPES or not names or not set(names)<=set(SCOPES[scope]):raise ValueError('Wrong local QC scope')
    selected=json.loads((root/'device_selection.json').read_text())['selected']
    if set(names)!=(set(selected)&set(SCOPES[scope])):raise ValueError('Incomplete selected signal QC')
    if not report.get('window'):raise ValueError('Local QC window unavailable')
    value=dict(version=1,scope=scope,session_id=root.name,native_sha256=sha(root/'native_capture_summary.json'),
        report_sha256=sha(p),rules_sha256=rules(scope),inputs=inputs(root,names),streams=names,window=window_key(report['window']))
    atomic(root/(scope+'_qc_binding.json'),value);return value
def verify(root,scope):
    root=Path(root);binding=root/(scope+'_qc_binding.json');report_path=root/(scope+'_qc_report.json')
    if not binding.is_file():raise ValueError('No data-bound Pi '+scope+' QC receipt')
    if binding.is_symlink() or binding.stat().st_size>4*1024*1024 or report_path.is_symlink() or report_path.stat().st_size>8*1024*1024:raise ValueError('Invalid local QC evidence')
    value=json.loads(binding.read_text());report=json.loads(report_path.read_text())
    if value.get('version')!=1 or value.get('scope')!=scope or value.get('session_id')!=root.name:raise ValueError('Local QC belongs to a different capture')
    if value.get('native_sha256')!=sha(root/'native_capture_summary.json') or value.get('report_sha256')!=sha(report_path):raise ValueError('Local QC result changed')
    if value.get('rules_sha256')!=rules(scope):raise ValueError('Pi signal QC rule fingerprint differs')
    selected=json.loads((root/'device_selection.json').read_text())['selected'];names=sorted(set(selected)&set(SCOPES[scope]))
    if not names or value.get('streams')!=names or sorted(report.get('streams',{}))!=names:raise ValueError('Local QC does not cover selected signals')
    if value.get('inputs')!=inputs(root,names):raise ValueError('Signal inputs changed since Pi QC')
    from embodied_brain_collect.checkers.marker import find_run_window
    if value.get('window')!=window_key(find_run_window(root)) or window_key(report.get('window'))!=value['window']:raise ValueError('Local QC has a different recording window')
    if report.get('level') not in ('INFO','WARN','ERROR'):raise ValueError('Invalid local QC verdict')
    return report,value
def restore_stream(name,value):
    from embodied_brain_collect.checkers.base import StreamReport,Finding,Span
    findings=[]
    for row in value.get('findings',[]):
        if row.get('level') not in ('INFO','WARN','ERROR'):raise ValueError('Invalid local finding')
        row=dict(row);row['spans']=[Span(**x) for x in row.get('spans',[])];findings.append(Finding(**row))
    out=StreamReport(stream=name,files=value.get('files',[]),series=value.get('series',{}),stats=value.get('stats',{}),findings=findings)
    if out.level!=value.get('level'):raise ValueError('Local stream verdict inconsistent')
    return out
