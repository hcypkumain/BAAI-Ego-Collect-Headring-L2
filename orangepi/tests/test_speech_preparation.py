from collections import deque
import threading
import time
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/audio'))
from cloud_speaker import Speaker


class SpeechPreparationTests(unittest.TestCase):
    def speaker(self):
        value=Speaker.__new__(Speaker)
        value._condition=threading.Condition();value._closed=False;value._generation=0
        value._event=threading.Event();value._queue=deque([(0,value._event,9999999999,'prefetch',([('task_title','标题')],'urgent'))])
        value._cancel_flag=False;value._skip_flag=False;value._prompt_event=None;value._pending_volume=None
        value._volume_revision=0;value._retired=deque();value._status=dict(speaking=False,last_outcome='queued')
        value._router=Mock();value._cache=type('Cache',(),{'closed':False,'process':None})();value._cache_generation=0
        return value

    def test_interrupt_keeps_shared_cache_and_fixed_warmup_jobs(self):
        voice=self.speaker();cache=voice._cache
        voice.interrupt()
        self.assertIs(voice._cache,cache);self.assertFalse(cache.closed)
        self.assertEqual([job[3] for job in voice._queue],['prefetch'])
        voice.close()
        self.assertTrue(cache.closed);self.assertTrue(voice._closed)

    def test_terminal_cache_failure_skips_prompt_without_waiting_for_ttl(self):
        voice=Speaker.__new__(Speaker);voice._assets_valid=True;voice._reason=lambda *_args,**_kwargs:''
        voice._get_router=Mock(return_value=object());voice._get_cache=Mock()
        voice._submit_volume=Mock();voice._publish=Mock()
        cache=Mock();cache.config={'enabled':True};cache.request.return_value=None;cache.terminal_error.return_value='missing_credentials'
        voice._get_cache.return_value=cache
        job=(1,threading.Event(),time.monotonic()+30,'text','missing voice prompt')
        started=time.monotonic();voice._perform(job)
        self.assertLess(time.monotonic()-started,.2)
        call=next(item for item in voice._publish.call_args_list if item.kwargs.get('last_outcome')=='failed')
        self.assertIn('missing_credentials',call.kwargs['warning'])


if __name__=='__main__':unittest.main()
