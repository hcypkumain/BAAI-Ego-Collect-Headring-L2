const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../src/orangepi_collect/control/phone_capture.js'),'utf8');
async function fixture(meta,options={}){
 const elements={},timers=new Map(),requests=[];let next=1,fetchCalls=0,closed=0,drawn=0;
 const document={hidden:false,getElementById(id){return elements[id]||(elements[id]={hidden:true,disabled:false,textContent:'',click(){},getContext(){return {drawImage(){drawn++;if(options.drawError)throw Error('canvas failed');}};}});}};
 const context={window:{},document,innerHeight:800,innerWidth:1000,performance:{now:()=>0},Date,Math,JSON,Promise,AbortController,
  URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL(){}},
  createImageBitmap:async()=>{if(options.decodeError)throw Error('decode failed');if(options.decoding)await options.decoding;return {width:640,height:480,close(){closed++;}};},
  setTimeout(fn,delay){const id=next++;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);},
  async fetch(url,request){fetchCalls++;requests.push({url,...request});if(options.hide)document.hidden=true;return {ok:options.ok!==false,headers:{get:()=>JSON.stringify(meta)},blob:async()=>({fixture:true})};}};
 vm.runInNewContext(source,context);const api=context.window.createCameraFitCheck({root:{innerHTML:''},active:()=>true,unlocked:()=>true,headers:()=>({}),action(){},back(){},refresh:async()=>{},confirmed(){}});
 const status={actions:['abandon'],camera_check:{eligible:options.eligible!==false,context:'fixture'},server_time:1};api.update(status);api.open();
 const scheduled=[...timers].find(([id,t])=>t.delay===0);assert.ok(scheduled);timers.delete(scheduled[0]);scheduled[1].fn();
 async function flush(){for(let i=0;i<20;i++)await Promise.resolve();}
 await flush();return {elements,fetchCalls,api,status,document,requests,timers,flush,counts:()=>({closed,drawn})};
}
const complete={check_id:'cc_'+'a'.repeat(32),complete:true,camera_count:4};
test('short authenticated JPEG request decodes onto canvas with server proof',async()=>{
 const f=await fixture({check_id:'cc_'+'a'.repeat(32),complete:true,camera_count:4});
 assert.equal(f.fetchCalls,1);assert.equal(f.elements['phone-fit-start'].disabled,false);assert.equal(f.elements['phone-fit-image'].hidden,false);
 assert.equal(f.requests[0].url,'/api/phone/camera-check.jpg?columns=3');assert.equal(f.requests[0].cache,'no-store');
 assert.deepEqual(f.counts(),{closed:1,drawn:1});assert.equal(f.elements['phone-fit-image'].width,640);
});
test('partial snapshot never enables confirmation',async()=>{
 const f=await fixture({check_id:'cc_'+'a'.repeat(32),complete:false,camera_count:4});assert.equal(f.elements['phone-fit-start'].disabled,true);
});
test('malformed proof never enables confirmation',async()=>{
 const f=await fixture({check_id:'invalid',complete:true,camera_count:4});assert.equal(f.elements['phone-fit-start'].disabled,true);
});
test('hidden page cannot display snapshot or enable confirmation',async()=>{
 const f=await fixture(complete,{hide:true});assert.equal(f.elements['phone-fit-start'].disabled,true);assert.equal(f.elements['phone-fit-image'].hidden,true);assert.equal(f.counts().drawn,0);
});
test('decode failure retains the confirmation gate',async()=>{
 const f=await fixture(complete,{decodeError:true});assert.equal(f.elements['phone-fit-start'].disabled,true);assert.equal(f.elements['phone-fit-image'].hidden,true);
});
test('canvas failure closes the bitmap without accepting a frame',async()=>{
 const f=await fixture(complete,{drawError:true});assert.equal(f.elements['phone-fit-start'].disabled,true);assert.equal(f.elements['phone-fit-image'].hidden,true);assert.equal(f.counts().closed,1);
});
test('ineligible preparation cannot confirm even a complete frame',async()=>{
 const f=await fixture(complete,{eligible:false});assert.equal(f.elements['phone-fit-start'].disabled,true);
});
test('suspend clears pixels and stops polling',async()=>{
 const f=await fixture(complete);f.api.suspend();assert.equal(f.elements['phone-fit-image'].width,0);assert.equal(f.elements['phone-fit-image'].height,0);
 assert.equal(f.elements['phone-fit-image'].hidden,true);assert.equal(f.elements['phone-fit-start'].disabled,true);assert.equal(f.timers.size,0);
});
test('old decode cannot reveal a frame after suspend',async()=>{
 let resolve;const decoding=new Promise(r=>{resolve=r;});const f=await fixture(complete,{decoding});f.api.suspend();resolve();await f.flush();
 assert.equal(f.elements['phone-fit-image'].hidden,true);assert.equal(f.elements['phone-fit-start'].disabled,true);assert.deepEqual(f.counts(),{closed:1,drawn:0});assert.equal(f.timers.size,0);
});
test('request timeout cannot accept a late decode',async()=>{
 let resolve;const decoding=new Promise(r=>{resolve=r;});const f=await fixture(complete,{decoding});
 const timeout=[...f.timers.values()].find(t=>t.delay===2200);assert.ok(timeout);timeout.fn();resolve();await f.flush();
 assert.equal(f.requests[0].signal.aborted,true);assert.equal(f.elements['phone-fit-start'].disabled,true);assert.deepEqual(f.counts(),{closed:1,drawn:0});
});
test('new collection context invalidates the previous check',async()=>{
 const f=await fixture(complete);f.api.update({...f.status,camera_check:{eligible:true,context:'new-session'}});
 assert.equal(f.elements['phone-fit-start'].disabled,true);assert.equal(f.elements['phone-fit-image'].hidden,true);
});
test('HTTP error never enables confirmation',async()=>{
 const f=await fixture(complete,{ok:false});assert.equal(f.elements['phone-fit-start'].disabled,true);assert.equal(f.counts().drawn,0);
});
