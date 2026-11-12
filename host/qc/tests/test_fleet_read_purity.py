import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
for rel in ('orangepi/src/orangepi_collect/control','host/qc/fleet_monitor'):sys.path.insert(0,str(ROOT/rel))
from read_model import ReadModel
from server import Hub,Handler,History


class StreamPurity(unittest.TestCase):
    def test_actual_sse_initial_reset_without_telemetry(self):
        model=ReadModel(['cp_0015'])
        with patch.object(History,'__init__',side_effect=AssertionError('History')):
            hub=Hub.read_only(model)
        hub.touch=lambda:(_ for _ in ()).throw(AssertionError('legacy touch'))
        class Stream(Handler):pass
        Stream.hub=hub
        server=ThreadingHTTPServer(('127.0.0.1',0),Stream)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            before=(model.revision,dict(model.issued))
            with patch.object(Hub,'start',side_effect=AssertionError('start')),patch.object(History,'sync',side_effect=AssertionError('sync')):
                c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                c.request('GET','/api/v1/fleet/events',headers={'Last-Event-ID':'retired:99'})
                r=c.getresponse();self.assertEqual(r.status,200)
                lines=[r.fp.readline().decode() for _ in range(4)]
                self.assertIn('id: '+model.event_id,lines[0])
                self.assertEqual(lines[1],'event: reset\n')
                self.assertIn(model.host_instance,lines[2])
                self.assertEqual(before,(model.revision,model.issued))
                r.close();c.close()
        finally:
            hub.stop.set();server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':unittest.main()
