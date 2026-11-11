"""Structured, filesystem-backed fixtures for required-device qualification."""
import json
from pathlib import Path
import sys

COMMON=Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'
if str(COMMON) not in sys.path:sys.path.insert(0,str(COMMON))
from required_device_contract import build_contract,HEADBAND,WRISTS,EMG

START_UNIX_NS=1_800_000_000_000_000_000
STOP_UNIX_NS=1_800_000_010_000_000_000
START_MONOTONIC_NS=1_000_000_000
STOP_MONOTONIC_NS=11_000_000_000
WINDOW={'t0':START_UNIX_NS/1e9,'t1':STOP_UNIX_NS/1e9}


def config(mode='emg'):
    value={'collection_mode':mode,
        'cameras':{name:{'camd':name,'fps':30} for name in ('left','right','bleft','bright')},
        'wrist_cameras':{name:{'device':'/dev/'+name} for name in WRISTS},
        'emg':{name:{'port':'/dev/'+name} for name in EMG},
        'board_imu':{'device':'/dev/icm20602'}}
    if mode=='eeg':value['eeg']={'enabled':True}
    return value


def readiness(names,stale=()):
    rows={name:{'ready':True,'fresh':name not in stale,'age_seconds':.1,
                'last_seen_monotonic':START_MONOTONIC_NS/1e9,'seen':20} for name in names}
    missing=sorted(set(stale)&set(names))
    return {'ready':not missing,'missing':missing,'devices':rows}


def _timing_row(label,count,total,interval,window,first=None,last=None,outside=0,batch=False):
    if batch:
        n=count;n_clock_batches=total-outside
    else:
        n=count;n_clock_batches=None
    first=window['t0']+.1 if first is None else first
    last=window['t1']-.1 if last is None else last
    row={'available':True,'n':n,'n_total':total,'n_outside_window':outside,
        'n_nonfinite':0,'n_backsteps':0,'n_duplicates':(n-n_clock_batches if batch else 0),
        'first':first,'last':last,'head_margin_s':first-window['t0'],
        'tail_margin_s':window['t1']-last,'max_actual_interval_s':interval,
        'window':dict(window),'interval_basis':'distinct_consecutive_host_batch_clocks' if batch else 'adjacent_original_recorded_timestamps'}
    if batch:row['n_clock_batches']=n_clock_batches
    return row


def _required_report_stream(name,summary,window,level='WARN',long_gap=False):
    stats={};series={}
    if name in HEADBAND+WRISTS:
        count=summary[name]['frames']
        series['frames']={'n':count,'n_outside_window':0,'t0':window['t0']+.1,
            't1':window['t1']-.1,'duration':9.8,'rate':count/9.8}
        stats={'FrameCountMatch':{'file':'frames.mp4','n_frames':count,'n_timestamps':count,
                'n_compared':count,'n_window':count},
            'PiTimingEvidence':{'schema_version':1,'available':True,'window':dict(window),
                'series':{'frames':_timing_row('frames',count,count,3.5 if long_gap else .1,window)}},
            'PiVideoEvidence':{'schema_version':1,'available':True,'coverage_complete':True,
                'coverage':'full_frame_luminance','n_checked_frames':count,
                'expected_decoded_frames':count,'checked_duration_s':9.8}}
        if name in WRISTS:
            stats['PiWristFrameGap']={'schema_version':1,'status':'warn','passed':False,
                'n_frames':count,'error_count':0,'max_interval_s':.12}
    elif name in EMG:
        emg_count=summary[name]['emg_frames'];imu_count=summary[name]['imu_frames']
        emg_interval=(9.8/max(1,emg_count-1))
        imu_interval=(9.8/max(1,imu_count-1))
        if long_gap:emg_interval=3.5
        series={'emg':{'n':emg_count,'n_outside_window':0,'t0':window['t0']+.01,
                    't1':window['t1']-.01,'duration':9.98,'rate':emg_count/9.98},
                'imu':{'n':imu_count,'n_outside_window':0,'t0':window['t0']+.01,
                    't1':window['t1']-.01,'duration':9.98,'rate':imu_count/9.98}}
        stats={'PiTimingEvidence':{'schema_version':1,'available':True,'window':dict(window),
                'series':{'emg':_timing_row('emg',emg_count,emg_count,emg_interval,window),
                          'imu':_timing_row('imu',imu_count,imu_count,imu_interval,window)}},
            'PiEmgContinuity':{'schema_version':1,'status':'pass','shared_interleave_verified':True,
                'frames_received':emg_count+imu_count,'minimum_frames_sent':emg_count+imu_count,
                'minimum_frames_dropped':0,'sample_gap':{'exceeds_warn_count':0}}}
    elif name=='board_imu':
        count=summary[name]['frames']
        series={'imu':{'n':count,'n_outside_window':0,'t0':window['t0']+.1,
            't1':window['t1']-.1,'duration':9.8,'rate':count/9.8}}
        stats={'PiTimingEvidence':{'schema_version':1,'available':True,'window':dict(window),
                'series':{'imu':_timing_row('imu',count,count,9.8/max(1,count-1),window)}}}
    elif name=='eeg':
        detail=summary['eeg'];count=detail['samples']
        mono_window={'t0':START_MONOTONIC_NS/1e9,'t1':STOP_MONOTONIC_NS/1e9}
        clock_rows={key:_timing_row(key,count,count,.2,mono_window,batch=True)
                    for key in ('read_begin_monotonic_ns','arrival_monotonic_ns','sdk_received_monotonic_ns')}
        stats={'PiTimingEvidence':{'schema_version':1,'available':True,'window':mono_window,
                'series':clock_rows},
            'OmniEegIntegrity':{'samples':count,'sample_rate_hz':250,'missing_samples':0,
                'invalid_samples':0,'usable_samples':count,
                'usable_first_arrival_monotonic_ns':START_MONOTONIC_NS+100_000_000,
                'usable_last_arrival_monotonic_ns':STOP_MONOTONIC_NS-100_000_000,
                'usable_max_batch_interval_s':.2,'usable_head_margin_s':.1,'usable_tail_margin_s':.1}}
    return {'level':level,'findings':[],'stats':stats,'series':series}


def complete_report(contract,summary,level='WARN',scope='full',missing_streams=(),long_gap=False,window=WINDOW):
    names=set(contract['required_devices'])-set(missing_streams)
    streams={name:_required_report_stream(name,summary,window,level,long_gap and name=='cam_ros_left') for name in sorted(names)}
    return {'scope':scope,'level':level,'window':dict(window),'streams':streams,'findings':[]}


def write_session(root,mode='emg',level='WARN',scope='full',missing_streams=(),stale=(),
                  long_gap=False,write_receipt=True,summary_status='success',
                  start_unix_ns=START_UNIX_NS, duration_ns=STOP_UNIX_NS-START_UNIX_NS):
    """Write profile-complete source files and realistic QC evidence structures."""
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    cfg=config(mode);contract=build_contract(cfg,mode=mode)
    required=contract['required_devices'];selected=dict(selected=required,device_config=cfg,
        collection_mode=mode,required_device_contract=contract)
    for name in required:
        directory=root/name;directory.mkdir(exist_ok=True)
        if name in HEADBAND+WRISTS:
            (directory/'frames.mp4').write_bytes(b'validated encoded video bytes')
            (directory/'timestamps.csv').write_text('frame_index,timestamp\n1,1\n',encoding='utf-8')
        elif name in EMG:
            (directory/(name+'.npz')).write_bytes(b'validated canonical sample archive')
            (directory/'raw_chunks').mkdir(exist_ok=True)
            (directory/'raw_chunks'/'chunk_000.npz').write_bytes(b'preserved raw sample archive')
        elif name=='board_imu':(directory/'raw.csv').write_text('stamp,x,y,z\n1,0,0,0\n',encoding='utf-8')
        elif name=='eeg':(directory/'eeg.npz').write_bytes(b'validated eeg sample archive')
    marker=root/'marker';marker.mkdir(exist_ok=True);(marker/'marker.npz').write_bytes(b'capture marker')
    start=readiness(required,stale);stop=readiness(required,stale)
    window={'t0':start_unix_ns/1e9,'t1':(start_unix_ns+duration_ns)/1e9}
    summary={'status':summary_status,'errors':[],'start_unix_ns':start_unix_ns,'stop_unix_ns':start_unix_ns+duration_ns,
        'start_monotonic_ns':START_MONOTONIC_NS,'stop_monotonic_ns':START_MONOTONIC_NS+duration_ns,
        'required_device_contract_version':contract['version'],'required_device_contract':contract,
        'required_devices':required,'required_device_start':start,'required_device_stop':stop,
        'qc_mode':'immediate','qc_state':'completed','qc_level':level}
    for name in required:
        if name in HEADBAND+WRISTS:summary[name]={'frames':100}
        elif name in EMG:summary[name]={'emg_frames':1000,'imu_frames':100}
        elif name=='board_imu':summary[name]={'frames':100}
        else:summary[name]={'samples':1000}
    start_gate={'start_unix_ns':start_unix_ns,'start_monotonic_ns':START_MONOTONIC_NS,
        'required_device_contract_version':contract['version'],'required_devices':required,
        'required_device_readiness':start['devices']}
    stop_gate={'stop_unix_ns':start_unix_ns+duration_ns,'stop_monotonic_ns':START_MONOTONIC_NS+duration_ns,
        'recording_active':False}
    report=complete_report(contract,summary,level,scope,missing_streams,long_gap,window)
    for filename,value in [('device_selection.json',selected),('capture_summary.json',summary),
        ('native_capture_summary.json',dict(summary,shutdown_complete=True)),
        ('meta.json',{'reference_sha256':'a'*64,'reference_image':'configs/environments/scene/task_1.png'}),('qc_report.json',report),
        ('start_gate.json',start_gate),('stop_gate.json',stop_gate)]:
        (root/filename).write_text(json.dumps(value,allow_nan=False),encoding='utf-8')
    import yaml
    (root/'meta.yaml').write_text(yaml.safe_dump({'status':summary_status,'retention_state':'kept',
        'qc_state':summary['qc_state'],'qc_mode':summary['qc_mode'],'qc_level':level,
        'reference_image':'configs/environments/scene/task_1.png','reference_sha256':'a'*64,
        'session_created_unix':1_800_000_000,'duration':10},allow_unicode=True),encoding='utf-8')
    if write_receipt and summary_status=='success' and scope=='full' and not missing_streams and not stale and not long_gap:
        from capture_qc_receipt import write as issue
        try:issue(root)
        except (OSError,ValueError,TypeError,KeyError):pass
    return contract,summary,report
