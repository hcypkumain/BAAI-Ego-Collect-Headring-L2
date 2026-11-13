package org.headring.usbentry;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.util.Base64;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.*;
import java.io.*;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.UUID;

public final class MainActivity extends Activity {
 private static final String HOME="http://localhost:8765/phone";
 private static final int PICK=101,SAVE=102;
 private final Handler handler=new Handler();
 private TextView status; private LinearLayout settings; private WebView web; private ValueCallback<Uri[]> chooser;
 private File pendingBlob; private String pendingName,pendingMime; private FileOutputStream blobStream; private long blobExpected,blobWritten,blobSerial;
 private final java.util.concurrent.Semaphore nativeDownload=new java.util.concurrent.Semaphore(1);
 private boolean firstLoad=true; private volatile String pageToken=""; private final SaveBridge bridge=new SaveBridge();
 public void onCreate(Bundle b){super.onCreate(b);
  if(android.os.Build.VERSION.SDK_INT>=33&&checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},21);
  LinearLayout root=new LinearLayout(this);root.setOrientation(1);root.setFitsSystemWindows(true);
  LinearLayout bar=new LinearLayout(this);Button home=new Button(this);home.setText("采集页面");Button setup=new Button(this);setup.setText("连接设置");bar.addView(home,new LinearLayout.LayoutParams(0,-2,1));bar.addView(setup,new LinearLayout.LayoutParams(0,-2,1));root.addView(bar);
  ScrollView panel=new ScrollView(this);settings=new LinearLayout(this);settings.setPadding(28,16,28,20);settings.setOrientation(1);
  TextView help=new TextView(this);help.setText("连接头环并打开 USB 网络共享。助手会自动发现21号设备。若仍显示等待，可输入设备当前地址进行一次连接和接口学习。请保持助手通知运行。");settings.addView(help);
  EditText ip=new EditText(this);ip.setSingleLine(true);ip.setHint("设备当前地址（仅首次诊断）");settings.addView(ip);
  Button test=new Button(this);test.setText("连接并记住USB接口");settings.addView(test);test.setOnClickListener(v->{Intent i=new Intent(this,EntryService.class);i.setAction("manual_probe");i.putExtra("ip",ip.getText().toString().trim());startForegroundService(i);});
  status=new TextView(this);status.setTextSize(13);status.setTextIsSelectable(true);settings.addView(status);panel.addView(settings);root.addView(panel);
  web=new WebView(this);web.getSettings().setJavaScriptEnabled(true);web.getSettings().setDomStorageEnabled(true);web.getSettings().setMediaPlaybackRequiresUserGesture(false);web.getSettings().setMixedContentMode(android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW);web.getSettings().setAllowFileAccess(false);web.getSettings().setAllowContentAccess(true);web.getSettings().setAllowFileAccessFromFileURLs(false);web.getSettings().setAllowUniversalAccessFromFileURLs(false);CookieManager.getInstance().setAcceptCookie(true);CookieManager.getInstance().setAcceptThirdPartyCookies(web,false);web.addJavascriptInterface(bridge,"HeadRingSave");
  web.setWebViewClient(new WebViewClient(){
   public boolean shouldOverrideUrlLoading(WebView v,WebResourceRequest req){if(local(req.getUrl()))return false;Toast.makeText(MainActivity.this,"外部链接无法在离线采集页打开",Toast.LENGTH_SHORT).show();return true;}
   public WebResourceResponse shouldInterceptRequest(WebView v,WebResourceRequest req){Uri u=req.getUrl();String scheme=u.getScheme();if(("http".equalsIgnoreCase(scheme)||"https".equalsIgnoreCase(scheme))&&!local(u))return new WebResourceResponse("text/plain","UTF-8",new ByteArrayInputStream(new byte[0]));return null;}
   public void onPageStarted(WebView v,String url,android.graphics.Bitmap icon){pageToken=UUID.randomUUID().toString();bridge.abortActive();}
   public void onPageFinished(WebView v,String url){if(local(Uri.parse(url))&&url.equals(v.getUrl()))v.evaluateJavascript(BLOB_HOOK.replace("__TOKEN__",pageToken),null);}
   public void onReceivedError(WebView v,WebResourceRequest req,android.webkit.WebResourceError err){if(req.isForMainFrame()&&local(req.getUrl())&&firstLoad){firstLoad=false;handler.postDelayed(()->v.loadUrl(HOME),1500);}}
  });
  web.setWebChromeClient(new WebChromeClient(){
   public boolean onShowFileChooser(WebView v,ValueCallback<Uri[]> cb,FileChooserParams params){if(chooser!=null)chooser.onReceiveValue(null);chooser=cb;try{startActivityForResult(params.createIntent(),PICK);return true;}catch(Exception e){chooser=null;cb.onReceiveValue(null);Toast.makeText(MainActivity.this,"无法打开系统文件选择器",Toast.LENGTH_LONG).show();return true;}}
  });
  web.setDownloadListener((url,agent,disposition,mime,length)->{
   if(!local(Uri.parse(url))){Toast.makeText(this,"无法下载外部链接",Toast.LENGTH_SHORT).show();return;}
   if(!nativeDownload.tryAcquire()){Toast.makeText(this,"已有下载进行中",Toast.LENGTH_SHORT).show();return;}String name=android.webkit.URLUtil.guessFileName(url,disposition,mime);new Thread(()->{try{download(url,agent,name,mime);}finally{nativeDownload.release();}}).start();
  });
  root.addView(web,new LinearLayout.LayoutParams(-1,0,1));setContentView(root);panel.setVisibility(View.GONE);
  home.setOnClickListener(v->{panel.setVisibility(View.GONE);web.setVisibility(View.VISIBLE);if(web.getUrl()==null)web.loadUrl(HOME);});
  setup.setOnClickListener(v->{panel.setVisibility(panel.getVisibility()==View.VISIBLE?View.GONE:View.VISIBLE);});
  startForegroundService(new Intent(this,EntryService.class));handler.postDelayed(()->web.loadUrl(HOME),600);refresh();
 }
 private boolean local(Uri u){if(u==null)return false;String h=u.getHost();return "http".equalsIgnoreCase(u.getScheme())&&("localhost".equalsIgnoreCase(h)||"127.0.0.1".equals(h))&&u.getPort()==8765;}
 private void refresh(){status.setText(Diagnostics.report());handler.postDelayed(this::refresh,2000);}
 public void onBackPressed(){if(web.canGoBack())web.goBack();else super.onBackPressed();}
 protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);
  if(request==PICK){if(chooser!=null){chooser.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result,data));chooser=null;}return;}
  if(request==SAVE){File file=pendingBlob;pendingBlob=null;if(file==null)return;if(result!=RESULT_OK||data==null||data.getData()==null){file.delete();return;}Uri dest=data.getData();new Thread(()->{boolean ok=false;try(InputStream in=new FileInputStream(file);OutputStream out=getContentResolver().openOutputStream(dest)){if(out==null)throw new IOException();byte[] b=new byte[16384];int n;while((n=in.read(b))!=-1)out.write(b,0,n);ok=true;}catch(Exception ignored){}finally{file.delete();}final boolean success=ok;runOnUiThread(()->Toast.makeText(this,success?"文件已保存":"保存失败，请重试",Toast.LENGTH_LONG).show());}).start();}

 }
 private void promptSave(){runOnUiThread(()->{try{Intent i=new Intent(Intent.ACTION_CREATE_DOCUMENT);i.addCategory(Intent.CATEGORY_OPENABLE);i.setType(pendingMime);i.putExtra(Intent.EXTRA_TITLE,pendingName);startActivityForResult(i,SAVE);}catch(Exception e){Toast.makeText(this,"无法打开保存窗口",Toast.LENGTH_LONG).show();bridge.abortInternal();}});}
 private void download(String url,String agent,String filename,String mime){File file=null;try{URL target=new URL(url);if(!local(Uri.parse(url)))throw new IOException();HttpURLConnection c=(HttpURLConnection)target.openConnection();c.setInstanceFollowRedirects(false);c.setConnectTimeout(3000);c.setReadTimeout(10000);String cookie=CookieManager.getInstance().getCookie(url);if(cookie!=null)c.setRequestProperty("Cookie",cookie);if(agent!=null)c.setRequestProperty("User-Agent",agent);if(c.getResponseCode()!=200)throw new IOException();file=File.createTempFile("download-",".tmp",getCacheDir());long count=0;try(InputStream in=c.getInputStream();OutputStream out=new FileOutputStream(file)){byte[] b=new byte[16384];int n;while((n=in.read(b))!=-1){count+=n;if(count>32L*1024*1024)throw new IOException();out.write(b,0,n);}}finally{c.disconnect();}if(!bridge.queueNative(file,filename,mime))throw new IOException();file=null;promptSave();}catch(Exception e){if(file!=null)file.delete();runOnUiThread(()->Toast.makeText(this,"下载失败，请重试",Toast.LENGTH_LONG).show());}}
 private final class SaveBridge {
  private boolean authorized(String token){return token!=null&&token.equals(pageToken)&&!token.isEmpty();}
  @JavascriptInterface public synchronized boolean begin(String token,String filename,String mime,long size){try{if(!authorized(token)||size<0||size>32L*1024*1024||blobStream!=null||pendingBlob!=null)return false;pendingName=safeName(filename);pendingMime=mime==null||mime.length()>100?"application/octet-stream":mime;pendingBlob=File.createTempFile("download-",".tmp",getCacheDir());blobStream=new FileOutputStream(pendingBlob);blobExpected=size;blobWritten=0;final long serial=++blobSerial;handler.postDelayed(()->{synchronized(SaveBridge.this){if(blobStream!=null&&serial==blobSerial)abortInternal();}},60000);return true;}catch(Exception e){abortInternal();return false;}}
  @JavascriptInterface public synchronized boolean chunk(String token,String base64){try{if(!authorized(token)||blobStream==null||base64==null||base64.length()>100000)return false;byte[] b=Base64.decode(base64,Base64.DEFAULT);blobWritten+=b.length;if(blobWritten>blobExpected)throw new IOException();blobStream.write(b);return true;}catch(Exception e){abortInternal();return false;}}
  @JavascriptInterface public synchronized void end(String token){try{if(!authorized(token))return;if(blobStream==null||blobWritten!=blobExpected){abortInternal();return;}blobStream.close();blobStream=null;promptSave();}catch(Exception e){abortInternal();}}
  @JavascriptInterface public synchronized void abort(String token){if(authorized(token))abortInternal();}
  synchronized boolean queueNative(File file,String name,String mime){if(blobStream!=null||pendingBlob!=null)return false;pendingBlob=file;pendingName=safeName(name);pendingMime=mime==null||mime.length()>100?"application/octet-stream":mime;return true;}
  synchronized void abortActive(){if(blobStream!=null)abortInternal();}
  synchronized void abortInternal(){try{if(blobStream!=null)blobStream.close();}catch(IOException ignored){}blobStream=null;if(pendingBlob!=null)pendingBlob.delete();pendingBlob=null;}
 }
 private static String safeName(String filename){String name=filename==null?"下载文件":filename.replaceAll("[^A-Za-z0-9._\\-\\u4e00-\\u9fff]","_");if(name.length()>100)name=name.substring(0,100);return name.isEmpty()?"下载文件":name;}
 private static final String BLOB_HOOK="(function(){if(window.__hrBlobHook)return;window.__hrBlobHook=true;var token='__TOKEN__';document.addEventListener('click',async function(e){var a=e.target.closest&&e.target.closest('a[download]');if(!a||!a.href.startsWith('blob:'))return;e.preventDefault();e.stopImmediatePropagation();try{var b=await(await fetch(a.href)).blob();if(!HeadRingSave.begin(token,a.download||'download',b.type||'application/octet-stream',b.size)){alert('文件过大或另一个保存正在进行');return;}for(var p=0;p<b.size;p+=49152){var data=await new Promise(function(ok,no){var r=new FileReader();r.onload=function(){ok(r.result.split(',')[1]);};r.onerror=no;r.readAsDataURL(b.slice(p,p+49152));});if(!HeadRingSave.chunk(token,data))throw Error('write');}HeadRingSave.end(token);}catch(err){HeadRingSave.abort(token);alert('保存失败，请重试');}},true);})();";
 protected void onPause(){if(web!=null)web.onPause();super.onPause();}
 protected void onResume(){super.onResume();if(web!=null)web.onResume();}
 protected void onDestroy(){handler.removeCallbacksAndMessages(null);if(chooser!=null)chooser.onReceiveValue(null);bridge.abortInternal();if(web!=null){web.removeJavascriptInterface("HeadRingSave");web.destroy();}super.onDestroy();}
}
