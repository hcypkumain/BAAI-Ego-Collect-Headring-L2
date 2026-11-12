'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../src/orangepi_collect/control/dashboard.html'),'utf8');
const source=html.match(/function impedanceDisplay\(value,serverTime\)\{[\s\S]*?\n\}/)[0];
const context={};vm.runInNewContext(source+';this.display=impedanceDisplay',context);
const state={phase:'completed',active:false,pending:false,error:null,updated_at:1,stale:false,
 impedance_acceptance_policy:'advisory',can_review:true,expected_channel_mask:7,window_ready:true,
 loff_off_confirmed:true,frontend_restored:true,normal_frame_confirmed:true,
 channels:Array.from({length:8},(_,i)=>({channel:i+1,enabled:i<3,value_kohm:i<3?270:null,reason:i<3?'ok':'disabled'})),
 quality:{policy:'all_enabled_impedance_lt_60kohm_v1',threshold_kohm:60,comparison:'lt',expected_mask:7,expected_count:3,valid_count:3,passed_count:0,window_ready:true,passed:false,state:'fail',channels:[]}};
assert.equal(context.display(state,10000).canContinue,true,'completed saved results do not expire');
assert.equal(context.display(state,10000).stale,false);
assert.equal(context.display({...state,can_review:false,reason_codes:['persist_failed']},10000).canContinue,false,'server is authoritative');
assert.equal(context.display({...state,can_review:undefined},10000).canContinue,false,'missing evidence fails closed');
assert.equal(context.display({...state,phase:'measuring'},10000).stale,true,'measurement freshness remains strict');
assert.match(html,/persist_failed:'结果未确认落盘'/);
console.log('5/5 impedance review UI cases passed');
