package org.headring.usbentry;
import android.content.Context;
import android.net.Network;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.os.Build;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
final class NsdDiscovery implements DiscoveryProvider {
 private static final String TYPE="_headring._tcp.";
 private final NsdManager nsd; private NsdManager.DiscoveryListener discovery; private boolean resolving;
 NsdDiscovery(Context context){nsd=(NsdManager)context.getSystemService(Context.NSD_SERVICE);}
 public void start(Listener out){discovery=new NsdManager.DiscoveryListener(){
  public void onDiscoveryStarted(String type){Diagnostics.nsd="已开始 "+type;} public void onDiscoveryStopped(String type){}
  public void onStartDiscoveryFailed(String type,int code){Diagnostics.nsd="启动失败 code="+code;out.failed();}
  public void onStopDiscoveryFailed(String type,int code){}
  public void onServiceFound(NsdServiceInfo info){Diagnostics.nsd="发现 "+info.getServiceName();if(!info.getServiceName().matches("HeadRing CP[0-9]{1,4}"))return; synchronized(NsdDiscovery.this){if(resolving)return;resolving=true;} try{nsd.resolveService(info,new NsdManager.ResolveListener(){
   public void onResolveFailed(NsdServiceInfo s,int code){Diagnostics.nsd="解析失败 code="+code;synchronized(NsdDiscovery.this){resolving=false;}}
   public void onServiceResolved(NsdServiceInfo s){synchronized(NsdDiscovery.this){resolving=false;}byte[] id=s.getAttributes().get("device_id");InetAddress a=s.getHost();Diagnostics.nsd="解析 "+s.getServiceName()+" / "+String.valueOf(a)+" / device_id="+(id==null?"缺失":new String(id,StandardCharsets.UTF_8));String device=id==null?"":new String(id,StandardCharsets.UTF_8);if(!DeviceIdentity.valid(device)||!DeviceIdentity.serviceName(device).equals(s.getServiceName())||s.getPort()!=8765||!(a instanceof Inet4Address)||!a.isSiteLocalAddress())return;Network n=Build.VERSION.SDK_INT>=33?s.getNetwork():null;out.found(new Endpoint(device,a,8765,n,null));}
  });}catch(RuntimeException e){synchronized(NsdDiscovery.this){resolving=false;}}}
  public void onServiceLost(NsdServiceInfo s){if(s.getServiceName().matches("HeadRing CP[0-9]{1,4}"))out.lost();}
 };try{nsd.discoverServices(TYPE,NsdManager.PROTOCOL_DNS_SD,discovery);}catch(RuntimeException e){out.failed();}}
 public void stop(){try{nsd.stopServiceDiscovery(discovery);}catch(Exception ignored){}}
}
