"""Final EEG gates resolve delayed STOP publication without changing raw data."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
from eeg_qc import standardize_eeg, finalize_eeg_gate
from test_eeg_framework import make, IntegrityChecker


class EegGateFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.summary=make(self.root,n=100)
        self.path=self.root/'eeg/chunk_000000.npz'
        with np.load(self.path) as z:self.arrays={key:z[key].copy() for key in z.files}

    def tail(self,first=75):
        self.arrays['read_begin_monotonic_ns'][first:]=160
        self.arrays['sdk_received_monotonic_ns'][first:]=210
        self.arrays['arrival_monotonic_ns'][first:]=220
        np.savez(self.path,**self.arrays)

    def standardize(self):
        standardize_eeg(self.root,self.summary,np.savez)
        return json.loads((self.root/'eeg/gate_finalization.json').read_text())

    def test_delayed_stop_publication_excludes_whole_tail_batch_preserves_raw(self):
        # These clocks were accepted while STOP's file was not yet visible.
        self.tail()
        original=self.path.read_bytes()
        proof=self.standardize()
        self.assertEqual((proof['input_samples'],proof['kept_samples'],proof['excluded_samples']),(100,75,25))
        self.assertEqual(proof['excluded_batches'],1)
        self.assertEqual((self.root/'eeg/raw_chunks'/self.path.name).read_bytes(),original)
        self.assertEqual(proof['source_chunks'][0]['sha256'],hashlib.sha256(original).hexdigest())
        with np.load(self.root/'eeg/eeg.npz') as z:
            np.testing.assert_array_equal(z['raw_counts'],self.arrays['raw_counts'][:75])
            np.testing.assert_array_equal(z['arrival_monotonic_ns'],self.arrays['arrival_monotonic_ns'][:75])
            np.testing.assert_array_equal(z['eeg_filtered_data'],self.arrays['eeg_filtered_uv'][:75])
            self.assertEqual(int(z['eeg_n_samples']),75)
        self.assertEqual(self.summary['eeg']['samples'],75)
        self.assertEqual(IntegrityChecker().run(self.root/'eeg').level,'INFO')

    def test_completion_exactly_at_stop_is_excluded(self):
        self.tail();self.arrays['arrival_monotonic_ns'][75:]=210
        self.arrays['sdk_received_monotonic_ns'][75:]=200
        np.savez(self.path,**self.arrays)
        (self.root/'stop_gate.json').write_text('{"stop_monotonic_ns":210}')
        self.assertEqual(self.standardize()['excluded_samples'],25)

    def test_worker_accepts_tail_before_stop_file_is_visible_then_final_gate_resolves_it(self):
        from eeg_worker import HostGate
        gate_path=self.root/'stop_gate.json';saved=gate_path.read_bytes();gate_path.unlink()
        gate=HostGate(self.root)
        self.assertTrue(gate.accepts(160,220,210))
        gate_path.write_bytes(saved)
        self.assertFalse(gate.accepts(160,220,210))
        self.tail()
        self.assertEqual(self.standardize()['excluded_samples'],25)

    def test_more_than_one_late_batch_is_audited(self):
        self.tail(50)
        self.arrays['read_begin_monotonic_ns'][75:]=220
        self.arrays['sdk_received_monotonic_ns'][75:]=230
        self.arrays['arrival_monotonic_ns'][75:]=240
        np.savez(self.path,**self.arrays)
        proof=self.standardize()
        self.assertEqual((proof['excluded_samples'],proof['excluded_batches']),(50,2))

    def test_gate_filter_applies_across_flushed_chunks(self):
        self.tail(50)
        second=copy.deepcopy(self.arrays)
        second['sample_indices']+=100
        second['read_begin_monotonic_ns'][:]=220
        second['sdk_received_monotonic_ns'][:]=230
        second['arrival_monotonic_ns'][:]=240
        np.savez(self.path.with_name('chunk_000001.npz'),**second)
        self.summary['eeg']['samples']=200
        proof=self.standardize()
        self.assertEqual((proof['kept_samples'],proof['excluded_samples']),(50,150))
        self.assertEqual(len(list((self.root/'eeg/raw_chunks').glob('*.npz'))),2)

    def test_pre_start_data_remains_error_and_source_is_untouched(self):
        self.arrays['read_begin_monotonic_ns'][0]=99
        np.savez(self.path,**self.arrays);original=self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'pre-START'):self.standardize()
        self.assertEqual(self.path.read_bytes(),original)
        self.assertFalse((self.root/'eeg/eeg.npz').exists())

    def test_invalid_clock_order_remains_error(self):
        self.arrays['sdk_received_monotonic_ns'][0]=190
        np.savez(self.path,**self.arrays)
        with self.assertRaisesRegex(RuntimeError,'clock order'):self.standardize()

    def test_no_remaining_batch_is_error(self):
        (self.root/'stop_gate.json').write_text('{"stop_monotonic_ns":150}')
        with self.assertRaisesRegex(RuntimeError,'no complete'):self.standardize()

    def test_invalid_final_gates_are_error(self):
        (self.root/'stop_gate.json').write_text('{"stop_monotonic_ns":100}')
        with self.assertRaisesRegex(RuntimeError,'invalid final'):self.standardize()

    def test_count_mismatch_cannot_be_hidden_by_tail_exclusion(self):
        self.tail();self.summary['eeg']['samples']=75
        with self.assertRaisesRegex(RuntimeError,'count differs'):self.standardize()

    def test_no_tail_keeps_every_sample(self):
        proof=self.standardize()
        self.assertEqual((proof['kept_samples'],proof['excluded_samples']),(100,0))
        self.assertEqual(self.summary['eeg']['samples'],100)

    def test_metadata_and_capture_summary_agree_with_final_output(self):
        self.tail()
        (self.root/'eeg/metadata.json').write_text(json.dumps({'statistics':{'samples':100,'chunks':1}}))
        (self.root/'eeg/eeg_summary.json').write_text(json.dumps({'samples':100,'chunks':1}))
        self.standardize()
        metadata=json.loads((self.root/'eeg/metadata.json').read_text())
        stored=json.loads((self.root/'eeg/eeg_summary.json').read_text())
        self.assertEqual((metadata['statistics']['samples'],stored['samples'],self.summary['eeg']['samples']),(75,75,75))
        self.assertEqual(metadata['statistics']['samples_before_gate_finalization'],100)

    def test_tampered_finalization_proof_is_error(self):
        proof=self.standardize();proof['excluded_samples']=100
        (self.root/'eeg/gate_finalization.json').write_text(json.dumps(proof))
        self.assertEqual(IntegrityChecker().run(self.root/'eeg').level,'ERROR')

    def test_missing_original_raw_evidence_is_error(self):
        self.tail();self.standardize()
        (self.root/'eeg/raw_chunks'/self.path.name).unlink()
        self.assertEqual(IntegrityChecker().run(self.root/'eeg').level,'ERROR')

    def test_nonincreasing_indices_in_excluded_tail_are_not_hidden(self):
        self.tail();self.arrays['sample_indices'][-1]=0
        np.savez(self.path,**self.arrays)
        with self.assertRaisesRegex(RuntimeError,'non-increasing'):self.standardize()

    def test_repeat_standardization_recovers_accepted_summary_count(self):
        self.tail();proof=self.standardize()
        self.summary['eeg']['samples']=100
        original=(self.root/'eeg/eeg.npz').read_bytes()
        standardize_eeg(self.root,self.summary,np.savez)
        self.assertEqual(self.summary['eeg']['samples'],proof['kept_samples'])
        self.assertEqual((self.root/'eeg/eeg.npz').read_bytes(),original)


if __name__=='__main__':unittest.main()
