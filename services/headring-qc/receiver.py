"""Short RAM-only LAN test; always resumes the task's paused rsync processes."""
import pathlib,os,socket,signal,subprocess,json,time,sys
from host_storage import data_root
ROOT=pathlib.Path(__file__).resolve().parent;DATA=str(data_root())+'/'
allowed=sys.argv[1];label=sys.argv[2];paused=[]
def task_rsync(pid):
    try:
        a=pathlib.Path('/proc',str(pid),'cmdline').read_bytes().decode(errors='replace').split('\0')
        return pathlib.Path(a[0]).name=='rsync' and any(DATA in x for x in a)
    except (OSError,IndexError):return False
def resume():
    for pid in paused:
        try:
            if task_rsync(pid):os.kill(pid,signal.SIGCONT)
        except OSError:pass
listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);listener.bind(('192.168.1.118',18888));listener.listen(1);listener.settimeout(20)
(ROOT/'logs/netbench-ready.json').write_text(json.dumps({'pid':os.getpid(),'label':label,'at':time.time()}))
try:
    conn,addr=listener.accept();conn.settimeout(12)
    if addr[0]!=allowed or conn.recv(8)!=b'RINGTEST':raise RuntimeError('Unexpected benchmark client')
    for p in pathlib.Path('/proc').iterdir():
        if p.name.isdigit() and task_rsync(p.name):
            try:os.kill(int(p.name),signal.SIGSTOP);paused.append(int(p.name))
            except OSError:pass
    # Independent watchdog also resumes these exact task processes on interruption.
    watchdog="import os,time,signal,pathlib;time.sleep(20)\nfor pid in "+repr(paused)+":\n try:\n  a=pathlib.Path('/proc',str(pid),'cmdline').read_bytes().decode(errors='replace');\n  if 'rsync' in a and "+repr(DATA)+" in a:os.kill(pid,signal.SIGCONT)\n except OSError:pass"
    subprocess.Popen(['/usr/bin/python3','-c',watchdog],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    time.sleep(.75);conn.sendall(b'GO');total=0;t0=time.monotonic()
    while True:
        data=conn.recv(256*1024)
        if not data:break
        total+=len(data)
    elapsed=time.monotonic()-t0
    result={'label':label,'client':addr[0],'bytes':total,'seconds':elapsed,'MB_s':total/elapsed/1e6,'Mbit_s':total/elapsed/1e6*8,'mode':'RAM-only TCP; task rsync temporarily paused; no capture/data files read or written'}
    (ROOT/'logs'/('netbench-'+label+'.json')).write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
finally:resume();listener.close()
