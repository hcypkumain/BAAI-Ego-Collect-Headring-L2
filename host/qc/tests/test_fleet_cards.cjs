const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(__dirname,'../../..');
const payload=JSON.parse(fs.readFileSync(process.argv[2]||path.join(root,'contracts/v1/fixtures/host.json')));
const context={};vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(root,'host/qc/fleet-v1.js'),'utf8'),context);
const html=context.FleetV1.render(payload);
assert.equal((html.match(/class="v1-card"/g)||[]).length,payload.devices.length);
assert(html.includes(`data-host-instance="${payload.host_instance}"`));
assert(!/<button|<img|green|stopped/.test(html));
assert(html.includes('未知'));
if(payload.devices.some(row=>row.reason==='INSTANCE_CHANGED')){
  for(const state of ['unknown','known','stale','error','reboot'])assert(html.includes(`data-quality="${state}"`),state);
}
const device=JSON.parse(fs.readFileSync(path.join(root,'contracts/v1/fixtures/device.json')));
device.resources.ram_mem_available_bytes=null;device.recording_active=true;device.session.ref_id=null;
const row={device_id:device.device_id,snapshot:device,last_seen_age_ms:0,connectivity:'unreachable',reason:null};
const card=context.FleetV1.card(row);
assert(card.includes('RAM MemAvailable: 未知'));assert(!card.includes('RAM MemAvailable: 0.00'));
assert(card.includes('disk available: 223.52 GiB'));assert(card.includes('录制中'));
assert(card.includes('当前 task refID: 未知'));assert(card.includes('installed_manifest_sha256: 未知'));
assert(card.includes('runtime_manifest_sha256: 未知'));assert(card.includes('approved_release_id: 未知'));
assert(context.FleetV1.card(row,20000).includes('data-quality="stale"'));
row.reason='INSTANCE_CHANGED';assert(context.FleetV1.card(row).includes('data-quality="reboot"'));
device.sample.quality='error';row.reason=null;assert(context.FleetV1.card(row).includes('data-quality="error"'));
device.session.ref_id='<script>';assert(!context.FleetV1.card(row).includes('<script>'));

// Exercise the actual receiver fixture entry, not a fake renderer/button.
const receiver=fs.readFileSync(path.join(root,'host/qc/receiver.html'),'utf8');
const blocks=[...receiver.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
const target={innerHTML:''},app={innerHTML:''},file={};let mounts=[];
const receiverContext={URLSearchParams,location:{search:'?v1=1'},document:{querySelector:()=>app,getElementById:id=>id==='v1-file'?file:target},FleetV1:{mount:(t,p)=>{mounts.push([t,p]);return ()=>{}}},fetch:async()=>({ok:true,json:async()=>payload})};receiverContext.window=receiverContext;
vm.createContext(receiverContext);vm.runInContext(blocks[0],receiverContext);
receiverContext.acceptFleetV1(payload);assert.equal(mounts.at(-1)[1],payload);

// Compile the actual workbench JS, execute only its explicit v1 consumer. The
// legacy job UI is deliberately not initialized by this read-only test.
const appjs=fs.readFileSync(path.join(root,'tools/fleet_maintenance/static/app.js'),'utf8');
const start=appjs.indexOf('let unmountFleetV1=');const end=appjs.indexOf("nav.append(button('v1",start);
const workbenchContext={fetch:async url=>{assert.equal(url,'/api/v1/fleet');return {ok:true,json:async()=>payload}},FleetV1:receiverContext.FleetV1,document:{getElementById:()=>target}};
vm.createContext(workbenchContext);vm.runInContext(appjs.slice(start,end),workbenchContext);
(async()=>{const p=await workbenchContext.loadFleetV1();assert.equal(p,payload);assert.equal(mounts.at(-1)[1],payload);console.log('PASS actual shared renderer + receiver entry + workbench consumer',payload.devices.length,'cards')})().catch(e=>{console.error(e);process.exitCode=1});
