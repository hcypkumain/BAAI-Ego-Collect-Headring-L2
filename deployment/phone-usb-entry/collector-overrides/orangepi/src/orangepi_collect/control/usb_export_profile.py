"""Inspect one capture profile in a short-lived, isolated Python process.

No profile selection is persisted or changed by this script. The parent must
run this script as a new process, never import and call configure() in the live
controller. Only source paths under the selected profile's validated SD volume
are accepted.
"""
import json
import os
import sys
from pathlib import Path

CONTROL = Path(__file__).resolve().parent
sys.path.insert(0,str(CONTROL.parent/'common'))


def configure(mode):
    import project_paths as paths
    from collection_profiles import MODES, profile_base, selection
    if mode not in MODES: raise ValueError('无效采集模式')
    value=selection(paths.CODE_BASE)
    paths.BASE=profile_base(paths.CODE_BASE,value,mode)
    paths.PROJECT=paths.BASE.parent
    paths.COLLECTION_MODE=mode
    paths.COLLECTION_CONTEXT=None
    return paths


def snapshot(store, source):
    from daily_collection import take_snapshot
    value=store.metadata(source)
    result={key:item for key,item in value.items() if key!='_image'}
    result['daily_snapshot']=take_snapshot(store,source,metadata=value)
    return result


def check_legacy_data(mount):
    path=Path(mount)/'data/baai_ego_task'
    if path.is_symlink():raise ValueError('旧版采集目录是链接，无法安全确认')
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError('发现旧版采集数据，当前版本不能安全自动清理，请联系维护人员')


def discover_sources(data_root):
    data_root=Path(data_root)
    if data_root.is_symlink():raise ValueError('采集数据根目录不能是链接')
    sources=[]
    if data_root.exists():
        for shift in sorted(data_root.iterdir()):
            if shift.is_symlink() or not shift.is_dir():raise ValueError('采集数据目录含未识别条目')
            for take in sorted(shift.iterdir()):
                if take.name == '.usb-export-cleanup':
                    if take.is_symlink() or not take.is_dir() or any(take.iterdir()):
                        raise ValueError('存在未完成的导出清理事务')
                    continue
                if take.is_symlink() or not take.is_dir():raise ValueError('采集批次目录含未识别条目')
                if take.name.startswith('.') or take.name.endswith(('.tmp','.partial','.active')):
                    raise ValueError('存在尚未完成的采集批次：'+take.name)
                sources.append(str(take))
    return sources


def main():
    raw=sys.stdin.buffer.read(16385)
    if len(raw)>16384: raise ValueError('请求过大')
    request=json.loads(raw)
    if not isinstance(request,dict) or set(request) != {'mode','source'}: raise ValueError('请求无效')
    paths=configure(request['mode'])
    # Imports happen only after the process-local profile context is selected.
    import storage_paths
    if not (paths.BASE/'configs/storage.yaml').exists():
        # A never-provisioned non-legacy profile can still have an empty SD
        # namespace. Derive its read-only storage identity from the shared
        # device configuration without creating a profile or changing mode.
        from collection_profiles import selection
        if request['mode'] == selection(paths.CODE_BASE)['legacy_mode']:
            raise ValueError('缺少采集存储配置')
        import yaml
        legacy=yaml.safe_load((paths.CODE_BASE/'configs/storage.yaml').read_text(encoding='utf-8'))
        if not isinstance(legacy,dict):raise ValueError('采集存储配置无效')
        legacy=dict(legacy,capture_profile=request['mode'])
        storage_paths.config=lambda:dict(legacy)
    import ledger
    from recording_store import RecordingStore
    store=RecordingStore(paths.BASE,paths.PROJECT,ledger)
    _, volume=store.roots()
    data_root=volume/'data'
    if data_root.is_symlink():raise ValueError('采集数据根目录不能是链接')
    root=data_root.resolve()
    if request['source'] is None:
        check_legacy_data(storage_paths.config()['mount_point'])
        sources=discover_sources(data_root)
        print(json.dumps(dict(mode=request['mode'],base=str(paths.BASE),project=str(paths.PROJECT),volume=str(volume),sources=sources),ensure_ascii=False))
        return
    source=Path(request['source'])
    if not source.is_absolute() or source.is_symlink() or not source.is_dir(): raise ValueError('采集目录无效')
    resolved=source.resolve()
    relative=resolved.relative_to(root)
    if len(relative.parts)!=2 or source != resolved or source.parent.is_symlink(): raise ValueError('采集目录不属于所选模式')
    print(json.dumps(dict(mode=request['mode'],base=str(paths.BASE),project=str(paths.PROJECT),
                          volume=str(volume),source=str(source),metadata=snapshot(store,source)),
                     ensure_ascii=False,allow_nan=False))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(str(error),file=sys.stderr);raise SystemExit(1)
