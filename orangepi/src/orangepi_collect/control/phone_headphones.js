/* Pi-owned Bluetooth; the browser is a control/status interface, not an audio relay. */
(function(global){
 'use strict';
 const allowed=new Set(['device_confirm','idle','failed_wait','exhausted','ready']);
 const controls=new Set(['device_confirm','idle','failed_wait','exhausted']);
 const errors={capture_busy:'采集状态已变化，请结束当前步骤后再操作耳机',bluetooth_unavailable:'Pi 蓝牙服务不可用',adapter_missing:'未发现 Pi 蓝牙适配器',bluetooth_off:'Pi 蓝牙尚未开启',permission_denied:'Pi 蓝牙操作权限不足',timeout:'连接超时，请确认耳机开机并在 Pi 附近',device_unavailable:'耳机暂不可用，请开启配对模式后重新搜索',pair_failed:'配对未完成，请将耳机置于配对模式并断开手机连接',trust_failed:'保存蓝牙信任失败，请重试',connect_failed:'连接失败，请确认耳机开机并断开手机连接',audio_unavailable:'蓝牙音频通道尚未就绪，请重新连接',selection_invalid:'已保存的耳机设置无效，请重新选择',selection_save_failed:'耳机选择未能保存',selection_sync_failed:'耳机选择尚未确认落盘，请重试',classic_unsupported:'蓝牙适配器不支持经典蓝牙耳机',classic_disabled:'经典蓝牙未启用，请维护人员加载双模式',classic_not_loaded:'双模式配置尚未在当前蓝牙服务生效，请维护人员检查',classic_config_le_unknown:'磁盘配置仅支持低功耗蓝牙，运行状态尚未核实，请维护人员检查',classic_config_le_enabled:'当前经典蓝牙已启用，但磁盘配置仅低功耗，重启后可能失效',classic_runtime_unknown:'经典蓝牙运行状态尚未核实',scan_failed:'搜索失败，请检查 Pi 蓝牙',scan_busy:'蓝牙正忙，本次扫描未确认，请稍后手动重试',disconnect_failed:'断开未完成，请刷新确认',test_failed:'试听未完成，请确认音频通道就绪',worker_unavailable:'耳机管理后台未就绪',operation_failed:'蓝牙操作未完成，请重试',no_saved_headphone:'尚未选择耳机',cancelled:'操作已取消'};
 global.describePhoneHeadphones=function(data,stale){
  if(!data||stale)return {title:'耳机状态待更新',detail:'连接到 Pi 后自动读取',ready:false};
  const name=data.selected_name||data.selected_address||'未选择耳机';
  const link=data.bluetooth_connected===true?'蓝牙已连接':data.status==='reconnecting'||data.status==='connecting'?'正在连接':'蓝牙未连接';
  const details=[];
  if(data.classic_warning)details.push(errors[data.classic_warning]||'经典蓝牙状态待确认');
  if(data.scan_error)details.push(errors[data.scan_error]||'耳机搜索未确认');
  if(data.error&&data.error!==data.scan_error)details.push(errors[data.error]||'耳机操作未完成');
  return {title:name+' · '+(data.audio_ready?'音频就绪':link),detail:details.length?details.join('；'):data.audio_ready?'提示语音优先通过耳机播放':'在 Pi 上连接耳机后，可点击试听验证',ready:!!data.audio_ready};
 };
 global.phoneHeadphoneNote=function(s,view,lastMessage,idle){
  let note=lastMessage||view.detail;
  if(s.scanning)note='正在搜索附近可用耳机，请等待约 12 秒…';
  else if(s.busy)note=({pairing:'正在配对，无需 Pi 显示屏',trusting:'正在保存蓝牙信任',connecting:'正在连接耳机',waiting_audio:'正在等待音频通道就绪'})[s.pairing_progress]||'正在处理，请稍候…';
  else if(s.test_result==='played'&&!s.error&&!s.scan_error&&!s.classic_warning)note='试听音已发送到耳机，请确认是否听到。';
  else if(s.auto_reconnect&&s.retry_seconds>0&&!s.audio_ready&&!s.error&&!s.scan_error&&!s.classic_warning)note='已记住耳机，将自动重连。';
  return idle?note:'当前采集步骤只显示状态；请在设备确认或空闲时连接、搜索或试听耳机。';
 };
 global.createPhoneHeadphones=function(options){
  const root=options.root,summary=options.summary;
  root.classList.add('phone-headphones');
  root.innerHTML='<h3>蓝牙耳机 · 连接 Pi</h3><p class="phone-hint">手机只负责控制。将耳机开机并置于配对模式；首次连接可先断开手机蓝牙，避免抢连。</p><dl class="phone-headphone-facts"><div><dt>已选耳机</dt><dd data-hp="name">未选择</dd></div><div><dt>蓝牙连接</dt><dd data-hp="link">待更新</dd></div><div><dt>音频通道</dt><dd data-hp="audio">待更新</dd></div><div><dt>连接记忆</dt><dd data-hp="saved">待更新</dd></div><div><dt>耳机电量</dt><dd data-hp="battery">不支持或未上报</dd></div></dl><div class="phone-row" data-hp="controls"></div><p data-hp="message" role="status" class="phone-hint"></p><div data-hp="devices"></div><p class="phone-hint">连接选择成功后会自动保存；Pi 重启或耳机重新开机后自动重连。蓝牙连接不代表已佩戴。</p>';
  if(summary)summary.innerHTML='<div><b data-hp="summary-title">耳机状态待更新</b><p data-hp="summary-detail" class="phone-hint">连接到 Pi 后自动读取</p></div><button type="button">耳机设置</button>';
  summary?.querySelector('button').addEventListener('click',()=>options.open?.());
  const q=key=>root.querySelector('[data-hp="'+key+'"]');
  let latest=null,lastSeen=0,lastTry=-Infinity,pending=null,generation=0,localBusy=false,lastMessage='',deviceSignature='';
  const visible=()=>!document.hidden&&options.unlocked()&&options.active();
  const idle=()=>controls.has(options.status()?.state)&&!options.status()?.busy;
  const text=(node,value)=>{if(node&&node.textContent!==String(value))node.textContent=String(value);};
  const makeButton=(label,fn)=>{const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=fn;return b;};
  const buttons={};
  for(const [action,label] of [['scan','搜索耳机'],['reconnect','重新连接'],['test','试听提示音'],['disconnect','暂时断开'],['forget','取消自动连接']]){
   const b=makeButton(label,()=>request(action));buttons[action]=b;q('controls').append(b);
  }
  function paint(){
   const stale=!lastSeen||performance.now()-lastSeen>6000,view=global.describePhoneHeadphones(latest,stale),s=latest||{};
   text(summary?.querySelector('[data-hp="summary-title"]'),view.title);text(summary?.querySelector('[data-hp="summary-detail"]'),view.detail);
   summary?.classList.toggle('phone-headphones-ready',view.ready);root.classList.toggle('phone-headphones-ready',view.ready);
   text(q('name'),s.selected_name||s.selected_address||'未选择');text(q('link'),stale?'状态待更新':s.bluetooth_connected?'已连接':s.busy?'正在处理连接':'未连接');
   text(q('audio'),stale?'状态待更新':s.audio_ready?'已就绪，可播放提示音':s.bluetooth_connected?'蓝牙已连，等待音频通道':'未就绪');
   text(q('saved'),s.selected_address?(s.auto_reconnect_paused?'已保存 · 暂停重连，Pi 重启后恢复':s.selected_saved?'已保存 · 自动重连':'选择尚未保存'):'未选择默认耳机');
   text(q('battery'),Number.isInteger(s.battery_percent)?s.battery_percent+'%':'不支持或未上报');
   const note=global.phoneHeadphoneNote(s,view,lastMessage,idle());
   text(q('message'),note);
   for(const [action,b] of Object.entries(buttons))b.disabled=!idle()||localBusy||!!s.busy||stale||
    (action==='scan'&&s.scanning)||(action==='test'&&!s.audio_ready)||(action==='disconnect'&&!s.bluetooth_connected)||(['reconnect','forget'].includes(action)&&!s.selected_address);
   const signature=JSON.stringify((s.devices||[]).map(row=>[row.address,row.name,row.connected,row.paired,row.selected]));
   if(signature!==deviceSignature){
    deviceSignature=signature;q('devices').replaceChildren();
    for(const row of s.devices||[]){
     const item=document.createElement('div');item.className='phone-headphone-device';
     const label=document.createElement('span');label.textContent=(row.name||row.address)+' · '+(row.connected?'已连接':row.paired?'已配对 · 未连接':'待配对')+(row.selected?' · 当前选择':'');
     const b=makeButton('连接并保存',()=>request('connect',row.address));item.append(label,b);q('devices').append(item);
    }
   }
   for(const b of q('devices').querySelectorAll('button'))b.disabled=!idle()||localBusy||s.busy||stale;
  }
  async function request(action,address){
   if(pending||!visible()||!allowed.has(options.status()?.state)||action&&!idle())return;
   const identity=generation,controller=new AbortController();pending=controller;lastTry=performance.now();localBusy=!!action;paint();
   const timeout=setTimeout(()=>controller.abort(),1900);
   try{
    const config={headers:options.headers(),cache:'no-store',signal:controller.signal};
    if(action){config.method='POST';config.headers={...config.headers,'Content-Type':'application/json'};config.body=JSON.stringify(address?{action,address}:{action});}
    const response=await fetch('/api/phone/sidecar/headphones',config);const value=await response.json();
    if(identity!==generation||!visible())return;
    if(!response.ok)throw Error(errors[value.error]||value.message||value.error||'耳机状态暂不可用');
    latest=value;lastSeen=performance.now();lastMessage='';
   }catch(error){if(identity===generation&&error.name!=='AbortError')lastMessage=error.message;}
   finally{clearTimeout(timeout);if(pending===controller)pending=null;localBusy=false;paint();}
  }
  const timer=setInterval(()=>{if(visible()&&!pending&&performance.now()-lastTry>=2000)request();else paint();},500);
  paint();
  return {update:paint,refresh:()=>request(),suspend(){generation++;pending?.abort();pending=null;localBusy=false;lastTry=-Infinity;},close(){clearInterval(timer);this.suspend();}};
 };
})(window);
