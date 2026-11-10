'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const window={};vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/orangepi_collect/control/dashboard_settings.js'),'utf8'),{window,Date,Number});
const html=fs.readFileSync(path.join(__dirname,'../src/orangepi_collect/control/dashboard.html'),'utf8');
const phoneHttp=fs.readFileSync(path.join(__dirname,'../src/orangepi_collect/control/phone_http.py'),'utf8');
assert(html.includes('<script src="/dashboard-settings.js"></script>'));
assert(phoneHttp.includes("'/dashboard-settings.js':('dashboard_settings.js'"));
assert(html.includes("settingsDraft.submitEeg(value,status||{})"));assert(html.includes("settingsDraft.submitVolume(value,status||{})"));
let now=1000;const settings=window.createDashboardSettingsDraft({now:()=>now,timeoutMs:100});
const base={privacy:{instance:'i1'},result:{time:1,action:'start',ok:true},eeg_config:{filter_preset:'general'},eeg_filter_presets:{general:{label:'通用'},eye:{label:'眼动'}},audio:{volume_generation:1,volume_requested:50,volume_applied:50,volume_state:'applied'}};
settings.observe(base);

settings.chooseEeg('eye');settings.submitEeg('eye',base);
let state=settings.observe({...base,eeg_config:{filter_preset:'general'},result:{time:2,action:'start',ok:true}});
assert.equal(state.eeg.dirty,true);assert.equal(state.eeg.pending.value,'eye');
state=settings.observe({...base,eeg_config:{filter_preset:'eye'},result:{time:2,action:'start',ok:true}});
assert.equal(state.eeg.dirty,false);assert.equal(state.eeg.pending,null);assert.equal(state.eeg.feedback,'已应用：眼动');

settings.chooseEeg('eye');settings.submitEeg('eye',{...base,eeg_config:{filter_preset:'general'}});
state=settings.observe({...base,eeg_config:{filter_preset:'general'},result:{time:3,action:'eeg_filter',ok:false,error:'拒绝'}});
assert.equal(state.eeg.dirty,true);assert.equal(state.eeg.pending,null);assert.match(state.eeg.feedback,/拒绝/);

settings.chooseVolume(80);settings.submitVolume(80,base);
state=settings.observe({...base,audio:{volume_generation:1,volume_requested:50,volume_applied:50,volume_state:'applied'},result:{time:4,action:'start',ok:true}});
assert.equal(state.volume.dirty,true);assert.equal(state.volume.pending.value,80);
state=settings.observe({...base,audio:{volume_generation:2,volume_requested:80,volume_applied:80,volume_state:'applied'},result:{time:4,action:'start',ok:true}});
assert.equal(state.volume.dirty,false);assert.equal(state.volume.pending,null);assert.match(state.volume.feedback,/80%/);

settings.chooseVolume(80);settings.submitVolume(80,base);settings.chooseVolume(90);
state=settings.observe({...base,audio:{volume_generation:2,volume_requested:80,volume_applied:80,volume_state:'applied'}});
assert.equal(state.volume.dirty,true);assert.equal(state.volume.value,90);assert.match(state.volume.feedback,/尚未应用/);

settings.chooseVolume(90);settings.submitVolume(90,base);
state=settings.observe({...base,audio:{volume_generation:2,volume_requested:90,volume_applied:80,volume_state:'failed',volume_error:'sink_apply_failed'}});
assert.equal(state.volume.dirty,true);assert.equal(state.volume.pending,null);assert.match(state.volume.feedback,/sink_apply_failed/);

settings.chooseVolume(100);settings.submitVolume(100,base);now+=101;
state=settings.observe(base);assert.equal(state.volume.pending,null);assert.equal(state.volume.dirty,true);assert.match(state.volume.feedback,/尚未确认/);

settings.chooseEeg('eye');settings.submitEeg('eye',base);settings.chooseVolume(75);settings.submitVolume(75,base);
state=settings.observe({...base,privacy:{instance:'i2'}});
assert.equal(state.eeg.pending,null);assert.equal(state.volume.pending,null);assert.equal(state.eeg.dirty,true);assert.equal(state.volume.dirty,true);
console.log('PASS EEG and volume drafts survive stale polls, report readback/failure, and release on timeout or instance restart');
