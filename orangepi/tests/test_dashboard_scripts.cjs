'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const control=path.join(__dirname,'../src/orangepi_collect/control');
const html=fs.readFileSync(path.join(control,'dashboard.html'),'utf8');

// Parse every complete script, not just extracted privacy/controller helpers.
// A syntax error anywhere in the main script leaves the initial privacy cover
// locked forever, even when the status API is healthy.
let scripts=0;
for(const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)){
 const src=match[1].match(/\bsrc="([^"]+)"/);
 const filename=src?path.basename(src[1].split('?')[0]).replace(/-/g,'_'):'dashboard-inline-'+scripts;
 const code=src?fs.readFileSync(path.join(control,filename),'utf8'):match[2];
 new vm.Script(code,{filename});scripts++;
}
assert(scripts>1);

const start=html.lastIndexOf("$$('#settings-card [data-action]').forEach");
const end=html.indexOf("$('#volume').oninput",start);
assert(start>=0&&end>start,'settings bindings must remain covered');
const binding=html.slice(start,end);

async function click(action,answers,accepted=true){
 const buttons=['shutdown','rescan'].map(action=>({dataset:{action}}));
 const calls=[],prompts=[],toasts=[];
 const context={
  $$:selector=>{assert.equal(selector,'#settings-card [data-action]');return buttons;},
  confirm:message=>{prompts.push(message);return answers.shift();},
  act:async action=>{calls.push(action);return accepted;},
  toast:message=>toasts.push(message)
 };
 vm.runInNewContext(binding,context);
 buttons.find(b=>b.dataset.action===action).onclick();
 await Promise.resolve();
 return {calls,prompts,toasts};
}

(async()=>{
 let result=await click('shutdown',[false]);
 assert.deepEqual(result.calls,[]);assert.equal(result.prompts.length,1);
 result=await click('shutdown',[true,false]);
 assert.deepEqual(result.calls,[]);assert.equal(result.prompts.length,2);
 result=await click('shutdown',[true,true]);
 assert.deepEqual(result.calls,['shutdown']);assert.equal(result.toasts.length,1);
 result=await click('shutdown',[true,true],false);
 assert.deepEqual(result.calls,['shutdown']);assert.deepEqual(result.toasts,[]);
 result=await click('rescan',[]);
 assert.deepEqual(result.calls,['rescan']);assert.deepEqual(result.prompts,[]);
 console.log('PASS complete dashboard scripts parse; shutdown needs both confirmations; ordinary settings still dispatch');
})().catch(error=>{console.error(error);process.exitCode=1;});
