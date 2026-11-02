import sys,tempfile,unittest,json,time
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths,capture,launcher,ledger

class DeliveryTests(unittest.TestCase):
    def test_stop_closes_gate_and_preserves_first_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            c=capture.Capture(Path(root));c.active=True;c.started=True
            c.request_stop();first=c.stop_ns
            self.assertFalse(c.active);self.assertEqual(c.commands.get_nowait(),'STOP')
            self.assertFalse(json.loads((c.path/'stop_gate.json').read_text())['recording_active'])
            time.sleep(.002);c.stop_gate();self.assertEqual(c.stop_ns,first)
    def test_fatal_closes_gate_before_error_queued(self):
        with tempfile.TemporaryDirectory() as root:
            c=capture.Capture(Path(root));c.active=True;c.fail('serial disconnected')
            self.assertFalse(c.active);self.assertTrue((c.path/'stop_gate.json').exists())
            self.assertEqual(c.errors.get_nowait(),'serial disconnected')
    def test_stop_ack_precedes_confirmation_speech(self):
        with tempfile.TemporaryDirectory() as root:
            c=launcher.Controller.__new__(launcher.Controller);c.path=Path(root)
            c.state='recording';c.state_write=Mock();c.worker=Mock();c.voice=Mock()
            def stop(command):
                self.assertEqual(command,'STOP');c.voice.say.assert_not_called()
                (c.path/'stop_gate.json').write_text('{}')
            c.command=Mock(side_effect=stop);c.stop_before_prompt()
            self.assertEqual(c.state,'confirm_stop');c.voice.say.assert_called_once()
    def test_completed_worker_waits_for_decision_without_ledger_write(self):
        c=launcher.Controller.__new__(launcher.Controller);c.worker=Mock();c.state='confirm_stop'
        c.worker.poll.return_value=0;c.voice=Mock()
        with patch('launcher.ledger.mark') as mark:c.poll();mark.assert_not_called()
        c.voice.say.assert_not_called()
    def test_nonfatal_warning_silent_during_recording(self):
        c=launcher.Controller.__new__(launcher.Controller);c.worker=Mock();c.worker.poll.return_value=None
        c.state='recording';c.warned_data_gap=False;c.voice=Mock()
        c.worker_status=Mock(return_value={'state':'recording','warning_count':10})
        c.poll();c.voice.say.assert_not_called();self.assertFalse(c.warned_data_gap)
    def test_legacy_mini_keyboard_keeps_dedicated_grab(self):
        text='N: Name="MINI_KEYBOARD Keyboard"\nH: Handlers=kbd event6\nB: KEY=1000050000000'
        with patch.object(Path,'read_text',return_value=text),patch('keyboard_input.os.open',return_value=99),patch('keyboard_input.fcntl.ioctl') as ioctl:
            keyboard=launcher.Keyboard()
            self.assertEqual(keyboard.names,['MINI_KEYBOARD Keyboard'])
            ioctl.assert_called_with(99,0x40044590,1)

    def test_scan_a_accepts_nonempty_subset_b_rescans(self):
        c=launcher.Controller.__new__(launcher.Controller);c.worker=None;c.state='device_confirm'
        c.quit=False;c.keyboard=None;c.scan_ok=False;c.voice=Mock();c.state_write=Mock();c.scan_devices=Mock()
        c.key('a');self.assertEqual(c.state,'device_confirm')
        c.key('b');c.scan_devices.assert_called_once()
        # A ready device without a side-calibration requirement still accepts subsets.
        c.scan_ok=True;c.scan_available=['board_imu'];c.key('a');self.assertEqual(c.state,'idle');self.assertEqual(c.selected_devices,('board_imu',))
    def test_selected_image_continues_after_it_and_skips_used(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);lib=root/'configs/environments/s';lib.mkdir(parents=True)
            images=[lib/('s_%04d.png'%i) for i in range(1,6)]
            for p in images:p.touch()
            (root/'runtime').mkdir()
            with patch.object(launcher,'PROJECT',root),patch.object(ledger,'BASE',root),patch.object(ledger,'LIBRARY',lib):
                ledger.mark(images[2],root/'used-session','success')
                self.assertEqual(launcher.image_queue('s_0002.png'),[images[1],images[3],images[4]])
if __name__=='__main__':unittest.main()
