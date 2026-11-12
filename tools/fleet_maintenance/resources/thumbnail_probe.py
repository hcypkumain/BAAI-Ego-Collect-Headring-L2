"""Bounded on-demand thumbnail GET; never fetches original or emits credentials."""
import sys,json,urllib.request,urllib.parse,base64,re,subprocess
from pathlib import Path
expected,mode,revision,*ids=sys.argv[1:]
if not re.fullmatch(r'cp_\d{4}',expected) or mode not in ('emg','eeg') or not re.fullmatch(r'\d{1,12}',revision) or not 1<=len(ids)<=6 or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',x) for x in ids): raise SystemExit(2)
if Path('/opt/egorecord/DEVICE_ID').read_text().strip()!=expected: raise SystemExit(3)
code=Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi')
service=subprocess.run(['systemctl','show','embodied-pi-collect','-p','ExecStart','--value'],capture_output=True,text=True,timeout=4).stdout
if str(code/'scripts/run.sh') not in service: raise SystemExit(5)
selection_path=code/'runtime/collection_profile.json'
selection=json.loads(selection_path.read_text())
if selection.get('active_mode')!=mode or selection.get('legacy_mode') not in ('emg','eeg'): raise SystemExit(4)
base=code if mode==selection['legacy_mode'] else code.parent/'profiles'/mode/'orangepi'
token=(base/'runtime/dashboard_token').read_text().strip()
def get(endpoint,limit):
 req=urllib.request.Request('http://127.0.0.1:8765/api/'+endpoint,headers={'X-Access-Token':token})
 with urllib.request.urlopen(req,timeout=4) as r: data=r.read(limit+1)
 if len(data)>limit: raise ValueError('oversized')
 return data
try:
 def safe():
  gate=json.loads(get('display-state',1048576)); privacy=gate.get('privacy') or {}; worker=gate.get('worker') or {}
  if gate.get('collection_mode')!=mode or gate.get('state') not in ('device_confirm','idle','failed_wait','exhausted') or gate.get('busy') is not False or gate.get('session') is not None or worker.get('recording_active') is True or privacy.get('required') is not False or privacy.get('phase')!='idle' or privacy.get('stop_confirmed') is not True: raise ValueError('privacy_or_capture')
 safe()
 output=[]
 for ident in ids:
  safe()
  try:
   raw=get('reference-thumbnail?'+urllib.parse.urlencode({'id':ident,'revision':revision}),2097152)
   output.append({'id':ident,'data':base64.b64encode(raw).decode()})
  except Exception: output.append({'id':ident,'error':'thumbnail_unavailable'})
 safe()
 if json.loads(selection_path.read_text())!=selection: raise ValueError('context_changed')
 print(json.dumps({'items':output}))
except Exception: print(json.dumps({'error':'privacy_context_or_api_unavailable'}))
