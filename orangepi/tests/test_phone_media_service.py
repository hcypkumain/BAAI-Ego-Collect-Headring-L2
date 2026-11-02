"""Phone APIs survive terminal retirement; no sensors or live services used."""
import io,json,struct,sys,tempfile,threading,unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'orangepi/src/orangepi_collect/common'))
sys.path.insert(0,str(ROOT/'orangepi/src/orangepi_collect/control'))
sys.path.insert(0,str(ROOT/'services/phone-media'))
import phone_media as media
import headphone_http,recording_replay,reference_credentials,wrist_preview_transport as transport

JPEG=b'\xff\xd8synthetic-preview'
TOKEN='test-media-token-not-a-credential'

class Core:
    def __init__(self):
        self.phone_quality='fast';self.condition=threading.Condition();self.selected=None;self.generation=0;self.encode_ms=0;self.frames=0
    def health(self):return {'selected':self.selected}
    def release(self):self.selected=None
    def select(self,name):self.selected=name;self.generation+=1;self.frames=0;return self.generation
    def frame(self,name,generation,after,wait):
        self.frames+=1
        if self.frames==1:return 200,JPEG,'phone:1'
        self.selected=None;return 503,b'',None

class PhoneMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        data=self.root/'data';take=data/'shift/take';take.mkdir(parents=True)
        (take/'capture_summary.json').write_text('{"status":"success","qc_level":"WARN"}')
        (take/'status.json').write_text('{"state":"success","recording_active":false}')
        (take/'stop_gate.json').write_text('{"recording_active":false}')
        self.replay=recording_replay.RecordingReplay(data)
        runtime=SimpleNamespace(backend=lambda:self.replay,health=lambda:{'enabled':True})
        reference=SimpleNamespace(get=lambda *args,**kwargs:(JPEG,'reference:1'),get_detail=lambda *args:(JPEG,'reference:1'))
        headphones=SimpleNamespace(snapshot=lambda:{'audio_ready':True},submit=lambda *args:(202,''))
        self.server=media.PreviewHTTPServer(('127.0.0.1',0),Core(),TOKEN,reference=reference,headphones=headphones,replay_runtime=runtime)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
    def stop(self):self.server.shutdown();self.server.server_close();self.thread.join(2)
    def request(self,path,token=TOKEN):
        connection=HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
        connection.request('GET',path,headers={'X-Access-Token':token});response=connection.getresponse();data=response.read();code=response.status;connection.close();return code,data
    def test_phone_source_preview_is_forwarded_unchanged(self):
        code,data=self.request('/phone/preview/cam_ros_left.jpg?quality=source')
        self.assertEqual((code,data),(200,JPEG))
    def test_phone_backend_health_keeps_feature_status(self):
        code,data=self.request('/health');self.assertEqual(code,200)
        status=json.loads(data);self.assertTrue(status['phone_source_preview']);self.assertTrue(status['recording_replay']['enabled'])
    def test_phone_stream_still_uses_framed_jpegs(self):
        code,data=self.request('/phone/stream/cam_ros_left.jpg?quality=source')
        self.assertEqual(code,200);size=struct.unpack('!I',data[:4])[0]
        meta=json.loads(data[4:4+size]);self.assertEqual(meta['frame_id'],'phone:1');self.assertEqual(data[4+size:],JPEG)
    def test_reference_headphones_and_history_remain_available(self):
        self.assertEqual(self.request('/reference.jpg')[1],JPEG)
        self.assertTrue(json.loads(self.request('/headphones')[1])['audio_ready'])
        code,data=self.request('/replay/sessions');self.assertEqual(code,200);self.assertEqual(json.loads(data)['total'],1)
    def test_terminal_only_routes_are_removed(self):
        for path in ('/preview/cam_ros_left.jpg','/calibration/wrist/cam_left_wrist.jpg'):
            with self.subTest(path=path):self.assertEqual(self.request(path)[0],404)
    def test_phone_media_still_requires_the_access_token(self):
        self.assertEqual(self.request('/phone/preview/cam_ros_left.jpg',token='wrong')[0],401)
    def test_former_direct_link_has_no_trusted_peer_bypass(self):
        for peers in (headphone_http.READ_PEERS,headphone_http.WRITE_PEERS,recording_replay.READ_PEERS,reference_credentials.TRUSTED_PEERS):
            self.assertEqual(peers,frozenset(('127.0.0.1','::1')))
    def test_high_rate_wrist_transport_keeps_source_jpeg(self):
        clock=[10.];directory=self.root/'ipc';lease=transport.LeaseWriter(directory,clock=lambda:clock[0]);producer=transport.Producer(directory,clock=lambda:clock[0])
        self.addCleanup(lease.close);self.addCleanup(producer.close)
        epoch=lease.select('cam_left_wrist');producer.tick();self.assertTrue(producer.offer('cam_left_wrist',JPEG))
        clock[0]+=.05;producer.tick();reader=transport.Reader(directory,clock=lambda:clock[0]);value=reader.read('cam_left_wrist',epoch)
        self.assertIsNotNone(value);self.assertEqual(value[0],JPEG)
    def test_producer_and_viewer_use_the_same_phone_transport(self):
        producer=ROOT/'orangepi/src/orangepi_collect/common/wrist_preview_transport.py'
        viewer=ROOT/'services/phone-media/wrist_preview_transport.py'
        self.assertEqual(producer.read_bytes(),viewer.read_bytes())
        self.assertEqual(transport.DEFAULT_DIRECTORY,'/dev/shm/pi-collect-wrist-preview')

if __name__=='__main__':unittest.main(verbosity=2)
