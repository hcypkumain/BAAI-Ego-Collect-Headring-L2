import fcntl, http.client, importlib.util, json, os, sys, tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import web, jobs, metrics
from http.server import ThreadingHTTPServer

class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config={'devices':{'cp_0014':'192.168.20.111'}}
        self.owner=jobs.Jobs(self.temp.name,self.config)
        self.addCleanup(self.owner.lockfile.close)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),web.Handler)
        self.server.host='127.0.0.1:%d'%self.server.server_port; self.server.csrf='test-token'; self.server.jobs=self.owner
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
    def req(self,method,path,headers=None,body=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
        c.request(method,path,body=body,headers=headers or {}); r=c.getresponse(); data=r.read(); c.close(); return r.status,data
    def test_static_and_host_traversal(self):
        for path in ('/','/app.js','/style.css','/api/state'): self.assertEqual(self.req('GET',path)[0],200)
        self.assertEqual(self.req('GET','/../web.py')[0],404)
        self.assertEqual(self.req('GET','/%2e%2e/web.py')[0],404)
        self.assertEqual(self.req('GET','/',{'Host':'evil.test'})[0],403)
    def test_origin_csrf_and_no_apply(self):
        h={'Origin':'http://'+self.server.host,'X-CSRF-Token':'test-token','Content-Type':'application/json'}
        self.assertEqual(self.req('POST','/api/refresh',{},'{}')[0],403)
        self.assertEqual(self.req('POST','/api/apply',h,'{}')[0],404)
        self.assertEqual(self.req('POST','/api/refresh',{**h,'Origin':'http://evil.test'},'{}')[0],403)
        self.assertEqual(self.req('POST','/api/refresh',{**h,'X-CSRF-Token':'bad'},'{}')[0],403)
        self.assertEqual(self.req('POST','/api/refresh',h,'{"device":"cp_9999"}')[0],400)
    def test_owner_flock(self):
        with self.assertRaises(BlockingIOError): jobs.Jobs(self.temp.name,self.config)
    def test_identity_mismatch_does_not_replace_data(self):
        self.owner.cache['cp_0014']={'identity':'cp_0014','history_cache':{'saved':3}}
        with patch.object(metrics,'collect',return_value={'identity_mismatch':True,'checked_at':1}):
            self.owner.refresh('cp_0014')
            self.owner.guard.acquire(timeout=2); self.owner.guard.release()
        row=self.owner.cache['cp_0014']; self.assertTrue(row['identity_mismatch']); self.assertFalse(row['online']); self.assertEqual(row['history_cache']['saved'],3)
    def test_only_explicit_refresh_uses_probe(self):
        with patch.object(metrics,'collect') as collect:
            self.req('GET','/api/state'); collect.assert_not_called()
    def test_probe_saved_semantics(self):
        spec=importlib.util.spec_from_file_location('probe',ROOT/'resources/metrics_probe.py'); probe=importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)
        row={'retained':True,'has_data':True,'status':'pending_qc'}
        self.assertTrue(probe.saved(row)); self.assertFalse(probe.saved({**row,'source_missing':True})); self.assertFalse(probe.saved({**row,'status':'discarded'}))
    def test_no_remote_secrets_in_probe_output_schema(self):
        text=(ROOT/'resources/metrics_probe.py').read_text()
        self.assertNotIn("'token':",text); self.assertNotIn('os.environ',text)

if __name__=='__main__': unittest.main()
