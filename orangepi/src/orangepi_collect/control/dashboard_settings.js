/* Device-setting drafts survive status polls and resolve only against fresh readback. */
(function(global){
 'use strict';
 global.createDashboardSettingsDraft=function({now=()=>Date.now(),timeoutMs=8000}={}){
  let instance=null;
  const eeg={value:null,dirty:false,pending:null,feedback:''};
  const volume={value:null,dirty:false,pending:null,feedback:''};
  function observe(snapshot){
   const current=snapshot.privacy?.instance||snapshot.system?.boot_id||snapshot.boot_id||'';
   if(instance===null||(!instance&&current))instance=current;
   else if(current&&instance&&current!==instance){
    instance=current;eeg.pending=null;volume.pending=null;
    eeg.dirty=eeg.value!==null;volume.dirty=volume.value!==null;
    if(eeg.dirty)eeg.feedback='控制实例已重启，脑电滤波结果需重新确认。';
    if(volume.dirty)volume.feedback='控制实例已重启，头环音量结果需重新确认。';
   }
   const result=snapshot.result||{},preset=snapshot.eeg_config?.filter_preset||'general';
   if(eeg.pending){
    if(result.time!==eeg.pending.previousResultTime&&result.action==='eeg_filter'){
     if(!result.ok){eeg.pending=null;eeg.dirty=true;eeg.feedback='应用失败：'+(result.error||'未能写入本段脑电滤波设置');}
     else if(preset===eeg.pending.value){const completed=eeg.pending,label=completed.pendingLabel;const draftChanged=eeg.value!==completed.value;eeg.pending=null;eeg.dirty=draftChanged;if(!draftChanged)eeg.feedback='已应用：'+label;}
    }
    if(eeg.pending&&preset===eeg.pending.value&&preset!==eeg.pending.previousPreset){const completed=eeg.pending,label=completed.pendingLabel;const draftChanged=eeg.value!==completed.value;eeg.pending=null;eeg.dirty=draftChanged;if(!draftChanged)eeg.feedback='已应用：'+label;}
    if(eeg.pending&&now()-eeg.pending.startedAt>timeoutMs){eeg.pending=null;eeg.dirty=true;eeg.feedback='应用结果尚未确认，请核对当前值后再试。';}
   }
   const audio=snapshot.audio||{};
   if(volume.pending){
    if(result.time!==volume.pending.previousResultTime&&result.action==='volume'&&!result.ok){volume.pending=null;volume.dirty=true;volume.feedback='应用失败：'+(result.error||'头环未确认音量更改');}
    if(volume.pending&&Number.isInteger(audio.volume_generation)&&audio.volume_generation>volume.pending.previousGeneration&&audio.volume_requested===volume.pending.value){
     if(audio.volume_state==='applied'&&audio.volume_applied===volume.pending.value){const completed=volume.pending,draftChanged=volume.value!==completed.value;volume.pending=null;volume.dirty=draftChanged;if(!draftChanged)volume.feedback='已应用并读回：'+audio.volume_applied+'%';}
     else if(['failed','unavailable','disabled'].includes(audio.volume_state)){volume.pending=null;volume.dirty=true;volume.feedback='头环未确认本次音量：'+(audio.volume_error||audio.volume_state);}
    }
    if(volume.pending&&now()-volume.pending.startedAt>timeoutMs){volume.pending=null;volume.dirty=true;volume.feedback='头环尚未确认本次音量，请核对后再应用。';}
   }
   return {eeg:{...eeg},volume:{...volume}};
  }
  return {
   observe,
   chooseEeg(value){eeg.value=value;eeg.dirty=true;eeg.feedback='所选值尚未应用。';},
   submitEeg(value,snapshot){eeg.value=value;eeg.dirty=true;eeg.pending={value,pendingLabel:snapshot.eeg_filter_presets?.[value]?.label||value,previousPreset:snapshot.eeg_config?.filter_preset||'general',previousResultTime:snapshot.result?.time,startedAt:now()};},
   chooseVolume(value){volume.value=value;volume.dirty=true;volume.feedback='新值尚未应用到头环。';},
   submitVolume(value,snapshot){volume.value=value;volume.dirty=true;volume.pending={value,previousGeneration:Number.isInteger(snapshot.audio?.volume_generation)?snapshot.audio.volume_generation:0,previousResultTime:snapshot.result?.time,startedAt:now()};},
  };
 };
})(window);
