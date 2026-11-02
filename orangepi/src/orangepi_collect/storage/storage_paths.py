"""Validate the intended SD volume before every new session/export. No disk formatting."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'common'))
from project_paths import BASE,PROJECT
import json,os,re,shutil,subprocess,tempfile,yaml

class StorageError(RuntimeError):pass

def config():return yaml.safe_load((BASE/'configs/storage.yaml').read_text())

def volume_root(check_write=False):
    cfg=config()
    if cfg['mode']!='sd':raise StorageError('存储模式必须明确配置为 SD 卡，禁止自动写回系统盘')
    mount=Path(cfg['mount_point']).resolve()
    try:
        info=json.loads(subprocess.check_output(['findmnt','-J','-M',str(mount),'-o','TARGET,SOURCE,UUID,FSTYPE,OPTIONS'],stderr=subprocess.DEVNULL,timeout=3))['filesystems'][0]
    except Exception as exc:raise StorageError('SD 卡未挂载，请检查 SD 卡并重新挂载') from exc
    if str(info.get('uuid','')).upper()!=str(cfg['expected_uuid']).upper():raise StorageError('SD 卡身份与配置不符，禁止写入，请核对卡的 UUID')
    if info['target']!=str(mount) or mount.stat().st_dev==Path('/').stat().st_dev:raise StorageError('SD 卡路径落在系统盘，禁止开始采集')
    if 'rw' not in info['options'].split(','):raise StorageError('SD 卡为只读，禁止开始采集')
    if not re.fullmatch(r'[A-Za-z0-9_-]+',cfg['device_id']):raise StorageError('设备编号只能包含英文字母、数字、下划线或连接号')
    for key in ('directory','shift'):
        if not cfg[key] or Path(cfg[key]).name!=cfg[key] or cfg[key] in ('.','..'):raise StorageError('SD 卡目录配置不合法')
    root=mount/cfg['directory']/cfg['device_id']
    try:root.resolve().relative_to(mount)
    except ValueError as exc:raise StorageError('SD 卡目录指向挂载点外部，禁止写入') from exc
    if check_write:
        if shutil.disk_usage(mount).free<int(cfg['minimum_start_free_mb'])*1024**2:raise StorageError('SD 卡空间不足，请转存数据后重试')
        root.mkdir(parents=True,exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(prefix='.write-test-',dir=root) as handle:
                handle.write(b'orangepi storage check\n');handle.flush();os.fsync(handle.fileno())
            fd=os.open(str(root),os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        except OSError as exc:raise StorageError('SD 卡写入或同步失败，禁止开始采集') from exc
    return root

def session_root():return volume_root()/ 'data'/config()['shift']
def diagnostics_root():return volume_root()/'diagnostics'
def export_root():return volume_root()/'delivery'

def validate_output(path):
    volume_root(check_write=True)
    try:Path(path).resolve().relative_to(volume_root())
    except ValueError as exc:raise StorageError('会话输出必须位于配置的 SD 卡设备目录中') from exc

if __name__=='__main__':
    root=volume_root(check_write=True)
    if len(sys.argv)>1 and sys.argv[1]=='export':
        print(export_root());sys.exit(0)
    print(json.dumps({'volume':str(root),'session_root':str(session_root()),'free_gib':round(shutil.disk_usage(root).free/1024**3,2)},ensure_ascii=False))
