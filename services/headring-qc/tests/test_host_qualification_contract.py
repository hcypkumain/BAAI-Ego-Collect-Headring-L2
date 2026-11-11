"""Filesystem-backed PI tests for signed manual-QC handoff qualification.

The fixture bytes are synthetic, but source/input identities, handoff/result
HMACs, manifests, report files and the host ledger are real temporary files.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'services/headring-qc/tests'),str(ROOT/'services/headring-qc'),
              str(ROOT/'orangepi/src/orangepi_collect/common')]
from raw_handoff import signed,sha_value,RAW_PURPOSE,RESULT_PURPOSE
import test_pi_custody_linux as pi_fixture_module
from required_device_contract import resolve_selection,build_contract
from capture_qc_receipt import input_signature,portable_input_signature


class HandoffQualificationContractTests(unittest.TestCase):
    def setUp(self):
        self.fx=pi_fixture_module.Tests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.agent=self.fx.agent
        self.source=self.fx.source
        self.project=self.fx.base.parent
        self.sid=self.fx.sid

    def _old_receipt(self):
        selection=json.loads((self.source/'device_selection.json').read_text())
        selection.pop('required_device_contract',None)
        (self.source/'device_selection.json').write_text(json.dumps(selection))
        pending=json.loads((self.source/'qc_pending.json').read_text())
        pending.pop('input_signature',None);pending.pop('local_input_signature',None)
        pending.pop('legacy_source',None)
        (self.source/'qc_pending.json').write_text(json.dumps(pending))
        manifest=self.agent.manifest(self.sid)
        return signed(dict(version=1,purpose=RAW_PURPOSE,device_id='cp_0020',server_id='fixture-server',
            session_id=self.sid,transaction_id='1'*32,manifest=manifest,qc_request_id=self.fx.request_id,
            host_verified=True,host_fsynced=True,host_path='/host/fixture',host_storage_uuid='HOST-TEST'),
            self.fx.key)

    def test_old_protocol_is_rejected_before_original_handoff_delete(self):
        receipt=self._old_receipt()
        original_manifest=self.agent.manifest(self.sid)
        with self.assertRaisesRegex(ValueError,'QC_HOST_UPGRADE_REQUIRED'):
            self.agent.raw_handoff({'receipt':receipt})
        self.assertTrue(self.source.is_dir())
        self.assertEqual(self.agent.manifest(self.sid)['digest'],original_manifest['digest'])
        self.assertFalse(self.agent._handoff_record(self.sid).exists())

    def _completed_handoff(self,reference=False,eeg=False,reference_project=None):
        selection=json.loads((self.source/'device_selection.json').read_text())
        if eeg:
            config=selection['device_config'];config['collection_mode']='eeg';config['eeg']={'enabled':True}
            contract=build_contract(config,mode='eeg')
            selection.update(device_config=config,collection_mode='eeg',selected=contract['selected_devices'],
                             required_device_contract=contract)
            (self.source/'device_selection.json').write_text(json.dumps(selection))
            directory=self.source/'eeg';directory.mkdir()
            (directory/'eeg.npz').write_bytes(b'fixture-eeg-array')
            capture=json.loads((self.source/'capture_summary.json').read_text())
            capture['eeg']={'samples':1};(self.source/'capture_summary.json').write_text(json.dumps(capture))
            raw_meta=json.loads((self.source/'meta.json').read_text());raw_meta['collection_mode']='eeg'
            (self.source/'meta.json').write_text(json.dumps(raw_meta))
        contract,errors,legacy=resolve_selection(selection['device_config'],selection)
        self.assertFalse(errors);self.assertFalse(legacy)
        meta=json.loads((self.source/'meta.json').read_text())
        if reference:
            reference_project=Path(reference_project or self.project)
            image=reference_project/'configs/environments/scene/task_1.png'
            image.parent.mkdir(parents=True,exist_ok=True);image.write_bytes(b'isolated reference')
            meta['reference_image']='configs/environments/scene/task_1.png'
            meta['reference_sha256']=hashlib.sha256(image.read_bytes()).hexdigest()
            (self.source/'meta.json').write_text(json.dumps(meta))
            self.agent.project_paths.PROJECT=reference_project
        pending=json.loads((self.source/'qc_pending.json').read_text())
        pending['reference_sha256']=meta['reference_sha256']
        pending['input_signature']=portable_input_signature(self.source,contract,require_marker=False)
        pending['local_input_signature']=input_signature(self.source,contract,require_marker=False)
        (self.source/'qc_pending.json').write_text(json.dumps(pending))
        manifest=self.agent.manifest(self.sid)
        receipt=signed(dict(version=1,purpose=RAW_PURPOSE,device_id='cp_0020',server_id='fixture-server',
            session_id=self.sid,transaction_id='2'*32,manifest=manifest,qc_request_id=self.fx.request_id,
            host_verified=True,host_fsynced=True,host_path='/host/fixture',host_storage_uuid='HOST-TEST',
            qc_protocol_version=2,qc_qualification_protocol='required-device-contract-v1',
            input_signature=pending['input_signature'],local_input_signature=pending['local_input_signature'],
            input_signature_kind='portable-content-sha256-v1',reference_sha256=meta['reference_sha256'],
            required_device_contract_version=contract['version']),self.fx.key)
        self.agent.raw_handoff({'receipt':receipt})
        record=json.loads(self.agent._handoff_record(self.sid).read_text())
        resultdir=self.agent.RESULTS/self.sid
        (resultdir/'artifacts').mkdir(parents=True,exist_ok=True)
        summary=record['source_summary'];required=contract['required_devices']
        report={'scope':'full','level':'WARN','session_dir':'/host/work/'+self.sid,
            'window':{'t0':summary['start_unix_ns']/1e9,'t1':summary['stop_unix_ns']/1e9},
            'streams':{name:{'level':'WARN'} for name in required}}
        result={'version':1,'session_id':self.sid,'request_id':record['qc_request_id'],
            'native_sha256':record['native_sha256'],'reference_sha256':meta['reference_sha256'],
            'input_signature':record['source_input_signature'],
            'local_input_signature':record['source_local_input_signature'],
            'checked_input_signature':'b'*64,'level':'WARN','raw_level':'WARN','passed':True,
            'derived_counts':{'eeg_samples':1} if eeg else {},'artifacts':{}}
        return record,resultdir,result,report,meta

    def _signed_result_receipt(self,record,result,passed=True,qualification_sha=None):
        return signed(dict(version=1,purpose=RESULT_PURPOSE,device_id='cp_0020',server_id='fixture-server',
            session_id=self.sid,handoff_receipt_sha256=record['receipt_sha256'],
            result_sha256=hashlib.sha256((self.agent.RESULTS/self.sid/'result.json').read_bytes()).hexdigest(),
            server_path='/host/result',qc_protocol_version=2,
            qc_qualification_protocol='required-device-contract-v1',
            input_signature=record['source_input_signature'],
            local_input_signature=record['source_local_input_signature'],
            checked_input_signature=result['checked_input_signature'],
            reference_sha256=result['reference_sha256'],passed=passed,
            qualification_receipt_sha256=qualification_sha),self.fx.key)

    def _qualification_receipt(self,record,result,resultdir):
        (resultdir/'result.json').write_text(json.dumps(result))
        return self._signed_result_receipt(record,result,passed=True,qualification_sha='c'*64)

    def test_full_report_and_saved_stream_manifest_qualify_bound_inputs(self):
        record,resultdir,result,report,_meta=self._completed_handoff()
        envelope=self._qualification_receipt(record,result,resultdir)
        contract=self.agent._handoff_result_qualification(record,envelope,result,resultdir,report)
        self.assertEqual(contract['required_devices'],record['frozen_selection']['required_device_contract']['required_devices'])

    def test_report_for_different_request_or_changed_input_is_rejected(self):
        record,resultdir,result,report,_meta=self._completed_handoff()
        changed=dict(result,input_signature='0'*64)
        envelope=self._qualification_receipt(record,changed,resultdir)
        with self.assertRaisesRegex(ValueError,'QC_HOST_UPGRADE_REQUIRED'):
            self.agent._handoff_result_qualification(record,envelope,changed,resultdir,report)
        changed=dict(result,request_id='another-request')
        envelope=self._qualification_receipt(record,changed,resultdir)
        with self.assertRaisesRegex(ValueError,'QC_HOST_UPGRADE_REQUIRED'):
            self.agent._handoff_result_qualification(record,envelope,changed,resultdir,report)
        changed_report=dict(report,session_dir='/host/work/another-session')
        with self.assertRaisesRegex(ValueError,'QC_HOST_UPGRADE_REQUIRED'):
            self.agent._handoff_result_qualification(record,envelope,result,resultdir,changed_report)

    def test_signed_passed_false_cannot_promote_warn_to_success(self):
        record,resultdir,result,report,_meta=self._completed_handoff()
        report_path=resultdir/'artifacts/qc_report.json';report_path.write_text(json.dumps(report))
        result['artifacts']={'qc_report.json':hashlib.sha256(report_path.read_bytes()).hexdigest()}
        result_path=resultdir/'result.json';result_path.write_text(json.dumps(result))
        envelope=self._signed_result_receipt(record,result,passed=False,qualification_sha=None)
        with self.assertRaisesRegex(ValueError,'QC_HOST_UPGRADE_REQUIRED'):
            self.agent.apply_handoff_result({'receipt':envelope})
        envelope=self._signed_result_receipt(record,result,passed=True,qualification_sha=None)
        with self.assertRaisesRegex(ValueError,'QC_HOST_UPGRADE_REQUIRED'):
            self.agent.apply_handoff_result({'receipt':envelope})
        self.assertFalse((self.agent.STATE/'results'/(self.sid+'.json')).exists())

    def test_pi_result_application_rejects_signed_but_unbound_host_result(self):
        record,resultdir,result,report,_meta=self._completed_handoff()
        report_path=resultdir/'artifacts/qc_report.json'
        report_path.write_text(json.dumps(report))
        result['artifacts']={'qc_report.json':hashlib.sha256(report_path.read_bytes()).hexdigest()}
        result['input_signature']='0'*64
        result_path=resultdir/'result.json';result_path.write_text(json.dumps(result))
        envelope=self._signed_result_receipt(record,result,passed=True,qualification_sha='c'*64)
        with self.assertRaisesRegex(ValueError,'QC_HOST_UPGRADE_REQUIRED'):
            self.agent.apply_handoff_result({'receipt':envelope})
        self.assertFalse((self.agent.STATE/'results'/(self.sid+'.json')).exists())

    def test_pi_result_application_rejects_changed_request_native_or_reference_identity(self):
        record,resultdir,result,report,_meta=self._completed_handoff()
        report_path=resultdir/'artifacts/qc_report.json';report_path.write_text(json.dumps(report))
        result['artifacts']={'qc_report.json':hashlib.sha256(report_path.read_bytes()).hexdigest()}
        variants=(('request_id','another-request'),('native_sha256','0'*64),('reference_sha256','0'*64))
        for field,value in variants:
            with self.subTest(field=field):
                changed=dict(result,**{field:value})
                result_path=resultdir/'result.json';result_path.write_text(json.dumps(changed))
                envelope=self._signed_result_receipt(record,changed,passed=True,qualification_sha='c'*64)
                with self.assertRaisesRegex(ValueError,'QC_HOST_UPGRADE_REQUIRED'):
                    self.agent.apply_handoff_result({'receipt':envelope})
        self.assertFalse((self.agent.STATE/'results'/(self.sid+'.json')).exists())

    def test_qualified_host_tombstone_remains_a_valid_completed_ledger_entry(self):
        profile_project=self.project/'profiles/eeg';profile_base=profile_project/'orangepi'
        profile_project.mkdir(parents=True,exist_ok=True);profile_base.mkdir(parents=True,exist_ok=True)
        record,resultdir,result,report,meta=self._completed_handoff(
            reference=True,eeg=True,reference_project=profile_project)
        self.agent.project_paths.BASE=profile_base
        self.agent.project_paths.COLLECTION_MODE='eeg'
        manifest={row['path']:row['sha256'] for row in record['receipt']['manifest']['files']}
        final=resultdir/'artifacts/eeg/gate_finalization.json';final.parent.mkdir(parents=True,exist_ok=True)
        proof={'schema_version':1,'policy':'whole_sdk_batches_final_controller_gate_v1',
            'start_monotonic_ns':record['start_gate']['start_monotonic_ns'],
            'stop_monotonic_ns':record['stop_gate']['stop_monotonic_ns'],
            'gate_sha256':{'start':manifest['start_gate.json'],'stop':manifest['stop_gate.json']},
            'input_samples':1,'kept_samples':1,'excluded_samples':0,'source_chunks':[{'name':'chunk_000.npz'}]}
        final.write_text(json.dumps(proof))
        result['artifacts']={'eeg/gate_finalization.json':hashlib.sha256(final.read_bytes()).hexdigest()}
        report_path=resultdir/'artifacts/qc_report.json';report_path.write_text(json.dumps(report))
        # Exercise the qualification function before committing this result.
        envelope=self._qualification_receipt(record,result,resultdir)
        self.agent._handoff_result_qualification(record,envelope,result,resultdir,report)
        result['artifacts']['qc_report.json']=hashlib.sha256(report_path.read_bytes()).hexdigest()
        result_path=resultdir/'result.json';result_path.write_text(json.dumps(result))
        envelope=self._signed_result_receipt(record,result,passed=True,qualification_sha='c'*64)
        ledger_path=ROOT/'orangepi/src/orangepi_collect/storage/ledger.py'
        spec=importlib.util.spec_from_file_location('handoff_test_ledger',ledger_path)
        ledger=importlib.util.module_from_spec(spec);spec.loader.exec_module(ledger)
        ledger.BASE=profile_base;ledger.PROJECT=profile_project
        ledger.LIBRARY=profile_project/'configs/environments'
        # This interpreter's C loader loses implicit tags after the dynamic
        # Pi-agent import fixture; the same safe pure-Python loader is used by
        # production as the documented fallback.
        import yaml
        ledger._SAFE_YAML_LOADER=yaml.SafeLoader
        ledger.project_paths.CODE_BASE=self.fx.base
        ledger.project_paths.PROJECT=profile_project
        ledger.project_paths.COLLECTION_MODE='eeg'
        self.agent.ledger=ledger
        value=self.agent.apply_handoff_result({'receipt':envelope})
        self.assertTrue(value['passed'])
        image=profile_project/meta['reference_image']
        book=ledger.read(image.parent/'used.yaml')
        used=book['used'][image.name]
        attempt=book['attempts'][image.name][0]
        self.assertEqual(used['status'],'success')
        self.assertEqual(used['data_location'],'host')
        self.assertTrue(attempt['pi_original_deleted'])
        self.assertTrue(ledger.valid_used(used,image))
        # A profile with the same task filename and image bytes still cannot
        # claim this handoff's completion.
        ledger.project_paths.COLLECTION_MODE='emg'
        self.assertFalse(ledger.valid_used(used,image))
        wrong_project=self.project/'profiles/emg';wrong_base=wrong_project/'orangepi'
        wrong_project.mkdir(parents=True,exist_ok=True);wrong_base.mkdir(parents=True,exist_ok=True)
        ledger.BASE=wrong_base;ledger.PROJECT=wrong_project
        ledger.LIBRARY=wrong_project/'configs/environments'
        ledger.project_paths.PROJECT=wrong_project;ledger.project_paths.COLLECTION_MODE='eeg'
        self.assertFalse(ledger.valid_used(used,image))
        ledger.BASE=profile_base;ledger.PROJECT=profile_project
        ledger.LIBRARY=profile_project/'configs/environments'
        ledger.project_paths.PROJECT=profile_project;ledger.project_paths.COLLECTION_MODE='eeg'
        forged=dict(used,result_sha256='0'*64)
        self.assertFalse(ledger.valid_used(forged,image))
        state_result=self.agent.STATE/'results'/(self.sid+'.json')
        saved=json.loads(state_result.read_text())
        saved['handoff_result_receipt']['signature']='0'*64
        state_result.write_text(json.dumps(saved))
        self.assertFalse(ledger.valid_used(used,image))

    def test_eeg_finalization_rejects_malformed_or_wrong_gate_hash_strings(self):
        record,resultdir,result,report,_meta=self._completed_handoff(eeg=True)
        final=resultdir/'artifacts/eeg/gate_finalization.json';final.parent.mkdir(parents=True,exist_ok=True)
        manifest={row['path']:row['sha256'] for row in record['receipt']['manifest']['files']}
        proof={'schema_version':1,'policy':'whole_sdk_batches_final_controller_gate_v1',
            'start_monotonic_ns':record['start_gate']['start_monotonic_ns'],
            'stop_monotonic_ns':record['stop_gate']['stop_monotonic_ns'],
            'gate_sha256':{'start':'x'*64,'stop':manifest['stop_gate.json']},
            'input_samples':1,'kept_samples':1,'excluded_samples':0,'source_chunks':[{'name':'chunk_000.npz'}]}
        envelope=self._qualification_receipt(record,result,resultdir)
        for bad_hash in ('x'*64,'0'*64,None):
            with self.subTest(bad_hash=bad_hash):
                proof['gate_sha256']['start']=bad_hash
                final.write_text(json.dumps(proof))
                with self.assertRaisesRegex(ValueError,'EEG STOP finalization proof is invalid'):
                    self.agent._handoff_result_qualification(record,envelope,result,resultdir,report)


if __name__=='__main__':unittest.main(verbosity=2)
