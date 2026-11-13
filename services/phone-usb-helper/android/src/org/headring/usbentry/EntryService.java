package org.headring.usbentry;

import android.app.*;
import android.content.*;
import android.net.nsd.*;
import android.os.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

public final class EntryService extends Service {
 private final ExecutorService pool=Executors.newFixedThreadPool(66);
 private final Object stateLock=new Object();
 private final AtomicInteger generation=new AtomicInteger();
 private final Semaphore clients=new Semaphore(32);
 private final Set<Socket> sockets=Collections.synchronizedSet(new HashSet<>());
 private DiscoveryProvider discovery;
 private ScheduledExecutorService scanTimer; private ScheduledExecutorService healthTimer; private final ExecutorService control=Executors.newFixedThreadPool(2);
 private volatile Endpoint target; private volatile boolean running; private ServerSocket server; private ServerSocket server6;
 private void notifyText(String s){ NotificationManager nm=getSystemService(NotificationManager.class); Notification n=new Notification.Builder(this,"entry").setSmallIcon(android.R.drawable.stat_sys_upload).setContentTitle("头环离线入口").setContentText(s).setOngoing(true).setContentIntent(PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT)).build(); nm.notify(21,n); }
 public void onCreate(){super.onCreate(); NotificationManager nm=getSystemService(NotificationManager.class);nm.createNotificationChannel(new NotificationChannel("entry","头环离线入口",NotificationManager.IMPORTANCE_LOW)); Notification n=new Notification.Builder(this,"entry").setSmallIcon(android.R.drawable.stat_sys_upload).setContentTitle("头环离线入口").setContentText("等待设备连接").setOngoing(true).build();startForeground(21,n); running=true; discovery=new NsdDiscovery(this); pool.execute(()->listen("127.0.0.1"));pool.execute(()->listen("::1")); discover();scanTimer=Executors.newSingleThreadScheduledExecutor();scanTimer.scheduleWithFixedDelay(()->{if(running&&target==null)scanCandidates(false);},3,20,TimeUnit.SECONDS);healthTimer=Executors.newSingleThreadScheduledExecutor();healthTimer.scheduleWithFixedDelay(()->{Endpoint e=target;if(e!=null){InetAddress source=e.source==null?null:new UsbScanDiscovery(this).sourceFor(e.address);if((e.source!=null&&(source==null||!source.equals(e.source)))||!IdentityProbe.verify(e.address,e.source,null,e.deviceId))invalidate(e);}},5,5,TimeUnit.SECONDS);}
 public int onStartCommand(Intent i,int flags,int id){
  if(i!=null&&"manual_probe".equals(i.getAction())){String raw=i.getStringExtra("ip");control.execute(()->manualProbe(raw));}
  if(i!=null&&"scan_candidates".equals(i.getAction()))control.execute(()->scanCandidates(true));
  if(i!=null&&"select_candidate".equals(i.getAction())){String key=i.getStringExtra("key");control.execute(()->selectCandidate(key));}return START_STICKY;
 }
 private void installTarget(Endpoint e,boolean selected){synchronized(stateLock){if(!running||(!selected&&target!=null))return;String last=getSharedPreferences("usb",MODE_PRIVATE).getString("last_device","");if(!selected&&!last.isEmpty()&&!last.equals(e.deviceId)){Diagnostics.manual="检测到不同设备，请在连接设置中选择";return;}generation.incrementAndGet();target=null;closeSockets();target=e;Diagnostics.current=e.deviceId;getSharedPreferences("usb",MODE_PRIVATE).edit().putString("last_device",e.deviceId).apply();}notifyText("已连接 "+e.deviceId);}
 private void scanCandidates(boolean requested){int gen=generation.get();List<Endpoint> matches=new UsbScanDiscovery(this).scan();synchronized(stateLock){if(!running||generation.get()!=gen)return;Diagnostics.candidates=Collections.unmodifiableList(new ArrayList<>(matches));if(matches.size()>1)Diagnostics.manual="发现多个设备，请选择";else if(requested&&matches.isEmpty())Diagnostics.manual="未发现设备，请检查USB共享";}String[] ids=new String[matches.size()];for(int i=0;i<ids.length;i++)ids[i]=matches.get(i).deviceId;String previous=getSharedPreferences("usb",MODE_PRIVATE).getString("last_device","");if(target==null&&DeviceSelection.autoConnect(ids,previous))installTarget(matches.get(0),false);else if(target==null&&matches.size()==1)Diagnostics.manual="检测到不同设备，请选择";}
 private void selectCandidate(String key){for(Endpoint e:Diagnostics.candidates){if((e.deviceId+"@"+e.address.getHostAddress()).equals(key)){if(IdentityProbe.verify(e.address,e.source,null,e.deviceId))installTarget(e,true);else Diagnostics.manual="设备已离线，请重新搜索";return;}}Diagnostics.manual="候选已变化，请重新搜索";}
 private void manualProbe(String raw){
  try{if(raw==null||!raw.matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}")){Diagnostics.manual="请输入IPv4地址";return;}InetAddress a=InetAddress.getByName(raw);if(!(a instanceof Inet4Address)||!a.isSiteLocalAddress()){Diagnostics.manual="只允许内网IPv4";return;}Diagnostics.manual="正在验证 "+raw;String id=IdentityProbe.identify(a,null,null);if(id==null){Diagnostics.manual="身份探针未通过 "+Diagnostics.probe;return;}InetAddress source;try(Socket route=new Socket()){route.connect(new InetSocketAddress(a,8766),1000);source=route.getLocalAddress();if(!new UsbScanDiscovery(this).learn(source))return;}Endpoint e=new Endpoint(id,a,8765,null,source);installTarget(e,true);notifyText("已连接 "+id);}catch(Exception e){Diagnostics.manual="失败 "+e.getClass().getSimpleName();}
 }

 public IBinder onBind(Intent i){return null;}
 private void discover(){discovery.start(new DiscoveryProvider.Listener(){
  public void found(Endpoint e){Diagnostics.nsd="发现 "+e.deviceId+"，正在检查USB候选";}
  public void lost(){Diagnostics.nsd="服务已离开，正在等待";}
  public void failed(){Diagnostics.nsd="设备发现不可用，使用USB扫描";}
 });}
 private void invalidate(Endpoint e){synchronized(stateLock){if(target==e){generation.incrementAndGet();target=null;Diagnostics.current="";closeSockets();notifyText("等待头环连接");}}}
 private void closeSockets(){synchronized(sockets){for(Socket s:sockets)try{s.close();}catch(IOException ignored){} sockets.clear();}}
 private void listen(String address){try{ServerSocket local=new ServerSocket();local.setReuseAddress(true);local.bind(new InetSocketAddress(InetAddress.getByName(address),8765),32);if(address.equals("::1"))server6=local;else server=local;while(running){Socket client=local.accept();if(!clients.tryAcquire()){client.close();continue;}sockets.add(client);pool.execute(()->handle(client));}}catch(IOException e){if(running&&address.equals("127.0.0.1"))notifyText("本机端口被占用，请关闭占用端口的应用");}}
 private void handle(Socket c){Socket upstream=null; try{InputStream input=c.getInputStream();ByteArrayOutputStream h=new ByteArrayOutputStream(); int last=0,b;long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(12);while(h.size()<16384){long remaining=deadline-System.nanoTime();if(remaining<=0)break;c.setSoTimeout((int)Math.max(1,TimeUnit.NANOSECONDS.toMillis(remaining)));b=input.read();if(b==-1)break;h.write(b);last=(last<<8)|b;if(last==0x0d0a0d0a)break;}byte[] head=h.toByteArray();String text=new String(head,StandardCharsets.ISO_8859_1);Endpoint endpoint; synchronized(stateLock){endpoint=target;}if(last!=0x0d0a0d0a||!validHost(text,endpoint)){fail(c,400,"请求无效");return;}if(endpoint==null){fail(c,503,"等待头环连接，请确认 USB 网络共享已开启后刷新。");return;}upstream=endpoint.network==null?new Socket():endpoint.network.getSocketFactory().createSocket();if(endpoint.source!=null)upstream.bind(new InetSocketAddress(endpoint.source,0));try{upstream.connect(new InetSocketAddress(endpoint.address,endpoint.port),2500);}catch(IOException ex){invalidate(endpoint);throw ex;}synchronized(stateLock){if(target!=endpoint)return;sockets.add(upstream);}c.setSoTimeout(0);upstream.setSoTimeout(0);upstream.getOutputStream().write(head);Socket u=upstream;pool.execute(()->copy(input,u));copy(u.getInputStream(),c);
 }catch(IOException ignored){}finally{close(c);close(upstream);clients.release();}}
 private boolean validHost(String h,Endpoint e){return OriginPolicy.valid(h,e==null?null:e.deviceId);}

 private void fail(Socket s,int status,String message)throws IOException{byte[] body=("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5><p>"+message+"</p>").getBytes(StandardCharsets.UTF_8);OutputStream o=s.getOutputStream();o.write(("HTTP/1.1 "+status+" Unavailable\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "+body.length+"\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));o.write(body);o.flush();}
 private void copy(InputStream in,Socket out){try{byte[] b=new byte[16384];OutputStream o=out.getOutputStream();int n;while((n=in.read(b))!=-1){o.write(b,0,n);o.flush();}out.shutdownOutput();}catch(IOException ignored){}}
 private void close(Socket s){if(s!=null){sockets.remove(s);try{s.close();}catch(IOException ignored){}}}
 public void onDestroy(){running=false;target=null;Diagnostics.current="";if(scanTimer!=null)scanTimer.shutdownNow();if(healthTimer!=null)healthTimer.shutdownNow();control.shutdownNow();discovery.stop();try{if(server!=null)server.close();if(server6!=null)server6.close();}catch(IOException ignored){}closeSockets();pool.shutdownNow();super.onDestroy();}
}
