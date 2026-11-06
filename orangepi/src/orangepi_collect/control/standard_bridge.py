"""Run the isolated v3.1 exporter before any new platform upload."""
import json,os,subprocess,shutil
from pathlib import Path
from cloud_common import CloudError,read_json,source_manifest,atomic,check_source,sync_dir,same_manifest

ROOT=Path('/home/orangepi/maintenance/standard-converter')

def prepare(job,directory,raw_manifest,emit):
 source=Path(job['source']);directory=Path(directory)
 attempt=read_json(directory/'platform.json',{})
 if attempt and not attempt.get('package_version'):
  raise CloudError('旧原始数据上传任务已有云端对象，不能在原路径混入标准包；请保留原记录并新建标准包上传任务')
 volume=Path(job['volume']).resolve()
 if not volume.is_mount():raise CloudError('SD卡未挂载，未执行转换')
 root=volume/'Embodied-Brain-Collect'/'standardized'
 if root.is_symlink():raise CloudError('标准包目录不能是链接')
 target=root/(source.name+'-'+raw_manifest['digest'][:16]+'-mf2')
 if not ROOT.joinpath('venv/bin/python').is_file():raise CloudError('标准格式转换环境未安装，已保留原始数据')
 # L3 is determined by the actual recording, not an old L2-prefixed folder name.
 profile='L3' if (source/'eeg/eeg.npz').is_file() else 'L2'
 emit(state='checking',phase='转换并校验标准数据包',current_file='')
 cmd=[str(ROOT/'venv/bin/python'),'-u',str(ROOT/'standard_export.py'),str(source),'--out',str(target),
      '--profile',profile,'--source-digest',raw_manifest['digest'],'--workers','1']
 # The worker forwards pause/cancel to this whole process group.
 process=subprocess.Popen(['nice','-n','15','ionice','-c','3',*cmd],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 try:stdout,stderr=process.communicate(timeout=7200)
 except subprocess.TimeoutExpired:
  process.kill();process.communicate();raise CloudError('标准包转换超时，原始数据保留')
 if process.returncode!=0:
  log=directory/'conversion-error.log';log.write_text(stderr[-16000:],encoding='utf-8')
  raise CloudError('标准格式转换未通过，原始数据保留；请检查转换报告')
 lines=[line[len('EXPORT_RESULT '):] for line in stdout.splitlines() if line.startswith('EXPORT_RESULT ')]
 if len(lines)!=1:raise CloudError('转换程序未返回有效校验结果，未开始上传')
 result=json.loads(lines[0])
 if result.get('root')!=str(target) or result.get('validation',{}).get('passed') is not True:raise CloudError('标准包校验未通过')
 package_manifest=source_manifest(target)
 old=read_json(directory/'package_manifest.json')
 if old:
  if not same_manifest(old,package_manifest):raise CloudError('标准包与本次上传清单不同，停止避免覆盖')
  package_manifest=old
 atomic(directory/'package_manifest.json',package_manifest)
 atomic(directory/'conversion.json',dict(result,source_digest=raw_manifest['digest'],package_version='pi-mf-v3.1-2'))
 return target,package_manifest,result

def check_cleanup(job,directory,receipt):
 if not receipt.get('package_version'):return None
 root=Path(job['volume']).resolve()/'Embodied-Brain-Collect'/'standardized'
 package=Path(receipt['package_path'])
 expected_name=job['session_id']+'-'+receipt['manifest_digest'][:16]+('-mf2' if receipt['package_version']=='pi-mf-v3.1-2' else '')
 if root.is_symlink() or package.is_symlink() or package.parent.resolve()!=root.resolve() or package.name!=expected_name:raise CloudError('标准包清理路径越界')
 manifest=read_json(Path(directory)/'package_manifest.json')
 if not manifest or manifest['digest']!=receipt['package_digest']:raise CloudError('标准包清理缺少一致的云端回执')
 if package.exists():check_source(package,manifest)
 return package

def cleanup_cache(job,directory,receipt):
 package=check_cleanup(job,directory,receipt)
 if package is None:return
 directory=Path(directory);intent_path=directory/'package_cleanup.json'
 trash=Path(job['volume']).resolve()/'uploaded-cleanup'
 if trash.is_symlink():raise CloudError('标准包清理暂存目录无效')
 trash.mkdir(exist_ok=True);destination=trash/(job['session_id']+'--'+job['id']+'--standard')
 intent=read_json(intent_path)
 if intent is None:
  if destination.exists():raise CloudError('标准包清理暂存路径冲突')
  intent={'package':str(package),'destination':str(destination),'phase':'prepared'};atomic(intent_path,intent)
 if intent['destination']!=str(destination) or intent['package']!=str(package):raise CloudError('标准包清理记录不一致')
 if package.exists():
  if destination.exists():raise CloudError('标准包清理暂存路径冲突')
  os.rename(package,destination);sync_dir(package.parent);sync_dir(trash)
 if destination.exists():shutil.rmtree(destination);sync_dir(trash)
 sidecar=package.with_name(package.name+'.receipt.json')
 if sidecar.is_symlink():raise CloudError('标准包缓存回执路径无效')
 if sidecar.exists():sidecar.unlink()
 intent['phase']='complete';atomic(intent_path,intent)
