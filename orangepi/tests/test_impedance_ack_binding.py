import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]/'src/orangepi_collect'
for folder in ('acquisition','common','control','storage','audio'):
    sys.path.insert(0,str(ROOT/folder))
from eeg_bridge import EegProcess
from web_controls import WebControls


class AckBindingTests(unittest.TestCase):
    def bridge(self,messages):
        obj=EegProcess.__new__(EegProcess)
        obj.state_lock=threading.RLock();obj.write_lock=threading.Lock()
        obj.pending_impedance={'command':'IMPEDANCE_REVIEW','request_id':'new','measurement_epoch':'epoch2'}
        obj.impedance={};obj.owner=None;obj.ready=False;obj.error=None;obj.seen=0;obj.last=0
        obj.process=SimpleNamespace(stdout=io.StringIO(''.join(json.dumps(m)+'\n' for m in messages)))
        return obj
    def test_old_request_and_old_epoch_do_not_ack_new_review(self):
        for ack in (dict(ack_request_id='old',ack_measurement_epoch='epoch2',measurement_epoch='epoch2'),
                    dict(ack_request_id='new',ack_measurement_epoch='epoch1',measurement_epoch='epoch2'),
                    dict(ack_request_id='new',ack_measurement_epoch='epoch2',measurement_epoch='epoch3')):
            state=dict(required=True,supported=True,phase='completed',pending=False,can_record=True,ack_ok=True,**ack)
            owner=self.bridge([{'event':'impedance','state':state}]);owner._watch()
            self.assertIsNotNone(owner.pending_impedance)
            self.assertFalse(owner.impedance['can_record'])
            self.assertTrue(owner.impedance['pending'])
    def test_matching_ack_clears_pending_but_failure_remains_blocked(self):
        for ok in (True,False):
            state=dict(required=True,supported=True,phase='completed',pending=False,can_record=True,
                ack_ok=ok,ack_request_id='new',ack_measurement_epoch='epoch2',measurement_epoch='epoch2')
            owner=self.bridge([{'event':'impedance','state':state}]);owner._watch()
            self.assertIsNone(owner.pending_impedance)
            self.assertEqual(owner.impedance['can_record'],ok)
    def test_review_enqueue_blocks_record_until_matching_ack(self):
        owner=self.bridge([]);owner.pending_impedance=None;owner.ready=True
        from test_impedance_advisory import fixture
        owner.impedance,_=fixture()
        owner.impedance.update(measurement_epoch='epoch2',saved_measurement_epoch='epoch2')
        owner.process=SimpleNamespace(poll=lambda:None,stdin=io.StringIO())
        self.assertTrue(owner.can_record())
        owner.send_impedance({'command':'IMPEDANCE_REVIEW','request_id':'new','measurement_epoch':'epoch2'})
        owner.ready=True
        self.assertFalse(owner.can_record())
        self.assertTrue(owner.impedance['pending'])
    def test_controller_rejects_old_ack_and_keeps_start_gate_closed(self):
        worker=object();owner=SimpleNamespace(worker=worker,_impedance_command_pending=dict(worker=worker,request_id='new',epoch='epoch2'))
        for state in (dict(ack_request_id='old',ack_measurement_epoch='epoch2',measurement_epoch='epoch2'),
                      dict(ack_request_id='new',ack_measurement_epoch='epoch1',measurement_epoch='epoch2'),
                      dict(ack_request_id='new',ack_measurement_epoch='epoch2',measurement_epoch='epoch3')):
            self.assertTrue(WebControls.impedance_command_pending(owner,state))
        self.assertFalse(WebControls.impedance_command_pending(owner,dict(ack_request_id='new',ack_measurement_epoch='epoch2',measurement_epoch='epoch2')))
    def test_begin_does_not_arm_while_review_ack_is_pending(self):
        from unittest.mock import Mock
        from test_stop_dashboard_dispatch import FakeTime, PrivacyGate, start_methods_type
        owner=start_methods_type(FakeTime())();gate=PrivacyGate();gate.arm=Mock()
        owner.dashboard=SimpleNamespace(privacy=gate);owner.worker=SimpleNamespace(poll=lambda:None)
        owner.worker_status=Mock(return_value={'state':'ready'})
        owner.impedance_command_pending=Mock(return_value=True)
        owner.voice=SimpleNamespace(say=Mock())
        self.assertFalse(owner.begin())
        gate.arm.assert_not_called()


if __name__=='__main__':unittest.main()
