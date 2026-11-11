"""Ledger completion gates and task-reservation behavior without hardware."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

COMMON=Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'
STORAGE=Path(__file__).resolve().parents[1]/'src/orangepi_collect/storage'
CONTROL=Path(__file__).resolve().parents[1]/'src/orangepi_collect/control'
SRC=Path(__file__).resolve().parents[1]/'src'
TESTS=Path(__file__).resolve().parent
sys.path.insert(0,str(COMMON))
sys.path.insert(0,str(STORAGE))
sys.path.insert(0,str(CONTROL));sys.path.insert(0,str(SRC));sys.path.insert(0,str(TESTS))
import ledger


class LedgerQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=self.root/'project';self.base=self.project/'orangepi'
        self.library=self.project/'configs/environments';self.scene=self.library/'scene'
        self.scene.mkdir(parents=True);self.base.mkdir(parents=True)
        self.image=self.scene/'task_1.png';self.image.write_bytes(b'reference')
        self.values=[patch.object(ledger,'PROJECT',self.project),patch.object(ledger,'BASE',self.base),
                     patch.object(ledger,'LIBRARY',self.library)]
        for item in self.values:item.start();self.addCleanup(item.stop)

    def take(self,name='take-001',status='pending_qc',proof=True):
        path=self.root/'sessions'/name;path.mkdir(parents=True)
        summary={'status':status,'errors':[]}
        if proof:summary['storage_durability']={'completed':True}
        (path/'capture_summary.json').write_text(json.dumps(summary),encoding='utf-8')
        metadata={'status':status,'reference_image':'configs/environments/scene/task_1.png',
                  'pi':{'reference_image':'configs/environments/scene/task_1.png'}}
        (path/'meta.yaml').write_text(yaml.safe_dump(metadata),encoding='utf-8')
        return path

    def test_pending_take_reserves_reference_but_is_not_completed(self):
        path=self.take()
        self.assertTrue(ledger.mark(self.image,path,'pending_qc'))
        entry=ledger.read(self.scene/'used.yaml')['used'][self.image.name]
        self.assertTrue(ledger.reserved_used(entry,self.image))
        self.assertFalse(ledger.valid_used(entry,self.image))
        self.assertNotIn(self.image,ledger.unused())


class DeferredApplyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=self.root/'project';self.base=self.project/'orangepi'
        self.library=self.project/'configs/environments';self.scene=self.library/'scene'
        self.scene.mkdir(parents=True);self.base.mkdir(parents=True)
        self.image=self.scene/'task_1.png';self.image.write_bytes(b'reference image')
        self.sessions=self.root/'volume'/'data'/'shift';self.sessions.mkdir(parents=True)
        self.path=self.sessions/'take-deferred'
        self.patchers=[patch.object(ledger,'PROJECT',self.project),patch.object(ledger,'BASE',self.base),
                       patch.object(ledger,'LIBRARY',self.library)]
        for item in self.patchers:item.start();self.addCleanup(item.stop)
        import required_capture_fixture as fixtures
        import capture_qc_receipt
        import qc_deferred
        self.fixtures=fixtures;self.capture_qc_receipt=capture_qc_receipt;self.qc_deferred=qc_deferred
        self.contract,self.summary,self.report=fixtures.write_session(self.path,mode='emg',level='WARN',write_receipt=False)
        self.reference_sha=__import__('hashlib').sha256(self.image.read_bytes()).hexdigest()
        raw={'reference_image':'configs/environments/scene/task_1.png','reference_sha256':self.reference_sha,
             'collection_mode':'emg','selected_devices':self.contract['selected_devices']}
        (self.path/'meta.json').write_text(json.dumps(raw),encoding='utf-8')
        self.report['session_dir']=str(self.path)
        self.report['window']=dict(fixtures.WINDOW)
        (self.path/'qc_report.json').write_text(json.dumps(self.report,allow_nan=False),encoding='utf-8')
        (self.path/'qc_raw_report.json').write_text(json.dumps({'level':'WARN','findings':[]}),encoding='utf-8')
        self.summary.update(status='pending_qc',capture_status='success',qc_mode='deferred',qc_state='pending',
                            qc_level='PENDING',storage_durability={'completed':True})
        (self.path/'capture_summary.json').write_text(json.dumps(self.summary,allow_nan=False),encoding='utf-8')
        meta={'status':'pending_qc','retention_state':'kept_pending_qc','reference_image':raw['reference_image'],
              'reference_sha256':self.reference_sha,'pi':raw}
        (self.path/'meta.yaml').write_text(yaml.safe_dump(meta),encoding='utf-8')
        (self.path/'status.json').write_text(json.dumps({'state':'pending_qc','recording_active':False}),encoding='utf-8')
        self.request={'version':1,'request_id':'request-deferred-1','session_id':self.path.name,'state':'pending',
                      'legacy_source':False,
                      'native_sha256':qc_deferred.digest(self.path/'native_capture_summary.json'),
                      'reference_sha256':self.reference_sha}
        self.request['input_signature']=capture_qc_receipt.portable_input_signature(self.path,self.contract,require_marker=False)
        self.request['local_input_signature']=capture_qc_receipt.input_signature(self.path,self.contract,require_marker=False)
        (self.path/'qc_pending.json').write_text(json.dumps(self.request),encoding='utf-8')
        self.result_dir=self.root/'results'/self.path.name/'artifacts';self.result_dir.mkdir(parents=True)

    def result(self,archive_chunk=False):
        if archive_chunk:
            source=self.path/'emg_left/raw_chunks/chunk_000.npz'
            target=source.parent.parent/'chunk_000.npz';source.rename(target)
            expected_target=target.parent/'raw_chunks'/target.name
            target.rename(expected_target)
            checked=self.capture_qc_receipt.portable_input_signature(self.path,self.contract,require_marker=True)
            expected_target.rename(target)
            self.request['input_signature']=self.capture_qc_receipt.portable_input_signature(self.path,self.contract,require_marker=False)
            self.request['local_input_signature']=self.capture_qc_receipt.input_signature(self.path,self.contract,require_marker=False)
            (self.path/'qc_pending.json').write_text(json.dumps(self.request),encoding='utf-8')
            return self._result(checked)
        return self._result(self.capture_qc_receipt.portable_input_signature(self.path,self.contract,require_marker=True))

    def _result(self,checked):
        files={'qc_raw_report.json':self.path/'qc_raw_report.json','qc_report.json':self.path/'qc_report.json'}
        artifacts={}
        for name,source in files.items():
            target=self.result_dir/name;target.write_bytes(source.read_bytes())
            artifacts[name]=self.qc_deferred.digest(target)
        value={'version':1,'session_id':self.path.name,'request_id':self.request['request_id'],
               'native_sha256':self.request['native_sha256'],'reference_sha256':self.reference_sha,
               'input_signature':self.request['input_signature'],
               'local_input_signature':self.request['local_input_signature'],
               'legacy_source':False,
               'checked_input_signature':checked,'level':'WARN','raw_level':'WARN','passed':True,
               'elapsed_seconds':1.2,'artifacts':artifacts}
        (self.result_dir.parent/'result.json').write_text(json.dumps(value,allow_nan=False),encoding='utf-8')
        return self.result_dir.parent

    def test_apply_can_resume_after_an_artifact_copy_interruption_and_is_idempotent(self):
        result=self.result()
        original=self.qc_deferred._copy_result_artifact;calls=[]
        def interrupted(*args,**kwargs):
            value=original(*args,**kwargs);calls.append(1)
            if len(calls)==1:raise OSError('simulated artifact interruption')
            return value
        with patch.object(self.qc_deferred,'_copy_result_artifact',side_effect=interrupted):
            with self.assertRaisesRegex(OSError,'simulated artifact interruption'):
                self.qc_deferred.apply_result(result,self.sessions,self.base)
        resumed=self.qc_deferred.apply_result(result,self.sessions,self.base)
        self.assertTrue(resumed['passed'])
        self.assertEqual(self.qc_deferred.apply_result(result,self.sessions,self.base),resumed)
        self.assertEqual(json.loads((self.path/'status.json').read_text())['state'],'success')

    def test_apply_recovers_after_a_raw_chunk_archive_interruption(self):
        result=self.result(archive_chunk=True)
        original=self.qc_deferred._move_recorded_chunk;calls=[]
        def interrupted(*args,**kwargs):
            value=original(*args,**kwargs);calls.append(1)
            if len(calls)==1:raise OSError('simulated chunk archive interruption')
            return value
        with patch.object(self.qc_deferred,'_move_recorded_chunk',side_effect=interrupted):
            with self.assertRaisesRegex(OSError,'simulated chunk archive interruption'):
                self.qc_deferred.apply_result(result,self.sessions,self.base)
        resumed=self.qc_deferred.apply_result(result,self.sessions,self.base)
        self.assertTrue(resumed['passed'])
        self.assertTrue((self.path/'emg_left/raw_chunks/chunk_000.npz').is_file())


class LedgerMalformedAndTombstoneTests(LedgerQualificationTests):
    def test_unqualified_success_and_malformed_entry_fail_closed(self):
        path=self.take(status='success',proof=False)
        self.assertFalse(ledger.mark(self.image,path,'success'))
        self.assertFalse(ledger.valid_used({'status':'success','session':[]},self.image))
        self.assertFalse(ledger.valid_used({'status':'success'},self.image))
        self.assertFalse(ledger.reserved_used({'status':'pending_qc','session':{}},self.image))

    def test_malformed_nested_durability_or_yaml_does_not_break_refresh(self):
        path=self.take()
        summary={'status':'pending_qc','storage_durability':['completed']}
        (path/'capture_summary.json').write_text(json.dumps(summary),encoding='utf-8')
        entry={'status':'pending_qc','session':str(path)}
        self.assertFalse(ledger.reserved_used(entry,self.image))
        (path/'capture_summary.json').write_text(json.dumps({'status':'success'}),encoding='utf-8')
        (path/'meta.yaml').write_text('status: [unterminated',encoding='utf-8')
        self.assertFalse(ledger.valid_used(dict(entry,status='success'),self.image))

    def test_cleaned_session_completion_requires_bound_upload_receipt(self):
        session=self.root/'sessions'/'take-cleaned'
        job_id='job-001';job_dir=self.base/'runtime/cloud_uploads/jobs'/job_id
        job_dir.mkdir(parents=True)
        manifest={'digest':'manifest-digest'}
        (job_dir/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        receipt={'status':'success','platform_completion_acknowledged':True,
                 'session_id':session.name,'job_id':job_id,'manifest_digest':manifest['digest']}
        (job_dir/'receipt.json').write_text(json.dumps(receipt),encoding='utf-8')
        job={'id':job_id,'session_id':session.name,'source':str(session),'state':'success','local_deleted':True,
             'recording':{'id':session.name,'qualified':True,
                          'reference_path':'configs/environments/scene/task_1.png'}}
        queue={'version':1,'jobs':[job]};queue_path=self.base/'runtime/cloud_uploads/queue.json'
        queue_path.parent.mkdir(parents=True,exist_ok=True);queue_path.write_text(json.dumps(queue),encoding='utf-8')
        entry={'status':'success','session':str(session)}
        self.assertTrue(ledger.valid_used(entry,self.image))
        job['recording']['reference_path']='configs/environments/scene/other.png'
        queue_path.write_text(json.dumps(queue),encoding='utf-8')
        self.assertFalse(ledger.valid_used(entry,self.image))


if __name__=='__main__':unittest.main()
