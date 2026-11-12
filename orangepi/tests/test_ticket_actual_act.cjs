'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {webcrypto}=require('node:crypto');
const html=fs.readFileSync(path.join(__dirname,'../src/orangepi_collect/control/dashboard.html'),'utf8');
const bounded=html.slice(html.indexOf('async function boundedJson('),html.indexOf('function reloadModePage('));
const act=html.slice(html.indexOf('async function act('),html.indexOf('// AbortSignal.timeout'));
const save=html.slice(html.indexOf('function saveStartIntent('),html.indexOf('const MODE_INTENT_KEY='));
const store=new Map(),seen=[];let drop=2;
const url=process.env.TEST_DASHBOARD_URL,token=process.env.TEST_DASHBOARD_TOKEN;
const rawFetch=global.fetch;
const client=async(input,opts={})=>{
 const body=opts.body?JSON.parse(opts.body):{};
 const response=await rawFetch(url+input,opts);
 if(body.action==='start_ticket'){
  seen.push({request_id:body.request_id,start_ticket:body.start_ticket});
  if(drop>0){drop--;await response.text();throw Error('simulated lost response')}
 }
 return response;
};
function context(initial){
 const item={AbortController,DOMException,setTimeout,clearTimeout,crypto:webcrypto,fetch:client,console,
  sessionStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>{if(process.env.TEST_SCENARIO==='storage')throw Error('storage unavailable');store.set(k,v)},removeItem:k=>store.delete(k)},
  START_INTENT_KEY:'pi-start-intent-v2',startIntent:initial,startStorageUsable:true,startRecoveryBusy:false,startRecoveryRestored:false,
  status:null,online:true,inFlight:false,modeIntent:null,phoneCompanion:null,wristPolicyPending:null,collectionContext:'',
  privacyScreen:{isLocked:()=>false,beforeStart:async()=>{},requestStop:async()=>true},
  authHeaders:()=>({'X-Access-Token':token}),toast:()=>{},renderEmg:()=>{},$$:()=>[],$:()=>({disabled:false})};
 item.poll=async()=>{const r=await client('/api/status?signals=&review=0',{headers:item.authHeaders()});item.status=await r.json();item.collectionContext=item.status.collection_context||''};
 item.readDisplayState=async()=>{const r=await client('/api/display-state',{headers:item.authHeaders()});return r.json()};
 vm.createContext(item);vm.runInContext(save+bounded+act,item);return item;
}
(async()=>{
 const first=context(null);await first.poll();
 await first.act('start');
 if(process.env.TEST_SCENARIO==='storage'){
  assert.equal(seen.length,0,'storage failure must not issue ticket or send START');
  console.log('PASS actual act + real Dashboard: storage failure refused before ticket');return;
 }
 assert.equal(seen.length,2);assert.deepEqual(seen[0],seen[1],'lost issue responses retry same identity');
 assert.equal(JSON.parse(store.get('pi-start-intent-v2')).stage,'issuing');
 let second;
 if(process.env.TEST_SCENARIO==='click'){
  await first.act('start');second=first;
 }else{
  // Simulate a reload: sessionStorage survives, proof was never persisted.
  second=context(JSON.parse(store.get('pi-start-intent-v2')));await second.poll();await second.recoverSavedStart();
 }
 assert.equal(store.has('pi-start-intent-v2'),false,'recovered and cancelled orphan');
 assert.equal(seen[2].request_id,seen[0].request_id);assert.equal(seen[2].start_ticket,seen[0].start_ticket);
 await second.poll();await second.act('start');
 assert.equal(seen[3].start_ticket,2,'next START can acquire a fresh ticket');
 assert.equal(JSON.parse(store.get('pi-start-intent-v2')).stage,'submitted');
 console.log('PASS actual act + real Dashboard: two lost replies, reload recovery, next START');
})().catch(e=>{console.error(e);process.exitCode=1});
