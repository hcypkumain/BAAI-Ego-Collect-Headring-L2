/* Descriptive preview checks only. No acquisition/impedance acceptance gate. */
(function(global){
 'use strict';
 const finite=Number.isFinite,MIN=-8388608,MAX=8388607;
 global.analyzeLiveSignalQuality=function(name,value={},snapshot={},ageOverride){
  const eeg=name==='eeg',device=(snapshot.devices||[]).find(d=>d.name===name),selected=snapshot.selected_devices;
  const result={state:'waiting',summary:'等待实时预览数据',issues:[],channels:[],scope:'preview_only'};
  if(device&&device.connected===false){result.state='missing';result.summary='设备未连接'+(Array.isArray(selected)&&!selected.includes(name)?' · 本段未选':'');return result;}
  if(Array.isArray(selected)&&!selected.includes(name)){result.state='unselected';result.summary='本段未选此设备；不参与本段录制';return result;}
  const age=finite(ageOverride)?ageOverride:Number(snapshot.server_time)-Number(value.time);
  if(value.live!==true||!finite(age)||age<0||age>=3){result.state=finite(age)&&age>=3&&value.count>0?'stale':'waiting';result.summary=result.state==='stale'?'实时预览已超时 · 上次更新 '+age.toFixed(1)+' 秒前':'已连接或等待连接 · 尚无新的预览数据';return result;}
  const labels=value.channel_labels||(eeg?(snapshot.eeg_config?.labels?.filter((_,i)=>!(snapshot.eeg_config.disabled_channels||[]).includes(i+1))||['FC3','C3','O1','Pz','Oz','O2','C4','FC4']):Array.from({length:8},(_,i)=>'CH'+(i+1)));
  const now=Number(snapshot.server_time),trace=(value.trace||[]).filter(r=>Array.isArray(r)&&finite(r[0])&&now-r[0]>=0&&now-r[0]<=2.1);
  const rows=(value.rows||[]).filter(r=>Array.isArray(r)&&finite(r[0])&&now-r[0]>=0&&now-r[0]<=2.1);
  const rawAvailable=eeg&&(value.mode==='impedance'||value.filter_preset==='raw'||trace.some(r=>Array.isArray(r[2])));
  let gaps=0;
  const sequence=eeg?trace:rows;
  for(let i=1;i<sequence.length;i++){const a=sequence[i-1],b=sequence[i];if(eeg&&Number.isInteger(a[3])&&Number.isInteger(b[3])){if(b[3]>a[3]+1)gaps++;}else if(b[0]-a[0]>(eeg?.08:.12))gaps++;}
  if(gaps)result.issues.push('预览窗口存在 '+gaps+' 处间断；是否正式丢样需查看采集 QC');
  labels.forEach((label,ch)=>{
   const alerts=[];let samples=[],invalid=0;
   if(eeg&&rawAvailable){for(const row of trace){const rawRow=Array.isArray(row[2])?row[2]:(value.mode==='impedance'||value.filter_preset==='raw'?row[1]:null);const v=rawRow?.[ch];if(finite(v))samples.push(v);else invalid++;}}
   else if(!eeg){for(const row of rows){const pair=row[1]?.[ch];if(Array.isArray(pair)&&pair.length===2&&pair.every(finite))samples.push(...pair);else invalid++;}}
   if(invalid)alerts.push('有无效预览点');
   const duration=sequence.length>1?sequence[sequence.length-1][0]-sequence[0][0]:0;
   if(duration>=1&&samples.length>=(eeg?200:80)&&samples.every(v=>v===samples[0]))alerts.push('窗口内数值不变');
   const bounds=eeg?value.adc_count_bounds?.[ch]:(samples.length?[Math.min(...samples),Math.max(...samples)]:null);
   if(Array.isArray(bounds)&&bounds.length===2&&bounds.every(finite)&&(bounds[0]<=MIN||bounds[1]>=MAX))alerts.push(eeg?'最近批次触及 ADC 边界':'触及 ADC 边界');
   result.channels.push({channel:String(label),alerts});
   if(alerts.length)result.issues.push(String(label)+'：'+alerts.join('、'));
  });
  result.state=result.issues.length?'notice':'fresh';result.summary=result.issues.length?'实时预览有 '+result.issues.length+' 项提示':'实时预览持续更新';
  if(eeg&&!rawAvailable)result.issues.push('尚无原始电压，未判断平直；ADC 边界仅按原始计数检查');
  result.note='以上为预览提示，不代表阻抗结论，不改变采集准入。';return result;
 };
})(window);
