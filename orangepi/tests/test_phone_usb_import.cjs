'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');

class Element{
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attributes={};this.className='';this.classList={toggle:()=>{}};this.hidden=false;this.disabled=false;this.parentElement=null;this.value='';this.textContent='';}
 append(...nodes){for(const node of nodes){if(node&&typeof node==='object'){node.parentElement=this;this.children.push(node);}else this.children.push(node);}}
 appendChild(node){this.append(node);return node;}
 insertBefore(node,before){node.parentElement=this;const i=this.children.indexOf(before);this.children.splice(i<0?this.children.length:i,0,node);}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 setAttribute(key,value){this.attributes[key]=value;}
 removeAttribute(key){delete this.attributes[key];}
 addEventListener(){}
 focus(){}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 querySelectorAll(selector){
  const selectors=selector.split(',').map(x=>x.trim()),out=[];
  const matches=(node,s)=>s==='button,input,select'?['BUTTON','INPUT','SELECT'].includes(node.tagName):s==='input[type=checkbox],input[type=radio]'?node.tagName==='INPUT'&&['checkbox','radio'].includes(node.type):s==='input'?node.tagName==='INPUT':s==='img'?node.tagName==='IMG':s==='button'?node.tagName==='BUTTON':s==='[data-switch-task]'?!!node.dataset?.switchTask:s==='.phone-reference-item button,.phone-reference-selection button'?false:s.startsWith('.')?(node.className||'').split(' ').includes(s.slice(1)):false;
  const walk=node=>{for(const child of node.children||[]){if(child&&typeof child==='object'){if(selectors.some(s=>matches(child,s)))out.push(child);walk(child);}}};walk(this);return out;
 }
}
const document={createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text}),};
let now=10000;const window={document,URL:{revokeObjectURL(){}},URLSearchParams,Set,console,confirm:()=>true};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/orangepi_collect/control/phone_references.js'),'utf8'),{window,document,URLSearchParams,Set,console,Date:{now:()=>now}});
const root=new Element('div');root.parentElement=new Element('main');
let server={state:'complete',updated_at:10},resolveAction,actionCalls=0,statusUnavailable=false;
const state={actions:['reference_list','usb_task_import'],task_list:{selection_scope:'mixed',id:'pool'}};
const jsonResponse=value=>({json:async()=>value});
async function request(url){
 if(url==='/api/phone/reference-access')return jsonResponse({access_code:'code'});
 if(url.startsWith('/api/reference-library?'))return jsonResponse({revision:4,lists:[{id:'list_a',name:'旧列表',deleted:false,readonly:false,count:1,remaining:1}],next_offset:null});
 if(url.startsWith('/api/reference-list?'))return jsonResponse({revision:4,list:{id:'list_a',name:'旧列表',deleted:false,readonly:false,count:1,remaining:1,items:[]},items:[],next_offset:null});
 if(url==='/api/phone/usb-task-import/status'){if(statusUnavailable)throw new TypeError('offline');return jsonResponse(server);}
 if(url==='/api/phone/usb-task-import/scan')return jsonResponse({scan_id:'scan-a',catalog_revision:4,files:[{id:'zip-a',name:'a.zip',relative_path:'a.zip',volume:'USB',size_bytes:100}],old_lists:[{id:'list_a',name:'旧列表',readonly:false,deleted:false}]});
 throw Error('unexpected request '+url);
}
const messages=[];const library=window.createPhoneReferenceLibrary({root,request,status:()=>state,action:()=>{actionCalls++;return new Promise(resolve=>resolveAction=resolve);},message:m=>messages.push(m),current:()=>true});
const byId=id=>{const all=[];const walk=n=>{for(const child of n.children||[])if(child&&typeof child==='object'){all.push(child);walk(child);}};walk(root);return all.find(x=>x.id===id);};
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
async function poll(stateValue){server=stateValue;now+=2001;library.update(state);await tick();}
(async()=>{
 await library.open();
 const scan=byId('phone-reference-usb-scan');assert(scan&&!scan.disabled);
 scan.onclick();await tick();
 const inputs=root.querySelectorAll('input');const zip=inputs.find(x=>x.dataset.zipId==='zip-a');assert(zip);zip.checked=true;zip.onchange();
 const apply=byId('phone-reference-usb-apply');assert(!apply.disabled);apply.onclick();
 assert.equal(actionCalls,1);assert(apply.disabled);
 // A stale idle/terminal response from the previous job cannot unlock a pending request.
 await poll({state:'complete',updated_at:10});assert(byId('phone-reference-usb-scan').disabled);
 resolveAction(false);await tick();await poll({state:'complete',updated_at:10});assert(byId('phone-reference-usb-scan').disabled);
 await poll({state:'building',updated_at:11});assert(byId('phone-reference-usb-scan').disabled);
 await poll({state:'failed',updated_at:12});assert(!byId('phone-reference-usb-scan').disabled);
 // Reopening clears the old selection and reads the active server job before enabling USB actions.
 server={state:'building',updated_at:13};await library.open();
 assert(byId('phone-reference-usb-scan').disabled);assert(byId('phone-reference-usb-apply').disabled);
 // An unavailable optional USB status endpoint must not disable ordinary library actions.
 statusUnavailable=true;server={state:'idle',updated_at:14};
 const root2=new Element('div');root2.parentElement=new Element('main');
 const library2=window.createPhoneReferenceLibrary({root:root2,request,status:()=>state,action:()=>false,message:()=>{},current:()=>true});
 await library2.open();
 assert(root2.querySelectorAll('button').find(x=>x.id==='phone-reference-usb-scan').disabled);
 assert(!root2.parentElement.querySelectorAll('input').find(x=>x.id==='phone-reference-files').disabled);
 console.log('PASS stale USB status cannot unlock a pending submission; fresh terminal status releases it');
})().catch(error=>{console.error(error);process.exitCode=1;});
