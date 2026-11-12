"""Short-lived local-only credential broker. Launch manually in a terminal."""
import argparse, fcntl, getpass, json, os, re, signal, socket, stat, struct, sys, time
from pathlib import Path
if __name__=='__main__':
    for root in (Path(__file__).resolve().parents[2],Path(__file__).resolve().parent):
        sys.path.insert(0,str(root))
import fleet, online, operations, web_config, imports

MAX_REQUEST=4096

def peer_uid(sock): return struct.unpack('3i',sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]
def receive(sock):
    data=b''
    while b'\n' not in data and len(data)<=MAX_REQUEST:
        part=sock.recv(MAX_REQUEST+1-len(data))
        if not part: break
        data+=part
    if len(data)>MAX_REQUEST or b'\n' not in data: raise ValueError('invalid_request')
    return json.loads(data.split(b'\n',1)[0])
def send(sock,value): sock.sendall(json.dumps(value,separators=(',',':')).encode()+b'\n')

class AuthSSH(online.SSH):
    def __init__(self,config,device,address=None,bind_interface=None,bind_address=None,*,registry,secret):
        expected=fleet.registry_device_identity(registry,device)
        if address!=expected['address']:raise online.Failure('registry address mismatch')
        transport=fleet.registry_ssh(online,registry,device,config,bind_interface,bind_address)
        self.device=expected['ssh_alias'];self.argv=transport.argv;self.secret=secret
    def require_tty(self): pass
    def run(self,command,data=None,interactive=False):
        if not interactive: return super().run(command,data,False)
        import pexpect
        argv=self.argv[:-1]+['-o','BatchMode=yes','-T',self.argv[-1],online.sudo_stdin_command(command)]
        child=pexpect.spawn(argv[0],argv[1:],encoding='utf-8',codec_errors='replace',echo=False,timeout=None)
        child.logfile=None; sent=False
        try:
            while True:
                n=child.expect([re.escape(online.SUDO_PROMPT),r'(?i)permission denied',r'(?i)password:',pexpect.EOF,pexpect.TIMEOUT])
                if n==0 and not sent:
                    sent=True; child.sendline(self.secret.decode()); continue
                if n==3:
                    child.close()
                    if child.exitstatus==0: return b''
                    raise online.Failure('root_transaction_failed')
                raise online.Failure('authentication_or_timeout')
        finally:
            if child.isalive(): child.close(force=True)

class Broker:
    def __init__(self,state,config,scope,ttl,secret,executor=operations.execute,operation="apply_plan",*,registry):
        operations.require_config(config,registry)
        if not set(scope)<=set(config['devices']):raise ValueError('unselected authorization scope')
        self.registry=registry;self.state=Path(state); self.config=json.loads(json.dumps(config)); self.scope=dict(scope); self.deadline=time.monotonic()+ttl; self.expires=time.time()+ttl
        self.secret=bytearray(secret); self.executor=executor; self.operation=operation; self.failed=False; self.stop_requested=False
    def allowed(self): return not self.failed and not self.stop_requested and time.monotonic()<self.deadline
    def status(self): return {'unlocked':self.allowed(),'expires_at':self.expires if self.allowed() else None,'targets':sorted(self.scope) if self.allowed() else [],'plans':dict(self.scope) if self.allowed() else {},'operation':self.operation,'registry':operations.REGISTRY if self.operation=='apply_plan' else imports.REGISTRY,'registry_digest':self.registry.digest}
    def handle(self,obj,uid,emit=lambda *_:None):
        if uid!=os.getuid() or not isinstance(obj,dict): return {'error':'forbidden'}
        if obj=={'operation':'status'}: return self.status()
        if set(obj)!={'operation','device','plan_hash','registry_entry'} or obj.get('operation')!=self.operation or obj.get('registry_entry')!=(operations.REGISTRY if self.operation=='apply_plan' else imports.REGISTRY): return {'error':'invalid_request'}
        device=obj.get('device'); digest=obj.get('plan_hash')
        if not self.allowed() or not isinstance(device,str) or not isinstance(digest,str) or self.scope.get(device)!=digest: return {'error':'locked_or_out_of_scope'}
        # The local overlay cannot drift after authorization.
        try:
            operations.require_config(self.config,self.registry)
            factory=lambda *args:AuthSSH(*args,registry=self.registry,secret=self.secret)
            def progress(stage):
                if stage=='applying' and not self.allowed(): raise ValueError('authorization_expired')
                try: emit({'event':stage})
                except OSError: pass
            result=(self.executor(self.state,self.config,device,digest,factory,progress,registry=self.registry) if self.operation=='apply_plan' else self.executor(self.state,self.config,device,digest,progress))
            return {'result':result}
        except operations.PartialFailure:
            self.failed=True; return {'error':'partial_failed_reconcile_required'}
        except Exception:
            self.failed=True; return {'error':'preflight_or_auth_failed'}
    def clear(self):
        for i in range(len(self.secret)): self.secret[i]=0
        self.failed=True

def serve(state,broker):
    state=Path(state); path=state/'broker.sock'; lock=open(state/'broker.lock','a+');os.chmod(state/'broker.lock',0o600)
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        lock.close();broker.clear();raise
    # Never unlink a pre-existing socket, even a stale or foreign one.
    sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); owned=None
    try:
        sock.bind(str(path));os.chmod(path,0o600); owned=os.lstat(path); sock.listen(4);sock.settimeout(1)
        while broker.allowed():
            try: conn,_=sock.accept()
            except socket.timeout: continue
            with conn:
                conn.settimeout(5)
                try: send(conn,broker.handle(receive(conn),peer_uid(conn),lambda event:send(conn,event)))
                except Exception:
                    try:send(conn,{'error':'invalid_request'})
                    except OSError:pass
    finally:
        sock.close();broker.clear()
        try:
            current=os.lstat(path)
            if owned and (current.st_dev,current.st_ino)==(owned.st_dev,owned.st_ino):os.unlink(path)
        except FileNotFoundError: pass
        lock.close()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--registry',type=Path,required=True);p.add_argument('--transport-config',type=Path,required=True);p.add_argument('--state-dir',type=Path,default=web_config.STATE);p.add_argument('--targets',nargs='+',required=True,type=fleet.target);p.add_argument('--ttl',type=int,default=600);p.add_argument('--operations',choices=('apply_plan','import_tasks'),default='apply_plan');a=p.parse_args()
    if not 1<=a.ttl<=600: p.error('ttl must be 1..600 seconds')
    if a.state_dir.resolve()!=web_config.STATE.resolve(): p.error('state directory must match UI state')
    registry,config=operations.load_inputs(a.registry,a.targets,a.transport_config)
    cache=json.loads((a.state_dir/'cache.json').read_text());scope={}
    for device in a.targets:
        item=cache.get(device,{}).get('review_plan' if a.operations=='apply_plan' else 'import_plan') or {};digest=item.get('plan_sha256')
        if device not in config['devices'] or cache.get(device,{}).get('address')!=config['devices'][device] or item.get('address')!=config['devices'][device] or not re.fullmatch(r'[a-f0-9]{64}',digest or '') or time.time()-item.get('created_at',0)>600: p.error('create a fresh reviewed plan for each target first')
        scope[device]=digest
        if a.operations=='apply_plan':fleet.require_registry_plan(online.load_plan(a.state_dir,digest),registry=registry,device=device)
    if not sys.stdin.isatty() or not sys.stdout.isatty(): p.error('local terminal required')
    if a.operations=='apply_plan':
        password=getpass.getpass('Remote sudo password (memory only, no SSH password): ')
        if not password: p.error('empty password')
        secret=password.encode();password=None
    else:
        if input('Authorize fixed USB import plans? Type UNLOCK: ')!='UNLOCK': p.error('authorization cancelled')
        secret=b''
    instance=Broker(a.state_dir,config,scope,a.ttl,secret,operations.execute if a.operations=='apply_plan' else imports.execute,operation=a.operations,registry=registry); secret=None
    def request_stop(*_): instance.stop_requested=True
    signal.signal(signal.SIGINT,request_stop); signal.signal(signal.SIGTERM,request_stop
    )
    print('Broker active for '+', '.join(scope)+'; Ctrl-C locks after an active transaction completes.',flush=True)
    serve(a.state_dir,instance)
if __name__=='__main__':main()
