"""Read-only topology and bounded diagnostic export without acquisition work."""
import io,json,os
from email.message import Message
from pathlib import Path
import subprocess,sys,tempfile,threading,unittest,zipfile
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/control'))
from device_diagnostics import DeviceDiagnostics,usb_topology,read_small,clean
from phone_http import handle_phone_request


class Handler:
    def __init__(self,path,authorized=True,origin=None):
        self.path=path;self.headers=Message();self.headers['Host']='pi.test';self.headers['Content-Length']='0'
        if origin:self.headers['Origin']=origin
        self.authorized=lambda:authorized;self.send=Mock()


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.sys=self.root/'sys';(self.sys/'bus/usb/devices').mkdir(parents=True)
        self.preview=self.root/'preview';self.preview.mkdir();self.review=self.root/'session';self.review.mkdir()
        self.state={'state':'device_confirm','selected_devices':['cam_ros_left'],'session':None,
            'privacy':{'instance':'controller','generation':0,'phase':'idle','required':False,'stop_confirmed':True}}
        self.owner=SimpleNamespace(lock=threading.Lock(),snapshot={'state':'device_confirm','devices':[
            {'name':'cam_ros_left','label':'头环前左','connected':True,'config':{'topic':'/fisheye/left/image_raw/compressed'}},
            {'name':'eeg','connected':False,'config':{'port':'/dev/missing'}}],
            'worker':{},'selected_devices':['cam_ros_left'],'errors':[]},preview=self.preview,review_root=self.review)
        self.owner.privacy=Mock();self.owner.privacy.snapshot.side_effect=lambda:self.state['privacy'].copy()
        self.owner.display_state=lambda:self.state.copy()
        self.runner=Mock(return_value=SimpleNamespace(returncode=0,stdout=b''))
        self.diag=DeviceDiagnostics(self.owner,self.sys,self.runner,clock=lambda:100.0);self.owner.device_diagnostics=self.diag

    def usb(self,name,speed='480',bus='1',product='Fixture USB'):
        path=self.sys/'bus/usb/devices'/name;path.mkdir()
        values={'speed':speed,'busnum':bus,'devnum':'2','idVendor':'0bda','idProduct':'5858','product':product,'bDeviceClass':'00'}
        for key,value in values.items():(path/key).write_text(value)
        return path

    def test_headband_ros_is_not_inferred_to_be_a_usb_camera(self):
        self.usb('1-1');value=self.diag.snapshot()
        self.assertEqual(value['usb']['roles'][0]['transport'],'ros');self.assertIsNone(value['usb']['roles'][0]['port'])
        self.assertEqual(value['usb']['warnings'],[]);self.assertFalse(value['usb']['roles'][1]['connected'])

    def test_negotiated_speeds_do_not_claim_measured_throughput(self):
        self.usb('1-1');self.usb('2-1','5000','2')
        value=usb_topology([],self.sys);self.assertEqual([v['speed_mbps'] for v in value['devices']],[480,5000])
        self.assertIn('不是实测吞吐量',value['rate_note'])

    def test_camd_and_direct_imu_metadata_survive_sanitized_context(self):
        self.owner.snapshot['devices']=[
            dict(name='cam_ros_left',connected=True,config={'camd':'left'}),
            dict(name='board_imu',connected=True,config={'device':'/dev/icm20602'})]
        roles=self.diag.snapshot()['usb']['roles']
        self.assertEqual(roles[0]['transport'],'camd')
        self.assertEqual(roles[0]['socket'],'/run/camd.sock')
        self.assertEqual(roles[0]['camd'],'left')
        self.assertEqual(roles[1]['transport'],'character_device')
        self.assertEqual(roles[1]['device'],'/dev/icm20602')
        self.assertTrue(all(role['bus'] is None for role in roles))

    @unittest.skipIf(os.name=='nt','Linux sysfs link mapping')
    def test_two_uvc_cameras_on_same_usb2_bus_are_flagged_but_separate_bus_is_not(self):
        root=self.usb('usb1');(root/'speed').write_text('480')
        roles=[]
        for index in range(2):
            port=self.usb('1-'+str(index+1));interface=port/(port.name+':1.0');interface.mkdir()
            path=self.sys/'class/video4linux'/('video'+str(index));path.mkdir(parents=True)
            (path/'device').symlink_to(interface)
            roles.append(dict(name='cam_wrist_'+str(index),connected=True,device='/dev/video'+str(index),config={}))
        value=usb_topology(roles,self.sys);self.assertEqual(len(value['warnings']),1)
        self.assertEqual(value['warnings'][0]['code'],'shared_usb2_camera_bus')
        (self.sys/'class/video4linux/video1/device').unlink();other=self.usb('2-1','5000','2');(self.sys/'class/video4linux/video1/device').symlink_to(other)
        self.assertEqual(usb_topology(roles,self.sys)['warnings'],[])

    def test_kernel_logs_are_filtered_redacted_and_cached(self):
        self.runner.return_value.stdout=b'ordinary kernel message\nusb 1-1: Not enough bandwidth for altsetting 7\nusb error -1 token=secret\n'
        value=self.diag.snapshot();self.diag.snapshot()
        self.assertEqual(self.runner.call_count,1);self.assertEqual(len(value['kernel_events']),1)
        self.assertIn('Not enough bandwidth',value['kernel_events'][0]);self.assertNotIn('secret',json.dumps(value))

    def test_command_timeout_degrades_to_note_without_failing_topology(self):
        self.runner.side_effect=subprocess.TimeoutExpired('journalctl',2)
        value=self.diag.snapshot();self.assertIn('超时',value['kernel_log_note']);self.assertEqual(value['usb']['warnings'],[])

    def test_preview_files_are_summarized_without_raw_samples(self):
        (self.preview/'eeg.json').write_text(json.dumps({'time':99,'count':25,'trace':[['sensitive-raw']],'rows':[['raw']],'units':'uV'}))
        value=self.diag.snapshot();self.assertEqual(value['signals']['eeg']['age_seconds'],1)
        self.assertNotIn('trace',value['signals']['eeg']);self.assertNotIn('sensitive-raw',json.dumps(value))

    def test_export_has_manifest_qc_and_no_secrets_images_or_raw_signals(self):
        (self.review/'qc_report.json').write_text(json.dumps({'level':'WARN','api_key':'secret_qc','nonce':'secret_nonce'}))
        (self.review/'frame.jpg').write_bytes(b'sensitive-image');(self.review/'eeg.bin').write_bytes(b'sensitive-raw')
        self.runner.return_value.stdout=b'normal log\npassword=secret_password\nX-Access-Token: secret_token\n'
        before={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        payload=self.diag.bundle(self.diag.snapshot())
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names=archive.namelist();self.assertIn('latest-finalized-session/qc_report.json',names)
            self.assertFalse(any(name.endswith(('.jpg','.bin')) for name in names))
            text='\n'.join(archive.read(name).decode() for name in names)
            for secret in ('secret_qc','secret_password','secret_token','secret_nonce','sensitive-image','sensitive-raw'):self.assertNotIn(secret,text)
            manifest=json.loads(archive.read('manifest.json'));self.assertTrue(manifest['skipped'])
            import hashlib
            for name,value in manifest['files'].items():self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(),value['sha256'])
        self.assertEqual(before,{str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_oversized_and_non_json_qc_are_skipped(self):
        (self.review/'qc_report.json').write_bytes(b'x'*(384*1024+1));(self.review/'capture_summary.json').write_text('invalid')
        with zipfile.ZipFile(io.BytesIO(self.diag.bundle(self.diag.snapshot()))) as archive:
            self.assertFalse(any(name.startswith('latest-finalized-session') for name in archive.namelist()))

    def test_credential_keys_and_nonfinite_values_are_redacted(self):
        self.assertEqual(clean({'token':'secret','value':float('nan')}),{'token':'[已隐藏]','value':None})

    def test_routes_require_authorization_and_same_origin(self):
        for authorized,origin,expected in [(False,None,401),(True,'http://other.test',403),(True,None,200)]:
            handler=Handler('/api/phone/diagnostics',authorized,origin);handle_phone_request(handler,self.owner,'GET')
            self.assertEqual(handler.send.call_args.args[0],expected)

    def test_recording_and_busy_diagnostics_do_not_wait_or_touch_devices(self):
        self.state['state']='recording';handler=Handler('/api/phone/diagnostics');handle_phone_request(handler,self.owner,'GET')
        self.assertEqual(handler.send.call_args.args[0],423);self.assertEqual(self.runner.call_count,0)
        self.state['state']='idle';self.diag.slots.acquire()
        try:
            handler=Handler('/api/phone/diagnostics.zip');handle_phone_request(handler,self.owner,'GET');self.assertEqual(handler.send.call_args.args[0],429)
        finally:self.diag.slots.release()

    def test_late_privacy_transition_discards_generated_export(self):
        original=self.diag.bundle
        def transition(snapshot):
            value=original(snapshot);self.state['privacy']=dict(self.state['privacy'],generation=1,required=True,phase='arming',stop_confirmed=False);return value
        self.diag.bundle=transition
        handler=Handler('/api/phone/diagnostics.zip');handle_phone_request(handler,self.owner,'GET')
        self.assertEqual(handler.send.call_args.args[0],423)

    def test_download_headers_and_no_arbitrary_query_paths(self):
        handler=Handler('/api/phone/diagnostics.zip');handle_phone_request(handler,self.owner,'GET')
        self.assertEqual(handler.send.call_args.args[0],200);self.assertEqual(handler.send.call_args.args[2],'application/zip')
        self.assertIn('attachment',handler.send.call_args.kwargs['extra_headers']['Content-Disposition'])
        handler=Handler('/api/phone/diagnostics.zip?path=/etc/passwd');handle_phone_request(handler,self.owner,'GET')
        self.assertEqual(handler.send.call_args.args[0],400)


if __name__=='__main__':unittest.main()
