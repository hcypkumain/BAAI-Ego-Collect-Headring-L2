"""Read finalized recordings; never commit, delete or transcode source data."""
import json
from pathlib import Path
import re
from qc_details import summarize_qc

CAMERAS={'cam_left_wrist':'左腕相机','cam_right_wrist':'右腕相机',
         'cam_ros_left':'头环前左','cam_ros_right':'头环前右',
         'cam_ros_bleft':'头环后左','cam_ros_bright':'头环后右'}

def read_json(path):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return {}

def latest_recording(root):
    # Collector-prefixed and historical names do not share a lexical ordering.
    # The durable STOP marker's actual mtime orders both without guessing IDs.
    try:directories=list(Path(root).iterdir())
    except OSError:return None
    candidates=[]
    for path in directories:
        try:
            gate=path/'stop_gate.json'
            if (path.is_symlink() or not path.is_dir() or gate.is_symlink()
                    or not gate.is_file() or not any((path/n/'video_check.json').is_file() for n in CAMERAS)):
                continue
            candidates.append((gate.stat().st_mtime_ns,path.name,path))
        except OSError:continue
    return max(candidates,key=lambda item:(item[0],item[1]))[2] if candidates else None


def video_path(root,session,name):
    if not root or name not in CAMERAS:raise ValueError('不存在的录像')
    root=Path(root).resolve()
    if session!=root.name or not (root/'stop_gate.json').is_file():raise ValueError('录像已变化，请刷新')
    directory=root/name
    if directory.is_symlink() or not (directory/'video_check.json').is_file():raise ValueError('录像正在收尾')
    path=directory/'frames.mp4'
    if path.is_symlink() or path.resolve().parent!=directory.resolve() or not path.is_file():raise ValueError('录像不可用')
    if path.stat().st_size<=0:raise ValueError('录像为空')
    return path

def byte_range(header,size):
    if not header:return 0,size-1,False
    match=re.fullmatch(r'bytes=(\d*)-(\d*)',header.strip())
    if not match or not any(match.groups()):raise ValueError('无效范围')
    left,right=match.groups()
    if not left:
        length=int(right)
        if length<=0:raise ValueError('无效范围')
        start,end=max(0,size-length),size-1
    else:start,end=int(left),min(int(right),size-1) if right else size-1
    if start>=size or start>end:raise ValueError('范围超出文件')
    return start,end,True

def describe(root):
    if not root:return None
    root=Path(root)
    if not (root/'stop_gate.json').exists():return None
    summary=read_json(root/'capture_summary.json');report=read_json(root/'qc_report.json') or read_json(root/'emg_qc_report.json')
    videos=[]
    for name,label in CAMERAS.items():
        try:
            path=video_path(root,root.name,name)
            probe=read_json(root/name/'video_check.json').get('streams',[{}])[0]
            videos.append({'name':name,'label':label,'bytes':path.stat().st_size,
                           'duration':float(probe.get('duration',0))})
        except (OSError,ValueError,IndexError,TypeError):pass
    findings=[f.get('message','') for f in report.get('findings',[]) if f.get('level') in ('WARN','ERROR')]
    qc=summarize_qc(dict(report,preprocessing=read_json(root/'emg_preprocessing.json')))
    for name,stream in report.get('streams',{}).items():
        label=CAMERAS.get(name,{'emg_left':'左臂肌电','emg_right':'右臂肌电'}.get(name,name))
        findings.extend(label+'：'+f.get('message','') for f in stream.get('findings',[]) if f.get('level') in ('WARN','ERROR'))
    return {'qc_state':summary.get('qc_state'),'qc_scope':report.get('scope','full'),'session':root.name,'videos':videos,'qc_level':report.get('level'),
            'error_count':qc['error_count'],'warn_count':qc['warn_count'],'qc_issues':qc['findings'][:5],
            'qc_modalities':{name:{k:v for k,v in value.items() if k!='findings'} for name,value in qc['modalities'].items()},
            'warnings':list(summary.get('warnings',[]))+findings,
            'qc_elapsed_seconds':summary.get('qc_elapsed_seconds')}
