package org.headring.usbentry;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.net.Inet4Address;

/** One immutable UI snapshot; discovery is not permission to connect. */
final class ConnectionState {
 static final long CANDIDATE_TTL=TimeUnit.SECONDS.toNanos(60);
 static final class Snapshot {
  final List<Endpoint> candidates;
  final Endpoint target;
  final boolean scanning,stale,ipv4,ipv6,choosing;
  final String message;
  final long connectionRevision;
  Snapshot(List<Endpoint> c,Endpoint t,boolean scan,boolean old,boolean v4,boolean v6,String m,long rev,boolean pending){
   candidates=c;target=t;scanning=scan;stale=old;ipv4=v4;ipv6=v6;message=m;connectionRevision=rev;choosing=pending;
  }
  boolean ready(){return ipv4&&ipv6;}
  String current(){return ready()&&target!=null?target.deviceId:"";}
 }
 private long scanId,choiceId,connectionRevision,candidateAt;
 private boolean scanning,fresh,ipv4,ipv6,closed,explicitChoicePending;
 private List<Endpoint> candidates=Collections.emptyList();
 private Endpoint target;
 private String message="等待手机 USB 网络共享";
 private String choiceKey;

 synchronized long beginScan(){if(closed||explicitChoicePending)return -1;scanning=true;fresh=false;message="正在搜索并验证 USB 头环…";return ++scanId;}
 synchronized boolean scanCurrent(long id){return !closed&&scanning&&scanId==id;}
 synchronized void found(long id,Endpoint endpoint,long now){
  if(!scanCurrent(id))return;
  List<Endpoint> next=new ArrayList<>(fresh?candidates:Collections.emptyList());
  next.removeIf(e->key(e).equals(key(endpoint)));next.add(endpoint);
  candidates=Collections.unmodifiableList(next);candidateAt=now;fresh=true;
  message="已验证 "+next.size()+" 台 USB 头环，可直接选择；搜索仍在继续";
 }
 synchronized boolean finishScan(long id,ScanResult result,long now){
  if(!scanCurrent(id))return false;
  scanning=false;
  if(!result.matches.isEmpty()){
   candidates=Collections.unmodifiableList(new ArrayList<>(result.matches));candidateAt=now;fresh=true;
   message=result.complete?"已验证 "+candidates.size()+" 台头环，请选择":"搜索未全部完成，已验证 "+candidates.size()+" 台；可选择或重新搜索";
  }else{
   fresh=false;
   message=candidates.isEmpty()?result.message:result.message+"；上次结果待重新验证";
  }
  return true;
 }
 synchronized long beginChoice(){return beginChoice(null);}
 synchronized long beginChoice(String key){if(closed||explicitChoicePending&&key!=null&&key.equals(choiceKey))return -1;choiceKey=key;scanId++;scanning=false;explicitChoicePending=true;message="正在验证所选头环…";return ++choiceId;}
 synchronized boolean choiceCurrent(long id){return !closed&&explicitChoicePending&&id==choiceId;}
 synchronized boolean choose(long id,Endpoint endpoint){
  if(!choiceCurrent(id))return false;
  target=endpoint;explicitChoicePending=false;connectionRevision++;message="USB 头环已验证";return true;
 }
 synchronized boolean autoChooseVerified(long scan,long choice,Endpoint endpoint,long now){
  if(closed||!scanning||scan!=scanId||choice!=choiceId||explicitChoicePending||target!=null||endpoint==null)return false;
  if(!DeviceIdentity.valid(endpoint.deviceId)||endpoint.port!=8765||endpoint.network!=null||!(endpoint.address instanceof Inet4Address)||!(endpoint.source instanceof Inet4Address)||!endpoint.address.isSiteLocalAddress()||endpoint.address.equals(endpoint.source))return false;
  target=endpoint;candidates=Collections.singletonList(endpoint);candidateAt=now;fresh=true;
  scanning=false;scanId++;connectionRevision++;message="USB 头环已验证";return true;
 }
 synchronized long choiceRevision(){return choiceId;}
 synchronized void choiceError(long id,String error){if(choiceCurrent(id)){explicitChoicePending=false;message=error;}}
 synchronized void finishChoice(long id){if(choiceCurrent(id)){explicitChoicePending=false;message="连接验证未完成，请重试";}}
 synchronized boolean invalidate(Endpoint endpoint){
  if(target!=endpoint)return false;
  target=null;choiceId++;scanId++;scanning=false;fresh=false;explicitChoicePending=false;connectionRevision++;message="连接已失效，正在重新搜索";return true;
 }
 synchronized void listener(boolean v6,boolean ready){
  if(closed)return;
  boolean old=v6?ipv6:ipv4;
  if(v6)ipv6=ready;else ipv4=ready;
  if(old!=ready)connectionRevision++;
 }
 synchronized Snapshot snapshot(long now){
  boolean expired=candidates.isEmpty()||now-candidateAt>CANDIDATE_TTL;
  String text=message;
  if(!ipv4||!ipv6)text="本机入口尚未就绪或端口被占用，正在重试；不会显示为已连接";
  else if(target!=null)text="已连接 "+target.deviceId+" · USB"+(message.equals("USB 头环已验证")?"":"；"+message);
  return new Snapshot(expired?Collections.emptyList():candidates,target,scanning,!fresh||expired,ipv4,ipv6,text,connectionRevision,explicitChoicePending);
 }
 synchronized void close(){closed=true;scanId++;choiceId++;target=null;candidates=Collections.emptyList();scanning=explicitChoicePending=false;ipv4=ipv6=false;connectionRevision++;message="连接服务已停止";}
 static String key(Endpoint e){return e.deviceId+"@"+e.address.getHostAddress();}
}
