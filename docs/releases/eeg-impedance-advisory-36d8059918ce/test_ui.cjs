const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('/tmp/opencode/8788-eeg-advisory-work/orangepi/src/orangepi_collect/control/dashboard.html','utf8');
const start=html.indexOf('function impedanceDisplay('), end=html.indexOf('\nfunction renderImpedance(',start);
const ctx={};vm.createContext(ctx);vm.runInContext(html.slice(start,end),ctx);
function fixture(v){const channels=Array.from({length:8},(_,i)=>({channel:i+1,enabled:true,value_kohm:v,reason:v===null?'adc_saturated':'ok'}));return {impedance_acceptance_policy:'advisory',phase:'completed',active:false,pending:false,error:null,window_ready:true,loff_off_confirmed:true,frontend_restored:true,normal_frame_confirmed:true,channels,quality:{policy:'all_enabled_impedance_lt_60kohm_v1',threshold_kohm:60,comparison:'lt',expected_mask:255,expected_count:8,valid_count:v===null?0:8,passed_count:0,window_ready:true,state:'fail',passed:false,channels:channels.map(c=>({channel:c.channel,state:'fail'}))}}}
for(const v of [60,270,1000])assert.strictEqual(ctx.impedanceDisplay(fixture(v),0).canContinue,true);
assert.strictEqual(ctx.impedanceDisplay(fixture(null),0).canContinue,false);
const stale=fixture(270);stale.channels[0].stale=true;assert.strictEqual(ctx.impedanceDisplay(stale,0).canContinue,false);
for(const key of ['active','pending']){const x=fixture(270);x[key]=true;assert.strictEqual(ctx.impedanceDisplay(x,0).canContinue,false)}
console.log('UI high values enabled; absent and stale values disabled');
