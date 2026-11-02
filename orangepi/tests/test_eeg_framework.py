"""EEG format, active channel QC and gate checks in the framework environment."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
from eeg_qc import standardize_eeg,PiEegChecker,OmniEegIntegrity
from eeg_settings import LAYOUT, LEGACY_LAYOUT

# Exercise the EEG adapter through its injected writer without importing the
# unrelated camera/EMG export pipeline and its runtime device dependencies.
atomic_npz = np.savez


class IntegrityChecker(PiEegChecker):
    # These fixtures cover sample and channel integrity. Durable impedance
    # START evidence is exercised separately in test_impedance_advisory.py.
    checks = [OmniEegIntegrity()]


def make(root,n=100, generation=1,legacy=False):
    directory=root/'eeg';directory.mkdir(exist_ok=True)
    idx=np.arange(n,dtype=np.uint64)
    uv=(np.sin(idx.astype(float)/8)[:,None]*np.arange(1,9)[None,:]+np.arange(8)[None,:]*10).astype(np.float32)
    filtered=uv.copy()
    if legacy:filtered[:,0]=np.nan
    np.savez(directory/'chunk_000000.npz',eeg_uv=uv,raw_counts=(uv*100).astype(np.int32),
        sequence=idx.astype(np.uint32),valid=np.ones(n,bool),sample_indices=idx,
        status=np.zeros((n,3),np.uint8),mode=np.ones(n,np.uint8),eeg_filtered_uv=filtered,
        read_begin_monotonic_ns=np.full(n,120,np.int64),arrival_monotonic_ns=np.full(n,180,np.int64),
        arrival_unix_ns=np.full(n,180,np.int64),sdk_received_monotonic_ns=np.full(n,170,np.int64),
        sample_rate_hz=250,generation=generation,channel_enabled=np.array([not legacy]+[True]*7),
        channel_labels=np.array(LEGACY_LAYOUT if legacy else LAYOUT),
        filter_preset=np.array('general'),timestamp_basis=np.array('host only'))
    (root/'start_gate.json').write_text('{"start_monotonic_ns":100}')
    (root/'stop_gate.json').write_text('{"stop_monotonic_ns":200}')
    return {'eeg':{'samples':n}}


class EegFrameworkTests(unittest.TestCase):
    def test_format_preserves_all_raw_columns_and_active_only_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);summary=make(root)
            with np.load(root/'eeg/chunk_000000.npz') as z:
                raw=z['raw_counts'].copy();raw_uv=z['eeg_uv'].copy()
            standardize_eeg(root,summary,atomic_npz)
            with np.load(root/'eeg/eeg.npz') as z:
                np.testing.assert_array_equal(z['raw_counts'],raw)
                self.assertEqual(z['eeg_data'].shape,(100,8))
                self.assertEqual(z['eeg_raw_uv'].shape,(100,8))
                self.assertEqual(z['eeg_channel_labels'].tolist(),LAYOUT)
                np.testing.assert_array_equal(z['eeg_raw_uv'],raw_uv)
                np.testing.assert_array_equal(z['eeg_data'],raw_uv)
                self.assertTrue(np.isfinite(z['eeg_filtered_data']).all())
                self.assertNotIn('eeg_timestamps_pc',z.files)
                self.assertNotIn('eeg_fit_fitted',z.files)
            self.assertTrue((root/'eeg/raw_chunks/chunk_000000.npz').exists())
            report=IntegrityChecker().run(root/'eeg')
            self.assertEqual(report.level,'INFO')

    def test_historical_seven_channel_recording_keeps_its_original_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);summary=make(root,legacy=True)
            standardize_eeg(root,summary,atomic_npz)
            with np.load(root/'eeg/eeg.npz') as z:
                self.assertEqual(z['eeg_data'].shape,(100,7))
                self.assertEqual(z['eeg_channel_labels'].tolist(),LEGACY_LAYOUT[1:])
            self.assertEqual(IntegrityChecker().run(root/'eeg').level,'INFO')

    def test_ch1_adc_saturation_is_included_in_eight_channel_qc(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);summary=make(root)
            path=root/'eeg/chunk_000000.npz'
            with np.load(path) as z:arrays={k:z[k].copy() for k in z.files}
            arrays['raw_counts'][0,0]=8388607
            np.savez(path,**arrays);standardize_eeg(root,summary,atomic_npz)
            report=IntegrityChecker().run(root/'eeg')
            self.assertEqual(report.level,'WARN')
            rails=report.stats['OmniEegIntegrity']['adc_rail_samples']
            self.assertEqual(rails,[1]+[0]*7)

    def test_old_channel_mask_cannot_be_mislabeled_as_new_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);summary=make(root,legacy=True)
            path=root/'eeg/chunk_000000.npz'
            with np.load(path) as z:arrays={k:z[k].copy() for k in z.files}
            arrays['channel_labels']=np.array(LAYOUT)
            np.savez(path,**arrays)
            with self.assertRaisesRegex(RuntimeError,'mask/layout'):standardize_eeg(root,summary,atomic_npz)
            self.assertTrue(path.exists())

    def test_count_mismatch_keeps_source_chunks(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);summary=make(root);summary['eeg']['samples']+=1
            with self.assertRaises(RuntimeError):standardize_eeg(root,summary,atomic_npz)
            self.assertTrue((root/'eeg/chunk_000000.npz').exists())

    def test_changed_generation_keeps_source_chunks(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);summary=make(root)
            with np.load(root/'eeg/chunk_000000.npz') as z:arrays={k:z[k].copy() for k in z.files}
            arrays['generation']=2
            np.savez(root/'eeg/chunk_000001.npz',**arrays)
            summary['eeg']['samples']=200
            with self.assertRaisesRegex(RuntimeError,'metadata changed'):standardize_eeg(root,summary,atomic_npz)

    def test_stop_gate_violation_is_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);summary=make(root);standardize_eeg(root,summary,atomic_npz)
            (root/'stop_gate.json').write_text('{"stop_monotonic_ns":175}')
            self.assertEqual(IntegrityChecker().run(root/'eeg').level,'ERROR')

    def test_index_gap_and_invalid_flag_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);summary=make(root)
            path=root/'eeg/chunk_000000.npz'
            with np.load(path) as z:arrays={k:z[k].copy() for k in z.files}
            arrays['sample_indices'][50:]+=5;arrays['valid'][2]=False
            np.savez(path,**arrays);standardize_eeg(root,summary,atomic_npz)
            report=IntegrityChecker().run(root/'eeg')
            stats=report.stats['OmniEegIntegrity']
            self.assertEqual(stats['missing_samples'],5);self.assertEqual(stats['invalid_samples'],1)
            self.assertEqual(report.level,'WARN')


if __name__=='__main__':unittest.main()
