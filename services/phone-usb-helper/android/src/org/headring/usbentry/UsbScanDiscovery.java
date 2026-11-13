package org.headring.usbentry;
import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import java.net.*;
import java.util.*;
import java.util.concurrent.*;
final class UsbScanDiscovery {
 private final Context context;
 UsbScanDiscovery(Context c){context=c;}
 private List<InterfaceAddress> candidates() throws Exception {
   Set<String> managed=new HashSet<>();ConnectivityManager cm=(ConnectivityManager)context.getSystemService(Context.CONNECTIVITY_SERVICE);
   for(Network n:cm.getAllNetworks()){LinkProperties lp=cm.getLinkProperties(n);NetworkCapabilities cap=cm.getNetworkCapabilities(n);if(lp!=null&&lp.getInterfaceName()!=null&&cap!=null&&(cap.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)||cap.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)||cap.hasTransport(NetworkCapabilities.TRANSPORT_VPN)))managed.add(lp.getInterfaceName());}
   List<InterfaceAddress> candidates=new ArrayList<>();List<InterfaceAddress> selected=new ArrayList<>();StringBuilder diagnostic=new StringBuilder();Enumeration<NetworkInterface> interfaces=NetworkInterface.getNetworkInterfaces();
   if(interfaces==null){Diagnostics.interfaces="系统未提供接口列表";return candidates;}
   while(interfaces.hasMoreElements()){
    NetworkInterface ni=interfaces.nextElement();String name=ni.getName().toLowerCase(Locale.ROOT);String learned=context.getSharedPreferences("usb",Context.MODE_PRIVATE).getString("interface","");
    boolean allowed=ni.isUp()&&!ni.isLoopback()&&!managed.contains(ni.getName())&&(name.startsWith("usb")||name.startsWith("rndis")||name.startsWith("ncm")||ni.getName().equals(learned));diagnostic.append(ni.getName()).append(" up=").append(ni.isUp()).append(" managed=").append(managed.contains(ni.getName())).append(" nameOK=").append(name.startsWith("usb")||name.startsWith("rndis")||name.startsWith("ncm"));for(InterfaceAddress ia:ni.getInterfaceAddresses())if(ia.getAddress() instanceof Inet4Address)diagnostic.append(" ").append(ia.getAddress().getHostAddress()).append("/").append(ia.getNetworkPrefixLength());diagnostic.append("\n");if(!allowed)continue;
    for(InterfaceAddress ia:ni.getInterfaceAddresses())if(ia.getAddress() instanceof Inet4Address&&ia.getAddress().isSiteLocalAddress()&&ia.getNetworkPrefixLength()>=24&&ia.getNetworkPrefixLength()<=30){candidates.add(ia);if(ni.getName().equals(learned))selected.add(ia);}
   }
   Diagnostics.interfaces=diagnostic.toString()+"候选数="+candidates.size()+" 已选="+selected.size();return selected.isEmpty()?candidates:selected;
 }
 InetAddress sourceFor(InetAddress address){try{List<InterfaceAddress> list=candidates();if(list.size()!=1||!(address instanceof Inet4Address))return null;InterfaceAddress ia=list.get(0);byte[] a=ia.getAddress().getAddress(), b=address.getAddress();int x=((a[0]&255)<<24)|((a[1]&255)<<16)|((a[2]&255)<<8)|(a[3]&255);int y=((b[0]&255)<<24)|((b[1]&255)<<16)|((b[2]&255)<<8)|(b[3]&255);int mask=-1<<(32-ia.getNetworkPrefixLength());return (x&mask)==(y&mask)?ia.getAddress():null;}catch(Exception e){Diagnostics.interfaces="接口读取失败 "+e.getClass().getSimpleName();return null;}}
 boolean learn(InetAddress source){try{NetworkInterface ni=NetworkInterface.getByInetAddress(source);if(ni==null||ni.isLoopback()||!ni.isUp()){Diagnostics.manual="接口不可见，不能自动记住";return false;}ConnectivityManager cm=(ConnectivityManager)context.getSystemService(Context.CONNECTIVITY_SERVICE);for(Network n:cm.getAllNetworks()){LinkProperties lp=cm.getLinkProperties(n);NetworkCapabilities c=cm.getNetworkCapabilities(n);if(lp!=null&&ni.getName().equals(lp.getInterfaceName())&&c!=null&&(c.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)||c.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)||c.hasTransport(NetworkCapabilities.TRANSPORT_VPN))){Diagnostics.manual="来源是Wi-Fi/蜂窝/VPN，不保存接口";return false;}}boolean valid=false;for(InterfaceAddress a:ni.getInterfaceAddresses())if(source.equals(a.getAddress())&&a.getNetworkPrefixLength()>=24&&a.getNetworkPrefixLength()<=30)valid=true;if(!valid){Diagnostics.manual="源接口前缀不支持自动发现";return false;}context.getSharedPreferences("usb",Context.MODE_PRIVATE).edit().putString("interface",ni.getName()).apply();Diagnostics.manual="已记住USB接口 "+ni.getName()+"，未保存设备IP";return true;}catch(Exception e){Diagnostics.manual="学习接口失败 "+e.getClass().getSimpleName();return false;}}
 Endpoint scan(){
  try{
   List<InterfaceAddress> candidates=candidates();if(candidates.size()!=1)return null;
   InterfaceAddress ia=candidates.get(0);byte[] a=ia.getAddress().getAddress();int ip=((a[0]&255)<<24)|((a[1]&255)<<16)|((a[2]&255)<<8)|(a[3]&255);int prefix=ia.getNetworkPrefixLength(), mask=-1<<(32-prefix), start=ip&mask, end=start|~mask;
   ExecutorService workers=Executors.newFixedThreadPool(8);List<Future<InetAddress>> futures=new ArrayList<>();
   try{for(int x=start+1;x<end;x++){final int v=x;if(v==ip)continue;futures.add(workers.submit(()->{byte[] bytes={(byte)(v>>>24),(byte)(v>>>16),(byte)(v>>>8),(byte)v};InetAddress addr=InetAddress.getByAddress(bytes);return IdentityProbe.verify(addr,ia.getAddress(),null)?addr:null;}));}
    long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(15);InetAddress match=null;for(Future<InetAddress> f:futures){long remaining=deadline-System.nanoTime();if(remaining<=0)return null;InetAddress a2=f.get(remaining,TimeUnit.NANOSECONDS);if(a2!=null){if(match!=null)return null;match=a2;}}return match==null?null:new Endpoint(ConfiguredDevice.ID,match,8765,null,ia.getAddress());
   }finally{workers.shutdownNow();}
  }catch(Exception e){Diagnostics.interfaces="扫描失败 "+e.getClass().getSimpleName();return null;}
 }
}
