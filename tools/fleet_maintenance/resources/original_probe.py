"""Read one catalog-bound original, after fresh controller privacy checks."""
import base64,hashlib,json,os,re,stat,subprocess,sys,urllib.request
from pathlib import Path
expected,mode,rev,ident=sys.argv[1:]
if not re.fullmatch(r'cp_\d{4}',expected) or mode not in ('emg','eeg') or not re.fullmatch(r'\d{1,12}',rev) or not re.fullmatch(r'image_[a-f0-9]{40}',ident): raise SystemExit(2)
if Path('/opt/egorecord/DEVICE_ID').read_text().strip()!=expected: raise SystemExit(3)
code=Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi')
service=subprocess.run(['systemctl','show','embodied-pi-collect','-p','ExecStart','--value'],capture_output=True,text=True,timeout=4).stdout
if str(code/'scripts/run.sh') not in service: raise SystemExit(4)
selection_path=code/'runtime/collection_profile.json'; selection=json.loads(selection_path.read_text())
if selection.get('active_mode') not in ('emg','eeg') or selection.get('legacy_mode') not in ('emg','eeg'): raise SystemExit(5)
base=code if mode==selection['legacy_mode'] else code.parent/'profiles'/mode/'orangepi'
token=(base/'runtime/dashboard_token').read_text().strip()
def safe():
 req=urllib.request.Request('http://127.0.0.1:8765/api/display-state',headers={'X-Access-Token':token})
 with urllib.request.urlopen(req,timeout=4) as r: gate=json.load(r)
 privacy=gate.get('privacy') or {}; worker=gate.get('worker') or {}
 if gate.get('collection_mode')!=selection.get('active_mode') or gate.get('state') not in ('device_confirm','idle','failed_wait','exhausted') or gate.get('busy') is not False or gate.get('session') is not None or worker.get('recording_active') is True or privacy.get('required') is not False or privacy.get('phase')!='idle' or privacy.get('stop_confirmed') is not True: raise ValueError('privacy')
def open_safe(path):
 if not path.is_absolute(): raise ValueError('absolute required')
 fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
 try:
  parts=path.parts[1:]
  if any(not p or p in ('.','..') for p in parts): raise ValueError('path')
  for index,part in enumerate(parts):
   flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
   if index<len(parts)-1: flags|=os.O_DIRECTORY
   new=os.open(part,flags,dir_fd=fd); os.close(fd); fd=new
  result=fd; fd=-1; return result
 finally:
  if fd>=0: os.close(fd)
def bounded(fd,limit):
 before=os.fstat(fd)
 if not stat.S_ISREG(before.st_mode) or before.st_size>limit: raise ValueError('invalid_file')
 parts=[]; total=0
 while True:
  chunk=os.read(fd,min(262144,limit+1-total))
  if not chunk: break
  total+=len(chunk)
  if total>limit: raise ValueError('oversized')
  parts.append(chunk)
 after=os.fstat(fd)
 if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns): raise ValueError('changed')
 return b''.join(parts)
try:
 safe(); catpath=base/'runtime/reference_library/catalog.json'; fd=open_safe(catpath)
 try: catalog=json.loads(bounded(fd,16777216))
 finally: os.close(fd)
 if catalog.get('revision')!=int(rev): raise ValueError('revision')
 item=catalog['images'][ident]
 if item.get('deleted') is not False: raise ValueError('deleted')
 path=item['path']
 if not isinstance(path,str) or '\x00' in path or path.startswith('/') or any(x in ('','.','..') for x in path.split('/')): raise ValueError('unsafe')
 fd=open_safe(base.parent/'configs/environments'/path)
 try: raw=bounded(fd,8388608)
 finally: os.close(fd)
 digest=hashlib.sha256(raw).hexdigest()
 if item.get('sha256') and item['sha256']!=digest: raise ValueError('hash')
 if not (raw.startswith(b'\xff\xd8\xff') or raw.startswith(b'\x89PNG\r\n\x1a\n')): raise ValueError('format')
 safe()
 fd=open_safe(catpath)
 try: final_catalog=json.loads(bounded(fd,16777216))
 finally: os.close(fd)
 if final_catalog.get('revision')!=int(rev) or json.loads(selection_path.read_text())!=selection: raise ValueError('context')
 print(json.dumps({'id':ident,'sha256':digest,'data':base64.b64encode(raw).decode()}))
except Exception: print(json.dumps({'error':'privacy_context_catalog_or_image_unavailable'}))
