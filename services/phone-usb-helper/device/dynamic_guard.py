#!/usr/bin/python3
"""Supervise the USB-limited Avahi publisher by USB identity, not interface name."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import time

BASE='/opt/headring-usb-entry-persistent'
META='/opt/headring-usb-entry-proof/proof-20261113-b524dab0/metadata.json'
spec=importlib.util.spec_from_file_location('usb_guard',BASE+'/guard.py')
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)

def find_interface(meta, sysfs=None):
 sysfs=sysfs or guard.Sysfs()
 try:
  names=list(sysfs.root.iterdir())
  if len(names)>256:return None
  matches=[]
  for item in names:
   try:
    if sysfs.matches(item.name,meta['selector']):matches.append(item.name)
   except (OSError,guard.Refused,UnicodeError):pass
  return matches[0] if len(matches)==1 else None
 except OSError:return None

def run():
 host=guard.Host();host.privileges();meta=host.load(META)
 if meta['device_id']!='cp_0021' or host.identity()!=meta['device_id']:raise guard.Refused('IDENTITY_MISMATCH')
 raw=guard.secure_read(meta['config_path'],65536);old=b'allow-interfaces='+meta['interface'].encode()
 if raw.count(old)!=1:raise guard.Refused('CONFIG_INVALID')
 stop=[False]
 def handle_signal(_sig,_frame):stop[0]=True
 for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,handle_signal)
 child=reader=None;current=None;next_check=0
 try:
  while not stop[0]:
   now=time.monotonic()
   if now>=next_check:
    host.privileges()
    if host.identity()!=meta['device_id']:raise guard.Refused('IDENTITY_CHANGED')
    interface=find_interface(meta)
    if interface!=current:
     if child is not None:
      if not guard.stop_child(child):raise guard.Refused('CLEANUP_FAILED')
      child=None
      if reader is not None:reader.close();reader=None
     current=interface
     if current:
      config=raw.replace(old,b'allow-interfaces='+current.encode('ascii'))
      path='/run/avahi-daemon/headring-dynamic.conf'
      fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
      with os.fdopen(fd,'wb') as f:f.write(config)
      child=subprocess.Popen([meta['binary_path'],'--no-drop-root','--no-chroot','--no-proc-title','--file='+path],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,shell=False,close_fds=True)
      reader=guard.LogReader(child.stderr)
      print(json.dumps({'state':'QUALIFIED','interface':current}),flush=True)
     else:print(json.dumps({'state':'WAITING','reason':'USB_ABSENT_OR_AMBIGUOUS'}),flush=True)
    next_check=now+1
   if child is not None:
    if child.poll() is not None:raise guard.Refused('CHILD_EXITED')
    view=dict(meta,interface=current)
    for line in reader.read(0.2):
     state=guard.log_status(line,view)
     if state:print(json.dumps({'state':state,'interface':current}),flush=True)
   else:time.sleep(0.2)
 finally:
  if child is not None:guard.stop_child(child)
  if reader is not None:reader.close()
if __name__=='__main__':
 try:run()
 except (guard.Refused,OSError,UnicodeError,ValueError):print(json.dumps({'state':'ERROR','reason':'GUARD_REFUSED'}),flush=True);raise SystemExit(2)
