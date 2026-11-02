import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'scripts/diagnostics/collect_receive_evidence.py'
spec = importlib.util.spec_from_file_location('receive_evidence', path)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class MetricTests(unittest.TestCase):
    def test_iowait_is_not_reported_as_cpu_execution(self):
        row = evidence.cpu_delta({'cpu0': [100, 0, 100, 100, 0, 0, 0, 0]},
                                 {'cpu0': [130, 0, 120, 120, 30, 0, 0, 0]})['cpu0']
        self.assertEqual(row['busy_percent'], 50.)
        self.assertEqual(row['iowait_percent'], 30.)

    def test_reset_or_disappeared_cpu_has_no_negative_utilization(self):
        self.assertFalse(evidence.cpu_delta({'cpu0': [10] * 8}, {'cpu0': [1] * 8}))
        self.assertFalse(evidence.cpu_delta({'cpu0': [10] * 8}, {}))

    def test_thread_stat_handles_spaces_and_parentheses_in_comm(self):
        tail = ['0'] * 40
        tail[0] = 'S'
        tail[11], tail[12], tail[19], tail[36] = '20', '10', '123', '7'
        value = evidence.stat_fields('42 (camera (input) thread) ' + ' '.join(tail))
        self.assertEqual(value, {'state': 'S', 'cpu_ticks': 30, 'start_ticks': 123, 'cpu': 7})


if __name__ == '__main__':
    unittest.main()
