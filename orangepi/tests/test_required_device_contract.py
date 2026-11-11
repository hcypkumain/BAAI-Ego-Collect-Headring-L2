import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
from required_capture_fixture import config as fixture_config, write_session
from required_device_contract import (CONTRACT_VERSION,build_contract,required_names,
    validate_scan,resolve_selection,data_errors,data_warnings)


def config(mode='emg'):
    return fixture_config(mode)


def readiness(names,ready=True,fresh=True):
    return dict(devices={name:dict(ready=ready,fresh=fresh,age_seconds=.1,last_seen_monotonic=1,seen=20) for name in names})


class RequiredDeviceContractTests(unittest.TestCase):
    def test_profile_required_set_is_independent_of_scan_and_eeg_adds_not_replaces_emg(self):
        emg=required_names(config('emg'));eeg=required_names(config('eeg'))
        self.assertEqual(len(emg),9);self.assertIn('emg_left',eeg);self.assertIn('emg_right',eeg);self.assertIn('eeg',eeg);self.assertEqual(len(eeg),10)
        incomplete=config();incomplete['cameras'].pop('bright')
        self.assertIn('cam_ros_bright',required_names(incomplete))
        self.assertTrue(any('cam_ros_bright' in error for error in __import__('required_device_contract').configuration_errors(incomplete)))

    def test_one_or_all_missing_devices_never_pass_scan_or_confirmation(self):
        contract=build_contract(config())
        complete=[dict(name=name,connected=True,status='ready') for name in contract['required_devices']]
        self.assertEqual(validate_scan(contract,complete),[])
        self.assertEqual(validate_scan(contract,[row for row in complete if row['name']!='cam_ros_left']),['cam_ros_left'])
        self.assertEqual(set(validate_scan(contract,[])),set(contract['required_devices']))
        with self.assertRaisesRegex(ValueError,'缺少必需设备'):
            build_contract(config(),[name for name in contract['required_devices'] if name!='cam_ros_left'])

    def test_new_frozen_contract_is_strict_and_legacy_is_explicit(self):
        cfg=config();contract=build_contract(cfg)
        selection=dict(selected=contract['selected_devices'],device_config=cfg,collection_mode='emg',required_device_contract=contract)
        resolved,errors,legacy=resolve_selection(cfg,selection)
        self.assertFalse(errors);self.assertFalse(legacy);self.assertEqual(resolved['version'],CONTRACT_VERSION)
        selection['required_device_contract']=None
        resolved,errors,legacy=resolve_selection(cfg,selection)
        self.assertIsNone(resolved);self.assertTrue(errors);self.assertFalse(legacy)
        selection.pop('required_device_contract')
        resolved,errors,legacy=resolve_selection(cfg,selection)
        self.assertFalse(errors);self.assertTrue(legacy)
        selection.pop('selected')
        self.assertTrue(resolve_selection(cfg,selection)[1])

    def make_complete_session(self,root,contract):
        _,summary,_=write_session(root,mode=contract['mode'],write_receipt=False)
        return summary

    def test_complete_new_session_passes_but_empty_or_stale_stream_fails(self):
        contract=build_contract(config())
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);summary=self.make_complete_session(root,contract)
            self.assertEqual(data_errors(root,summary,contract),[])
            del summary['cam_ros_bright'];summary['errors'].append('cam_ros_bright: disconnected')
            self.assertTrue(data_errors(root,summary,contract))

    def test_legacy_without_online_proof_is_warned_and_new_evidence_without_contract_errors(self):
        contract=build_contract(config())
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);summary=self.make_complete_session(root,contract)
            summary.pop('required_device_contract_version');summary.pop('required_device_start');summary.pop('required_device_stop')
            self.assertEqual(data_errors(root,summary,contract,legacy=True),[])
            self.assertEqual(data_warnings(summary,legacy=True),['legacy_online_evidence_unavailable'])
            summary['required_device_start']=readiness(contract['required_devices'])
            self.assertTrue(data_errors(root,summary,contract,legacy=True))


if __name__=='__main__':unittest.main()
