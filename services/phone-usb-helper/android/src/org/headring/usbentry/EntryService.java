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
 public void onCreate(){super.onCreate(); NotificationManager nm=getSystemService(NotificationManager.class);nm.createNotificationChannel(new NotificationChannel("entry","头环离线入口",NotificationManager.IMPORTANCE_LOW)); Notification n=new Notification.Builder(this,"entry").setSmallIcon(android.R.drawable.stat_sys_upload).setContentTitle("头环离线入口").setContentText("等待设备连接").setOngoing(true).build();startForeground(21,n); running=true; discovery=new NsdDiscovery(this); pool.execute(()->listen("127.0.0.1"));pool.execute(()->listen("::1")); discover();scanTimer=Executors.newSingleThreadScheduledExecutor();scanTimer.scheduleWithFixedDelay(()->{if(running&&target==null){int gen=generation.get();Endpoint e=new UsbScanDiscovery(this).scan();synchronized(stateLock){if(running&&e!=null&&target==null&&generation.get()==gen){target=e;notifyText("已发现 设备");}}}},3,20,TimeUnit.SECONDS);healthTimer=Executors.newSingleThreadScheduledExecutor();healthTimer.scheduleWithFixedDelay(()->{Endpoint e=target;if(e!=null){InetAddress source=e.source==null?null:new UsbScanDiscovery(this).sourceFor(e.address);if((e.source!=null&&(source==null||!source.equals(e.source)))||!IdentityProbe.verify(e.address,e.source,null))invalidate(e);}},5,5,TimeUnit.SECONDS);}
 public int onStartCommand(Intent i,int flags,int id){
  if(i!=null&&"manual_probe".equals(i.getAction())){String raw=i.getStringExtra("ip");control.execute(()->manualProbe(raw));}return START_STICKY;
 }
 private void manualProbe(String raw){
  try{if(raw==null||!raw.matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}")){Diagnostics.manual="请输入IPv4地址";return;}InetAddress a=InetAddress.getByName(raw);if(!(a instanceof Inet4Address)||!a.isSiteLocalAddress()){Diagnostics.manual="只允许内网IPv4";return;}Diagnostics.manual="正在验证 "+raw;if(!IdentityProbe.verify(a,null,null)){Diagnostics.manual="身份探针未通过 "+Diagnostics.probe;return;}synchronized(stateLock){generation.incrementAndGet();target=null;closeSockets();target=new Endpoint(ConfiguredDevice.ID,a,8765,null,null);}try(Socket route=new Socket()){route.connect(new InetSocketAddress(a,8766),1000);new UsbScanDiscovery(this).learn(route.getLocalAddress());}notifyText("已通过临时地址连接21号");}catch(Exception e){Diagnostics.manual="失败 "+e.getClass().getSimpleName();}
 }

 public IBinder onBind(Intent i){return null;}
 private void discover(){discovery.start(new DiscoveryProvider.Listener(){
  public void found(Endpoint e){final int gen=generation.incrementAndGet();control.execute(()->{InetAddress source=new UsbScanDiscovery(EntryService.this).sourceFor(e.address);if(source==null){Diagnostics.probe="NSD地址被USB接口筛选拒绝 "+e.address;return;}if(!IdentityProbe.verify(e.address,source,null))return;Diagnostics.probe="设备身份探针通过 "+e.address;synchronized(stateLock){if(!running||generation.get()!=gen)return;if(target!=null&&target.address.equals(e.address)&&source.equals(target.source)&&target.port==e.port)return;target=null;closeSockets();target=new Endpoint(e.deviceId,e.address,e.port,null,source);notifyText("已发现 设备");}});}
  public void lost(){synchronized(stateLock){generation.incrementAndGet();target=null;closeSockets();}notifyText("等待 设备连接");}
  public void failed(){notifyText("设备发现失败，请重新打开助手");}
 });}
 private void invalidate(Endpoint e){synchronized(stateLock){if(target==e){generation.incrementAndGet();target=null;closeSockets();notifyText("等待 设备连接");}}}
 private void closeSockets(){synchronized(sockets){for(Socket s:sockets)try{s.close();}catch(IOException ignored){} sockets.clear();}}
 private void listen(String address){try{ServerSocket local=new ServerSocket();local.setReuseAddress(true);local.bind(new InetSocketAddress(InetAddress.getByName(address),8765),32);if(address.equals("::1"))server6=local;else server=local;while(running){Socket client=local.accept();if(!clients.tryAcquire()){client.close();continue;}sockets.add(client);pool.execute(()->handle(client));}}catch(IOException e){if(running&&address.equals("127.0.0.1"))notifyText("本机端口被占用，请关闭占用端口的应用");}}
 private void handle(Socket c){Socket upstream=null; try{InputStream input=c.getInputStream();ByteArrayOutputStream h=new ByteArrayOutputStream(); int last=0,b;long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(12);while(h.size()<16384){long remaining=deadline-System.nanoTime();if(remaining<=0)break;c.setSoTimeout((int)Math.max(1,TimeUnit.NANOSECONDS.toMillis(remaining)));b=input.read();if(b==-1)break;h.write(b);last=(last<<8)|b;if(last==0x0d0a0d0a)break;}byte[] head=h.toByteArray();String text=new String(head,StandardCharsets.ISO_8859_1);if(last!=0x0d0a0d0a||!validHost(text)){fail(c,400,"请求无效");return;}Endpoint endpoint; synchronized(stateLock){endpoint=target;}if(endpoint==null){fail(c,503,"等待 设备连接，请确认 USB 网络共享已开启后刷新。");return;}upstream=endpoint.network==null?new Socket():endpoint.network.getSocketFactory().createSocket();if(endpoint.source!=null)upstream.bind(new InetSocketAddress(endpoint.source,0));try{upstream.connect(new InetSocketAddress(endpoint.address,endpoint.port),2500);}catch(IOException ex){invalidate(endpoint);throw ex;}synchronized(stateLock){if(target!=endpoint)return;sockets.add(upstream);}c.setSoTimeout(0);upstream.setSoTimeout(0);upstream.getOutputStream().write(head);Socket u=upstream;pool.execute(()->copy(input,u));copy(u.getInputStream(),c);
 }catch(IOException ignored){}finally{close(c);close(upstream);clients.release();}}
 private boolean validHost(String h){String host=null,origin=null;String[] lines=h.split("\\r\\n");for(int i=1;i<lines.length;i++){int p=lines[i].indexOf(':');if(p<1)continue;String k=lines[i].substring(0,p).trim().toLowerCase(Locale.ROOT),v=lines[i].substring(p+1).trim().toLowerCase(Locale.ROOT);if(k.equals("host")){if(host!=null)return false;host=v;}if(k.equals("origin")){if(origin!=null)return false;origin=v;}}return ("127.0.0.1:8765".equals(host)||"localhost:8765".equals(host))&&(origin==null||origin.equals("http://"+host));}
 private void fail(Socket s,int status,String message)throws IOException{byte[] body=("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5><p>"+message+"</p>").getBytes(StandardCharsets.UTF_8);OutputStream o=s.getOutputStream();o.write(("HTTP/1.1 "+status+" Unavailable\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "+body.length+"\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));o.write(body);o.flush();}
 private void copy(InputStream in,Socket out){try{byte[] b=new byte[16384];OutputStream o=out.getOutputStream();int n;while((n=in.read(b))!=-1){o.write(b,0,n);o.flush();}out.shutdownOutput();}catch(IOException ignored){}}
 private void close(Socket s){if(s!=null){sockets.remove(s);try{s.close();}catch(IOException ignored){}}}
 public void onDestroy(){running=false;target=null;if(scanTimer!=null)scanTimer.shutdownNow();if(healthTimer!=null)healthTimer.shutdownNow();control.shutdownNow();discovery.stop();try{if(server!=null)server.close();if(server6!=null)server6.close();}catch(IOException ignored){}closeSockets();pool.shutdownNow();super.onDestroy();}
}
