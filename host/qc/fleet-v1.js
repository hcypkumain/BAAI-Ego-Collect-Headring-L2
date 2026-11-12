/* Shared read-only presentation: host projection only, never maintenance jobs. */
(function(global){
  'use strict';
  const esc=x=>String(x??'未知').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const val=x=>x===null||x===undefined?'未知':String(x);
  const bytes=x=>typeof x==='number'?(x/1024**3).toFixed(2)+' GiB':'未知';
  const bool=x=>x===true?'有效':x===false?'无效':'未知';
  function card(row,elapsed=0){
    const s=row.snapshot, age=row.last_seen_age_ms===null?null:row.last_seen_age_ms+elapsed;
    const quality=!s?'unknown':s.sample.quality==='error'?'error':age===null||age>10000||s.sample.age_ms===null||s.sample.age_ms+elapsed>s.sample.stale_after_ms?'stale':s.sample.quality;
    const state=row.reason==='INSTANCE_CHANGED'?'reboot':quality;
    const freshness=sample=>!sample?'unknown':sample.age_ms===null?'unknown':sample.quality==='known'&&sample.age_ms+elapsed>sample.stale_after_ms?'stale':sample.quality;
    const line=(label,value)=>`<div>${esc(label)}: ${esc(value)}</div>`;
    let html=`<article class="v1-card" data-device="${esc(row.device_id)}" data-quality="${esc(state)}"><h3>${esc(row.device_id)} · ${esc(state)}</h3>`;
    html+=line('连接',row.connectivity)+line('观测 age ms',age===null?'未知':age);
    if(!s)return html+line('录制 / 任务 / policy / RAM / disk','未知')+'</article>';
    const p=s.progress,r=s.resources,c=s.session,v=s.version;
    html+=line('录制事实（非连接推断）',s.recording_active===null?'未知':s.recording_active?'录制中':'已报告未录制');
    html+=line('mode / lifecycle',val(s.mode)+' / '+val(s.lifecycle));
    html+=line('当前 task refID',val(c.ref_id))+line('session',val(c.session_id));
    html+=line('进度 completed / total',val(p.completed)+' / '+val(p.total));
    html+=line('scope list / revision / profile epoch',val(c.list_id)+' / '+val(c.list_revision)+' / '+val(c.profile_epoch));
    html+=line('完成 policy / version / 来源',val(c.task_policy_id)+' / '+val(c.task_policy_version)+' / '+p.sample.source);
    html+=line('disk available',bytes(r.disk_available_bytes)+' · '+freshness(r.disk_sample));
    html+=line('RAM MemAvailable',bytes(r.ram_mem_available_bytes)+' · '+freshness(r.ram_sample));
    html+=line('UTC / trusted',val(s.time.utc)+' / '+bool(s.time.utc_trusted));
    html+=line('左右校准',bool(s.roles.left.calibration_valid)+' / '+bool(s.roles.right.calibration_valid));
    html+=line('阻抗 review（设备权威）',bool(s.health.impedance.can_review));
    html+='<details><summary>来源 / 版本 / age</summary>';
    html+=line('device boot / controller / seq',val(s.boot_id)+' / '+val(s.controller_instance)+' / '+val(s.seq));
    for(const k of ['base_commit','overlay_manifest_sha256','installed_manifest_sha256','runtime_manifest_sha256','approved_release_id'])html+=line(k,v[k]);
    html+=line('来源 / age ms',s.sample.source+' / '+(s.sample.age_ms===null?'未知':s.sample.age_ms+elapsed));
    return html+'</details></article>';
  }
  function render(payload,elapsed=0){
    return `<section data-host-instance="${esc(payload.host_instance)}" data-revision="${esc(payload.revision)}"><h2>v1 只读 · ${esc(payload.host_instance)}:${esc(payload.revision)}</h2><p>离线 fixture / 显式注入；非现场实时。失联不代表停止，未知不代表零。</p>${payload.devices.map(r=>card(r,elapsed)).join('')}</section>`;
  }
  function mount(target,payload){
    const received=performance.now();
    const draw=()=>{target.innerHTML=render(payload,Math.max(0,Math.floor(performance.now()-received)))};
    draw();const timer=setInterval(draw,1000);return ()=>clearInterval(timer);
  }
  global.FleetV1={render,card,mount};
})(globalThis);
