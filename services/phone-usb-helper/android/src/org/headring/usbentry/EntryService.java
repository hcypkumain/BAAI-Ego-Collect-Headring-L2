package org.headring.usbentry;

import android.app.*;
import android.content.*;
import android.os.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;

public final class EntryService extends Service {
 private final Object stateLock=new Object(),scanLock=new Object();
 private final ExecutorService headers=new ThreadPoolExecutor(8,8,0,TimeUnit.SECONDS,new ArrayBlockingQueue<>(16));
 private final ExecutorService transfers=Executors.newFixedThreadPool(80),control=Executors.newFixedThreadPool(2),scanner=Executors.newSingleThreadExecutor();
 private final Semaphore headerSlots=new Semaphore(24),dataSlots=new Semaphore(24),controlSlots=new Semaphore(8);
 private final Set<Socket> sockets=Collections.synchronizedSet(new HashSet<>());
 private final Set<Session> sessions=ConcurrentHashMap.newKeySet();
 private final ConnectionState state=new ConnectionState();
 private volatile boolean running;
 private volatile ServerSocket server,server6;
 private ScheduledExecutorService timers;
 private DiscoveryProvider discovery;
 private Endpoint checkedWorkbench;
 private int workbenchFailures;
 private boolean scanActive;
 private ScanJob pendingScan;
 private static final class ScanJob {
  final long id,choice;final Endpoint hint;
  ScanJob(long n,long c,Endpoint h){id=n;choice=c;hint=h;}
 }
 private final class Session {
  final Socket client,upstream;final TransferBudget budget;
  Session(Socket c,Socket u,ProxyRequest r){client=c;upstream=u;budget=new TransferBudget(System.nanoTime(),r.control,r.stream);}
  void close(){EntryService.this.close(client);EntryService.this.close(upstream);sessions.remove(this);}
 }
 private void notifyState(){
  if(!running)return;
  Notification n=new Notification.Builder(this,"entry").setSmallIcon(android.R.drawable.stat_sys_upload).setContentTitle("头环离线入口").setContentText(state.snapshot(System.nanoTime()).message).setOngoing(true).setContentIntent(PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT)).build();
  getSystemService(NotificationManager.class).notify(21,n);
 }
 public void onCreate(){
  super.onCreate();getSystemService(NotificationManager.class).createNotificationChannel(new NotificationChannel("entry","头环离线入口",NotificationManager.IMPORTANCE_LOW));
  startForeground(21,new Notification.Builder(this,"entry").setSmallIcon(android.R.drawable.stat_sys_upload).setContentTitle("头环离线入口").setContentText("正在准备本机入口").setOngoing(true).build());
  running=true;Diagnostics.connection=state;
  new Thread(()->listen("127.0.0.1",false),"headring-ipv4").start();
  new Thread(()->listen("::1",true),"headring-ipv6").start();
  discovery=new NsdDiscovery(this);discovery.start(new DiscoveryProvider.Listener(){
   public void found(Endpoint e){Diagnostics.nsd="发现服务 "+e.deviceId+"，待 USB 身份验证";if(!state.snapshot(System.nanoTime()).scanning)requestScan(e,true);}
   public void lost(){Diagnostics.nsd="服务已离开；以 USB 身份验证结果为准";}
   public void failed(){Diagnostics.nsd="服务发现不可用，使用 USB 扫描";}
  });
  timers=Executors.newScheduledThreadPool(3);
  timers.scheduleWithFixedDelay(()->{ConnectionState.Snapshot s=state.snapshot(System.nanoTime());if(running&&s.target==null&&!s.scanning&&!s.choosing)requestScan(null,true);},1,20,TimeUnit.SECONDS);
  timers.scheduleWithFixedDelay(this::checkTarget,5,5,TimeUnit.SECONDS);
  timers.scheduleWithFixedDelay(()->{long now=System.nanoTime();for(Session session:sessions)if(session.budget.expired(now))session.close();},1,1,TimeUnit.SECONDS);
 }
 public int onStartCommand(Intent intent,int flags,int id){
  if(intent!=null&&running){
   String action=intent.getAction();
   if("scan_candidates".equals(action))requestScan(null,false);
   if("select_candidate".equals(action)||"manual_probe".equals(action)){
    // Allocate before queuing: completion order cannot change user intent order.
    String key=intent.getStringExtra("key"),ip=intent.getStringExtra("ip");long choice;
    synchronized(stateLock){
     ConnectionState.Snapshot s=state.snapshot(System.nanoTime());
     if("select_candidate".equals(action)&&!s.current().isEmpty()&&!s.choosing&&s.target!=null&&ConnectionState.key(s.target).equals(key)){notifyState();return START_STICKY;}
     choice=state.beginChoice(action+":"+("select_candidate".equals(action)?key:ip==null?"":ip.trim()));
    }
    if(choice<0)return START_STICKY;
    try{control.execute(()->{try{if("select_candidate".equals(action))selectCandidate(key,choice);else manualProbe(ip,choice);}catch(Exception e){state.choiceError(choice,"连接验证失败，请重试");}finally{state.finishChoice(choice);notifyState();}});}catch(RejectedExecutionException e){state.choiceError(choice,"连接服务正在关闭");}
   }
  }
  return START_STICKY;
 }
 private void requestScan(Endpoint hint,boolean automatic){
  synchronized(stateLock){
   ConnectionState.Snapshot s=state.snapshot(System.nanoTime());if(!running||s.choosing||automatic&&s.target!=null)return;
   synchronized(scanLock){long id=state.beginScan();if(id<0)return;pendingScan=new ScanJob(id,state.choiceRevision(),hint);startScanner();}
  }
 }
 // Called with stateLock then scanLock. The worker never holds scanLock while probing.
 private void startScanner(){
  if(scanActive||!running)return;scanActive=true;
  try{scanner.execute(this::drainScans);}catch(RejectedExecutionException e){scanActive=false;if(pendingScan!=null)state.finishScan(pendingScan.id,new ScanResult(Collections.emptyList(),false,"搜索服务正在关闭"),System.nanoTime());pendingScan=null;}
 }
 private void drainScans(){
  try{
   while(true){
    ScanJob job;synchronized(scanLock){job=pendingScan;pendingScan=null;if(job==null)return;}
    if(!running||!state.scanCurrent(job.id))continue;
    UsbScanDiscovery discovery=new UsbScanDiscovery(this);ScanResult result;
    try{result=discovery.scan(job.hint,()->!running||!state.scanCurrent(job.id),e->verifiedPeer(job,discovery,e));}
    catch(Exception e){result=new ScanResult(Collections.emptyList(),false,"搜索未完成，请重试");}
    if(discovery.hadSource()&&!discovery.sourceCurrent())result=new ScanResult(Collections.emptyList(),false,"USB 来源已变化，请重新搜索");
    synchronized(stateLock){if(!state.finishScan(job.id,result,System.nanoTime()))continue;}
    notifyState();
   }
  }finally{
   synchronized(stateLock){synchronized(scanLock){scanActive=false;if(running&&pendingScan!=null)startScanner();}}
  }
 }
 private void verifiedPeer(ScanJob job,UsbScanDiscovery discovery,Endpoint endpoint){
  if(!discovery.sourceCurrent(endpoint))return;
  boolean installed=false;
  try{
   synchronized(stateLock){
    if(!running||!state.scanCurrent(job.id))return;
    long now=System.nanoTime();state.found(job.id,endpoint,now);
    installed=state.autoChooseVerified(job.id,job.choice,endpoint,now);
    if(installed){closeSockets();remember(endpoint);}
   }
  }finally{if(installed)notifyState();}
 }
 private void selectCandidate(String key,long choice){
  if(!state.choiceCurrent(choice))return;
  for(Endpoint e:state.snapshot(System.nanoTime()).candidates)if(ConnectionState.key(e).equals(key)){
   InetAddress source=new UsbScanDiscovery(this).sourceFor(e.address);
   if(source==null||!source.equals(e.source)||!IdentityProbe.verify(e.address,source,null,e.deviceId)){state.choiceError(choice,"设备或 USB 接口已变化，请重新搜索");return;}
   install(e,choice);return;
  }
  state.choiceError(choice,"候选已过期，请重新搜索；搜索完成后再选择");
 }
 private void manualProbe(String raw,long choice){
  try{
   if(!state.choiceCurrent(choice))return;
   if(raw==null||!raw.matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}"))throw new IOException("请输入 IPv4 地址");
   InetAddress address=InetAddress.getByName(raw);if(!(address instanceof Inet4Address)||!address.isSiteLocalAddress())throw new IOException("只允许内网 IPv4");
   // Learning a nonstandard interface requires proving its actual route and
   // excluding Wi-Fi/cellular/VPN before any connection can be installed.
   InetAddress source;try(Socket route=new Socket()){route.connect(new InetSocketAddress(address,8766),1000);source=route.getLocalAddress();}
   synchronized(stateLock){if(!state.choiceCurrent(choice))return;if(!new UsbScanDiscovery(this).learn(source))throw new IOException(Diagnostics.manual);}
   String id=IdentityProbe.identify(address,source,null);if(id==null)throw new IOException("USB 身份探针未通过");
   if(!source.equals(new UsbScanDiscovery(this).sourceFor(address)))throw new IOException("USB 接口已变化");
   install(new Endpoint(id,address,8765,null,source),choice);
  }catch(Exception e){state.choiceError(choice,"连接失败："+e.getMessage());}
 }
 private void install(Endpoint e,long choice){synchronized(stateLock){if(!running||!state.choose(choice,e))return;closeSockets();remember(e);}notifyState();}
 private void remember(Endpoint e){getSharedPreferences("usb",MODE_PRIVATE).edit().putString("last_device",e.deviceId).apply();}
 private void checkTarget(){
  Endpoint e=state.snapshot(System.nanoTime()).target;if(!running||e==null)return;
  InetAddress source=new UsbScanDiscovery(this).sourceFor(e.address);
  if(e!=checkedWorkbench){checkedWorkbench=e;workbenchFailures=0;}
  if(source!=null&&source.equals(e.source)&&IdentityProbe.verify(e.address,source,null,e.deviceId)){
   if(WorkbenchProbe.reachable(e.address,source,e.port)){workbenchFailures=0;return;}
   if(++workbenchFailures<3)return;
  }
  checkedWorkbench=null;workbenchFailures=0;
  synchronized(stateLock){if(!state.invalidate(e))return;closeSockets();}notifyState();requestScan(null,true);
 }
 private void listen(String address,boolean v6){
  while(running){
   ServerSocket local=null;
   try{
    local=new ServerSocket();local.setReuseAddress(true);local.bind(new InetSocketAddress(InetAddress.getByName(address),EntryAddress.PORT),32);
    if(v6)server6=local;else server=local;state.listener(v6,true);notifyState();
    while(running){Socket client=local.accept();if(!headerSlots.tryAcquire()){client.close();continue;}sockets.add(client);
     try{headers.execute(()->readRequest(client));}catch(RejectedExecutionException e){close(client);headerSlots.release();}
    }
   }catch(IOException ignored){}
   finally{state.listener(v6,false);if(local!=null)try{local.close();}catch(IOException ignored){}if(running)notifyState();}
   if(running)try{Thread.sleep(2000);}catch(InterruptedException e){Thread.currentThread().interrupt();return;}
  }
 }
 private void readRequest(Socket client){
  boolean dispatched=false;
  try{
   long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(5);InputStream input=client.getInputStream();ByteArrayOutputStream bytes=new ByteArrayOutputStream();int last=0;
   while(bytes.size()<16384){long left=deadline-System.nanoTime();if(left<=0)throw new IOException("header timeout");client.setSoTimeout((int)Math.max(1,TimeUnit.NANOSECONDS.toMillis(left)));int b=input.read();if(b<0)break;bytes.write(b);last=(last<<8)|b;if(last==0x0d0a0d0a)break;}
   ConnectionState.Snapshot view=state.snapshot(System.nanoTime());
   if(last!=0x0d0a0d0a){fail(client,400,"请求无效");return;}
   ProxyRequest request;
   try{request=ProxyRequest.parse(new String(bytes.toByteArray(),StandardCharsets.ISO_8859_1),view.current().isEmpty()?null:view.current());}
   catch(IOException e){fail(client,400,"请求无效或设备已变化");return;}
   if(!view.ready()){fail(client,503,"本机入口未就绪，请检查端口冲突");return;}
   if(request.path.equals("/__headring/transport")){transport(client,view);return;}
   if(view.target==null){fail(client,503,"等待头环连接，请开启 USB 网络共享");return;}
   Semaphore slots=request.control?controlSlots:dataSlots;
   if(!slots.tryAcquire()){fail(client,503,request.control?"控制请求繁忙，请重试":"显示请求繁忙，请稍后重试");return;}
   try{transfers.execute(()->forward(client,input,request,view.target,slots));dispatched=true;}
   catch(RejectedExecutionException e){slots.release();}
  }catch(IOException ignored){}
  finally{headerSlots.release();if(!dispatched)close(client);}
 }
 private void forward(Socket client,InputStream input,ProxyRequest request,Endpoint endpoint,Semaphore slots){
  Socket upstream=null;Session session=null;
  try{
   upstream=new Socket();upstream.bind(new InetSocketAddress(endpoint.source,0));upstream.connect(new InetSocketAddress(endpoint.address,endpoint.port),2500);
   synchronized(stateLock){ConnectionState.Snapshot view=state.snapshot(System.nanoTime());if(!running||!view.ready()||view.target!=endpoint)return;sockets.add(upstream);}
   client.setSoTimeout(2000);upstream.setSoTimeout(2000);session=new Session(client,upstream,request);sessions.add(session);
   upstream.getOutputStream().write(request.header);session.budget.touch(System.nanoTime());
   Session active=session;Socket peer=upstream;
   transfers.execute(()->{try{copy(input,peer.getOutputStream(),active,request.bodyLength);peer.shutdownOutput();}catch(IOException e){active.close();}});
   copy(upstream.getInputStream(),client.getOutputStream(),session,-1);
  }catch(IOException|RejectedExecutionException ignored){}
  finally{if(session!=null)session.close();else{close(client);close(upstream);}slots.release();}
 }
 private void copy(InputStream input,OutputStream output,Session session,long remaining)throws IOException{
  byte[] bytes=new byte[16384];
  while(running&&remaining!=0&&!session.budget.expired(System.nanoTime())){
   int n;try{n=input.read(bytes,0,remaining<0?bytes.length:(int)Math.min(bytes.length,remaining));}catch(SocketTimeoutException e){continue;}
   if(n<0){if(remaining>0)throw new EOFException();return;}output.write(bytes,0,n);output.flush();session.budget.touch(System.nanoTime());if(remaining>0)remaining-=n;
  }
 }
 private void transport(Socket client,ConnectionState.Snapshot view)throws IOException{
  Endpoint e=view.target;String json="{\"transport\":\"USB\",\"entry_port\":"+EntryAddress.PORT+",\"listener_ready\":true,\"connected\":"+(e!=null)+",\"device_id\":\""+(e==null?"":e.deviceId)+"\",\"device_address\":\""+(e==null?"":e.address.getHostAddress())+"\"}";
  reply(client,200,"application/json; charset=utf-8",json);
 }
 private void fail(Socket socket,int status,String message)throws IOException{reply(socket,status,"text/html; charset=utf-8","<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5><p>"+message+"</p>");}
 private void reply(Socket socket,int status,String type,String text)throws IOException{
  byte[] body=text.getBytes(StandardCharsets.UTF_8);OutputStream out=socket.getOutputStream();out.write(("HTTP/1.1 "+status+" "+(status==200?"OK":"Unavailable")+"\r\nContent-Type: "+type+"\r\nContent-Length: "+body.length+"\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));out.write(body);out.flush();
 }
 private void close(Socket socket){if(socket!=null){sockets.remove(socket);try{socket.close();}catch(IOException ignored){}}}
 private void closeSockets(){synchronized(sockets){for(Socket socket:sockets)try{socket.close();}catch(IOException ignored){}sockets.clear();}sessions.clear();}
 public IBinder onBind(Intent intent){return null;}
 public void onDestroy(){
  synchronized(stateLock){running=false;state.close();closeSockets();}
  if(timers!=null)timers.shutdownNow();if(discovery!=null)discovery.stop();
  for(ServerSocket listener:new ServerSocket[]{server,server6})if(listener!=null)try{listener.close();}catch(IOException ignored){}
  scanner.shutdownNow();control.shutdownNow();headers.shutdownNow();transfers.shutdownNow();super.onDestroy();
 }
}
