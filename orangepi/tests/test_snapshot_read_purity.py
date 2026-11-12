import builtins
import ast
import hmac
from types import SimpleNamespace
from urllib.parse import parse_qs,urlsplit
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'orangepi/src/orangepi_collect/control'))
from snapshot_publisher import SnapshotPublisher
from snapshot_http import snapshot_get


class ReadPurity(unittest.TestCase):
    def test_route_actual_http_and_100_reads_spies(self):
        clock=[100000]
        publisher=SnapshotPublisher('cp_0015','11111111-1111-4111-8111-111111111111',clock=lambda:clock[0])
        privacy=Mock();privacy.display.side_effect=AssertionError('privacy')
        demands=Mock();demands.touch.side_effect=AssertionError('demand')
        # Compile the REAL nested Dashboard Handler without importing hardware
        # dependencies or running Dashboard.__init__ (which creates token/files).
        source=(ROOT/'orangepi/src/orangepi_collect/control/dashboard.py').read_text()
        tree=ast.parse(source)
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name=='Handler')
        owner=SimpleNamespace(snapshot_publisher=publisher,access_code_required=True,token='synthetic-test',privacy=privacy,demands=demands)
        context=dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler,owner=owner,snapshot_get=snapshot_get,hmac=hmac,urlsplit=urlsplit,parse_qs=parse_qs)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'dashboard.py','exec'),context)
        Handler=context['Handler']
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def request(path='/api/v1/snapshot',token=True,etag=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            h={'X-Access-Token':'synthetic-test'} if token else {}
            if etag:h['If-None-Match']=etag
            c.request('GET',path,headers=h);r=c.getresponse();result=(r.status,r.read(),dict(r.getheaders()));c.close();return result
        self.assertEqual(request()[0],503)
        item=publisher.publish();before=(publisher.seq,publisher.read())
        original_connect=socket.socket.connect
        def local_only(sock,address):
            if address[0]!='127.0.0.1' or address[1]!=server.server_port:raise AssertionError('outbound socket')
            return original_connect(sock,address)
        with ExitStack() as spies:
            for owner,name in ((os,'scandir'),(os,'statvfs'),(builtins,'open'),(io,'open'),(subprocess,'Popen'),(subprocess,'run'),(threading.Thread,'start')):
                spies.enter_context(patch.object(owner,name,side_effect=AssertionError(name)))
            spies.enter_context(patch.object(socket.socket,'connect',local_only))
            # ThreadingHTTPServer starts a request thread; permit just that HTTP
            # infrastructure, not route-created telemetry threads, via sync server.
            from http.server import HTTPServer
            server.process_request=HTTPServer.process_request.__get__(server)
            for index in range(100):
                clock[0]+=100
                status,body,headers=request(etag=item.etag if index%2 else None)
                self.assertEqual(status,304 if index%2 else 200)
                self.assertEqual(int(headers['X-Snapshot-Age-Ms']),(index+1)*100)
                self.assertEqual(request(token=False)[0],401)
                self.assertEqual(request('/api/v1/snapshot?')[0],400)
            self.assertEqual((publisher.seq,publisher.read()),before)
        privacy.display.assert_not_called();demands.touch.assert_not_called()

    def test_dashboard_dispatch_precedes_legacy(self):
        # Actual source integration is additionally exercised by Dashboard suite.
        text=(ROOT/'orangepi/src/orangepi_collect/control/dashboard.py').read_text()
        body=text[text.index('            def do_GET(self):'):]
        self.assertLess(body.index('snapshot_get('),body.index('handle_phone_request('))


if __name__=='__main__':unittest.main()
