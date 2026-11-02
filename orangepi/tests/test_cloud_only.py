import sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
from cloud_speaker import Speaker,CloudVoiceError

class CloudOnlyTests(unittest.TestCase):
 def make(self):
  s=Speaker.__new__(Speaker);s.silent=False;s.cancelled=lambda:False;s.cloud=Mock();s.safety=Mock();s.play=Mock()
  s.cloud.config={'foreground_wait_seconds':8,'wait_prompt_after_seconds':3};s.cloud.last_error.return_value=None
  return s
 def test_wait_after_three_seconds_then_cached_cloud_audio(self):
  s=self.make();clock=[0]
  def request(text,wait=0):
   clock[0]+=wait
   return Path('cloud.wav') if clock[0]>=4 else None
  s.cloud.request.side_effect=request
  with patch('cloud_speaker.time.monotonic',side_effect=lambda:clock[0]):s.say('hello')
  s.safety.assert_called_once_with('wait');s.play.assert_called_once_with(Path('cloud.wav'))
 def test_deadline_stops_instead_of_silent_success(self):
  s=self.make();clock=[0]
  def request(text,wait=0):clock[0]+=wait;return None
  s.cloud.request.side_effect=request
  with patch('cloud_speaker.time.monotonic',side_effect=lambda:clock[0]):
   with self.assertRaises(CloudVoiceError):s.say('hello')
  self.assertEqual([c.args[0] for c in s.safety.call_args_list],['wait','timeout','fatal'])
  s.play.assert_not_called()
 def test_api_code_spelled_using_cloud_clips(self):
  s=self.make();s.speak_error('HTTP_401')
  self.assertEqual([c.args[0] for c in s.safety.call_args_list],['api_error','char_h','char_t','char_t','char_p','char_underscore','char_4','char_0','char_1'])
 def test_recording_remains_silent_even_on_cloud_problem(self):
  s=self.make();s.silent=True;s.say('warning');s.cloud.request.assert_not_called();s.safety.assert_not_called()
 def test_cancel_does_not_start_cloud_request(self):
  s=self.make();s.cancelled=lambda:True
  with self.assertRaises(CloudVoiceError):s.say('hello')
  s.cloud.request.assert_not_called()
if __name__=='__main__':unittest.main()
