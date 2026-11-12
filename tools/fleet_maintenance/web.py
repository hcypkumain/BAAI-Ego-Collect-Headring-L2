"""Local fleet dashboard with terminal-unlocked reviewed operations."""
import json, secrets, sys, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import web_config, jobs, fleet, operations, broker_client

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,status,data,content='application/json; charset=utf-8'):
        if not isinstance(data,bytes): data=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status); self.send_header('Content-Type',content); self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Content-Security-Policy',"default-src 'self'; frame-ancestors 'none'; base-uri 'none'"); self.end_headers(); self.wfile.write(data)
    def valid_host(self): return self.headers.get('Host')==self.server.host
    def do_GET(self):
        if not self.valid_host(): return self.send(403,{'error':'host'})
        path=urlsplit(self.path).path
        match=re.fullmatch(r'/api/assets/([a-f0-9]{64})',path)
        if match:
            key=match.group(1)
            allowed=any(item.get('key')==key for row in self.server.jobs.cache.values() for item in [*(row.get('thumbnails') or {}).values(),*(row.get('originals') or {}).values(),*(x for values in (row.get('inactive_assets') or {}).values() for x in values.values())] if isinstance(item,dict))
            if not allowed: return self.send(404,{'error':'not found'})
            try: raw=(self.server.jobs.state/'assets'/key).read_bytes()
            except OSError: return self.send(404,{'error':'not found'})
            return self.send(200,raw,'image/png' if raw.startswith(b'\x89PNG') else 'image/jpeg')
        report=re.fullmatch(r'/api/reports/(cp_\d{4})',path)
        if report:
            row=self.server.jobs.cache.get(report.group(1),{}); plan=row.get('review_plan')
            if not plan: return self.send(404,{'error':'not found'})
            return self.send(200,{'device':report.group(1),'address':row.get('address'),'profile_context':row.get('profile_context'),'plan':plan,'protected_snapshot_aggregate':(row.get('deep_check') or {}).get('calibration',{}).get('aggregate_sha256'),'note':'redacted summary; review original local plan before terminal actions'})
        if path=='/api/registry': return self.send(200,{'versions':operations.registry()})
        if path=='/api/authstatus': return self.send(200,broker_client.status(self.server.jobs.state))
        if path=='/api/jobs': return self.send(200,{'jobs':self.server.jobs.jobs})
        if path=='/api/state':
            return self.send(200,{'devices':self.server.jobs.config['devices'],'cache':self.server.jobs.cache,'jobs':self.server.jobs.jobs,'csrf':self.server.csrf,'pin':fleet.PIN})
        files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
        if path not in files: return self.send(404,{'error':'not found'})
        name,typ=files[path]; self.send(200,(Path(__file__).parent/'static'/name).read_bytes(),typ)
    def do_POST(self):
        if not self.valid_host() or self.headers.get('Origin')!='http://'+self.server.host or self.headers.get('X-CSRF-Token')!=self.server.csrf: return self.send(403,{'error':'forbidden'})
        if self.path not in ('/api/refresh','/api/check','/api/operations/plans','/api/audit','/api/operations/apply','/api/imports/scan','/api/imports/preview','/api/imports/apply','/api/thumbnails','/api/original'): return self.send(404,{'error':'not found'})
        if self.headers.get('Content-Type')!='application/json': return self.send(415,{'error':'content type'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<1 or length>(4096 if self.path=='/api/imports/preview' else 512): raise ValueError()
            obj=json.loads(self.rfile.read(length))
            if not isinstance(obj,dict) or not isinstance(obj.get('device'),str): raise ValueError()
            if self.path=='/api/imports/scan':
                if set(obj)!={'device'}:raise ValueError()
                jid=self.server.jobs.usb_read(obj['device'],'usb_scan')
            elif self.path=='/api/imports/preview':
                if set(obj)!={'device','scan_id','zip_ids'}:raise ValueError()
                jid=self.server.jobs.usb_read(obj['device'],'usb_preview',obj['scan_id'],obj['zip_ids'])
            elif self.path=='/api/imports/apply':
                if set(obj)!={'device','plan_hash','typed_device','mode','keep_old_lists'}:raise ValueError()
                jid=self.server.jobs.import_apply(obj['device'],obj['plan_hash'],obj['typed_device'],obj['mode'],obj['keep_old_lists'])
            elif self.path=='/api/operations/apply':
                if set(obj)!={'device','plan_hash','typed_device','registry_entry'}: raise ValueError()
                jid=self.server.jobs.apply(obj['device'],obj['plan_hash'],obj['typed_device'],obj['registry_entry'])
            elif self.path in ('/api/thumbnails','/api/original'):
                if set(obj) not in ({'device','ids'},{'device','ids','mode'}): raise ValueError()
                jid=self.server.jobs.thumbnails(obj['device'],obj['ids'],self.path=='/api/original',obj.get('mode'))
            else:
                if set(obj)!={'device'}: raise ValueError()
                jid=self.server.jobs.refresh(obj['device'], 'check' if self.path=='/api/check' else 'plan' if self.path=='/api/operations/plans' else 'audit' if self.path=='/api/audit' else 'refresh')
        except PermissionError: return self.send(423,{'error':'broker locked'})
        except (ValueError,TypeError): return self.send(400,{'error':'invalid request'})
        except RuntimeError: return self.send(409,{'error':'job busy'})
        self.send(202,{'job':jid})

def main():
    config=web_config.setup()
    try: owner=jobs.Jobs(web_config.STATE,config)
    except BlockingIOError: sys.exit('UI already running for this user')
    port=int(sys.argv[1]) if len(sys.argv)>1 else 0
    if not 0<=port<=65535: sys.exit('invalid port')
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.host='127.0.0.1:%d'%server.server_port; server.jobs=owner; server.csrf=secrets.token_urlsafe(32)
    print('http://'+server.host+'/',flush=True)
    server.serve_forever()
if __name__=='__main__': main()
