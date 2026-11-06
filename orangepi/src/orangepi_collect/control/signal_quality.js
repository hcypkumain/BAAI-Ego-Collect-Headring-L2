/* Descriptive preview checks only. No acquisition acceptance gate. */
(function(global){
 'use strict';
 const finite=Number.isFinite,MIN=-8388608,MAX=8388607;
 global.analyzeLiveSignalQuality=function(name,value={},snapshot={},ageOverride){
  const device=(snapshot.devices||[]).find(d=>d.name===name),selected=snapshot.selected_devices;
  const result={state:'waiting',summary:'等待实时预览数据',issues:[],channels:[],scope:'preview_only'};
  if(device&&device.connected===false){result.state='missing';result.summary='设备未连接'+(Array.isArray(selected)&&!selected.includes(name)?' · 本段未选':'');return result;}
  if(Array.isArray(selected)&&!selected.includes(name)){result.state='unselected';result.summary='本段未选此设备；不参与本段录制';return result;}
  const age=finite(ageOverride)?ageOverride:Number(snapshot.server_time)-Number(value.time);
  if(value.live!==true||!finite(age)||age<0||age>=3){result.state=finite(age)&&age>=3&&value.count>0?'stale':'waiting';result.summary=result.state==='stale'?'实时预览已超时 · 上次更新 '+age.toFixed(1)+' 秒前':'已连接或等待连接 · 尚无新的预览数据';return result;}
  const labels=value.channel_labels||Array.from({length:8},(_,i)=>'CH'+(i+1));
  const now=Number(snapshot.server_time),rows=(value.rows||[]).filter(r=>Array.isArray(r)&&finite(r[0])&&now-r[0]>=0&&now-r[0]<=2.1);
  let gaps=0;
  const sequence=rows;
  for(let i=1;i<sequence.length;i++){const a=sequence[i-1],b=sequence[i];if(b[0]-a[0]>.12)gaps++;}
  if(gaps)result.issues.push('预览窗口存在 '+gaps+' 处间断；是否正式丢样需查看采集 QC');
  labels.forEach((label,ch)=>{
   const alerts=[];let samples=[],invalid=0;
   {for(const row of rows){const pair=row[1]?.[ch];if(Array.isArray(pair)&&pair.length===2&&pair.every(finite))samples.push(...pair);else invalid++;}}
   if(invalid)alerts.push('有无效预览点');
   const duration=sequence.length>1?sequence[sequence.length-1][0]-sequence[0][0]:0;
   if(duration>=1&&samples.length>=80&&samples.every(v=>v===samples[0]))alerts.push('窗口内数值不变');
   const bounds=(samples.length?[Math.min(...samples),Math.max(...samples)]:null);
   if(Array.isArray(bounds)&&bounds.length===2&&bounds.every(finite)&&(bounds[0]<=MIN||bounds[1]>=MAX))alerts.push('触及 ADC 边界');
   result.channels.push({channel:String(label),alerts});
   if(alerts.length)result.issues.push(String(label)+'：'+alerts.join('、'));
  });
  result.state=result.issues.length?'notice':'fresh';result.summary=result.issues.length?'实时预览有 '+result.issues.length+' 项提示':'实时预览持续更新';
  result.note='以上为预览提示，不改变采集准入。';return result;
 };
})(window);
