/* Offline browser regression for dashboard EMG manual assignment.
 * Needs Playwright on NODE_PATH and a local Chromium/Chrome executable.
 * Every request is fulfilled/aborted locally; no Pi, devices or recording.
 */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const crypto=require('node:crypto');
const {chromium}=require('playwright');
const pageFile=path.join(__dirname,'../src/orangepi_collect/control/dashboard.html');
const source=fs.readFileSync(pageFile,'utf8');
new vm.Script(source.match(/<script>([\s\S]*?)<\/script>/)[1],{filename:'dashboard-inline.js'});
assert(!source.includes('data-action="swap_emg"'));
assert(source.includes('data-action="calibrate_emg"'));
assert(source.includes('setInterval(()=>renderEmg(status),500)'));
// Only scheduling/auth are replaced in the fixture; the production functions,
// event handlers, DOM and CSS execute unchanged in a real browser.
const fixtureHtml=source.replace('<body>','<body data-access-code-required="false">')
 .replace('setInterval(poll,750);setInterval(frames,500);setInterval(()=>renderEmg(status),500);poll();','');
const clone=value=>JSON.parse(JSON.stringify(value));
function snapshot(){
 const s={state:'emg_manual',revision:7,preview_id:'preview-1',server_time:1000,
  system:{boot_id:'boot-1',controller_pid:101},actions:['assign_emg_roles'],
  emg_assignment:{session_id:'session-1',preview_id:'preview-1',sources:{emg_left:'/dev/test-a',emg_right:'/dev/test-b'}},
  emg_calibration:{valid:false},emg_serial_roles:{emg_left:'UNTRUSTED11111',emg_right:'UNTRUSTED22222'},
  wrist_calibration:{valid:false},previews:{},signals:{},devices:[],
  wrist_camera_settings:{preference:null,can_change:true,devices:{}}};
 for(const [i,slot] of ['emg_left','emg_right'].entries()){
  s.signals[slot]={live:true,preview_id:s.preview_id,source_path:s.emg_assignment.sources[slot],
   time:999.95,count:500,rows:[999.6,999.9].map((t,j)=>[t,Array.from({length:8},(_,ch)=>[i*20+ch*100-j*7,i*20+ch*100+j*11+4])])};
  s.devices.push({name:slot,label:i?'右臂肌电':'左臂肌电',connected:true,
   config:{discovery:{serial:i?'UNTRUSTED22222':'UNTRUSTED11111'}}});
 }
 return s;
}
let browser,passed=0,cases=[];
const dashboardSha=crypto.createHash('sha256').update(source).digest('hex');
const validationPath=path.resolve(__dirname,'../../../ui_validation.json');
function saveValidation(ok,error){fs.writeFileSync(validationPath,JSON.stringify({passed:ok,dashboard_sha256:dashboardSha,cases,javascript_syntax:'passed',network:'All requests locally intercepted; no Pi/device access',error:error||null},null,2));}
async function harness(viewport={width:1250,height:1000}){
 const context=await browser.newContext({viewport,serviceWorkers:'block'});
 // Deterministic UI time: browser scrolling/CI load cannot consume the 3s
 // freshness allowance of our deliberately frozen mock server timestamps.
 await context.addInitScript(()=>{window.__emgTestNow=10000;Object.defineProperty(performance,'now',{value:()=>window.__emgTestNow});});
 const h={context,current:snapshot(),posts:[],offline:false,holdAction:false,releaseAction:null,errors:[]};
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(url.origin!=='http://emg-ui.test')return route.abort();
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:fixtureHtml});
  if(url.pathname==='/api/status')return h.offline?route.abort():route.fulfill({json:clone(h.current)});
  if(url.pathname==='/api/action'){
   h.posts.push(route.request().postDataJSON());
   if(h.holdAction)await new Promise(resolve=>h.releaseAction=resolve);
   return route.fulfill({status:202,json:{ok:true}});
  }
  return route.fulfill({status:404,body:'No external resources in offline test'});
 });
 h.page=await context.newPage();
 h.page.on('pageerror',error=>h.errors.push(error.message));
 await h.page.goto('http://emg-ui.test/',{waitUntil:'domcontentloaded'});
 h.set=async s=>{h.current=clone(s);await h.page.evaluate(value=>{online=true;lastStatusReceivedAt=performance.now();render(value)},h.current);};
 await h.set(h.current);
 h.pick=async(slot,side)=>{await h.set(h.current);await h.page.click('#'+slot+' .emg-role-buttons [data-side="'+side+'"]');};
 h.selected=async()=>h.page.locator('.emg-role-buttons [aria-pressed="true"]').count();
 h.close=async()=>{if(h.releaseAction)h.releaseAction();await context.close();};
 return h;
}
async function test(name,run){
 const h=await harness();
 try{await run(h);assert.deepEqual(h.errors,[]);assert(!(await h.page.locator('#notice').textContent()).includes('页面显示异常'));passed++;cases.push({name,passed:true});console.log('PASS '+name);}
 finally{await h.close();}
}
async function main(){
 const chrome=process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
 browser=await chromium.launch({headless:true,...(fs.existsSync(chrome)?{executablePath:chrome}:{}),args:['--disable-background-networking']});
 try{
  await test('neutral A/B until explicit confirmation; no serial-side hints',async h=>{
   assert.equal(await h.page.locator('#emg_left b').textContent(),'肌电 A');
   assert.equal(await h.page.locator('#emg_right b').textContent(),'肌电 B');
   assert.equal(await h.selected(),0);assert(await h.page.locator('#confirm-emg').isDisabled());
   assert.equal(await h.page.locator('#device-rows tr').first().locator('td').first().textContent(),'肌电 A');
   assert(!(await h.page.locator('#subtitle').textContent()).includes('左肌电'));
   assert(!(await h.page.locator('#emg_left .signal-meta').textContent()).includes('11111'));
   assert.equal(h.posts.length,0);
  });
  await test('all four side choices are complementary; changing selection never submits',async h=>{
   for(const [slot,side,left] of [['emg_left','left','A'],['emg_right','right','A'],['emg_left','right','B'],['emg_right','left','B']]){
    await h.pick(slot,side);assert.equal(await h.selected(),2);
    assert((await h.page.locator('#emg-selection').textContent()).includes(left+' → 左臂'));
    assert(!(await h.page.locator('#confirm-emg').isDisabled()));
   }
   assert.equal(h.posts.length,0);
  });
  await test('confirmation posts exact session/preview/left_slot/revision only',async h=>{
   await h.pick('emg_right','left');await h.page.click('#confirm-emg');
   await h.page.waitForFunction(()=>!inFlight);
   assert.deepEqual(h.posts,[{action:'assign_emg_roles',value:{session_id:'session-1',preview_id:'preview-1',left_slot:'emg_right'},revision:7}]);
  });
  await test('one stale/missing/wrong-source/wrong-preview signal clears draft and drawing',async h=>{
   const mutations=[s=>s.signals.emg_left.live=false,s=>delete s.signals.emg_right,
    s=>s.signals.emg_right.time=990,s=>s.signals.emg_right.time=1001,
    s=>s.signals.emg_right.preview_id='old-preview',s=>s.signals.emg_right.source_path='/dev/wrong',
    s=>s.signals.emg_right.rows=[],s=>s.signals.emg_right.rows=[[999.9,[[1,2]]]]];
   for(const mutate of mutations){
    await h.set(snapshot());await h.pick('emg_left','left');
    const s=snapshot();mutate(s);await h.set(s);
    assert.equal(await h.selected(),0);assert(await h.page.locator('#confirm-emg').isDisabled());
    assert(await h.page.locator('#emg_right .emg-role-buttons button').first().isDisabled());
    const staleSlot=!s.signals.emg_left.live?'emg_left':'emg_right';
    assert.equal(await h.page.locator('#'+staleSlot+' .live-tag').textContent(),'未收到实时数据');
   }
   assert.equal(h.posts.length,0);
  });
  await test('fresh payload timestamp cannot hide old envelope rows',async h=>{
   await h.pick('emg_left','left');const s=snapshot();
   s.signals.emg_right.rows=s.signals.emg_right.rows.map(row=>[990,row[1]]);
   await h.set(s);assert.equal(await h.selected(),0);assert(await h.page.locator('#confirm-emg').isDisabled());
  });
  await test('freshness is rechecked at click, before a stale enabled-looking button can submit',async h=>{
   await h.pick('emg_left','left');
   await h.page.evaluate(()=>{lastStatusReceivedAt=performance.now()-3100;document.querySelector('#confirm-emg').onclick()});
   assert.equal(h.posts.length,0);assert.equal(await h.selected(),0);assert(await h.page.locator('#confirm-emg').isDisabled());
  });
  await test('session, preview, boot, process and source changes invalidate the draft',async h=>{
   const mutations=[s=>s.emg_assignment.session_id='session-2',
    s=>{s.preview_id=s.emg_assignment.preview_id='preview-2';for(const v of Object.values(s.signals))v.preview_id='preview-2';},
    s=>s.system.boot_id='boot-2',s=>s.system.controller_pid=202,
    s=>{s.emg_assignment.sources.emg_left='/dev/replug';s.signals.emg_left.source_path='/dev/replug';}];
   for(const mutate of mutations){
    await h.set(snapshot());await h.pick('emg_left','left');const s=snapshot();mutate(s);await h.set(s);
    assert.equal(await h.selected(),0);assert(await h.page.locator('#confirm-emg').isDisabled());
   }
  });
  await test('disconnect clears draft; same-session reconnect does not restore it',async h=>{
   await h.pick('emg_left','left');h.offline=true;await h.page.evaluate(()=>poll());
   assert.equal(await h.selected(),0);assert(await h.page.locator('#confirm-emg').isDisabled());
   h.offline=false;await h.page.evaluate(()=>poll());
   assert.equal(await h.selected(),0);assert(await h.page.locator('#confirm-emg').isDisabled());
   await h.pick('emg_right','left');assert.equal(await h.selected(),2);
  });
  await test('ordinary status refresh preserves draft; pending request locks both cards',async h=>{
   await h.pick('emg_right','right');await h.set(snapshot());assert.equal(await h.selected(),2);
   h.holdAction=true;await h.page.locator('#confirm-emg').click();
   await h.page.waitForFunction(()=>inFlight);
   assert(await h.page.locator('#confirm-emg').isDisabled());
   for(const button of await h.page.locator('.emg-role-buttons button').all())assert(await button.isDisabled());
   assert.equal(h.posts.length,1);
   h.holdAction=false;h.releaseAction();h.releaseAction=null;await h.page.waitForFunction(()=>!inFlight);
  });
  await test('leaving manual clears draft; confirmed names use only dynamic roles',async h=>{
   await h.pick('emg_left','left');const s=snapshot();s.state='idle';s.actions=['calibrate_emg'];s.emg_assignment=null;
   s.emg_calibration={valid:true,method:'manual'};s.emg_serial_roles={emg_left:'NEWLEFT33333',emg_right:'NEWRIGHT44444'};
   await h.set(s);assert.equal(await h.selected(),0);assert(await h.page.locator('#emg-assignment').isHidden());
   assert.equal(await h.page.locator('#emg_left b').textContent(),'左臂肌电');
   assert((await h.page.locator('#emg_left .signal-meta').textContent()).includes('33333'));
   await h.page.locator('[data-action="calibrate_emg"]').click();await h.page.waitForFunction(()=>!inFlight);
   assert.equal(h.posts[0].action,'calibrate_emg');
   await h.set(snapshot());assert.equal(await h.selected(),0);
  });
  await test('global A/B shortcuts do not infer sides or confirm a manual draft',async h=>{
   await h.pick('emg_left','left');await h.page.locator('h1').click();
   await h.page.keyboard.press('a');await h.page.keyboard.press('b');assert.equal(h.posts.length,0);
  });
  await test('EEG removed; wrist-manual and exposure controls remain available',async h=>{
   assert.equal(await h.page.locator('#eeg').count(),0);
   assert.equal(await h.page.locator('#wrist-methods button').count(),2);
   const s=snapshot();s.state='idle';s.actions=['wrist_camera_settings'];s.emg_assignment=null;
   await h.set(s);await h.page.selectOption('#wrist-fps-policy','false');await h.page.click('#set-wrist-fps-policy');
   await h.page.waitForFunction(()=>!inFlight);assert.equal(h.posts[0].action,'wrist_camera_settings');assert.equal(h.posts[0].value,false);
   assert.equal(await h.page.locator('#set-eeg-filter').count(),0);
  });
  const h=await harness({width:390,height:844});
  try{
   const bounds=await h.page.evaluate(()=>{
    const card=document.querySelector('#emg-card').getBoundingClientRect();
    const buttons=[...document.querySelectorAll('.emg-role-buttons button')].map(b=>{const r=b.getBoundingClientRect();return {height:r.height,left:r.left,right:r.right}});
    return {width:innerWidth,card:{left:card.left,right:card.right},buttons,columns:getComputedStyle(document.querySelector('#signals')).gridTemplateColumns};
   });
   assert(bounds.card.left>=0&&bounds.card.right<=bounds.width);
   assert(bounds.buttons.every(b=>b.height>=44&&b.left>=0&&b.right<=bounds.width));
   assert(!bounds.columns.includes(' '));
   await h.pick('emg_right','left');assert(!(await h.page.locator('#confirm-emg').isDisabled()));
   assert.deepEqual(h.errors,[]);passed++;cases.push({name:'390px mobile controls visible/single-column/minimum44px',passed:true});console.log('PASS 390px mobile controls remain visible, single-column, >=44px');
  }finally{await h.close();}
  saveValidation(true);console.log(JSON.stringify({passed,network:'All requests locally intercepted; no Pi/device access',javascript_syntax:'passed',dashboard_sha256:dashboardSha,validation:validationPath}));
 }finally{await browser.close();}
}
main().catch(error=>{cases.push({name:'unresolved failure',passed:false,error:error.message});saveValidation(false,error.message);console.error(error);process.exitCode=1;});
