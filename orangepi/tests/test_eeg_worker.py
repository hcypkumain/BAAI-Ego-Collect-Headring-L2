"""Python 3.11: stateful filtering, loss reporting and durable raw chunks."""
import copy
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/acquisition'))
from eeg_settings import DEFAULT
from eeg_worker import Writer, Pump
from eeg_filter import FilterSettings, StreamingFilter
from eeg_frontend import disable_channels, enable_all_channels
from omnibci import FrontendConfig, SampleBatch, PartialReadError, OmniBCIError


def batch(start=0,n=25):
    idx=np.arange(start,start+n,dtype=np.uint64)
    values=np.tile(np.sin(idx.astype(float)/8.)[:,None],(1,8)).astype(np.float32)
    return SampleBatch(values,(values*100).astype(np.int32),idx.astype(np.uint32),
        np.ones(n,bool),idx,np.zeros((n,3),np.uint8),np.ones(n,np.uint8),1,250,1.)


class EegWorkerTests(unittest.TestCase):
    def test_enable_all_restores_previous_disabled_ch1_and_shared_routing(self):
        cfg=FrontendConfig(reference='srb2',enabled_mask=254,bias_mask=254,srb2_mask=254,gains=(12,)*8,verified=True)
        expected=FrontendConfig(reference='srb2',enabled_mask=255,bias_mask=255,srb2_mask=255,gains=(12,)*8,verified=True)
        board=Mock();board.get_config.side_effect=[cfg,expected]
        before,after=enable_all_channels(board)
        self.assertEqual(before,cfg);self.assertEqual(after,expected)
        board.configure.assert_called_once_with(expected,timeout=5)

    def test_enable_all_preserves_independent_reference_and_disabled_bias(self):
        cfg=FrontendConfig(reference='srb1',enabled_mask=254,bias_mask=0,srb2_mask=0,verified=True)
        expected=FrontendConfig(reference='srb1',enabled_mask=255,bias_mask=0,srb2_mask=0,verified=True)
        board=Mock();board.get_config.side_effect=[cfg,expected]
        self.assertEqual(enable_all_channels(board)[1],expected)
        board.configure.assert_called_once_with(expected,timeout=5)

    def test_enable_all_requires_hardware_acknowledgement(self):
        cfg=FrontendConfig(enabled_mask=254,bias_mask=254,srb2_mask=0,verified=True)
        board=Mock();board.get_config.return_value=cfg
        with self.assertRaisesRegex(RuntimeError,'not acknowledged'):enable_all_channels(board)

    def test_all_eight_filtered_and_raw_columns_are_saved_in_physical_order(self):
        with tempfile.TemporaryDirectory() as temp:
            writer=Writer(Path(temp)/'eeg',copy.deepcopy(DEFAULT),FrontendConfig(enabled_mask=255,bias_mask=255,verified=True),{})
            b=batch(n=500);b.eeg_uv[:]+=np.arange(8)*10
            filtered=writer.prepare(b)
            self.assertTrue(np.isfinite(filtered).all())
            writer.append(b,filtered,100,200,300);writer.close('closed')
            with np.load(writer.path/'chunk_000000.npz',allow_pickle=False) as z:
                np.testing.assert_array_equal(z['eeg_uv'],b.eeg_uv)
                np.testing.assert_array_equal(z['eeg_filtered_uv'],filtered)
                self.assertEqual(z['channel_labels'].tolist(),['FC3','C3','O1','Pz','Oz','O2','C4','FC4'])
                self.assertTrue(z['channel_enabled'].all())

    def test_hardware_disable_preserves_reference_gain_and_other_masks(self):
        cfg=FrontendConfig(reference='srb2',bias_mask=255,srb2_mask=255,gains=(12,)*8,verified=True)
        board=Mock();board.get_config.side_effect=[cfg,FrontendConfig(reference='srb2',enabled_mask=254,bias_mask=254,srb2_mask=254,gains=(12,)*8,verified=True)]
        before,after=disable_channels(board,[1])
        self.assertEqual(after.enabled_mask,254);self.assertEqual(after.gains,cfg.gains)
        self.assertEqual(after.reference,cfg.reference);board.configure.assert_called_once()

    def test_unacknowledged_frontend_refuses_acquisition(self):
        board=Mock();board.get_config.return_value=FrontendConfig(verified=False)
        with self.assertRaises(RuntimeError):disable_channels(board,[1])

    def test_filter_is_batch_invariant_and_disabled_ch1_is_excluded(self):
        b=batch(n=1000);settings=FilterSettings(.5,45,(50.,),average_reference=True)
        a=StreamingFilter(settings,250,[False]+[True]*7);c=StreamingFilter(settings,250,[False]+[True]*7)
        full=a.apply(b.eeg_uv,b.sample_indices,b.valid)
        pieces=np.concatenate([c.apply(b.eeg_uv[i:i+25],b.sample_indices[i:i+25],b.valid[i:i+25]) for i in range(0,1000,25)])
        np.testing.assert_allclose(full,pieces,equal_nan=True,atol=1e-6)
        self.assertTrue(np.isnan(full[:,0]).all());np.testing.assert_allclose(full[:,1:].sum(axis=1),0,atol=1e-6)

    def test_gap_and_invalid_rows_reset_filter(self):
        settings=FilterSettings(1.,45.,(50.,));enabled=[False]+[True]*7
        stream=StreamingFilter(settings,250,enabled);fresh=StreamingFilter(settings,250,enabled)
        b=batch(n=100);stream.apply(b.eeg_uv,b.sample_indices,b.valid)
        tail=batch(200,n=25)
        np.testing.assert_allclose(stream.apply(tail.eeg_uv,tail.sample_indices,tail.valid),
            fresh.apply(tail.eeg_uv,tail.sample_indices,tail.valid),equal_nan=True)

    def test_raw_chunks_two_seconds_and_partial_tail_are_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            writer=Writer(Path(temp)/'eeg',copy.deepcopy(DEFAULT),FrontendConfig(enabled_mask=254,bias_mask=254,verified=True),{})
            for start in range(0,525,25):
                b=batch(start);writer.append(b,writer.prepare(b),100+start,200+start,300+start)
            result=writer.close('closed')
            self.assertEqual(result['samples'],525);self.assertEqual(result['chunks'],2)
            with np.load(writer.path/'chunk_000000.npz',allow_pickle=False) as z:
                self.assertEqual(len(z['sample_indices']),500)
                np.testing.assert_array_equal(z['raw_counts'],batch(n=500).raw_counts)
            with np.load(writer.path/'chunk_000001.npz') as z:self.assertEqual(len(z['sample_indices']),25)

    def test_no_samples_are_saved_during_preparation(self):
        with tempfile.TemporaryDirectory() as temp:
            writer=Writer(Path(temp)/'eeg',copy.deepcopy(DEFAULT),FrontendConfig(enabled_mask=254,bias_mask=254,verified=True),{})
            writer.prepare(batch());writer.close('closed')
            self.assertFalse(list(writer.path.glob('chunk_*.npz')))

    def test_nonincreasing_indices_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            writer=Writer(Path(temp)/'eeg',copy.deepcopy(DEFAULT),FrontendConfig(enabled_mask=254,bias_mask=254,verified=True),{})
            b=batch();writer.append(b,writer.prepare(b),1,2,3)
            with self.assertRaises(RuntimeError):writer.append(b,writer.prepare(b),1,2,3)
            writer.close('error')

    def test_pump_retains_partial_read_before_transport_error(self):
        board=Mock();error=OmniBCIError('disconnected');error.partial=batch()
        board.read.side_effect=[error,PartialReadError(None)]
        pump=Pump(board);pump.run()
        self.assertEqual(len(pump.jobs.get_nowait()[0]),25)
        self.assertIn('disconnected',pump.error);board.stop.assert_called_once()

    def test_queue_overflow_reports_unsaved_samples(self):
        pump=Pump(Mock())
        for i in range(64):pump.publish(batch(i*25),1)
        with self.assertRaisesRegex(RuntimeError,'queue full'):pump.publish(batch(1600),1)
        self.assertEqual(pump.unqueued,25)


if __name__=='__main__':unittest.main()
