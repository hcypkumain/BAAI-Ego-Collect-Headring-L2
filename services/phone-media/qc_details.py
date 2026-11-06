"""Structured, read-only QC findings shared by summary and replay adapters."""
import math
import sys
from pathlib import Path
_common=Path(__file__).resolve().parents[1]/'common'
if _common.is_dir():sys.path.insert(0,str(_common))
from qc_explanations import explain_finding

LEVELS={'INFO':0,'WARN':1,'ERROR':2}
LABELS={'cam_ros_left':'头环前左','cam_ros_right':'头环前右','cam_ros_bleft':'头环后左','cam_ros_bright':'头环后右',
        'cam_left_wrist':'左腕相机','cam_right_wrist':'右腕相机','cam_third':'第三视角','emg_left':'左臂肌电',
        'emg_right':'右臂肌电','board_imu':'头环惯性','marker':'起止标记'}

def summarize_qc(report):
    report=report if isinstance(report,dict) else {}
    result={'scope':report.get('scope','full'),'level':str(report.get('level','UNKNOWN')),'error_count':0,'warn_count':0,'findings':[],'modalities':{}}
    def add(raw,stream):
        if not isinstance(raw,dict):return
        level=str(raw.get('level','INFO')).upper()
        if level not in ('WARN','ERROR'):return
        subject=str(raw.get('subject') or stream or 'session')[:96]
        # A session finding may still identify an exact modality in subject.
        modality=subject if subject in LABELS else stream or 'session'
        item={'level':level,'modality':modality,'label':LABELS.get(modality,modality),'message':str(raw.get('message',''))[:3000],
              'check':str(raw.get('check',''))[:96],'subject':subject,'field':str(raw.get('field',''))[:128]}
        for key in ('threshold','observed'):
            value=raw.get(key)
            if type(value) in (int,float) and math.isfinite(value):item[key]=value
        item['spans']=[{k:(v[:160] if isinstance(v,str) else v) for k,v in span.items() if k in ('t','dur','msg','duration','label') and (isinstance(v,str) or type(v) in (int,float) and math.isfinite(v))} for span in raw.get('spans',[])[:50] if isinstance(span,dict)]
        item.update(explain_finding(raw,modality,report.get('window'),(report.get('preprocessing') or {}).get(modality)))
        result['findings'].append(item);key='error_count' if level=='ERROR' else 'warn_count';result[key]+=1
        detail=result['modalities'].setdefault(modality,{'id':modality,'label':LABELS.get(modality,modality),'level':'INFO','error_count':0,'warn_count':0,'findings':[]})
        detail[key]+=1;detail['findings'].append(item)
        if LEVELS[level]>LEVELS.get(detail['level'],0):detail['level']=level
    for raw in report.get('findings',[]):add(raw,'session')
    for name,value in (report.get('streams') or {}).items():
        if not isinstance(value,dict):continue
        for raw in value.get('findings',[]):add(raw,str(name))
    result['findings'].sort(key=lambda value:(-LEVELS[value['level']],value['modality']))
    result['total_findings']=len(result['findings'])
    return result

def compact_qc(value):
    result=dict(value);result['findings']=value['findings'][:20]
    result['next_offset']=20 if value['total_findings']>20 else None
    result['modalities']={key:dict(item,findings=item['findings'][:3]) for key,item in value['modalities'].items()}
    return result
