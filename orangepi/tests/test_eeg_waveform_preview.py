"""Derived traces are bounded, mode-aware and never enter recording queues."""
import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, MagicMock, patch

import numpy as np
COMMON=Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'
sys.path.insert(0,str(COMMON))
import live_preview


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.pub=live_preview.Publisher(self.tmp.name);self.pub.close()
    def tearDown(self):self.tmp.cleanup()
    def feed(self,n=25,generation=1,mode='eeg'):
        raw=np.tile(100000+20*np.sin(np.arange(n)*2*np.pi/8),(8,1)).T
        filtered=raw-100000
        self.pub.eeg(filtered,np.ones(n,bool),['FC3','C3','O1','Pz','Oz','O2','C4','FC4'],'general',100.,
            sample_indices=np.arange(n),generation=generation,raw_values=raw,mode=mode)
        return raw,filtered
    def test_trace_preserves_raw_filtered_values_and_physical_units(self):
        raw,filtered=self.feed();self.pub._publish_once()
        value=json.loads((Path(self.tmp.name)/'eeg.json').read_text())
        self.assertEqual(value['units'],'uV');self.assertEqual(value['physical_channels'],list(range(1,9)))
        self.assertEqual(value['channel_labels'],['FC3','C3','O1','Pz','Oz','O2','C4','FC4'])
        self.assertEqual(len(value['trace'][2][1]),8)
        self.assertEqual(len(value['trace']),25);self.assertEqual(value['trace'][2][1][0],20)
        self.assertEqual(value['trace'][2][2][0],100020);self.assertEqual(value['trace'][2][3],2)
        np.testing.assert_array_equal(raw-filtered,np.full(raw.shape,100000))
    def test_trace_is_bounded_to_500_samples_and_preview_size(self):
        for _ in range(30):self.feed()
        self.pub._publish_once();raw=(Path(self.tmp.name)/'eeg.json').read_bytes();value=json.loads(raw)
        self.assertEqual(len(value['trace']),500);self.assertLess(len(raw),128*1024)
    def test_invalid_samples_make_gaps_and_do_not_create_zero_voltage(self):
        values=np.ones((25,8))*100000;valid=np.ones(25,bool);valid[2]=False;values[3,0]=float('nan')
        self.pub.eeg(values,valid,['unused']+[str(i) for i in range(7)],'raw',100.,generation=1,mode='impedance')
        _,signals=self.pub._snapshot();trace=signals['eeg']['trace']
        self.assertIsNone(trace[2][1][0]);self.assertIsNone(trace[3][1][0]);self.assertEqual(trace[1][1][0],100000)
    def test_mode_or_generation_changes_clear_the_previous_trace(self):
        self.feed();self.feed(generation=2,mode='impedance')
        _,signals=self.pub._snapshot();self.assertEqual(len(signals['eeg']['trace']),25)
        self.assertEqual(signals['eeg']['mode'],'impedance');self.assertEqual(signals['eeg']['generation'],2)
    def test_unrequested_eeg_adds_no_trace_or_work(self):
        self.pub.demand.signals=frozenset();self.feed();self.assertNotIn('eeg',self.pub.signals)


class PumpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=COMMON.parent/'acquisition/eeg_worker.py'
        spec=importlib.util.spec_from_file_location('waveform_test_eeg_worker',path)
        cls.worker=importlib.util.module_from_spec(spec)
        # Windows-only test import shim; no device operations are invoked.
        with patch.dict(sys.modules,{'fcntl':types.ModuleType('fcntl')} if os.name=='nt' else {}):spec.loader.exec_module(cls.worker)
    def test_measurement_trace_is_published_without_enqueuing_recording_samples(self):
        batch=MagicMock();batch.__len__.return_value=25;batch.received_at=12.;batch.eeg_uv=np.ones((25,8));batch.valid=np.ones(25,bool);batch.sample_indices=np.arange(25);batch.generation=4
        preflight=Mock();preflight.state={'phase':'measuring'};preflight.config={'labels':['unused']+list('abcdefg')};preflight.last_received_at=12.
        pump=self.worker.Pump(Mock(),preflight);pub=Mock()
        with patch.object(self.worker,'publisher',return_value=pub):pump.publish(batch,100)
        preflight.observe.assert_called_once_with(batch);pub.eeg.assert_called_once();self.assertTrue(pump.jobs.empty())
        self.assertEqual(pub.eeg.call_args.kwargs['mode'],'impedance');self.assertEqual(pub.eeg.call_args.args[3],'raw')
    def test_rejected_stale_measurement_never_appears_as_a_fresh_waveform(self):
        batch=MagicMock();batch.__len__.return_value=25;batch.received_at=12.
        preflight=Mock();preflight.state={'phase':'measuring'};preflight.last_received_at=None
        pump=self.worker.Pump(Mock(),preflight);pub=Mock()
        with patch.object(self.worker,'publisher',return_value=pub):pump.publish(batch,100)
        pub.eeg.assert_not_called();self.assertTrue(pump.jobs.empty())


if __name__=='__main__':unittest.main()
