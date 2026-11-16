'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const control=path.join(__dirname,'../src/orangepi_collect/control');
const flush=async()=>{for(let i=0;i<60;i++)await Promise.resolve();};
function browser(module,fetch,extra={}){
 const nodes=new Map(),timers=new Map(),intervals=[];let serial=0,now=0;
 const node=()=>({children:[],textContent:'',hidden:true,disabled:false,style:{},append(...values){this.children.push(...values);},getContext(){return {drawImage(){}};},click(){}});
 const document={hidden:false,createElement:()=>node(),getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);}};
 const context={window:{},document,fetch,location:{hostname:'cp-0017.localhost',host:'cp-0017.localhost:8765'},innerHeight:800,innerWidth:1000,performance:{now:()=>now},Date,Math,JSON,Promise,AbortController,DOMException,
  URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL(){}},
  createImageBitmap:async()=>({width:640,height:480,close(){}}),
  setTimeout(fn,delay){const id=++serial;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),setInterval:fn=>intervals.push(fn),...extra};
 vm.runInNewContext(fs.readFileSync(path.join(control,module),'utf8'),context);
 async function fire(delay){const entry=[...timers].find(([,t])=>t.delay===delay);assert.ok(entry,'timer '+delay+' exists');timers.delete(entry[0]);entry[1].fn();await flush();}
 return {context,nodes,timers,intervals,node,fire,setNow:value=>now=value};
}
const response=(value,ok=true)=>({ok,status:ok?200:503,json:async()=>value});
test('USB proxy shows verified transport and actual device address, not Wi-Fi/localhost',async()=>{
 const transport={transport:'USB',connected:true,listener_ready:true,device_id:'cp_0017',device_address:'192.168.9.17'};
 const b=browser('phone_connections.js',async url=>response(url==='/__headring/transport'?transport:{links:[]}));let kind,endpoint;
 const root=b.node();b.context.window.createPhoneConnections({root,headers:()=>({}),onKind:k=>kind=k,onEndpoint:e=>endpoint=e});await flush();
 assert.equal(kind,'USB');assert.equal(endpoint.device_address,'192.168.9.17');assert.match(root.children[1].textContent,/192\.168\.9\.17:8765/);assert.doesNotMatch(root.children[0].textContent,/Wi-Fi/);
});
test('old helper/no metadata is unknown transport, never an invented Wi-Fi connection',async()=>{
 const b=browser('phone_connections.js',async()=>response({},false));let kind;
 b.context.window.createPhoneConnections({root:b.node(),headers:()=>({}),onKind:k=>kind=k});await flush();assert.match(kind,/传输待确认/);assert.doesNotMatch(kind,/Wi-Fi/);
});
test('parallel App entry18765 still reports physical device HTTP8765 separately',async()=>{
 const b=browser('phone_connections.js',async url=>response(url==='/__headring/transport'?{transport:'USB',connected:true,listener_ready:true,entry_port:18765,device_id:'cp_0017',device_address:'192.168.9.17'}:{links:[]}),{location:{hostname:'cp-0017.localhost',host:'cp-0017.localhost:18765',port:'18765'}});
 const root=b.node();let kind;b.context.window.createPhoneConnections({root,headers:()=>({}),onKind:k=>kind=k});await flush();
 assert.equal(kind,'USB');assert.match(root.children[0].textContent,/18765/);assert.match(root.children[1].textContent,/192\.168\.9\.17:8765/);
});
test('metadata for another device cannot relabel current origin',async()=>{
 const b=browser('phone_connections.js',async url=>response(url==='/__headring/transport'?{transport:'USB',connected:true,listener_ready:true,device_id:'cp_0023',device_address:'192.168.9.23'}:{links:[]}));let endpoint;
 b.context.window.createPhoneConnections({root:b.node(),headers:()=>({}),onEndpoint:e=>endpoint=e});await flush();assert.equal(endpoint,null);
});
test('metadata timeout clears pending and permits refresh even if fetch ignores abort',async()=>{
 let calls=0;
 const b=browser('phone_connections.js',url=>{calls++;return url==='/__headring/transport'?new Promise(()=>{}):Promise.resolve(response({links:[]}));});
 const ui=b.context.window.createPhoneConnections({root:b.node(),headers:()=>({})});await flush();await b.fire(2500);await flush();const before=calls;ui.refresh();await flush();assert.ok(calls>before);
});
test('shows multiple interface IP addresses',async()=>{
 const b=browser('phone_connections.js',async url=>response(url==='/__headring/transport'?{transport:'USB',connected:true,listener_ready:true,device_id:'cp_0017',device_address:'192.168.9.17'}:{interfaces:[{kind:'wireless',interface:'wlan0',family:'IPv4',address:'192.168.20.101'},{kind:'usb',interface:'usb0',family:'IPv6',address:'fe80::1'}]}));
 const root=b.node();b.context.window.createPhoneConnections({root,headers:()=>({})});await flush();assert.match(root.children[1].textContent,/wlan0.*192\.168\.20\.101/);assert.match(root.children[1].textContent,/usb0.*fe80::1/);
});
