/* Run with: gjs -m orangepi/tests/test_mode_switch_confirmation.js */
import GLib from 'gi://GLib';

const [filename]=GLib.filename_from_uri(import.meta.url);
const sourcePath=ARGV[0]||GLib.build_filenamev([GLib.path_get_dirname(filename),'../src/orangepi_collect/control/phone_shell.js']);
const [,bytes]=GLib.file_get_contents(sourcePath);
const source=new TextDecoder().decode(bytes);
const match=source.match(/  function requestCollectionMode\(mode\)\{[\s\S]*?\n  \}/);
if(!match)throw Error('Missing production mode confirmation handler');

let checks=0;
function assert(value,message){checks++;if(!value)throw Error(message);}
function fixture(answer){
 const calls=[],prompts=[];
 const result=Promise.resolve('existing action result');
 const options={action:(...args)=>{calls.push(args);return result;}};
 const global={confirm:message=>{
  assert(calls.length===0,'No mode action may run before confirmation');
  prompts.push(message);
  if(answer instanceof Error)throw answer;
  return answer;
 }};
 const request=Function('global','options','return ('+match[0].trim()+');')(global,options);
 return {request,calls,prompts,result};
}

for(const [mode,label] of [['emg','肌电版'],['eeg','脑电版（含肌电）']]){
 const accepted=fixture(true);
 assert(accepted.request(mode)===accepted.result,'Preserve the existing action result');
 assert(accepted.calls.length===1,'Confirmation must submit exactly one action');
 assert(accepted.calls[0][0]==='collection_mode'&&accepted.calls[0][1]===mode,'Preserve the selected mode and action');
 assert(accepted.prompts[0].includes('是否确认切换到“'+label+'”？'),'Name the target mode clearly');
 assert(accepted.prompts[0].includes('取消将保持当前模式'),'Explain cancellation');
 for(const answer of [false,undefined,null]){
  const cancelled=fixture(answer);
  assert(cancelled.request(mode)===false,'Cancellation or dismissal must not switch modes');
  assert(cancelled.calls.length===0,'Cancellation must not call the existing action');
 }
}

for(const mode of ['unknown','',null,undefined]){
 const invalid=fixture(true);
 assert(invalid.request(mode)===false,'Reject unsupported modes');
 assert(invalid.prompts.length===0&&invalid.calls.length===0,'Invalid modes must not prompt or submit');
}
const unavailable=fixture(Error('Dialog unavailable'));
let failed=false;
try{unavailable.request('emg');}catch(error){failed=error.message==='Dialog unavailable';}
assert(failed&&unavailable.calls.length===0,'A failed dialog must never submit a mode action');
assert(source.includes("for(const mode of ['emg','eeg'])$('phone-mode-'+mode).onclick=()=>requestCollectionMode(mode);"),'Both mode buttons must use the confirmation handler');
assert(!source.includes(".onclick=()=>options.action('collection_mode',mode)"),'Do not retain a direct mode-switch click handler');

print('Mode switch confirmation: '+checks+' assertions passed; no hardware or network requests.');
