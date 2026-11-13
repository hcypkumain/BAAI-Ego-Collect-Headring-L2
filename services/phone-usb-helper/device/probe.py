#!/usr/bin/python3
"""Read-only USB identity and installer, bound only to the qualified address."""
import hashlib
import importlib.util
import ipaddress
import json
import os
import select
import signal
import socket
import stat
import subprocess
import time

BASE='/opt/headring-usb-entry-persistent'
META='/opt/headring-usb-entry-proof/proof-20261113-b524dab0/metadata.json'
spec=importlib.util.spec_from_file_location('dynamic_guard',BASE+'/dynamic_guard.py')
dynamic=importlib.util.module_from_spec(spec);spec.loader.exec_module(dynamic);guard=dynamic.guard
host=guard.Host(); host.privileges(); meta=host.load(META)
if host.identity()!=meta['device_id'] or meta['device_id']!='cp_0021': raise SystemExit(2)
stop=False

def quit_handler(_sig,_frame):
 global stop
 stop=True
for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,quit_handler)

def qualified_address():
 try:
  interface=dynamic.find_interface(meta)
  if host.identity()!=meta['device_id'] or not interface:return None
  p=subprocess.run(['/usr/sbin/ip','-j','-4','address','show','dev',interface],capture_output=True,text=True,timeout=2)
  if p.returncode:return None
  addresses=[x['local'] for row in json.loads(p.stdout) for x in row.get('addr_info',[]) if x.get('family')=='inet']
  if len(addresses)!=1:return None
  addr=ipaddress.ip_address(addresses[0])
  return str(addr) if addr.version==4 and any(addr in ipaddress.ip_network(x) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')) else None
 except (OSError,ValueError,KeyError,subprocess.TimeoutExpired):return None

def handle(c):
 deadline=time.monotonic()+1;request=b''
 while b'\r\n\r\n' not in request and len(request)<4096:
  remaining=deadline-time.monotonic()
  if remaining<=0:return
  c.settimeout(remaining);part=c.recv(1024)
  if not part:return
  request+=part
 if b'\r\n\r\n' not in request:return
 line=request.split(b'\r\n',1)[0]
 if line==b'GET /.well-known/headring HTTP/1.1':
  body=json.dumps({'schema':1,'device_id':host.identity(),'http_port':8765},separators=(',',':')).encode();kind='application/json'
 elif line==b'GET /headring-helper.apk HTTP/1.1':
  path=BASE+'/headring-helper.apk';fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW);info=os.fstat(fd)
  if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or info.st_size>16*1024*1024:os.close(fd);return
  with os.fdopen(fd,'rb') as f:body=f.read()
  kind='application/vnd.android.package-archive'
 else:
  c.sendall(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n');return
 hdr=('HTTP/1.1 200 OK\r\nContent-Type: '+kind+'\r\nContent-Length: '+str(len(body))+'\r\nCache-Control: no-store\r\nConnection: close\r\nX-Content-Type-Options: nosniff\r\n\r\n').encode();c.sendall(hdr+body)

listener=None;current=None
while not stop:
 address=qualified_address()
 if address!=current:
  if listener:listener.close();listener=None
  current=address
  if address:
   try:listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);listener.bind((address,8766));listener.listen(8);print('LISTEN '+address,flush=True)
   except OSError:
    if listener:listener.close();listener=None
    current=None
 if listener:
  try:
   ready,_,_=select.select([listener],[],[],1)
   if ready:
    c,_=listener.accept()
    if qualified_address()!=current:
     c.close();continue
    with c:
     try:handle(c)
     except (OSError,ValueError):pass
  except OSError:listener.close();listener=None;current=None
 else:time.sleep(1)
if listener:listener.close()
