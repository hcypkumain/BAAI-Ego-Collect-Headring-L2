import copy
import ast
import hmac
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs
from http.server import BaseHTTPRequestHandler
from contextlib import ExitStack
import http.client
from http.server import ThreadingHTTPServer, HTTPServer
import io
import builtins
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
for path in ('orangepi/src/orangepi_collect/control','host/qc/fleet_monitor','tools/fleet_maintenance'):sys.path.insert(0,str(ROOT/path))
from snapshot_contract import loads, validate
from snapshot_publisher import SnapshotPublisher
from snapshot_http import snapshot_get
from read_model import ReadModel
import server as fleet_server
import web


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.clock=[100000]
        self.model=ReadModel(['cp_0014','cp_0015','cp_0018','cp_0020','cp_0022'],clock=lambda:self.clock[0])
        self.value=loads((ROOT/'contracts/v1/fixtures/device.json').read_bytes())

    def accept(self,value=None,ticket=None):
        return self.model.accept(ticket or self.model.begin_observation('cp_0015'),json.dumps(value or self.value).encode(),identity_verified=True,snapshot_age_ms=0)

    def test_order_reboot_late_retired_and_reset(self):
        self.assertTrue(self.accept())
        self.assertFalse(self.accept())
        late=self.model.begin_observation('cp_0015')
        new=copy.deepcopy(self.value);new['controller_instance']='77777777-7777-4777-8777-777777777777';new['seq']=1
        self.assertTrue(self.accept(new))
        self.value['seq']=99
        self.assertFalse(self.accept(ticket=late))
        self.assertFalse(self.accept())  # Retired instance even with a new ticket.
        snap=self.model.snapshot();validate(snap,'host')
        restored=ReadModel(list(self.model.rows),restored=snap,clock=lambda:self.clock[0])
        row=restored.rows['cp_0015']
        self.assertIsNone(row['last_seen_age_ms']);self.assertEqual(row['connectivity'],'unknown')
        self.assertEqual(row['snapshot']['sample']['quality'],'stale')
        self.assertIsNone(row['snapshot']['sample']['age_ms'])

    def test_missing_response_age_and_untrusted_order(self):
        old=self.model.begin_observation('cp_0015')
        newer=self.model.begin_observation('cp_0015')
        self.assertTrue(self.model.accept(newer,json.dumps(self.value),identity_verified=True))
        self.assertIsNone(self.model.snapshot()['devices'][1]['snapshot']['sample']['age_ms'])
        other=copy.deepcopy(self.value);other['controller_instance']='77777777-7777-4777-8777-777777777777'
        self.assertFalse(self.model.accept(old,json.dumps(other),identity_verified=True,snapshot_age_ms=0))
        self.assertFalse(self.model.accept(self.model.begin_observation('cp_0015'),json.dumps(other),identity_verified=False,snapshot_age_ms=0))

    def test_offline_not_stopped_sse_overflow_and_source_error(self):
        self.value['recording_active']=True;self.accept()
        q=self.model.subscribe()
        for _ in range(20):self.model.unavailable('cp_0015')
        self.assertIn('reset',[q.get_nowait()['event'] for _ in range(q.qsize())])
        self.model.unsubscribe(q)
        self.assertTrue(self.model.rows['cp_0015']['snapshot']['recording_active'])
        self.model.source_unavailable()
        self.assertEqual(self.model.snapshot()['pipeline']['reason'],'SOURCE_UNAVAILABLE')
        self.clock[0]+=20000
        self.assertEqual(self.model.snapshot()['devices'][1]['snapshot']['sample']['quality'],'stale')

    def test_vertical_device_hub_both_http_consumers_pure(self):
        publisher=SnapshotPublisher(self.value['device_id'],self.value['boot_id'],self.value['controller_instance'],clock=lambda:self.clock[0])
        item=publisher.publish(self.value)
        tree=ast.parse((ROOT/'orangepi/src/orangepi_collect/control/dashboard.py').read_text())
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name=='Handler')
        context=dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler,
                     owner=SimpleNamespace(snapshot_publisher=publisher,access_code_required=True,token='synthetic-test'),
                     snapshot_get=snapshot_get,hmac=hmac,urlsplit=urlsplit,parse_qs=parse_qs)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'dashboard.py','exec'),context)
        device_http=ThreadingHTTPServer(('127.0.0.1',0),context['Handler'])
        threading.Thread(target=device_http.serve_forever,daemon=True).start()
        self.addCleanup(device_http.server_close);self.addCleanup(device_http.shutdown)
        ticket=self.model.begin_observation('cp_0015')
        connection=http.client.HTTPConnection('127.0.0.1',device_http.server_port,timeout=3)
        connection.request('GET','/api/v1/snapshot',headers={'X-Access-Token':'synthetic-test'})
        response=connection.getresponse();self.assertEqual(response.status,200)
        raw=response.read();age=int(response.headers['X-Snapshot-Age-Ms']);connection.close()
        self.assertEqual(raw,item.body)
        with patch.object(fleet_server.History,'__init__',side_effect=AssertionError('History constructor')),patch.object(fleet_server.Hub,'start',side_effect=AssertionError('Hub.start')):
            self.model.accept(ticket,raw,identity_verified=True,snapshot_age_ms=age)
            hub=fleet_server.Hub.read_only(self.model)
        hub.touch=lambda:(_ for _ in ()).throw(AssertionError('legacy touch'))
        class Fleet(fleet_server.Handler):pass
        Fleet.hub=hub
        servers=[]
        for handler in (Fleet,web.Handler):
            service=ThreadingHTTPServer(('127.0.0.1',0),handler)
            service.host='127.0.0.1:%d'%service.server_port
            service.fleet_read_model=self.model
            service.process_request=HTTPServer.process_request.__get__(service)
            threading.Thread(target=service.serve_forever,daemon=True).start()
            servers.append(service);self.addCleanup(service.server_close);self.addCleanup(service.shutdown)
        original=socket.socket.connect
        def local(sock,address):
            if address[0]!='127.0.0.1' or address[1] not in [s.server_port for s in servers]:raise AssertionError('outbound')
            return original(sock,address)
        def get(s,path='/api/v1/fleet'):
            c=http.client.HTTPConnection('127.0.0.1',s.server_port,timeout=3);c.request('GET',path);r=c.getresponse();status,body=r.status,r.read();c.close();return status,loads(body)
        with ExitStack() as spies:
            for owner,name in ((os,'scandir'),(os,'statvfs'),(builtins,'open'),(io,'open'),(subprocess,'run'),(subprocess,'Popen'),(threading.Thread,'start'),(fleet_server.History,'sync')):
                spies.enter_context(patch.object(owner,name,side_effect=AssertionError(name)))
            spies.enter_context(patch.object(socket.socket,'connect',local))
            before=(self.model.revision,copy.deepcopy(self.model.rows))
            for _ in range(100):
                a=get(servers[0]);b=get(servers[1]);self.assertEqual(a,b);self.assertEqual(a[0],200)
                self.assertEqual(get(servers[0],'/api/v1/fleet?x=1')[0],400)
            self.assertEqual(before,(self.model.revision,self.model.rows))
            self.model.source_unavailable()
            validate(get(servers[0])[1],'host')
        # Complete the same vertical chain through the actual JS renderer and
        # both actual consumer entry functions, using these HTTP response bytes.
        with tempfile.TemporaryDirectory(prefix='v1-ui-',dir='/tmp/opencode') as temp:
            fixture=Path(temp)/'fleet.json';fixture.write_text(json.dumps(a[1]))
            result=subprocess.run(['node',str(ROOT/'host/qc/tests/test_fleet_cards.cjs'),str(fixture)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
