"""Fail-closed, bounded authentication of the controller's capture generation."""
import json
import urllib.request

SAFE=frozenset(('device_confirm','idle','failed_wait','exhausted'))

def generation(value):
    if not isinstance(value,dict):return None
    privacy=value.get('privacy');worker=value.get('worker')
    if not isinstance(privacy,dict) or not isinstance(worker,dict):return None
    if (value.get('state') not in SAFE or value.get('busy') is not False or value.get('session')
            or privacy.get('required') is not False or privacy.get('stop_confirmed') is not True
            or privacy.get('phase')!='idle' or worker.get('recording_active') is True
            or worker.get('state') not in (None,'idle','stopped','success','cancelled')):return None
    revision=value.get('revision');instance=privacy.get('instance');gen=privacy.get('generation')
    if type(revision) is not int or not isinstance(instance,str) or not instance or type(gen) is not int:return None
    return (value.get('collection_context'),revision,instance,gen)

class CaptureGate:
    def __init__(self, token):self.token=token
    def __call__(self):
        if not self.token:return None
        try:
            req=urllib.request.Request('http://127.0.0.1:8765/api/display-state',headers={'X-Access-Token':self.token})
            with urllib.request.urlopen(req,timeout=.8) as response:
                if response.status!=200:return None
                raw=response.read(65537)
            if len(raw)>65536:return None
            return generation(json.loads(raw))
        except Exception:return None
