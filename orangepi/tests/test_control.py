import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import unittest
from unittest.mock import Mock
import random
import capture
import launcher

class FrameTests(unittest.TestCase):
    def test_fragmented_stream_resynchronizes(self):
        frames=[capture.HEADER+bytes([0xaa if i%10 else 0xbb,i%256])+bytes([i%251])*24 for i in range(300)]
        stream=b'noise'+b''.join(frames)
        rng=random.Random(20260922); buf=bytearray(); got=[]
        while stream:
            n=rng.randint(1,97); buf.extend(stream[:n]); stream=stream[n:]; got.extend(capture.parse_frames(buf))
        self.assertEqual(got,frames)
        self.assertLess(len(buf),29)

    def test_noise_memory_bounded(self):
        buf=bytearray(b'bad'*100000+b'\xd2\xd2')
        self.assertEqual(capture.parse_frames(buf),[])
        self.assertEqual(buf,b'\xd2\xd2')

    def test_signed_24bit(self):
        values=[-8388608,-1,0,1,8388607,-9,34,-1000]
        self.assertEqual(capture.s24(b''.join(x.to_bytes(3,'big',signed=True) for x in values)),values)

class ControlTests(unittest.TestCase):
    def controller(self,state):
        c=launcher.Controller.__new__(launcher.Controller)
        c.state=state;c.keyboard=None;c.image='reference';c.quit=False;c.voice=Mock();c.state_write=Mock()
        for name in ['poll','prepare','begin','finish','announce','discard_and_retry']:
            setattr(c,name,Mock())
        return c

    def test_three_a_presses(self):
        for state,action in [('idle','prepare'),('ready','begin'),('confirm_stop','finish')]:
            c=self.controller(state);c.key('a');getattr(c,action).assert_called_once()

    def test_enter_gate_and_stop_confirmation(self):
        c=self.controller('waiting_enter');c.key('a');c.prepare.assert_not_called()
        c.key('enter');self.assertEqual(c.state,'idle')
        c=self.controller('recording');c.stop_before_prompt=Mock();c.key('a')
        c.stop_before_prompt.assert_called_once();c.poll.assert_not_called();c.finish.assert_not_called()

    def test_b_retries_only_after_confirmation(self):
        c=self.controller('confirm_stop');c.key('b')
        c.discard_and_retry.assert_called_once();c.finish.assert_not_called()

    def test_b_does_not_start_recording(self):
        c=self.controller('ready');c.key('b');c.announce.assert_called_once();c.begin.assert_not_called()

    def test_ignore_a_during_finalizing(self):
        c=self.controller('finalizing');c.key('a');c.prepare.assert_not_called();c.begin.assert_not_called()

    def test_ignore_b_during_recording(self):
        c=self.controller('recording');c.key('b');c.announce.assert_not_called()

    def test_original_emg_guidance_is_spoken(self):
        c=self.controller('error')
        c.troubleshooting('emg_left no first data')
        c.voice.say.assert_called_with('拔插 USB 适配器,按紧接线(臂环接口容易松)')

    def test_confirm_stop_precedes_warning_poll(self):
        c=self.controller('confirm_stop');c.worker=Mock();c.worker.poll.return_value=None
        c.key('a');c.finish.assert_called_once();c.poll.assert_not_called()

    def test_escape_exits(self):
        c=self.controller('recording');c.key('escape');self.assertTrue(c.quit)

if __name__=='__main__':unittest.main()
