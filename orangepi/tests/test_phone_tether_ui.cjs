'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const control=path.join(__dirname,'../src/orangepi_collect/control');
const app=fs.readFileSync(path.join(control,'phone_app.js'),'utf8');
const start=app.indexOf("let wakeState='released';"),end=app.indexOf("document.addEventListener('visibilitychange'",start);
assert(start>=0&&end>start);
function wake({secure=true,api=true,deny=false,privacy=false}={}){
 const button={dataset:{}},listeners={},sentinel={released:false,addEventListener:(kind,fn)=>listeners[kind]=fn,
  release:async()=>{sentinel.released=true;listeners.release?.();}};
 let requests=0;
 const context={global:{isSecureContext:secure},navigator:api?{wakeLock:{request:async()=>{requests++;if(deny)throw Error('denied');return sentinel;}}}:{},
  document:{hidden:false},safe:()=>!privacy,$:()=>button,sessionStorage:{setItem(){}},
  keepAwake:false,wakeLock:null,wakePending:false,message:()=>assert.fail('wake status must not add a toast over privacy')};
 vm.createContext(context);vm.runInContext(app.slice(start,end),context);
 return {button,context,sentinel,requests:()=>requests,click:()=>button.onclick(),acquire:()=>context.acquireWakeLock(),
  release:()=>{sentinel.released=true;listeners.release?.();}};
}
test('insecure HTTP reports insecure, never claims permission success',async()=>{
 const h=wake({secure:false});await h.click();assert.equal(h.button.dataset.wakeState,'insecure');assert.equal(h.requests(),0);
 assert(!h.button.textContent.includes('允许息屏'));
});
test('unsupported API and actual rejection stay distinct',async()=>{
 const missing=wake({api:false});await missing.click();assert.equal(missing.button.dataset.wakeState,'unsupported');
 const denied=wake({deny:true});await denied.click();assert.equal(denied.button.dataset.wakeState,'denied');
 await denied.acquire();assert.equal(denied.requests(),1,'no repeated denied requests during status updates');
});
test('held only after fulfilled request, OS release is reflected',async()=>{
 const h=wake();const pending=h.click();assert.equal(h.button.dataset.wakeState,'requesting');await pending;
 assert.equal(h.button.dataset.wakeState,'held');h.release();assert.equal(h.button.dataset.wakeState,'released');
});
test('explicit disable releases actual sentinel',async()=>{
 const h=wake();await h.click();await h.click();assert(h.sentinel.released);assert.equal(h.button.dataset.wakeState,'released');
});
test('privacy and background prevent new wake requests or messages',async()=>{
 const h=wake({privacy:true});await h.click();assert.equal(h.requests(),0);
 const hidden=wake();hidden.context.document.hidden=true;await hidden.click();assert.equal(hidden.requests(),0);
});
function connections(){
 const elements=[],intervals=[];let response=null,reject=false;
 const context={window:{},document:{hidden:false,createElement:()=>{const e={style:{}};elements.push(e);return e;}},
  location:{host:'192.168.77.2:8765'},AbortController,setTimeout:()=>1,clearTimeout(){},setInterval:fn=>intervals.push(fn),
  fetch:async()=>{if(reject)throw Error('offline');return {ok:true,json:async()=>response};}};
 vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(control,'phone_connections.js'),'utf8'),context);
 const ui=context.window.createPhoneConnections({root:{append(){}},headers:()=>({})});
 return {elements,ui,set:async(value,fail=false)=>{response=value;reject=fail;await Promise.resolve();await Promise.resolve();await ui.refresh();}};
}
test('transport labels and identity use server classification, no fake ready',async()=>{
 const h=connections();await h.set({configured_transport:'usb_tether',current_transport:'usb_tether',state:'addressed',device_id:'cp_test',controller_identity:'instance',boot_id:'boot',links:[{kind:'usb_tether',origin:'http://192.168.77.2:8765'}]});
 assert.match(h.elements[0].textContent,/USB 网络共享/);assert.match(h.elements[1].textContent,/cp_test/);
 assert.match(h.elements[1].textContent,/仅地址/);assert(!h.elements[0].textContent.includes('就绪'));
});
test('loopback does not claim ADB; failed fetch removes stale addresses',async()=>{
 const h=connections();await h.set({current_transport:'loopback',configured_transport:'adb',state:'absent',links:[]});
 assert.match(h.elements[0].textContent,/不能证明 ADB/);
 await h.set(null,true);assert.match(h.elements[0].textContent,/未知传输/);assert(!h.elements[1].textContent.includes('/phone'));
});
test('complete edited JS parses and privacy protocol stays separate',()=>{
 new vm.Script(app);new vm.Script(fs.readFileSync(path.join(control,'phone_connections.js'),'utf8'));
 const wakeCode=app.slice(start,end);assert(!wakeCode.includes("message("));assert(!wakeCode.includes('privacy_ack'));
 assert(!wakeCode.includes("action('start'"));
});
