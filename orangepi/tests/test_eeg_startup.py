"""Bounded pre-recording command retries; never reset USB or hide failures."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
from serial_v1_impedance import SerialV1Board


class EegStartupTests(unittest.TestCase):
    def board(self):
        board=SerialV1Board.__new__(SerialV1Board)
        board.transport=Mock();board.closed=False;board.running=False;board.frames=[];board.generation=0
        board.startup_diagnostics={'config_queries':0,'start_commands':0}
        board.parser=SimpleNamespace(crc_errors=2,discarded=3)
        return board

    def test_slow_boot_reissues_readonly_config_inside_same_budget(self):
        board=self.board();board._wait=Mock(side_effect=[TimeoutError(),{'verified':True}])
        frontend=Mock();sdk=SimpleNamespace(FrontendConfig=SimpleNamespace(_from_wire=frontend))
        with patch.dict(sys.modules,{'omnibci':sdk}):board._query_config(5)
        self.assertEqual(board.startup_diagnostics['config_queries'],2)
        self.assertEqual([call.args[0] for call in board.transport.write.call_args_list],[b'c',b'c'])

    def test_config_timeout_stops_at_absolute_deadline_with_diagnostics(self):
        board=self.board();board._wait=Mock(side_effect=TimeoutError())
        sdk=SimpleNamespace(FrontendConfig=Mock())
        with patch.dict(sys.modules,{'omnibci':sdk}),patch('serial_v1_impedance.time.monotonic',side_effect=[0,0,.1,5.1]):
            with self.assertRaisesRegex(TimeoutError,'crc=2 discarded=3'):board._query_config(5)
        self.assertEqual(board.transport.write.call_count,1)

    def test_no_first_frame_retries_start_without_reopening_port(self):
        board=self.board();board._wait=Mock(side_effect=[TimeoutError(),object()])
        board.start(3);self.assertTrue(board.running);self.assertEqual(board.generation,1)
        self.assertEqual([call.args[0] for call in board.transport.write.call_args_list],[b'b',b'b'])

    def test_start_timeout_issues_stop_and_remains_failed(self):
        board=self.board();board._wait=Mock(side_effect=TimeoutError())
        with patch('serial_v1_impedance.time.monotonic',side_effect=[0,0,.1,3.1]):
            with self.assertRaises(TimeoutError):board.start(3)
        self.assertFalse(board.running);self.assertEqual(board.transport.write.call_args.args[0],b's')

if __name__=='__main__':unittest.main()
