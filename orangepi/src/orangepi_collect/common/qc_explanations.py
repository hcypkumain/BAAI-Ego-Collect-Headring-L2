"""Read-only operator explanations; never alter findings or QC severity."""
import math

EMG_LABELS={'emg_left':'左臂肌电','emg_right':'右臂肌电'}
SKIP_REASONS={
 'shared_timeline_remains_nonincreasing':'重建后的共享时间线仍有重复或回退',
 'receipt_clock_backwards':'接收时钟发生回退',
 'shared_sequence_repeat_or_full_wrap_ambiguous':'共享帧序号存在无法证明的重复或整圈丢失',
 'wire_loss_exceeds_1_percent':'序号丢失超过自动修复范围',
 'too_many_malformed_frames':'拼接异常帧数量超过自动修复范围',
 'continuous_missing_data_reaches_hard_gap':'修复后连续缺口仍超过原始阈值'}

def explain_finding(finding,stream,window=None,preprocessing=None):
 if stream not in EMG_LABELS:return {}
 f=finding if isinstance(finding,dict) else {};level=f.get('level','WARN');check=f.get('check','')
 subject=f.get('subject');label=EMG_LABELS[stream]+('的腕带 IMU' if subject=='imu' else '')
 info={'label':label,'title':'肌电检查','reason':str(f.get('message','')),'action':'查看该侧原始波形和检查报告；若持续异常，检查佩戴及数据连接。'}
 def finite(x):return type(x) in (int,float) and math.isfinite(x)
 observed=f.get('observed');threshold=f.get('threshold');detail=f.get('detail') or {}
 segments=detail.get('segments') or []
 offset=segments[0].get('t_offset') if segments and isinstance(segments[0],dict) else None
 if not finite(offset) and isinstance(window,dict) and finite(window.get('t0')):
  spans=f.get('spans') or []
  if spans and finite(spans[0].get('t')):offset=spans[0]['t']-window['t0']
 if finite(offset):info['offset_seconds']=max(0.,float(offset))
 if check=='TimestampGap':
  info['title']='时间线缺口'
  if finite(observed) and finite(threshold):
   info['reason']='保存后的时间线最长缺口 %.1f 毫秒，原始阈值 %.1f 毫秒。'%(observed*1000,threshold*1000)
  if finite(offset):info['reason']+='约在开录后 %.2f 秒。'%max(0.,offset)
  info['action']='保留本段原始数据并重采该任务；若同侧反复出现，检查该侧 USB 数据线和串口连接，并保留原始帧排查解析。'
 elif check in ('TimestampSanity','PiEmgContinuity'):
  info['title']='时间戳或时间线异常'
  info['action']='保留原始数据，重新准备并采集；无法验证的时间线异常不会自动放行，需检查该侧串口数据和时间线重建记录。'
 elif check=='PostprocessRecovery':
  info.update(title='拼接异常已修复',reason='少量拼接坏帧已通过验证并修复，原始备份已保留。',action='此项为 WARN；若本段没有其他 ERROR，可以继续采集。')
 elif check=='MadOutlier':
  info.update(title='信号异常值',action='检查该侧佩戴、电极接触和原始波形；此 WARN 本身不阻断采集。')
 elif check=='SnGap':
  info.update(title='线缆帧序号告警',action='序号由肌电与腕带 IMU 共用；保留告警记录，若连续出现则检查该侧 USB 和串口连接。')
 elif check in ('TimestampJump','IntervalJitter','SlidingWindowRate'):
  info.update(title='采样时序告警',action='此 WARN 本身不阻断；若重复出现，检查该侧数据连接并观察原始波形。')
 if level=='ERROR' and isinstance(preprocessing,dict) and preprocessing.get('skipped_reason'):
  reason=str(preprocessing['skipped_reason'])
  description=SKIP_REASONS.get(reason,'现有原始证据不足以验证自动修复')
  info['reason']+='自动修复未通过：'+description+'；该缺口不直接等同于已经证实的物理断流。'
 return info

def emg_error_messages(report,preprocessing=None):
 report=report if isinstance(report,dict) else {};out=[];preprocessing=preprocessing or {}
 for stream,value in (report.get('streams') or {}).items():
  if stream not in EMG_LABELS or not isinstance(value,dict):continue
  for finding in value.get('findings') or []:
   if finding.get('level')!='ERROR':continue
   item=explain_finding(finding,stream,report.get('window'),preprocessing.get(stream))
   out.append(item['label']+'即时质检：'+item['reason']+'处理建议：'+item['action'])
 return out[:8] or (['肌电即时质检未通过：会话级检查失败，请打开本段检查详情。'] if report.get('level')=='ERROR' else [])
