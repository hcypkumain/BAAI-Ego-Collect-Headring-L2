/* Offline display gate regressions. No Pi control or recording requests. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const page=process.argv[2]||path.join(__dirname,'../src/orangepi_collect/control/dashboard.html');
const html=fs.readFileSync(page,'utf8');
for(const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))new vm.Script(match[1]);
const source=html.match(/<script id="screen-privacy-controller">([\s\S]*?)<\/script>/)[1];
const clone=value=>JSON.parse(JSON.stringify(value));
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function idle(){return {state:'ready',revision:7,server_time:100,result:{ok:true,time:99},
 worker:{recording_active:false},privacy:{policy:'screen_black_before_capture_v1',instance:'pi',
 generation:0,phase:'idle',required:false,stop_confirmed:true,nonce:'',session_id:''}};}
async function harness(){
 const global={crypto:webcrypto};vm.runInNewContext(source,{window:global,console});
 const h={now:0,visible:true,locked:true,events:[],gate:idle(),readFails:false,heartbeatFails:false};
 const options={now:()=>h.now,visible:()=>h.visible,needsLogin:()=>false,frame:cb=>cb(),
  setLocked:value=>{h.locked=value;h.events.push(value?'lock':'reveal')},setLogin:()=>{},
  post:async(action)=>{h.events.push(action);if(h.heartbeatFails)throw Error('brief heartbeat delay')},
  readStatus:async()=>clone(h.gate),readDisplayState:async()=>{if(h.readFails)throw Error('unavailable');
   // Match the small production response: no result/server_time payload.
   const s=clone(h.gate);delete s.result;delete s.server_time;return s;}};
 h.screen=global.createPrivacyScreen(options);h.screen.authorized();
 const first=await h.screen.synchronize(idle());h.screen.revealIfSafe(first.snapshot);
 assert.equal(h.locked,false);h.events=[];
 h.tick=async(now)=>{h.now=now;h.screen.tick();await flush()};
 return h;
}
const cases=[];
function test(name,run){cases.push({name,run})}
test('slow full impedance/status refresh does not flash black while the display gate is live',async()=>{
 const h=await harness();for(let t=750;t<=3750;t+=750){await h.tick(t);await h.screen.heartbeat(false)}
 assert.equal(h.locked,false);assert(!h.events.includes('lock'));
});
test('a failed large status fetch does not hide a freshly confirmed idle display',async()=>{
 const h=await harness();await h.tick(750);h.screen.disconnected();assert.equal(h.locked,false);
});
// The budget must outlast a slow-but-working reader (750ms issue interval plus a
// full round trip) yet stay inside the Pi's 5s client lease, so these deadlines
// are checked just past REGISTRATION_DEADLINE rather than at a tighter instant.
test('actual loss of both readers still blacks out inside the Pi lease',async()=>{
 const h=await harness();await h.tick(750);h.readFails=true;
 await h.tick(3000);assert.equal(h.locked,false,'must tolerate a 3s stall');
 await h.tick(4600);assert.equal(h.locked,true,'must black out before the 5s lease');
});
test('recording arming locks and paints before sending its black-screen acknowledgement',async()=>{
 const h=await harness();Object.assign(h.gate.privacy,{generation:1,phase:'arming',required:true,stop_confirmed:false,nonce:'n',session_id:'s'});
 await h.tick(750);assert.equal(h.locked,true);assert(h.events.indexOf('lock')<h.events.indexOf('privacy_ack'));assert(h.events.includes('privacy_ack'));
});
test('a local START latch cannot be revealed by an idle heartbeat',async()=>{
 const h=await harness();await h.tick(750);await h.screen.beforeStart();await h.tick(1500);assert.equal(h.locked,true);
});
test('an asynchronously rejected START can recover after a lightweight refresh',async()=>{
 const h=await harness();await h.tick(750);await h.screen.beforeStart();await h.tick(1500);
 const rejected=idle();rejected.result={ok:false,time:102};rejected.server_time=102;
 const answer=await h.screen.synchronize(rejected);h.screen.revealIfSafe(answer.snapshot);assert.equal(h.locked,false);
});
test('an old idle response cannot reveal a newer recording generation',async()=>{
 const h=await harness();const recording=idle();recording.state='recording';recording.worker.recording_active=true;
 Object.assign(recording.privacy,{generation:2,phase:'recording',required:true,stop_confirmed:false,nonce:'n',session_id:'s'});
 await h.screen.synchronize(recording);await h.tick(750);assert.equal(h.locked,true);
});
test('a malformed gate cannot keep the reference visible',async()=>{
 const h=await harness();h.gate.privacy={};await h.tick(750);assert.equal(h.locked,true);
});
test('a worker that is recording cannot be exposed by an idle privacy phase',async()=>{
 const h=await harness();h.gate.worker.recording_active=true;await h.tick(750);assert.equal(h.locked,true);
});
test('a transient display heartbeat failure does not invalidate a still-live registration',async()=>{
 const h=await harness();await h.tick(750);h.heartbeatFails=true;await h.screen.heartbeat(true);assert.equal(h.locked,false);
 h.readFails=true;await h.tick(4600);assert.equal(h.locked,true);
});
test('a hidden page still locks immediately',async()=>{
 const h=await harness();h.visible=false;await h.tick(750);assert.equal(h.locked,true);
});
test('a changed controller cannot reveal without a fresh registration',async()=>{
 const h=await harness();await h.tick(750);h.gate.privacy.instance='replacement';h.heartbeatFails=true;
 await h.tick(1500);assert.equal(h.locked,true);
});
test('idle timeout never enables a long-press STOP gesture',async()=>{
 const h=await harness();h.readFails=true;h.heartbeatFails=true;await h.tick(4600);
 assert.equal(h.locked,true);assert.equal(h.screen.canRequestStop(),false);
 assert.equal(h.screen.presentation().stale,true);assert.equal(await h.screen.requestStop(),false);
});
test('a live recording enables STOP and confirmed idle removes it',async()=>{
 const h=await harness();h.gate.state='recording';Object.assign(h.gate.privacy,{generation:1,phase:'recording',required:true,stop_confirmed:false,nonce:'n',session_id:'s'});
 await h.tick(750);assert.equal(h.screen.canRequestStop(),true);
 h.gate=idle();h.gate.privacy.generation=2;await h.tick(1500);
 assert.equal(h.screen.canRequestStop(),false);assert.equal(h.locked,false);
});
(async()=>{let failed=0;for(const {name,run} of cases){try{await run();console.log('PASS '+name)}catch(error){failed++;console.error('FAIL '+name+'\n'+error.stack)}}
 console.log(`${cases.length-failed}/${cases.length} passed`);process.exitCode=failed?1:0;})();
