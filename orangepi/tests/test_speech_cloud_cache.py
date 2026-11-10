import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import wave
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/audio'))
from speech_cloud import CloudCache


def write_wav(path,percent=50):
    path.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(path),'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000);out.writeframes(b'\0\0'*240)


class SpeechCloudCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);(self.base/'configs').mkdir()
        self.config=dict(enabled=True,model='test',voice='Cherry',url='wss://test',language='Chinese',
            request_timeout_seconds=1,retry_initial_seconds=.05,retry_max_seconds=.1)
        self.caches=[]

    def cache(self,runner):
        result=CloudCache(self.base,config=self.config,runner=runner);self.caches.append(result);self.addCleanup(result.close);return result

    def test_valid_cache_hit_does_not_generate_again(self):
        calls=[]
        cache=self.cache(lambda text,path:(calls.append(text),write_wav(path),{'ok':True})[-1])
        first=cache.request('稳定测试句',wait=2,prompt_id='stable_test')
        self.assertTrue(first.is_file());self.assertEqual(calls,['稳定测试句'])
        self.assertEqual(cache.request('稳定测试句'),first);self.assertEqual(calls,['稳定测试句'])
        self.assertEqual(cache.manifest()[0]['state'],'ready')

    def test_pre_versioned_valid_cache_is_reused_without_generation(self):
        calls=[];cache=self.cache(lambda text,path:(calls.append(text),write_wav(path),{'ok':True})[-1])
        legacy=cache._legacy_path('old cached prompt');write_wav(legacy)
        self.assertEqual(cache.request('old cached prompt'),legacy);self.assertEqual(calls,[])

    def test_corrupt_cache_is_quarantined_and_rebuilt(self):
        calls=[];cache=self.cache(lambda text,path:(calls.append(text),write_wav(path),{'ok':True})[-1])
        path=cache.path('damaged');path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'not a wav')
        self.assertTrue(cache.request('damaged',wait=2).is_file())
        self.assertEqual(calls,['damaged']);self.assertTrue(list(path.parent.glob(path.stem+'.corrupt-*.wav')))

    def test_format_and_voice_are_part_of_the_cache_key(self):
        cache=self.cache(lambda text,path:{'ok':False,'error':'not used'})
        first=cache.path('same');other=CloudCache(self.base,config=dict(self.config,voice='Other'),runner=lambda *_:{'ok':False})
        self.caches.append(other);self.addCleanup(other.close)
        self.assertNotEqual(first,other.path('same'))
        other.config['language']='English';self.assertNotEqual(first,other.path('same'))

    def test_failed_prompt_rotates_and_does_not_block_next_prompt(self):
        attempts={'slow':0};slow_started=threading.Event()
        def runner(text,path):
            if text=='slow':
                attempts['slow']+=1;slow_started.set()
                return {'ok':False,'error':'TimeoutError'}
            write_wav(path);return {'ok':True}
        cache=self.cache(runner)
        cache.request('slow',priority='warm',prompt_id='slow')
        self.assertTrue(slow_started.wait(1))
        ready=cache.request('next',wait=2,priority='urgent',prompt_id='next')
        self.assertTrue(ready.is_file());self.assertEqual(attempts['slow'],1)
        deadline=time.monotonic()+1
        while time.monotonic()<deadline and cache.errors.get(cache.digest('slow')) is None:time.sleep(.01)
        self.assertEqual(cache.rows[cache.digest('next')]['state'],'ready')
        self.assertLessEqual(attempts['slow'],3)

    def test_per_key_lock_is_singleflight_and_cache_miss_has_target_path(self):
        started=threading.Event();release=threading.Event()
        def runner(text,path):
            started.set();release.wait(1);write_wav(path);return {'ok':True}
        cache=self.cache(runner)
        target=cache.path('lock');first=cache._acquire_key_lock(target);self.assertIsNotNone(first)
        try:self.assertIsNone(cache._acquire_key_lock(target))
        finally:
            import fcntl;fcntl.flock(first,fcntl.LOCK_UN);__import__('os').close(first)
        cache.request('queued',priority='urgent')
        key=cache.digest('queued')
        self.assertTrue(started.wait(1))
        self.assertEqual(cache.pending[key]['path'],cache.path('queued'))
        release.set()

    def test_concurrent_same_text_uses_one_generator(self):
        calls=[];started=threading.Event();release=threading.Event()
        def runner(text,path):
            calls.append(text);started.set();release.wait(1);write_wav(path);return {'ok':True}
        cache=self.cache(runner)
        cache.request('same text')
        self.assertTrue(started.wait(1))
        path=cache.request('same text',wait=2,priority='urgent')
        release.set()
        deadline=time.monotonic()+1
        while not path and time.monotonic()<deadline:path=cache.lookup('same text');time.sleep(.01)
        self.assertTrue(path.is_file());self.assertEqual(calls,['same text'])


if __name__=='__main__':unittest.main()
