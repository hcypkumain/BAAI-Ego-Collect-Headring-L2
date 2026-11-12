'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const html=fs.readFileSync(path.join(__dirname,'../src/orangepi_collect/control/dashboard.html'),'utf8');
const source=html.match(/<script id="screen-privacy-controller">([\s\S]*?)<\/script>/)[1];
const global={crypto:webcrypto};vm.runInNewContext(source,{window:global});
const id='a'.repeat(32);
function snapshot(seq=1){return {state:'ready',busy:false,publish_seq:seq,publish_age_ms:0,_displayRequestedAt:0,
 worker:{state:'ready',recording_active:false},terminal_results:[],privacy:{policy:'screen_black_before_capture_v1',instance:'pi',generation:0,phase:'idle',required:false,stop_confirmed:true,nonce:'',session_id:''}}}
async function fixture(){let locked=true,stop=async()=>{};
 const s=global.createPrivacyScreen({now:()=>0,frame:cb=>cb(),visible:()=>true,needsLogin:()=>false,
  setLocked:x=>{locked=x},setLogin:()=>{},post:async()=>{},readStatus:async()=>snapshot(),stop:async(...args)=>stop(...args)});
 s.authorized();const first=await s.synchronize(snapshot());s.revealIfSafe(first.snapshot);await s.beforeStart(id,{start_ticket:1,ticket_proof:'private'});
 return {s,locked:()=>locked,setStop:x=>{stop=x}};
}
async function show(h,value){const out=await h.s.synchronize(value);if(out.accepted)h.s.revealIfSafe(out.snapshot);return out}
function terminal(seq,commit='not_committed',ok=false){return {request_id:id,start_ticket:1,action:'start',ok,commit_state:commit,publish_seq:seq}}
async function run(){
 {const h=await fixture();const x=snapshot(3);x.terminal_results=[terminal(2)];x.result={action:'prepare',ok:true};await show(h,x);assert.equal(h.locked(),false,'matching failed terminal survives unrelated result');}
 {const h=await fixture();const x=snapshot(2);x.terminal_results=[terminal(2)];await show(h,x);assert(h.locked(),'terminal without newer publication');x.publish_seq=3;x.publish_age_ms=2000;await show(h,x);assert(h.locked(),'stale controller publication');}
 {const h=await fixture();const x=snapshot(4);x.terminal_results=[terminal(2)];x.worker.recording_active=true;await show(h,x);assert(h.locked(),'recording worker');x.worker.recording_active=false;x.busy=true;await show(h,x);assert(h.locked(),'pending action');}
 {const h=await fixture();let seen;h.setStop(async(_s,request)=>{seen=request;throw Error('500/response lost')});assert.equal(await h.s.requestStop(),false);assert.equal(seen.requestId,id);assert.equal(seen.ticket.start_ticket,1);assert(h.locked());assert.match(h.s.presentation().stopError,/500/);}
 {const h=await fixture();const x=snapshot(4);x.terminal_results=[terminal(2,'committed',true)];await show(h,x);assert(h.locked(),'commit without proof generation');x.privacy.generation=2;await show(h,x);assert(!h.locked(),'same-instance stopped proof with later publish');}
 {const h=await fixture();const newer=snapshot(8);await show(h,newer);const old=snapshot(7);old.terminal_results=[terminal(2)];assert.equal((await show(h,old)).accepted,false);assert(h.locked(),'out of order state');}
 {const h=await fixture();const reboot=snapshot(5);reboot.privacy.instance='new';reboot.terminal_results=[terminal(2)];await show(h,reboot);assert(h.locked(),'controller replacement cannot prove old request');const old=snapshot(6);old.terminal_results=[terminal(2)];assert.equal((await show(h,old)).accepted,false);assert(h.locked(),'late old controller response');}
 {const h=await fixture();const wrong=snapshot(5);wrong.terminal_results=[{...terminal(2),request_id:'b'.repeat(32)}];await show(h,wrong);assert(h.locked(),'another request cannot release latch');}
 {const h=await fixture();h.s.disconnected();const x=snapshot(3);x.result={action:'start',ok:false,error:'500'};await show(h,x);assert(h.locked(),'lost/500 response without terminal proof');}
 {const h=await fixture();const x=snapshot(4);x.privacy={...x.privacy,generation:2,phase:'stopping',required:true,stop_confirmed:false,nonce:'n',session_id:'s'};x.terminal_results=[terminal(2,'committed',true),{request_id:id,start_ticket:1,action:'stop',ok:false,error:'STOP failed'}];x.result={action:'volume',ok:true};await show(h,x);assert.match(h.s.presentation().stopError,/STOP failed/);assert(h.locked());}
 {const h=await fixture();const x=snapshot(4);x.terminal_results=[{...terminal(2),start_ticket:2}];await show(h,x);assert(h.locked(),'same request id with another ticket cannot release');}
 {const h=await fixture();const x=snapshot(4);x.privacy.generation=4;await h.s.beforeStart(id,{start_ticket:1,ticket_proof:'private'},'pi',0);x.terminal_results=[terminal(2,'committed',true)];await show(h,x);assert(!h.locked(),'restored original generation permits proven stopped result');}
 console.log('12/12 START lifecycle screen cases passed');
}
run().catch(error=>{console.error(error);process.exitCode=1});
