/* Derived display only. All voltages/counts and acquisition filters stay intact. */
(function(global){
 'use strict';
 const states=new WeakMap(),finite=Number.isFinite;
 const fmt=(v,counts=false)=>!finite(v)?'—':counts?v.toFixed(0):Math.abs(v)>=10000?v.toFixed(1):Math.abs(v)>=1?v.toFixed(2):v.toFixed(3);
 function controls(card,name){
  if(states.has(card))return states.get(card);
  card.classList.add('has-waveform');
  const eeg=name==='eeg',bar=document.createElement('div');bar.className='wave-controls';
  bar.innerHTML=(eeg?'<label>数据 <select class="wave-source"><option value="display">当前显示滤波</option><option value="raw">原始电压</option></select></label>':'')+
   '<label><input class="wave-center" type="checkbox" checked>居中观察</label><label>纵轴半量程 <select class="wave-scale"><option value="auto">自动（标注刻度）</option>'+ (eeg?[50,100,500,5000]:[100,1000,10000,100000]).map(v=>'<option value="'+v+'">'+v+(eeg?' µV':' counts')+'</option>').join('')+'</select></label>';
  const note=document.createElement('p');note.className='wave-note';note.setAttribute('role','status');
  const quality=document.createElement('section');quality.className='wave-quality';quality.setAttribute('role','status');
  const details=document.createElement('details');details.className='wave-values';details.innerHTML='<summary>查看各通道数值</summary><div class="wave-table-scroll"><table><thead></thead><tbody></tbody></table></div>';
  const canvas=card.querySelector('canvas');card.insertBefore(bar,canvas);canvas.after(note,quality,details);
  const state={card,name,eeg,bar,note,quality,details,canvas,value:{},snapshot:{},receivedAt:0};states.set(card,state);
  bar.addEventListener('change',()=>paint(state));return state;
 }
 function paint(state){
  const {card,eeg,name,canvas,value,snapshot,bar,note,quality,details}=state;
  if(!card.getClientRects().length||document.body.classList.contains('privacy-locked'))return;
  const unit=eeg?'µV':'ADC counts',labels=value.channel_labels||(eeg?(snapshot.eeg_config?.labels?.filter((_,i)=>!(snapshot.eeg_config.disabled_channels||[]).includes(i+1))||['FC3','C3','O1','Pz','Oz','O2','C4','FC4']):Array.from({length:8},(_,i)=>'CH'+(i+1)));
  const count=labels.length,centered=bar.querySelector('.wave-center').checked,scale=bar.querySelector('.wave-scale').value;
  const source=bar.querySelector('.wave-source'),trace=Array.isArray(value.trace)?value.trace:[];
  if(source){source.options[0].textContent=value.mode==='impedance'?'检测原始电压（未滤波）':value.filter_preset==='raw'?'原始电压（未滤波）':'当前显示滤波';source.options[1].disabled=!trace.some(r=>Array.isArray(r[2]))&&value.filter_preset!=='raw';if(source.selectedOptions[0].disabled)source.value='display';}
  const raw=source?.value==='raw',age=Number(snapshot.server_time)-Number(value.time)+(performance.now()-state.receivedAt)/1000;
  const arriving=value.live===true&&finite(age)&&age>=0&&age<3;
  const health=global.analyzeLiveSignalQuality?.(name,value,snapshot,age);
  const live=arriving&&!['missing','unselected'].includes(health?.state);
  if(health){quality.dataset.state=health.state;quality.replaceChildren();const title=document.createElement('b');title.textContent=health.summary;quality.append(title);if(health.issues.length){const list=document.createElement('ul');for(const issue of health.issues){const item=document.createElement('li');item.textContent=issue;list.append(item);}quality.append(list);}if(health.note){const help=document.createElement('small');help.textContent=health.note;quality.append(help);}card.dataset.signalQuality=JSON.stringify(health);}
  const rows=(Array.isArray(value.rows)?value.rows:[]).filter(r=>Array.isArray(r)&&finite(r[0])&&snapshot.server_time-r[0]>=0&&snapshot.server_time-r[0]<3);
  const points=live?trace.filter(r=>Array.isArray(r)&&finite(r[0])&&snapshot.server_time-r[0]>=0&&snapshot.server_time-r[0]<3):[];
  const width=Math.max(280,canvas.clientWidth),height=Math.max(320,canvas.clientHeight),dpr=Math.min(global.devicePixelRatio||1,2);
  if(canvas.width!==Math.round(width*dpr)||canvas.height!==Math.round(height*dpr)){canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);}
  const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,width,height);ctx.font='11px sans-serif';
  const left=48,right=width-82,top=20,bottom=height-22,band=(bottom-top)/count,statistics=[];
  ctx.fillStyle='#9fb5c7';ctx.fillText(centered?'居中显示 · '+unit:'绝对数值 · '+unit,8,12);
  const end=points.length?points[points.length-1][0]:rows.length?rows[rows.length-1][0]:Number(snapshot.server_time);
  for(let ch=0;ch<count;ch++){
   const chosen=points.filter(r=>r[0]>=end-2).map(r=>({time:r[0],value:(raw?(r[2]||r[1]):r[1])?.[ch],index:r[3]}));
   const envelopes=rows.map(r=>({time:r[0],bounds:r[1]?.[ch]})).filter(r=>r.time>=end-2&&Array.isArray(r.bounds)&&r.bounds.length===2&&r.bounds.every(finite));
   const values=live?(chosen.length?chosen.map(r=>r.value).filter(finite):envelopes.flatMap(r=>r.bounds)):[];
   const min=values.length?Math.min(...values):null,max=values.length?Math.max(...values):null;
   const mean=values.length?values.reduce((a,b)=>a+b,0)/values.length:0,offset=centered?mean:0;
   const half=scale==='auto'?Math.max((max-min)||0,eeg?20:20)*.575:Number(scale);
   const middle=scale==='auto'&&values.length?(min+max)/2-offset:mean-offset;
   const lower=middle-half,upper=middle+half,y0=top+ch*band+7,y1=top+(ch+1)*band-7;
   const y=v=>y1-(v-offset-lower)/(upper-lower)*(y1-y0),x=t=>left+(t-(end-2))/2*(right-left);
   ctx.strokeStyle='#30434c';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(left,(y0+y1)/2);ctx.lineTo(right,(y0+y1)/2);ctx.stroke();
   ctx.fillStyle='#b8cbd7';ctx.fillText(String(labels[ch]),3,(y0+y1)/2+4);
   ctx.fillStyle='#9fb5c7';ctx.fillText(values.length?fmt(upper,!eeg):'—',right+7,y0+5);ctx.fillText(values.length?fmt(lower,!eeg):'—',right+7,y1+4);
   ctx.save();ctx.beginPath();ctx.rect(left,y0,right-left,y1-y0);ctx.clip();ctx.strokeStyle=ch%2?'#8cbde5':'#63d6bf';ctx.lineWidth=1.2;ctx.beginPath();
   if(chosen.length){let previous=null;for(const p of chosen){if(!finite(p.value)||x(p.time)<left){previous=null;continue;}const gap=!previous||p.time-previous.time>.02||Number.isInteger(p.index)&&Number.isInteger(previous.index)&&p.index!==previous.index+1;if(gap)ctx.moveTo(x(p.time),y(p.value));else ctx.lineTo(x(p.time),y(p.value));previous=p;}}
   else if(live)for(const p of envelopes){if(x(p.time)<left)continue;ctx.moveTo(x(p.time),y(p.bounds[0]));ctx.lineTo(x(p.time),y(p.bounds[1]));}
   ctx.stroke();ctx.restore();
   statistics.push({channel:labels[ch],min,max,peak_to_peak:values.length?max-min:null,center:values.length?mean:null,axis_min:values.length?lower:null,axis_max:values.length?upper:null});
  }
  ctx.fillStyle='#9fb5c7';for(const t of [-2,-1,0])ctx.fillText(t+' s',left+(t+2)/2*(right-left)-4,height-5);
  const preset=snapshot.eeg_filter_presets?.[value.filter_preset]?.label||value.filter_preset||'未标明';
  const sourceText=eeg?(value.mode==='impedance'?'阻抗检测激励 · 原始电压 · 未经过脑电显示滤波':raw?'原始电压 · 未滤波':preset):'原始有符号 24 位 ADC · 20 ms 包络';
  note.textContent=(live?sourceText:'等待新的实时数据')+'。'+(centered?'图中仅减去各通道窗口中心；数值表保留当前数据源的绝对值。':'图中刻度为当前数据源的绝对值。')+(eeg?' 近 2 秒，µV；断点不连线。':' ADC counts，尚未换算成电压；中心为包络估计。');
  const columns=['通道','窗口最小','窗口最大','峰峰值','窗口中心','纵轴下限','纵轴上限'];
  if(value.mode==='impedance')columns.push('31.25Hz拟合峰值','拟合直流值');
  details.querySelector('summary').textContent='查看各通道数值（'+unit+'）';
  details.querySelector('thead').innerHTML='<tr>'+columns.map(c=>'<th>'+c+'</th>').join('')+'</tr>';
  const body=details.querySelector('tbody');body.replaceChildren();for(const [ch,row] of statistics.entries()){const tr=document.createElement('tr'),cells=[row.channel,row.min,row.max,row.peak_to_peak,row.center,row.axis_min,row.axis_max];if(value.mode==='impedance'){const fit=(snapshot.eeg_impedance?.channels||[]).find(c=>c.channel===(value.physical_channels||[])[ch]);cells.push(fit?.fitted_peak_uv,fit?.dc_offset_uv);}for(const v of cells){const td=document.createElement('td');td.textContent=typeof v==='string'?v:fmt(v,!eeg);tr.append(td);}body.append(tr);}
  card.dataset.waveUnits=unit;card.dataset.waveMode=value.mode||'envelope';card.dataset.waveCentered=String(centered);card.dataset.waveStats=JSON.stringify(statistics);
  const tag=card.querySelector('.live-tag');tag.textContent=live?'● 数据持续到达':'未收到实时数据';tag.classList.toggle('live',live);
  card.querySelector('.signal-meta').textContent=eeg?labels.length+' 个脑电通道 · '+sourceText:(snapshot.emg_calibration?.valid?'已确认左右':'候选 '+(name==='emg_left'?'A':'B'))+' · 单位 ADC counts';
 }
 global.drawLiveSignalPlot=function(card,name,value,snapshot){const state=controls(card,name);state.value=value;state.snapshot=snapshot;state.receivedAt=performance.now();paint(state);};
})(window);
