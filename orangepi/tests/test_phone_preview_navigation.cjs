/* Real-browser regression; all status, assets and images are local fixtures.
 * NODE_PATH must contain Playwright; CHROME_PATH may select an installed Chrome.
 */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const control=path.join(__dirname,'../src/orangepi_collect/control');
const html=fs.readFileSync(path.join(control,'dashboard.html'),'utf8')
 .replace('<body>','<body data-access-code-required="false">');
const cameras=['cam_ros_left','cam_ros_right','cam_ros_bleft','cam_ros_bright','cam_left_wrist','cam_right_wrist'];
const image=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5foAAAAASUVORK5CYII=','base64');
const clone=value=>JSON.parse(JSON.stringify(value));
const snapshot={state:'device_confirm',revision:7,collection_mode:'emg',collection_context:'emg:test',
 preview_id:'preview-1',session:null,busy:false,worker:{},actions:['rescan','shutdown'],devices:[],previews:{},signals:{},
 privacy:{policy:'screen_black_before_capture_v1',instance:'instance-1',generation:0,phase:'idle',required:false,
  stop_confirmed:true,nonce:'',session_id:''}};

(async()=>{
 const browser=await chromium.launch({headless:true,
  ...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}),args:['--disable-background-networking']});
 const context=await browser.newContext({viewport:{width:390,height:844},serviceWorkers:'block'});
 const page=await context.newPage(),errors=[],streams=[];let current=clone(snapshot),serial=0,failStream=false,navigations=0;
 page.on('pageerror',error=>errors.push(error.message));
 page.on('framenavigated',frame=>{if(frame===page.mainFrame())navigations++;});
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(url.origin!=='http://preview-ui.test')return route.abort();
  if(url.pathname==='/phone')return route.fulfill({contentType:'text/html',body:html});
  if(url.pathname==='/api/status')return route.fulfill({json:clone(current)});
  if(url.pathname==='/api/display-state')return route.fulfill({json:{...clone(current),
   display_registration:{client_id:url.searchParams.get('client_id'),instance:current.privacy.instance}}});
  if(url.pathname==='/api/action'){
   assert(['privacy_display','privacy_ack'].includes(route.request().postDataJSON().action),'fixture must never start capture or shut down');
   return route.fulfill({status:202,json:{accepted:true}});
  }
  if(url.pathname.startsWith('/api/phone/stream/')){
   streams.push(url.pathname);
   await new Promise(resolve=>setTimeout(resolve,80));
   if(failStream)return route.fulfill({status:503,json:{error:'测试预览暂不可用'}});
   const meta=Buffer.from(JSON.stringify({bytes:image.length,frame_id:String(++serial),server_ms:1}));
   const prefix=Buffer.alloc(4);prefix.writeUInt32BE(meta.length);
   return route.fulfill({contentType:'application/octet-stream',body:Buffer.concat([prefix,meta,image])});
  }
  const asset=path.join(control,path.basename(url.pathname).replace(/-/g,'_'));
  if(asset.endsWith('.js')&&fs.existsSync(asset))return route.fulfill({contentType:'text/javascript',
   body:fs.readFileSync(url.pathname==='/phone-shell.js'&&process.env.PREVIEW_SHELL_SOURCE?process.env.PREVIEW_SHELL_SOURCE:asset,'utf8')});
  if(asset.endsWith('.css')&&fs.existsSync(asset))return route.fulfill({contentType:'text/css',body:fs.readFileSync(asset,'utf8')});
  return route.fulfill({status:404,body:'Offline fixture'});
 });
 const preview=()=>page.locator('main').getAttribute('data-phone-page');
 async function state(value){current={...current,...value};await page.evaluate(()=>poll());await page.waitForFunction(state=>status?.state===state,value.state);}
 try{
  await page.goto('http://preview-ui.test/phone');
  await page.waitForFunction(()=>document.documentElement.classList.contains('privacy-open'));
  await page.locator('#phone-tab-preview').click();
  for(const camera of cameras){
   await page.locator('[data-camera="'+camera+'"]').click();
   await page.waitForFunction(()=>!document.getElementById('phone-live-image').hidden);
   assert.equal(await preview(),'preview');
  }
  console.log('PASS six camera switches remain on the preview page');

  for(let i=0;i<2;i++){
   await state({state:'scanning',preview_id:null});
   assert.equal(await preview(),'preview');
   assert(await page.locator('#phone-live-image').isHidden());
   assert.match(await page.locator('#phone-live-empty').textContent(),/正在扫描设备/);
   await page.waitForTimeout(200);const count=streams.length;
   await page.waitForTimeout(1100);assert.equal(streams.length,count,'no preview retry storm during scan');
   await state({state:'device_confirm',preview_id:'preview-recovered-'+i});
   assert.equal(await preview(),'preview','automatic retry must not navigate home');
   assert.equal(await page.locator('#phone-camera').inputValue(),cameras.at(-1));
   await page.waitForFunction(()=>!document.getElementById('phone-live-image').hidden);
  }
  console.log('PASS repeated scans pause requests, retain selected camera/tab, and resume automatically');

  failStream=true;
  await page.waitForFunction(()=>document.getElementById('phone-live-stats').textContent.includes('测试预览暂不可用'));
  assert.equal(await preview(),'preview');failStream=false;
  console.log('PASS temporary stream failure does not refresh or navigate home');

  await state({state:'ready',collection_mode:'eeg',eeg_impedance:{required:false,phase:'skipped',can_record:true},camera_check:{eligible:true}});
  assert.equal(await preview(),'workflow','actual workflow transitions still guide the operator');
  assert(await page.locator('#impedance-panel').isHidden(),'skipped impedance must stay hidden');
  for(let i=0;i<3;i++){await page.waitForTimeout(900);assert(await page.locator('#impedance-panel').isHidden(),'polling must not re-show skipped impedance');}
  await state({state:'ready',eeg_impedance:{required:true,supported:false,phase:'initializing',can_record:false},camera_check:{eligible:false}});
  assert(await page.locator('#impedance-panel').isHidden(),'transient initialization must not show impedance');
  await state({state:'ready',eeg_impedance:{required:true,supported:true,phase:'not_checked',can_record:false},camera_check:{eligible:false}});
  assert.equal(await page.locator('#impedance-panel').evaluate(e=>e.hidden),false,'required impedance remains enabled');
  await state({state:'ready',eeg_impedance:{required:false,phase:'skipped',can_record:true},camera_check:{eligible:true}});
  assert(await page.locator('#impedance-panel').isHidden(),'switching back to skip hides impedance');
  console.log('PASS skipped impedance stays hidden; required impedance remains available');
  await state({state:'device_confirm'});assert.equal(await preview(),'overview','invalidated workflow still returns to device confirmation');
  assert.equal(navigations,1,'no document reloads');assert.deepEqual(errors,[]);
  console.log('PASS workflow safety navigation preserved; no document reloads or script errors');
 }finally{await context.unrouteAll({behavior:'ignoreErrors'});await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
