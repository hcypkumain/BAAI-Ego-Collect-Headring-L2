"""Persistent capture profiles. Original files belong only to the legacy mode.

Code, virtual environments and physical hardware stay shared. Reference images,
task ledgers, runtime queues and SD output have separate roots. No old take is
moved or re-labelled when a profile is selected.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import uuid

MODES=('emg','eeg')
LABELS={'emg':'肌电','eeg':'脑电'}


def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    try:
        with tmp.open('x',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(str(tmp),str(path))
        if os.name!='nt':
            fd=os.open(str(path.parent),os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
    finally:
        try:tmp.unlink()
        except FileNotFoundError:pass


def selection(code_base):
    base=Path(code_base).resolve();path=base/'runtime/collection_profile.json'
    if path.exists():
        if path.is_symlink() or path.stat().st_size>4096:raise ValueError('采集模式设置无效')
        value=json.loads(path.read_text(encoding='utf-8'))
        if (value.get('version')!=1 or value.get('active_mode') not in MODES
                or value.get('legacy_mode') not in MODES or type(value.get('generation')) is not int
                or value['generation']<1):raise ValueError('采集模式设置无效')
        return value
    default=base/'configs/collection_mode.json'
    mode=json.loads(default.read_text(encoding='utf-8'))['default_mode'] if default.exists() else 'emg'
    if mode not in MODES:raise ValueError('默认采集模式无效')
    return dict(version=1,active_mode=mode,legacy_mode=mode,generation=0)


def profile_base(code_base,value=None,mode=None):
    base=Path(code_base).resolve();value=value or selection(base);mode=mode or value['active_mode']
    if mode not in MODES:raise ValueError('无效采集模式')
    return base if mode==value['legacy_mode'] else base.parent/'profiles'/mode/'orangepi'


def context(value):
    return value['active_mode']+':'+str(value['generation']) if value['generation'] else None


def initialize(code_base,legacy_mode):
    base=Path(code_base).resolve();path=base/'runtime/collection_profile.json'
    if path.exists():
        value=selection(base)
        if value['legacy_mode']!=legacy_mode:raise ValueError('已有文件的采集模式归属不能改变')
    else:
        if legacy_mode not in MODES:raise ValueError('无效采集模式')
        value=dict(version=1,active_mode=legacy_mode,legacy_mode=legacy_mode,generation=1)
        atomic(path,value)
    for mode in MODES:provision(base,value,mode)
    return value


def provision(code_base,value,mode):
    base=Path(code_base).resolve();target=profile_base(base,value,mode)
    if target==base:return target
    target.mkdir(parents=True,exist_ok=True);project=target.parent
    (target/'configs').mkdir(exist_ok=True);(target/'runtime').mkdir(exist_ok=True)
    (project/'configs/environments').mkdir(parents=True,exist_ok=True)
    # Only small station settings. Never seed another profile with references,
    # completed-image ledgers, a catalog, pending jobs, or USB replacement state.
    for source in (base/'configs').iterdir():
        if (source.name!='task_block_policy.json' and source.is_file()
                and source.suffix in ('.json','.yaml','.env') and source.stat().st_size<1024*1024):
            dest=target/'configs'/source.name
            if not dest.exists():shutil.copy2(source,dest)
    import yaml
    storage=target/'configs/storage.yaml'
    cfg=yaml.safe_load(storage.read_text(encoding='utf-8'));cfg['capture_profile']=mode
    storage.write_text(yaml.safe_dump(cfg,allow_unicode=True,sort_keys=False),encoding='utf-8')
    preferences=target/'configs/reference_control.json'
    marker=target/'runtime/profile_initialized.json'
    if not marker.exists():
        prefs=json.loads(preferences.read_text(encoding='utf-8'))
        prefs['collector_id']=prefs['collector_id']+'-'+mode.upper();prefs['selected_list_id']='all'
        atomic(preferences,prefs)
        cfgpath=target/'configs/devices.yaml';cfg=yaml.safe_load(cfgpath.read_text(encoding='utf-8'))
        cfg.pop('first_image',None)
        if mode=='eeg':cfg['eeg']=dict(enabled=True,serial='')
        else:cfg.pop('eeg',None)
        cfgpath.write_text(yaml.safe_dump(cfg,allow_unicode=True,sort_keys=False),encoding='utf-8')
        atomic(marker,dict(version=1,mode=mode))
    # Code links are explicit and never used for profile-owned writable state.
    for dest,source in [(target/'.venvs',base/'.venvs'),(target/'src',base/'src'),
                        (target/'scripts',base/'scripts'),(target/'assets',base/'assets'),
                        (project/'src',base.parent/'src')]:
        if not dest.exists() and not dest.is_symlink():dest.symlink_to(source,target_is_directory=True)
    for source in (base.parent/'configs').glob('*'):
        if source.is_file() and not (project/'configs'/source.name).exists():shutil.copy2(source,project/'configs'/source.name)
    # Same control capability on this physical device, independent library keys.
    token=base/'runtime/dashboard_token'
    if token.exists():
        dest=target/'runtime/dashboard_token'
        if not dest.exists():shutil.copy2(token,dest)
    return target


def activate(code_base,mode,expected):
    base=Path(code_base).resolve();value=selection(base)
    if value!=expected:raise ValueError('采集模式已变化，请刷新')
    if mode not in MODES:raise ValueError('请选择脑电或肌电')
    provision(base,value,mode)
    value=dict(value,active_mode=mode,generation=value['generation']+1)
    atomic(base/'runtime/collection_profile.json',value)
    return value


def device_config(config,mode):
    config=copy.deepcopy(config)
    if mode=='emg':config.pop('eeg',None)
    else:
        item=config.setdefault('eeg',{'serial':''});item['enabled']=True
    config['collection_mode']=mode
    return config


def storage_root(root,config):
    mode=config.get('capture_profile')
    if mode is None:return Path(root)
    if mode not in MODES:raise ValueError('存储模式隔离目录无效')
    return Path(root)/'profiles'/mode
