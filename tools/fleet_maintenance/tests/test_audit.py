import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from dataclasses import asdict
from host.qc.tests.governance_fixtures import sample_registry

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
import audit_recordings as audit
import fleet
import online

class AuditTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.registry=sample_registry(('cp_0018',))
        self.registry_path=Path(temp.name)/'registry.json'
        self.registry_path.write_text(json.dumps({'schema_version':1,'devices':[asdict(d) for d in self.registry.devices]}))
        patcher=mock.patch.object(online,'config_for',return_value={'user':'fixture','hostkeyalias':'test-key-cp_0018'})
        patcher.start();self.addCleanup(patcher.stop)
    def test_remote_audit_fixture_reads_metadata_without_writes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); device_id=root/'DEVICE_ID'; device_id.write_text('cp_0014')
            project=root/'project'; config=project/'orangepi/configs/storage.yaml'; config.parent.mkdir(parents=True)
            mount=root/'media'; session=mount/'capture'/'L2-014-2026-11-10-02-30-52'; session.mkdir(parents=True)
            (session/'native_capture_summary.json').write_text('{"status":"success"}')
            (session/'video.mp4').write_bytes(b'video-not-to-be-read')
            config.write_text('mount_point: '+str(mount)+'\ndirectory: capture\n')
            code=audit.REMOTE.replace("/opt/egorecord/DEVICE_ID",str(device_id)).replace(
                '/home/orangepi/Desktop/Embodied-Brain-Collect-main.2',str(project)).replace("'/media/orangepi/'",repr(str(root)+'/'))
            before={str(p.relative_to(root)) for p in root.rglob('*')}
            result=json.loads(subprocess.run([sys.executable,'-c',code,'cp_0014'],check=True,stdout=subprocess.PIPE).stdout)
            after={str(p.relative_to(root)) for p in root.rglob('*')}
            self.assertEqual(before,after); self.assertEqual(len(result['sessions']),1)
            self.assertEqual(result['sessions'][0]['state'],'unknown')
            self.assertEqual(result['sessions'][0]['native_sha256'],hashlib.sha256(b'{"status":"success"}').hexdigest())

    def test_same_name_different_native_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'L2-018-2026-01/native_capture_summary.json'; p.parent.mkdir(); p.write_bytes(b'other')
            result=audit.local_evidence('cp_0018','L2-018-2026-01',hashlib.sha256(b'original').hexdigest(),[d])
            self.assertEqual(result['status'],'unknown')
            self.assertFalse(result['candidates'][0]['matches_native'])
            self.assertEqual(audit.local_evidence('cp_0018','L2-018-missing','0'*64,[d])['status'],'unknown')

    def test_native_match_is_only_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'L2-018-2026-01/native_capture_summary.json'; p.parent.mkdir(); p.write_bytes(b'actual')
            result=audit.local_evidence('cp_0018','L2-018-2026-01',hashlib.sha256(b'actual').hexdigest(),[d])
            self.assertEqual(result['status'],'metadata_only')

    def test_audit_dispatch_does_not_build_or_deploy(self):
        live={'device':'cp_0018','sessions':[],'roots':[]}
        class Fake:
            def __init__(self,*args): self.argv=['ssh','-F',str(args[0]),args[1]]
            def run(self,command):
                self.command=command
                assert 'sudo ' not in command and 'systemctl' not in command
                return json.dumps(live).encode()
        with mock.patch.object(online,'SSH',Fake), mock.patch.object(fleet,'build',side_effect=AssertionError('must not build')), \
             mock.patch.object(audit,'receiver_evidence',return_value=[]), contextlib.redirect_stdout(io.StringIO()) as out:
            rc=fleet.main(['--registry',str(self.registry_path),'--audit-recordings','--targets','cp_0018','--local-root','/fixture'])
        self.assertEqual(rc,0); self.assertIn('"fresh_full_hash": false',out.getvalue())

    def test_unreachable_not_audited(self):
        with mock.patch.object(online,'SSH',side_effect=online.Failure('unreachable')), contextlib.redirect_stdout(io.StringIO()) as out:
            rc=fleet.main(['--registry',str(self.registry_path),'--audit-recordings','--targets','cp_0018'])
        self.assertEqual(rc,2); self.assertIn('not_audited',out.getvalue())

if __name__=='__main__': unittest.main()
