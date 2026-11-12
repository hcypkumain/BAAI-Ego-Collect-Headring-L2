"""Offline synthetic vertical slice; never loads production config or databases.

python3 tools/system_read_model_fixture.py --output /tmp/opencode/fleet-v1.json
python3 tools/system_read_model_fixture.py --serve   # loopback ephemeral only
"""
import argparse
import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
for rel in ('orangepi/src/orangepi_collect/control','host/qc/fleet_monitor','tools/fleet_maintenance'):
    sys.path.insert(0,str(ROOT/rel))
from snapshot_contract import loads, validate, unknown
from snapshot_publisher import SnapshotPublisher
from read_model import ReadModel
from server import Hub,Handler
import web


def synthetic_model():
    clock=lambda:100000
    model=ReadModel(['cp_0014','cp_0015','cp_0018','cp_0020','cp_0022'],clock=clock)
    base=loads((ROOT/'contracts/v1/fixtures/device.json').read_bytes())
    for device,kind in zip(model.rows,('unknown','known','stale','error','reboot')):
        if kind=='unknown':continue
        value=copy.deepcopy(base);value['device_id']=device
        publisher=SnapshotPublisher(device,value['boot_id'],value['controller_instance'],clock=clock)
        if kind=='error':value['sample'].update(quality='error',reason='SYNTHETIC_ERROR')
        item=publisher.publish(value)
        if kind=='error':
            data=loads(item.body);data['sample'].update(quality='error',reason='SYNTHETIC_ERROR');raw=json.dumps(data).encode()
        else:raw=item.body
        model.accept(model.begin_observation(device),raw,identity_verified=True,snapshot_age_ms=20000 if kind=='stale' else 0)
        if kind=='reboot':
            publisher.reset(device,publisher.boot_id)
            model.accept(model.begin_observation(device),publisher.publish().body,identity_verified=True,snapshot_age_ms=0)
    validate(model.snapshot(),'host')
    return model


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);parser.add_argument('--serve',action='store_true');args=parser.parse_args()
    model=synthetic_model();hub=Hub.read_only(model)
    if args.output:args.output.write_text(json.dumps(hub.v1_snapshot(),indent=2)+'\n')
    if not args.serve:
        if not args.output:print(json.dumps(hub.v1_snapshot(),indent=2))
        return
    class FixtureHandler(Handler):
        def do_GET(self):
            route=self.path.split('?',1)[0]
            files={'/receiver.html':ROOT/'host/qc/receiver.html','/fleet-v1.js':ROOT/'host/qc/fleet-v1.js',
                   '/':ROOT/'tools/fleet_maintenance/static/index.html','/app.js':ROOT/'tools/fleet_maintenance/static/app.js','/style.css':ROOT/'tools/fleet_maintenance/static/style.css'}
            if route in files:
                kind='text/html' if route in ('/','/receiver.html') else 'text/javascript' if route.endswith('.js') else 'text/css'
                return self.send(200,files[route].read_bytes(),kind)
            if route in ('/api/v1/fleet','/api/v1/fleet/events'):return super().do_GET()
            # Workbench legacy GETs receive empty point-in-time data. No Jobs.
            if route=='/api/state':return self.send(200,dict(devices={},cache={},jobs={},csrf='',pin='FIXTURE'))
            if route=='/api/authstatus':return self.send(200,dict(unlocked=False))
            return self.send(404,dict(error='FIXTURE_ONLY'))
    FixtureHandler.hub=hub
    server=ThreadingHTTPServer(('127.0.0.1',0),FixtureHandler)
    print('SYNTHETIC ONLY http://127.0.0.1:%d/receiver.html?v1=1 ; workbench / then v1 button'%server.server_port,flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:hub.stop.set();server.server_close()


if __name__=='__main__':main()
