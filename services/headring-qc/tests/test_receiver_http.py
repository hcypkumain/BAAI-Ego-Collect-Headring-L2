import http.server,json
from pathlib import Path
import sys,tempfile,threading,types,unittest,urllib.error,urllib.request,uuid
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT/'services/headring-qc'))
from receiver_state import ReceiverStore
import receiver_http

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=ReceiverStore(Path(self.temp.name)/'ledger');receiver=types.SimpleNamespace(store=self.store,wake=threading.Event(),snapshot=lambda:{'control':self.store.control(),'jobs':[]})
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(s):
                if not receiver_http.get(s,receiver):s.send_error(404)
            def do_POST(s):
                if not receiver_http.post(s,receiver):s.send_error(404)
            def log_message(s,*args):pass
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url='http://127.0.0.1:'+str(self.server.server_port);self.cookie='';self.csrf=''
    def call(self,path,body=None,origin=None,csrf=None):
        headers={'Origin':origin or self.url,'Content-Type':'application/json'}
        if self.cookie:headers['Cookie']=self.cookie
        if csrf is not None:headers['X-CSRF-Token']=csrf
        q=urllib.request.Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with urllib.request.urlopen(q,timeout=3) as r:return r.status,json.load(r),r.headers
        except urllib.error.HTTPError as r:return r.code,json.load(r),r.headers
    def login(self):
        status,value,headers=self.call('/api/receive/login',{'key':self.store.admin_key});self.assertEqual(status,200)
        self.cookie=headers['Set-Cookie'].split(';')[0];self.csrf=value['csrf']
        self.assertIn('HttpOnly',headers['Set-Cookie']);self.assertIn('SameSite=Strict',headers['Set-Cookie'])
    def test_no_login_or_csrf_no_control(self):
        body={'receiving':True,'revision':0,'operation_id':str(uuid.uuid4())}
        self.assertEqual(self.call('/api/receive/control',body)[0],403)
        self.login();self.assertEqual(self.call('/api/receive/control',body)[0],403)
        self.assertFalse(self.store.control()['receiving'])
    def test_foreign_origin_rejected(self):
        self.login();body={'receiving':True,'revision':0,'operation_id':str(uuid.uuid4())}
        self.assertEqual(self.call('/api/receive/control',body,origin='http://other-site',csrf=self.csrf)[0],403)
        self.assertFalse(self.store.control()['receiving'])
    def test_authorized_start_stop_and_stale_browser(self):
        self.login();on={'receiving':True,'revision':0,'operation_id':str(uuid.uuid4())}
        self.assertEqual(self.call('/api/receive/control',on,csrf=self.csrf)[0],200)
        off={'receiving':False,'revision':1,'operation_id':str(uuid.uuid4())}
        self.assertEqual(self.call('/api/receive/control',off,csrf=self.csrf)[0],200)
        stale={'receiving':True,'revision':1,'operation_id':str(uuid.uuid4())}
        self.assertEqual(self.call('/api/receive/control',stale,csrf=self.csrf)[0],409)
        self.assertFalse(self.store.control()['receiving'])
    def test_frontend_cannot_claim_cloud_success_or_sha_pass(self):
        self.login();body={'receiving':True,'revision':0,'operation_id':str(uuid.uuid4()),'cloud_state':'remote_confirmed','sha_passed':True}
        self.assertEqual(self.call('/api/receive/control',body,csrf=self.csrf)[0],400)
        self.assertFalse(self.store.control()['receiving'])

if __name__=='__main__':unittest.main(verbosity=2)
