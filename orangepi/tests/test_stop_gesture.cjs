'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../src/orangepi_collect/control/dashboard.html'),'utf8');
const source=html.match(/<script id="screen-privacy-controller">([\s\S]*?)<\/script>/)[1];
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function harness(fail=false){
 const global={};vm.runInNewContext(source,{window:global,console});let now=0,calls=0,commits=0,progress=0;
 const snap={state:'recording',revision:7,result:{ok:true,time:1},worker:{recording_active:true},privacy:{policy:'screen_black_before_capture_v1',instance:'pi',generation:1,phase:'recording',required:true,stop_confirmed:false,nonce:'n',session_id:'s'}};
 const screen=global.createPrivacyScreen({now:()=>now,needsLogin:()=>false,visible:()=>true,frame:cb=>cb(),setLocked(){},setLogin(){},post:async()=>{},readStatus:async()=>snap,stop:async()=>{calls++;if(fail)throw Error('request refused')}});
 screen.authorized();await screen.synchronize(snap);
 const handlers={};const doc={hidden:false,addEventListener:(name,fn)=>{(handlers[name]??=[]).push(fn)}};
 const input=global.bindPrivacyInput(screen,doc,()=>now,{frame:()=>{},progress:v=>progress=v,commit:()=>commits++});
 const event=(name,extra={})=>{for(const fn of handlers[name]||[])fn({pointerId:1,button:0,clientX:0,clientY:0,target:{closest:()=>null},preventDefault(){},stopImmediatePropagation(){},...extra})};
 return {screen,doc,input,event,time:n=>now=n,stats:()=>({calls,commits,progress})};
}
(async()=>{
 let h=await harness();h.event('pointerdown');h.time(2999);h.input.sample();assert.equal(h.stats().calls,0);
 h.time(3000);h.input.sample();await flush();assert.equal(h.stats().calls,1);assert.equal(h.stats().commits,1);
 h.time(6000);h.input.sample();assert.equal(h.stats().calls,1);assert.equal(h.screen.canRequestStop(),false);
 h=await harness();h.event('pointerdown');h.time(2999);h.event('pointerup');h.time(3000);h.input.sample();assert.equal(h.stats().calls,0);
 h=await harness();h.event('pointerdown');h.event('pointermove',{clientX:40});h.time(3000);h.input.sample();assert.equal(h.stats().calls,0);
 h=await harness(true);h.event('pointerdown');h.time(3000);h.input.sample();await flush();assert.equal(h.stats().calls,1);assert.equal(h.screen.presentation().stopError,'request refused');
 h.event('pointerup');h.time(3500);h.event('pointerdown');h.time(6500);h.input.sample();await flush();assert.equal(h.stats().calls,2);
 console.log('4/4 real long-press handler cases passed: threshold, early release, movement, failure then retry');
})().catch(error=>{console.error(error);process.exitCode=1});
