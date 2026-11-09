'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../src/orangepi_collect/control/dashboard.html'),'utf8');
const bounded=html.slice(html.indexOf('async function boundedJson('),html.indexOf('function reloadModePage('));
const stop=html.slice(html.indexOf('async function privacyStop('),html.indexOf('async function privacyDiscard('));
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 let passed=0;
 for(const bodyHangs of [false,true]){
  let expire,refresh=0,request;
  const context={AbortController,DOMException,authHeaders:()=>({}),poll:()=>{refresh++},
   setTimeout:(fn,delay)=>{assert.equal(delay,4000);expire=fn;return 1},clearTimeout(){},
   fetch:async(url,config)=>{request=JSON.parse(config.body);if(bodyHangs)return {ok:true,json:()=>new Promise(()=>{})};return new Promise(()=>{});}};
  vm.createContext(context);vm.runInContext(bounded+stop,context);
  const privacy={instance:'boot',session_id:'session',nonce:'nonce',generation:4};
  const promise=context.privacyStop({revision:7,collection_context:'eeg:9',privacy});
  const check=assert.rejects(promise,e=>e.name==='TimeoutError');
  await flush();expire();await check;
  assert.deepEqual(request,{action:'stop',revision:7,collection_context:'eeg:9',privacy_target:privacy});assert.equal(refresh,1);passed++;
 }
 let refresh=0;
 const context={AbortController,DOMException,setTimeout,clearTimeout,authHeaders:()=>({}),poll:()=>{refresh++},
  fetch:async()=>({ok:false,json:async()=>({error:'流程已变化'})})};
 vm.createContext(context);vm.runInContext(bounded+stop,context);
 await assert.rejects(context.privacyStop({revision:7,collection_context:'emg:3'}),/流程已变化/);
 assert.equal(refresh,1);passed++;
 console.log(`${passed}/3 STOP transport cases passed (hung request, hung body, rejection)`);
})().catch(error=>{console.error(error);process.exitCode=1});
