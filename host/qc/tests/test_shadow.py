import copy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'orangepi/src/orangepi_collect/control'))
from snapshot_contract import normalize,validate


def shadow_matrix():
    fixture=json.loads((ROOT/'contracts/v1/fixtures/shadow-phase1.json').read_text())
    matrix=[]
    for old in fixture['devices']:
        evidence={'mode':old['mode'],'time':{'utc_trusted':old['clock_trusted']}}
        if old['device_id']=='cp_0015':
            evidence['roles']={side:{'calibration_valid':False} for side in ('left','right')}
        value=normalize(old['device_id'],evidence)
        assert value['mode']==old['mode']
        matrix.append(dict(device_id=old['device_id'],field='mode',classification='known_consistent' if old['mode']=='eeg' else 'legacy_missing_unknown',reason=None if old['mode']=='eeg' else 'NOT_OBSERVED'))
        if old['device_id']=='cp_0015':
            assert value['recording_active'] is None
            assert value['time']['utc_trusted'] is False
            matrix.extend([
                dict(device_id=old['device_id'],field='clock_trusted',classification='known_consistent',reason='DEVICE_CLOCK_APPROX_55H_BEHIND'),
                dict(device_id=old['device_id'],field='roles.calibration_valid',classification='known_consistent',reason='ROLE_PROOF_INVALID'),
                dict(device_id=old['device_id'],field='recording_active',classification='legacy_inference_discarded',reason='WORKER_ACK_ABSENT'),
                dict(device_id=old['device_id'],field='block_size',classification='legacy_inference_discarded',reason='GROUP_POLICY_ABSENT_NOT_LEDGER_ABSENT'),
                dict(device_id=old['device_id'],field='version.runtime',classification='legacy_inference_discarded',reason='SOURCE_NOT_RUNTIME'),
                dict(device_id=old['device_id'],field='trusted_today',classification='conflict',reason='UNTRUSTED_DEVICE_CLOCK'),
                dict(device_id=old['device_id'],field='plan_progress',classification='legacy_missing_unknown',reason='SCOPE_NOT_EXPORTED')])
    # This is NOT cp15 live progress; it proves the explicitly synthetic existing
    # WARN-accepting ledger is retained even without task-block grouping.
    synthetic=json.loads((ROOT/'contracts/v1/fixtures/device.json').read_text())
    synthetic['session']['task_group_id']=None
    validate(synthetic)
    assert synthetic['progress']['completed']==fixture['synthetic_ledger_case']['completed']
    matrix.append(dict(device_id='synthetic-only',field='WARN_accepted_ledger_completed',classification='known_consistent',reason='EXISTING_RULE_UNCHANGED'))
    return matrix


class ShadowTests(unittest.TestCase):
    def test_frozen_summary_and_four_unknown(self):
        actual=shadow_matrix()
        expected=json.loads((ROOT/'docs/system/shadow-diff.json').read_text())
        self.assertEqual(actual,expected)
        self.assertEqual(sum(x['classification']=='legacy_missing_unknown' and x['field']=='mode' for x in actual),4)


if __name__=='__main__':unittest.main()
