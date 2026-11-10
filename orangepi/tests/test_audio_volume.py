import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/audio'))
import advisory_output
import cloud_speaker


class ImmediateJob:
    def __init__(self,argv,result):self.argv=argv;self.result=result
    def poll(self):return self.result
    def stop(self):pass
    def terminate_nowait(self):pass


class AudioVolumeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);(self.base/'configs').mkdir()
        (self.base/'configs/audio_output.json').write_text(json.dumps({
            'policy':'advisory','enabled':True,'server':'unix:/tmp/pulse-socket',
            'headphone_sink':advisory_output.HEADPHONE,'speaker_sink':advisory_output.SPEAKER,
            'player':advisory_output.PLAYER
        }))
        self.router=advisory_output.AudioRouter(self.base,environment={})
        self.commands=[];self.readback=b'Volume: front-left: 65536 / 50% / 0.00 dB, front-right: 65536 / 50% / 0.00 dB'
        def factory(argv,env,timeout):
            self.commands.append(list(argv))
            if argv[3:]==['list','short','sinks']:
                result=advisory_output._Result(code=0,out=('0\t'+advisory_output.SPEAKER+'\tmodule\tformat\n').encode())
            elif 'set-sink-volume' in argv:result=advisory_output._Result(code=0)
            elif 'get-sink-volume' in argv:result=advisory_output._Result(code=0,out=self.readback)
            else:raise AssertionError(argv)
            return ImmediateJob(argv,result)
        self.router._job_factory=factory
        self.router._probe=lambda cancelled,skipped:({advisory_output.SPEAKER},None)

    def test_set_volume_requires_successful_readback_on_selected_sink(self):
        self.router.request_volume(50,request_id=1)
        self.assertTrue(self.router.flush_volume())
        state=self.router.status
        self.assertEqual(state['volume_state'],'applied')
        self.assertEqual(state['volume_requested'],50)
        self.assertEqual(state['volume_applied'],50)
        self.assertEqual(state['volume_sink'],advisory_output.SPEAKER)
        self.assertEqual([cmd[3] for cmd in self.commands if len(cmd)>3 and cmd[3] in ('set-sink-volume','get-sink-volume')],['set-sink-volume','get-sink-volume'])

    def test_mismatched_readback_does_not_reuse_previous_applied_value(self):
        self.router.request_volume(50,request_id=1);self.assertTrue(self.router.flush_volume())
        self.readback=self.readback.replace(b'50%',b'55%')
        self.router.request_volume(50,request_id=2);self.assertFalse(self.router.flush_volume())
        state=self.router.status
        self.assertEqual(state['volume_state'],'failed')
        self.assertEqual(state['volume_error'],'sink_readback_mismatch')
        self.assertEqual(state['volume_applied'],50)
        self.assertNotEqual(self.router._applied.get(advisory_output.SPEAKER),(50,self.router._version))

    def test_old_router_publish_cannot_replace_newer_speaker_request(self):
        speaker=cloud_speaker.Speaker.__new__(cloud_speaker.Speaker)
        speaker._condition=threading.Condition();speaker._generation=0;speaker._closed=False
        speaker._volume_revision=1;speaker._pending_volume=None
        speaker._status={'volume_generation':1,'volume_requested':50,'volume_percent':50,'volume_state':'applied','speaking':False}
        old_changes=speaker._volume_changes(0,{'volume_generation':1,'volume_requested':50,'volume_applied':50,'volume_state':'applied','volume_sink':'old'})
        speaker.set_volume(80)
        speaker._publish(0,**old_changes)
        self.assertEqual(speaker._status['volume_requested'],80)
        self.assertEqual(speaker._status['volume_state'],'pending')


if __name__=='__main__':unittest.main()
