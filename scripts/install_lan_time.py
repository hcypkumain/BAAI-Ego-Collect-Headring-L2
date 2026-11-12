#!/usr/bin/env python3
"""Install the optional best-effort headring LAN time services; no collector restart."""
import argparse,datetime,json,os,pathlib,re,shutil,subprocess,tempfile
SOURCE=pathlib.Path(__file__).resolve().parents[1]
def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--project-root',type=pathlib.Path,default=SOURCE)
 parser.add_argument('--server',default='192.168.1.118')
 parser.add_argument('--apply',action='store_true');args=parser.parse_args()
 if not re.fullmatch(r'(?:\d{1,3}\.){3}\d{1,3}',args.server) or any(int(x)>255 for x in args.server.split('.')):parser.error('Use the LAN time server IPv4 address')
 root=args.project_root.resolve();base=root/'orangepi'
 helper=(SOURCE/'deployment/lan-time/headring_lan_time.py').read_text(encoding='utf-8').replace("BASE=Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi')",'BASE=Path('+repr(str(base))+')')
 unit='''[Unit]
Description=Best effort headring LAN clock alignment
Wants=network-online.target chrony.service
After=network-online.target chrony.service
Before=camd.service embodied-pi-collect.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/headring-lan-time --boot
RemainAfterExit=yes
TimeoutStartSec=20
UMask=0077
[Install]
WantedBy=multi-user.target
'''
 files={'/usr/local/sbin/headring-lan-time':helper,
 '/etc/headring-lan-time.json':json.dumps(dict(version=1,server=args.server,step_threshold_seconds=.05,boot_retry_seconds=8,unavailable_policy='continue_capture'),indent=2)+'\n',
 '/etc/systemd/system/headring-lan-time.service':unit,
 '/etc/systemd/system/headring-lan-time-refresh.service':'[Unit]\nDescription=Retry LAN clock only while safely idle\nAfter=network-online.target chrony.service\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/headring-lan-time\nTimeoutStartSec=20\nUMask=0077\n',
 '/etc/systemd/system/headring-lan-time-refresh.timer':'[Unit]\nDescription=Best effort idle LAN clock retry\n[Timer]\nOnBootSec=45s\nOnUnitActiveSec=60s\nAccuracySec=1s\nRandomizedDelaySec=5s\nUnit=headring-lan-time-refresh.service\n[Install]\nWantedBy=timers.target\n'}
 for name in ['camd','embodied-pi-collect']:files['/etc/systemd/system/'+name+'.service.d/90-lan-time.conf']='[Unit]\nWants=headring-lan-time.service\nAfter=headring-lan-time.service\n'
 print(json.dumps({'server':args.server,'policy':'continue_capture_when_unavailable','targets':list(files)+['/etc/chrony/chrony.conf'],'apply':args.apply}))
 if not args.apply:return
 parser.error('Automatic LAN-time migration is disabled until old collector and old time services can be quiesced together; use a controlled maintenance window')
 if os.name!='posix' or os.geteuid()!=0:parser.error('--apply requires Linux root')
 chrony=pathlib.Path('/etc/chrony/chrony.conf')
 text=chrony.read_text();text=re.sub(r'\n# BEGIN HEADRING_LAN_TIME\n.*?# END HEADRING_LAN_TIME\n?','\n',text,flags=re.S)
 lines=[('# Previous setting: '+line if re.match(r'^\s*(server|pool|peer|makestep|initstepslew|maxslewrate|leapsecmode)\s',line) else line) for line in text.splitlines()]
 files[str(chrony)]='\n'.join(lines)+'\n# BEGIN HEADRING_LAN_TIME\nserver '+args.server+' iburst prefer minpoll 4 maxpoll 6\nmaxslewrate 500\nleapsecmode slew\n# END HEADRING_LAN_TIME\n'
 backup=pathlib.Path('/var/backups/baai-ego-collect/lan-time')/datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f');backup.mkdir(parents=True)
 originals={}
 try:
  for name,text in files.items():
   target=pathlib.Path(name);originals[name]=target.read_bytes() if target.exists() else None
   if target.exists():saved=backup/target.relative_to('/');saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,saved)
   target.parent.mkdir(parents=True,exist_ok=True)
   fd,tmp=tempfile.mkstemp(prefix='.'+target.name+'-',dir=target.parent)
   with os.fdopen(fd,'w') as f:f.write(text);f.flush();os.fsync(f.fileno())
   os.chmod(tmp,0o755 if name.startswith('/usr/local/sbin/') else 0o644);os.replace(tmp,target)
  subprocess.run(['systemctl','daemon-reload'],check=True)
  subprocess.run(['systemd-analyze','verify','/etc/systemd/system/headring-lan-time.service','/etc/systemd/system/headring-lan-time-refresh.service','/etc/systemd/system/headring-lan-time-refresh.timer'],check=True)
  subprocess.run(['systemctl','enable','chrony','headring-lan-time.service','headring-lan-time-refresh.timer'],check=True)
  subprocess.run(['systemctl','restart','chrony'],check=True)
  subprocess.run(['systemctl','start','headring-lan-time.service','headring-lan-time-refresh.timer'],check=True)
  print(json.dumps({'installed':True,'backup':str(backup),'collector_restarted':False}))
 except BaseException:
  for name,data in originals.items():
   target=pathlib.Path(name)
   if data is None:target.unlink(missing_ok=True)
   else:target.write_bytes(data)
  subprocess.run(['systemctl','daemon-reload']);subprocess.run(['systemctl','restart','chrony']);raise
if __name__=='__main__':main()
