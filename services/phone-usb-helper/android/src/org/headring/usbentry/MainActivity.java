package org.headring.usbentry;

import android.Manifest;
import android.app.Activity;
import android.content.*;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.*;
import android.util.Base64;
import android.view.*;
import android.webkit.*;
import android.widget.*;
import java.io.*;
import java.util.UUID;

public final class MainActivity extends Activity {
 private static final String HOME="http://localhost:"+EntryAddress.PORT+"/phone";
 private static final int PICK=101,SAVE=102;
 private final Handler handler=new Handler();
 private TextView status;
 private TextView connectionTitle,connectionNote,deviceSummary,pendingName;
 private ScrollView panel;
 private LinearLayout nativePane,advancedSection,exportSection;
 private Button choose,scan,savePending,enter,back;
 private CheckBox awake;
 private EditText manualAddress;
 private int buttonMode=-1;
 private android.app.AlertDialog candidateDialog;
 private UiSession session;
 private boolean resumed;

 /** Retain the actual WebView and its file callback, not an old Activity. */
 private static final class UiSession {
  final MutableContextWrapper context;
  final ExportStore exports;
  final Handler handler=new Handler(Looper.getMainLooper());
  final PageRecovery recovery=new PageRecovery();
  final WebTheme theme;
  WebView web;
  ValueCallback<Uri[]> chooser;
  String chooserDevice="",lastDevice="";
  volatile String token="";
  volatile MainActivity activity;
  long connectionRevision=-1,navigation,blobSerial;
  String uiNonce="";
  long uiNavigation=-1;
  boolean nativeRequested;
  boolean picking;
  volatile boolean saving;
  UiSession(Activity owner)throws IOException{context=new MutableContextWrapper(owner);exports=new ExportStore(owner.getFilesDir());WebTheme loaded;try{loaded=new WebTheme(owner);}catch(IOException e){loaded=null;}theme=loaded;}
  void toast(String text){handler.post(()->{MainActivity a=activity;if(a!=null)Toast.makeText(a,text,Toast.LENGTH_LONG).show();});}
 }
 public void onCreate(Bundle saved){
  super.onCreate(saved);
  Object retained=getLastNonConfigurationInstance();
  try{session=retained instanceof UiSession?(UiSession)retained:new UiSession(this);}
  catch(IOException e){Toast.makeText(this,"无法准备导出目录，请检查手机剩余空间",Toast.LENGTH_LONG).show();finish();return;}
  session.context.setBaseContext(this);session.activity=this;
  if(android.os.Build.VERSION.SDK_INT>=33&&checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},21);
  LinearLayout root=UiTheme.column(this);root.setBackgroundColor(android.graphics.Color.BLACK);
  root.setOnApplyWindowInsetsListener((view,insets)->{view.setPadding(insets.getSystemWindowInsetLeft(),insets.getSystemWindowInsetTop(),insets.getSystemWindowInsetRight(),insets.getSystemWindowInsetBottom());return insets.consumeSystemWindowInsets();});
  createConnectionPage(root,saved);
  if(session.web==null){session.web=new WebView(session.context);configureSettings(session.web);}
  WebView web=session.web;ViewParent previous=web.getParent();if(previous instanceof ViewGroup)((ViewGroup)previous).removeView(web);
  bindWeb(web);web.setBackgroundColor(android.graphics.Color.BLACK);root.addView(web,new LinearLayout.LayoutParams(-1,0,1));setContentView(root);
  boolean nativeOpen=saved!=null&&saved.getBoolean("settings_open",false)||web.getUrl()==null;
  nativePane.setVisibility(nativeOpen?View.VISIBLE:View.GONE);web.setVisibility(nativeOpen?View.GONE:View.VISIBLE);root.requestApplyInsets();
  if(retained==null&&saved!=null)web.restoreState(saved);
  startForegroundService(new Intent(this,EntryService.class));refresh();
 }
 private void createConnectionPage(LinearLayout root,Bundle saved){
  nativePane=UiTheme.column(this);nativePane.setBackgroundColor(UiTheme.BACKGROUND);root.addView(nativePane,new LinearLayout.LayoutParams(-1,0,1));
  LinearLayout toolbar=new LinearLayout(this);toolbar.setGravity(Gravity.CENTER_VERTICAL);toolbar.setPadding(UiTheme.dp(this,12),UiTheme.dp(this,4),UiTheme.dp(this,12),UiTheme.dp(this,4));toolbar.setMinimumHeight(UiTheme.dp(this,56));
  back=new Button(this);back.setText("返回采集");UiTheme.button(back,false);back.setCompoundDrawables(UiTheme.icon(this,true,UiTheme.PRIMARY),null,null,null);back.setCompoundDrawablePadding(UiTheme.dp(this,6));back.setOnClickListener(v->openCollection());toolbar.addView(back,new LinearLayout.LayoutParams(-2,-2));
  TextView title=UiTheme.text(this,"USB 连接管理",18,UiTheme.INK,true);title.setPadding(UiTheme.dp(this,12),0,0,0);toolbar.addView(title,new LinearLayout.LayoutParams(0,-2,1));nativePane.addView(toolbar,new LinearLayout.LayoutParams(-1,-2));
  panel=new ScrollView(this);panel.setFillViewport(true);panel.setClipToPadding(false);nativePane.addView(panel,new LinearLayout.LayoutParams(-1,0,1));
  LinearLayout settings=UiTheme.column(this);settings.setPadding(UiTheme.dp(this,20),UiTheme.dp(this,18),UiTheme.dp(this,20),UiTheme.dp(this,32));panel.addView(settings);
  LinearLayout summary=UiTheme.surface(this);
  connectionTitle=UiTheme.text(this,"等待连接头环",24,UiTheme.INK,true);summary.addView(connectionTitle);UiTheme.gap(summary,10);
  connectionNote=UiTheme.text(this,"连接 USB 数据线，并在手机系统中开启 USB 网络共享。",16,UiTheme.MUTED,false);summary.addView(connectionNote);UiTheme.gap(summary,16);
  deviceSummary=UiTheme.text(this,"尚未选择头环",16,UiTheme.PRIMARY,true);deviceSummary.setCompoundDrawables(UiTheme.icon(this,false,UiTheme.PRIMARY),null,null,null);deviceSummary.setCompoundDrawablePadding(UiTheme.dp(this,10));summary.addView(deviceSummary);settings.addView(summary,new LinearLayout.LayoutParams(-1,-2));UiTheme.gap(settings,20);
  scan=new Button(this);scan.setText("搜索头环");UiTheme.button(scan,true);scan.setOnClickListener(v->search());settings.addView(scan,new LinearLayout.LayoutParams(-1,-2));UiTheme.gap(settings,10);
  choose=new Button(this);choose.setText("选择已验证的头环");UiTheme.button(choose,false);choose.setOnClickListener(v->chooseDevice());settings.addView(choose,new LinearLayout.LayoutParams(-1,-2));UiTheme.gap(settings,10);
  enter=new Button(this);enter.setText("进入采集工作台");UiTheme.button(enter,true);enter.setOnClickListener(v->openCollection());settings.addView(enter,new LinearLayout.LayoutParams(-1,-2));UiTheme.gap(settings,24);
  TextView help=UiTheme.text(this,"首次连接",16,UiTheme.INK,true);settings.addView(help);UiTheme.gap(settings,8);
  settings.addView(UiTheme.text(this,"连接 USB 数据线后，在手机系统设置中开启 USB 网络共享。搜索过程中，已验证的头环即可选择。",15,UiTheme.MUTED,false));UiTheme.gap(settings,24);
  LinearLayout preference=UiTheme.surface(this);awake=new CheckBox(this);awake.setText("前台保持亮屏");awake.setTextSize(16);awake.setTextColor(UiTheme.INK);awake.setButtonTintList(android.content.res.ColorStateList.valueOf(UiTheme.PRIMARY));awake.setMinHeight(UiTheme.dp(this,48));awake.setChecked(getSharedPreferences("usb",MODE_PRIVATE).getBoolean("keep_awake",true));preference.addView(awake);
  preference.addView(UiTheme.text(this,"仅在 App 前台生效；切到后台后会释放。",14,UiTheme.MUTED,false));awake.setOnCheckedChangeListener((button,checked)->{getSharedPreferences("usb",MODE_PRIVATE).edit().putBoolean("keep_awake",checked).apply();updateAwake();});settings.addView(preference,new LinearLayout.LayoutParams(-1,-2));UiTheme.gap(settings,16);
  exportSection=UiTheme.surface(this);exportSection.addView(UiTheme.text(this,"有文件等待保存",16,UiTheme.INK,true));UiTheme.gap(exportSection,8);pendingName=UiTheme.text(this,"",14,UiTheme.MUTED,false);exportSection.addView(pendingName);UiTheme.gap(exportSection,12);
  savePending=new Button(this);savePending.setText("继续保存文件");UiTheme.button(savePending,false);savePending.setOnClickListener(v->promptSave());exportSection.addView(savePending,new LinearLayout.LayoutParams(-1,-2));settings.addView(exportSection,new LinearLayout.LayoutParams(-1,-2));UiTheme.gap(settings,16);
  Button advanced=new Button(this);advanced.setText("高级连接与诊断");UiTheme.button(advanced,false);advanced.setContentDescription("展开或收起高级连接与诊断");settings.addView(advanced,new LinearLayout.LayoutParams(-1,-2));
  advancedSection=UiTheme.column(this);advancedSection.setVisibility(saved!=null&&saved.getBoolean("advanced_open",false)?View.VISIBLE:View.GONE);UiTheme.gap(advancedSection,16);
  final EditText ip=new EditText(this);manualAddress=ip;ip.setSingleLine(true);ip.setHint("设备当前 USB 地址");ip.setTextSize(16);ip.setTextColor(UiTheme.INK);ip.setHintTextColor(UiTheme.MUTED);ip.setMinHeight(UiTheme.dp(this,52));ip.setPadding(UiTheme.dp(this,14),UiTheme.dp(this,12),UiTheme.dp(this,14),UiTheme.dp(this,12));ip.setBackground(UiTheme.background(this,UiTheme.SURFACE,UiTheme.LINE,12));ip.setInputType(android.text.InputType.TYPE_CLASS_PHONE);
  if(saved!=null)ip.setText(saved.getString("manual_ip",""));advancedSection.addView(ip,new LinearLayout.LayoutParams(-1,-2));UiTheme.gap(advancedSection,10);
  Button manual=new Button(this);manual.setText("连接并记住 USB 接口");UiTheme.button(manual,false);advancedSection.addView(manual,new LinearLayout.LayoutParams(-1,-2));manual.setOnClickListener(v->{session.nativeRequested=false;Intent i=new Intent(this,EntryService.class);i.setAction("manual_probe");i.putExtra("ip",ip.getText().toString().trim());startForegroundService(i);});UiTheme.gap(advancedSection,16);
  status=UiTheme.text(this,"",14,UiTheme.MUTED,false);status.setTextIsSelectable(true);advancedSection.addView(status);settings.addView(advancedSection,new LinearLayout.LayoutParams(-1,-2));advanced.setOnClickListener(v->advancedSection.setVisibility(advancedSection.getVisibility()==View.VISIBLE?View.GONE:View.VISIBLE));
 }
 private void showConnections(boolean requested){if(requested)session.nativeRequested=true;nativePane.setVisibility(View.VISIBLE);session.web.setVisibility(View.GONE);}
 private void openCollection(){
  if(!Diagnostics.snapshot().ready()){session.toast(Diagnostics.snapshot().message);return;}
  session.nativeRequested=false;nativePane.setVisibility(View.GONE);session.web.setVisibility(View.VISIBLE);
  if(session.recovery.failed()||session.web.getUrl()==null||!sameOrigin(session.web.getUrl(),desiredHome())){session.recovery.manual();loadHome();}
 }
 private void applyTheme(WebView view,String url){
  if(session.theme==null||view!=session.web||!url.equals(view.getUrl())||!allowed(Uri.parse(url))||!WebTheme.page(Uri.parse(url)))return;
  Uri uri=Uri.parse(url);String origin=uri.getScheme()+"://"+uri.getHost()+":"+uri.getPort()+uri.getPath();session.uiNavigation=session.navigation;
  view.evaluateJavascript(session.theme.injection(session.uiNonce,origin),null);
 }
 private void configureSettings(WebView web){
  WebSettings s=web.getSettings();s.setJavaScriptEnabled(true);s.setDomStorageEnabled(true);s.setMediaPlaybackRequiresUserGesture(false);s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);s.setAllowFileAccess(false);s.setAllowContentAccess(true);s.setAllowFileAccessFromFileURLs(false);s.setAllowUniversalAccessFromFileURLs(false);
  CookieManager.getInstance().setAcceptCookie(true);CookieManager.getInstance().setAcceptThirdPartyCookies(web,false);
 }
 private void bindWeb(WebView web){
  web.removeJavascriptInterface("HeadRingSave");web.addJavascriptInterface(new SaveBridge(session),"HeadRingSave");
  web.setWebViewClient(new WebViewClient(){
   public boolean shouldOverrideUrlLoading(WebView v,WebResourceRequest request){
    Uri uri=request.getUrl();
    if(WebTheme.CONNECTION_PATH.equals(uri.getPath())){
     if(request.isForMainFrame()&&request.hasGesture()&&allowed(uri)&&v.getUrl()!=null&&allowed(Uri.parse(v.getUrl()))&&WebTheme.page(Uri.parse(v.getUrl()))&&session.uiNavigation==session.navigation&&!session.uiNonce.isEmpty()&&session.uiNonce.equals(uri.getQueryParameter("nonce"))&&uri.getQueryParameterNames().size()==1&&uri.getFragment()==null)showConnections(true);
     return true;
    }
    if(allowed(uri))return false;session.toast("设备已变化或链接不属于当前头环");return true;
   }
   public WebResourceResponse shouldInterceptRequest(WebView v,WebResourceRequest request){Uri u=request.getUrl();String scheme=u.getScheme();if(("http".equalsIgnoreCase(scheme)||"https".equalsIgnoreCase(scheme))&&!allowed(u))return new WebResourceResponse("text/plain","UTF-8",new ByteArrayInputStream(new byte[0]));return null;}
   public void onPageStarted(WebView v,String url,android.graphics.Bitmap icon){session.navigation++;session.token=UUID.randomUUID().toString();session.uiNonce=UUID.randomUUID().toString();session.uiNavigation=-1;session.exports.abortActive();session.recovery.started();}
   public void onPageCommitVisible(WebView v,String url){applyTheme(v,url);}
   public void onPageFinished(WebView v,String url){if(allowed(Uri.parse(url))&&url.equals(v.getUrl())){session.recovery.success();v.evaluateJavascript(BLOB_HOOK.replace("__TOKEN__",session.token),null);applyTheme(v,url);}}
   public void onReceivedError(WebView v,WebResourceRequest request,WebResourceError error){if(request.isForMainFrame()&&"GET".equals(request.getMethod())&&allowed(request.getUrl()))pageFailed();}
   public void onReceivedHttpError(WebView v,WebResourceRequest request,WebResourceResponse response){if(request.isForMainFrame()&&"GET".equals(request.getMethod())&&allowed(request.getUrl())&&response.getStatusCode()>=400)pageFailed();}
  });
  web.setWebChromeClient(new WebChromeClient(){
   public boolean onShowFileChooser(WebView view,ValueCallback<Uri[]> callback,FileChooserParams params){
    if(session.chooser!=null)session.chooser.onReceiveValue(null);session.chooser=callback;session.chooserDevice=Diagnostics.snapshot().current();session.picking=true;
    try{startActivityForResult(params.createIntent(),PICK);return true;}catch(Exception e){session.chooser=null;session.picking=false;callback.onReceiveValue(null);session.toast("无法打开系统文件选择器");return true;}
   }
  });
  web.setDownloadListener((url,agent,disposition,mime,length)->{
   if(!allowed(Uri.parse(url))){session.toast("无法下载其他来源的链接");return;}
   if(length>ExportStore.LIMIT){session.toast("单个导出文件不能超过32 MiB");return;}
   String name=URLUtil.guessFileName(url,disposition,mime);
   web.evaluateJavascript("(async function(){try{let r=await fetch("+org.json.JSONObject.quote(url)+",{credentials:'same-origin'});if(!r.ok)throw Error('服务器返回 '+r.status+'，请稍后重试');if(Number(r.headers.get('Content-Length'))>33554432)throw Error('文件超过 32 MiB，无法在 App 中保存');let b=await r.blob();if(b.size>33554432)throw Error('文件超过 32 MiB，无法在 App 中保存');let u=URL.createObjectURL(b),a=document.createElement('a');a.href=u;a.download="+org.json.JSONObject.quote(name)+";document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),30000);}catch(e){alert(e.message||'下载失败，请检查连接后重试');}})()",null);
  });
  if(!session.token.isEmpty()&&web.getUrl()!=null&&allowed(Uri.parse(web.getUrl()))){web.evaluateJavascript(BLOB_HOOK.replace("__TOKEN__",session.token),null);applyTheme(web,web.getUrl());}
 }
 private void search(){Intent i=new Intent(this,EntryService.class);i.setAction("scan_candidates");startForegroundService(i);scan.setEnabled(false);status.setText("正在搜索并验证 USB 头环…");}
 private void chooseDevice(){
  ConnectionState.Snapshot view=Diagnostics.snapshot();
  if(!view.current().isEmpty()){openCollection();return;}
  if(view.choosing||candidateDialog!=null&&candidateDialog.isShowing())return;
  if(view.candidates.isEmpty()){
   session.toast(view.scanning?"正在搜索，已验证的设备出现后即可选择":"没有可用候选，已重新开始搜索");if(!view.scanning)search();return;
  }
  String[] labels=new String[view.candidates.size()];for(int i=0;i<labels.length;i++){Endpoint e=view.candidates.get(i);labels[i]=e.deviceId+" ("+e.address.getHostAddress()+")"+(view.stale?" · 将重新验证":"");}
  candidateDialog=new android.app.AlertDialog.Builder(this).setTitle("选择 USB 头环").setItems(labels,(dialog,index)->{session.nativeRequested=false;Endpoint e=view.candidates.get(index);Intent i=new Intent(this,EntryService.class);i.setAction("select_candidate");i.putExtra("key",ConnectionState.key(e));startForegroundService(i);}).setNegativeButton("取消",null).create();candidateDialog.setOnDismissListener(dialog->candidateDialog=null);candidateDialog.show();
 }
 private String desiredHome(){String id=Diagnostics.snapshot().current();return DeviceIdentity.valid(id)?"http://cp-"+id.substring(3)+".localhost:"+EntryAddress.PORT+"/phone":HOME;}
 private boolean allowed(Uri uri){return uri!=null&&"http".equalsIgnoreCase(uri.getScheme())&&uri.getPort()==EntryAddress.PORT&&sameOrigin(uri.toString(),desiredHome());}
 private static boolean sameOrigin(String a,String b){if(a==null||b==null)return false;Uri x=Uri.parse(a),y=Uri.parse(b);return x.getScheme()!=null&&x.getScheme().equalsIgnoreCase(y.getScheme())&&x.getHost()!=null&&x.getHost().equalsIgnoreCase(y.getHost())&&x.getPort()==y.getPort();}
 private void loadHome(){if(session.picking||!Diagnostics.snapshot().ready())return;session.web.loadUrl(desiredHome());}
 private void pageFailed(){
  long delay=session.recovery.failure(),navigation=session.navigation,revision=Diagnostics.snapshot().connectionRevision;
  if(delay<0){session.toast("页面仍不可用；恢复连接后点击“采集页面”重试");return;}
  handler.postDelayed(()->{if(session.navigation==navigation&&Diagnostics.snapshot().connectionRevision==revision&&session.recovery.failed()&&!session.picking)loadHome();},delay);
 }
 private void refresh(){
  if(session==null)return;ConnectionState.Snapshot view=Diagnostics.snapshot();status.setText(Diagnostics.report());scan.setEnabled(!view.scanning&&!view.choosing);scan.setText(view.choosing?"正在验证…":view.scanning?"正在搜索…":"重新搜索头环");choose.setEnabled(!view.candidates.isEmpty());choose.setText("选择已验证的头环（"+view.candidates.size()+"）");savePending.setVisibility(session.exports.pending()!=null?View.VISIBLE:View.GONE);savePending.setEnabled(!session.saving&&!session.picking);
  exportSection.setVisibility(session.exports.pending()!=null?View.VISIBLE:View.GONE);if(session.exports.pending()!=null)setText(pendingName,session.exports.pending().name);
  boolean connected=!view.current().isEmpty();setText(connectionTitle,connected?"USB 头环已验证":view.scanning?"正在发现头环":"等待连接头环");setText(connectionNote,view.message);setText(deviceSummary,view.target!=null?view.target.deviceId+"  ·  "+view.target.address.getHostAddress():view.candidates.isEmpty()?"尚未选择头环":"已验证 "+view.candidates.size()+" 台，可直接选择");
  choose.setVisibility(!connected&&!view.candidates.isEmpty()?View.VISIBLE:View.GONE);choose.setEnabled(!view.choosing&&!view.candidates.isEmpty());
  enter.setEnabled(connected);setText(enter,session.recovery.failed()?"重试打开采集页":"进入采集工作台");back.setVisibility(session.web.getUrl()!=null?View.VISIBLE:View.GONE);
  int mode=connected?2:view.candidates.isEmpty()?0:1;if(mode!=buttonMode){buttonMode=mode;UiTheme.button(scan,mode==0);UiTheme.button(choose,mode==1);}
  if(!view.ready()&&session.web.getUrl()==null)showConnections(false);
  if(view.connectionRevision!=session.connectionRevision&&!session.picking){
   session.connectionRevision=view.connectionRevision;String id=view.current();
   if(view.ready()&&(session.web.getUrl()==null||!id.equals(session.lastDevice)||session.recovery.failed())){session.recovery.manual();loadHome();if(!id.isEmpty()&&!session.nativeRequested){nativePane.setVisibility(View.GONE);session.web.setVisibility(View.VISIBLE);}}
   session.lastDevice=id;
  }
  handler.postDelayed(this::refresh,1000);
 }
 private static void setText(TextView view,String text){if(!android.text.TextUtils.equals(view.getText(),text))view.setText(text);}
 public void onBackPressed(){if(nativePane.getVisibility()==View.VISIBLE){if(session.web.getUrl()!=null&&Diagnostics.snapshot().ready())openCollection();else super.onBackPressed();return;}if(session.web.canGoBack())session.web.goBack();else super.onBackPressed();}
 protected void onSaveInstanceState(Bundle out){super.onSaveInstanceState(out);if(session!=null){session.web.saveState(out);out.putBoolean("settings_open",nativePane.getVisibility()==View.VISIBLE);out.putBoolean("advanced_open",advancedSection.getVisibility()==View.VISIBLE);out.putString("manual_ip",manualAddress.getText().toString());}}
 public Object onRetainNonConfigurationInstance(){return session;}
 protected void onActivityResult(int request,int result,Intent data){
  super.onActivityResult(request,result,data);if(session==null)return;
  if(request==PICK){
   session.picking=false;ValueCallback<Uri[]> callback=session.chooser;session.chooser=null;
   if(callback==null){session.toast("页面已被系统重建，请重新选择导入文件");return;}
   if(!session.chooserDevice.equals(Diagnostics.snapshot().current())){callback.onReceiveValue(null);session.toast("头环已切换，请在当前设备重新选择文件");return;}
   callback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result,data));return;
  }
  if(request==SAVE){session.picking=false;ExportStore.Pending pending=session.exports.pending();if(pending==null)return;if(result!=RESULT_OK||data==null||data.getData()==null){session.toast("导出文件已保留，可在连接设置中继续保存");return;}saveTo(pending,data.getData());}
 }
 private void promptSave(){
  ExportStore.Pending pending=session.exports.pending();if(pending==null||session.picking||session.saving)return;
  try{Intent intent=new Intent(Intent.ACTION_CREATE_DOCUMENT);intent.addCategory(Intent.CATEGORY_OPENABLE);intent.setType(pending.mime);intent.putExtra(Intent.EXTRA_TITLE,pending.name);session.picking=true;startActivityForResult(intent,SAVE);}
  catch(Exception e){session.picking=false;session.toast("无法打开保存窗口，文件已保留，可重试");}
 }
 private void saveTo(ExportStore.Pending pending,Uri destination){
  if(session.saving)return;session.saving=true;UiSession retained=session;ContentResolver resolver=getContentResolver();
  new Thread(()->{
   try{
    try(InputStream input=new FileInputStream(pending.file);OutputStream output=resolver.openOutputStream(destination)){if(output==null)throw new IOException();byte[] buffer=new byte[16384];int n;while((n=input.read(buffer))!=-1)output.write(buffer,0,n);}
    retained.exports.complete(pending);retained.toast("文件已保存");
   }catch(Exception e){retained.toast("保存失败，原导出文件已保留，可重试");}
   finally{retained.saving=false;}
  },"headring-export").start();
 }
 private static final class SaveBridge {
  final UiSession ui;SaveBridge(UiSession s){ui=s;}
  private boolean authorized(String token){return token!=null&&!token.isEmpty()&&token.equals(ui.token);}
  @JavascriptInterface public synchronized String begin(String token,String name,String mime,long size){
   if(!authorized(token))return "页面已失效，请刷新后重试";
   if(size<0||size>ExportStore.LIMIT)return "文件超过 32 MiB，无法在 App 中保存";
   if(ui.exports.pending()!=null)return "有文件等待保存，请在连接设置中点击“继续保存文件”";
   if(ui.exports.active())return "另一个文件正在准备保存，请稍后重试";
   try{if(!ui.exports.begin(name,mime,size))return "无法开始保存，请稍后重试";long serial=++ui.blobSerial;ui.handler.postDelayed(()->{if(serial==ui.blobSerial)ui.exports.abortActive();},60000);return "";}
   catch(IOException e){ui.exports.abortActive();return "无法准备保存文件，请检查手机剩余空间";}
  }
  @JavascriptInterface public synchronized boolean chunk(String token,String base64){try{if(!authorized(token)||base64==null||base64.length()>100000)return false;return ui.exports.write(Base64.decode(base64,Base64.DEFAULT));}catch(Exception e){ui.exports.abortActive();return false;}}
  @JavascriptInterface public synchronized boolean end(String token){
   if(!authorized(token))return false;
   try{ui.exports.finish();ui.handler.post(()->{MainActivity a=ui.activity;if(a!=null)a.promptSave();});return true;}catch(IOException e){ui.exports.abortActive();return false;}
  }
  @JavascriptInterface public synchronized void abort(String token){if(authorized(token))ui.exports.abortActive();}
 }
 private static final String BLOB_HOOK="(function(){window.__hrBlobToken='__TOKEN__';if(window.__hrBlobHook)return;window.__hrBlobHook=true;document.addEventListener('click',async function(e){var a=e.target.closest&&e.target.closest('a[download]');if(!a||!a.href.startsWith('blob:'))return;e.preventDefault();e.stopImmediatePropagation();var token=window.__hrBlobToken,started=false;try{var b=await(await fetch(a.href)).blob();var reason=HeadRingSave.begin(token,a.download||'download',b.type||'application/octet-stream',b.size);if(reason)throw Error(reason);started=true;for(var p=0;p<b.size;p+=49152){var data=await new Promise(function(ok,no){var r=new FileReader();r.onload=function(){ok(r.result.split(',')[1]);};r.onerror=no;r.readAsDataURL(b.slice(p,p+49152));});if(!HeadRingSave.chunk(token,data))throw Error('写入文件失败，请检查手机空间或重试');}if(!HeadRingSave.end(token))throw Error('完成文件准备失败，请检查手机空间或重试');started=false;}catch(err){if(started)HeadRingSave.abort(token);alert(err.message||'读取文件失败，请重试');}},true);})();";

 private void updateAwake(){if(resumed&&awake!=null&&awake.isChecked())getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);}
 protected void onPause(){resumed=false;updateAwake();if(session!=null)session.web.onPause();super.onPause();}
 protected void onResume(){super.onResume();resumed=true;updateAwake();if(session!=null)session.web.onResume();}
 protected void onDestroy(){
  handler.removeCallbacksAndMessages(null);
  if(candidateDialog!=null)candidateDialog.dismiss();
  if(session!=null){
   if(session.activity==this)session.activity=null;
   if(isChangingConfigurations()){
    ViewParent parent=session.web.getParent();if(parent instanceof ViewGroup)((ViewGroup)parent).removeView(session.web);session.context.setBaseContext(getApplicationContext());
   }else{
    if(session.chooser!=null)session.chooser.onReceiveValue(null);session.chooser=null;session.exports.abortActive();session.handler.removeCallbacksAndMessages(null);session.web.removeJavascriptInterface("HeadRingSave");session.web.destroy();
   }
  }
  super.onDestroy();
 }
}
