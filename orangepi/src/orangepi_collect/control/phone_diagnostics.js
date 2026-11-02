/* On-demand read-only diagnostics; all acquisition remains owned by Pi. */
(function(global){
 'use strict';
 global.createPhoneDiagnostics=function({root,headers,status,unlocked,active}){
  root.innerHTML='<details id="phone-diagnostics"><summary>设备、USB 与诊断包</summary><p class="phone-hint">查看实际接入方式与协商速率。导出包包含状态、近期日志及已有 QC，不含图像、原始波形或网络密码。</p><div class="phone-row"><button id="phone-diagnostics-refresh">刷新诊断</button><button id="phone-diagnostics-export">导出诊断包</button></div><p id="phone-diagnostics-message" role="status">展开后读取诊断；采集中暂停此功能。</p><div id="phone-diagnostics-body"></div></details>';
  const $=id=>document.getElementById(id),details=$('phone-diagnostics'),body=$('phone-diagnostics-body'),message=$('phone-diagnostics-message');
  let version=0,controller=null,busy=false,loaded=false;
  const safe=()=>active()&&!document.hidden&&unlocked()&&['device_confirm','idle','failed_wait','exhausted','confirm_stop'].includes(status()?.state);
  const text=(tag,value,className)=>{const node=document.createElement(tag);node.textContent=String(value??'');if(className)node.className=className;return node;};
  function controls(){for(const id of ['phone-diagnostics-refresh','phone-diagnostics-export'])$(id).disabled=busy||!safe();}
  function suspend(){version++;controller?.abort();controller=null;busy=false;controls();}
  function render(value){
   body.replaceChildren();const info=value.status||{},usb=value.usb||{};
   body.append(text('p','读取时间：'+new Date(value.created_at_unix*1000).toLocaleString(),'phone-hint'));
   const trigger=value.system?.headband_trigger_hz_from_device_tree||{};if(Object.keys(trigger).length)body.append(text('p','头环同步触发配置：'+Object.entries(trigger).map(([name,hz])=>name+' '+hz+' Hz').join(' · ')+'。实际话题帧率需单独测量。','phone-hint'));
   const devices=text('div','','phone-diagnostic-devices');
   for(const role of usb.roles||[]){const row=text('article','','phone-diagnostic-device');row.append(text('b',role.label||role.name));
    let detail=!role.connected?'未连接':role.transport==='camd'?'已连接 · camd 相机服务':role.transport==='character_device'?'已连接 · 板载 IMU 直接读取':role.transport==='ros'?'已连接 · ROS 话题':role.port?'已连接 · USB '+role.port:'已连接 · USB 路径暂未映射';
    if(role.connected&&role.transport==='usb'&&role.speed_mbps!=null)detail+=' · '+role.speed_mbps+' Mb/s';
    const device=(info.devices||[]).find(d=>d.name===role.name);
    if(role.connected&&role.name.startsWith('cam_'))detail+=device?.preview_fresh?' · 有近期预览帧':' · 预览尚未启用或无近期帧';
    row.append(text('p',detail));if(role.camd)row.append(text('small',role.socket+' · '+role.camd));else if(role.device)row.append(text('small',role.device));else if(role.topic)row.append(text('small',role.topic));devices.append(row);
   }body.append(devices);
   if(usb.error)body.append(text('p',usb.error,'phone-diagnostic-notice'));
   for(const item of usb.warnings||[])body.append(text('p',item.message,'phone-diagnostic-notice'));
   const ports=document.createElement('details');ports.append(text('summary','查看 USB 拓扑（'+(usb.devices||[]).length+' 个设备）'));
   const list=document.createElement('ul');for(const device of usb.devices||[])list.append(text('li',device.port+' · '+device.name+' · '+(device.speed_mbps==null?'速率未知':device.speed_mbps+' Mb/s')+(device.drivers?.length?' · '+device.drivers.join(', '):'')));ports.append(list,text('p',usb.rate_note||'','phone-hint'));body.append(ports);
   const events=document.createElement('details');events.append(text('summary','近 15 分钟 USB 内核事件（'+(value.kernel_events||[]).length+'）'));const log=document.createElement('pre');log.textContent=(value.kernel_events||[]).join('\n')||value.kernel_log_note||'未发现相关内核事件';events.append(log);body.append(events);
  }
  async function load(download=false){
   if(busy||!safe())return;const id=++version,request=new AbortController();controller=request;busy=true;controls();message.textContent=download?'正在生成诊断包…':'正在读取设备与 USB 信息…';
   const timeout=setTimeout(()=>request.abort(),10000);
   try{
    const response=await fetch('/api/phone/diagnostics'+(download?'.zip':''),{headers:headers(),cache:'no-store',signal:request.signal});
    if(!response.ok){const error=await response.json().catch(()=>({}));throw Error(error.error||'诊断读取失败，请重试');}
    if(download){const blob=await response.blob();if(id!==version||!safe())return;const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;const filename=response.headers.get('Content-Disposition')?.match(/filename="(pi-diagnostics-[\d-]+\.zip)"/);link.download=filename?.[1]||'pi-diagnostics.zip';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);message.textContent='诊断包已生成，请在浏览器下载列表中查看。';}
    else {const value=await response.json();if(id!==version||!safe())return;render(value);loaded=true;message.textContent='已更新。协商速率不是实测吞吐量；未连接设备不会被判断为带宽故障。';}
   }catch(error){if(id===version&&safe())message.textContent=error.name==='AbortError'?'诊断读取超时，请重试。':error.message;}
   finally{clearTimeout(timeout);if(id===version){controller=null;busy=false;controls();}}
  }
  $('phone-diagnostics-refresh').onclick=()=>load();$('phone-diagnostics-export').onclick=()=>load(true);
  details.addEventListener('toggle',()=>{if(details.open&&!loaded)load();});
  return {suspend,open(){details.open=true;if(loaded)load();},update(){if(!active()&&controller)suspend();controls();}};
 };
})(window);
