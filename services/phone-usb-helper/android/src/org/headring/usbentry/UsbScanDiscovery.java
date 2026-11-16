package org.headring.usbentry;
import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import java.net.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.function.*;
final class UsbScanDiscovery {
  private final Context context;
  private volatile UsbSource scanSource;
 UsbScanDiscovery(Context c){context=c;}
  private List<InterfaceAddress> candidates() throws Exception {return candidates(true);}
  private List<InterfaceAddress> candidates(boolean preferLearned) throws Exception {
   Set<String> managed=new HashSet<>();ConnectivityManager cm=(ConnectivityManager)context.getSystemService(Context.CONNECTIVITY_SERVICE);
   for(Network n:cm.getAllNetworks()){LinkProperties lp=cm.getLinkProperties(n);NetworkCapabilities cap=cm.getNetworkCapabilities(n);if(lp!=null&&lp.getInterfaceName()!=null&&cap!=null&&(cap.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)||cap.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)||cap.hasTransport(NetworkCapabilities.TRANSPORT_VPN)))managed.add(lp.getInterfaceName());}
   List<InterfaceAddress> candidates=new ArrayList<>();List<InterfaceAddress> selected=new ArrayList<>();StringBuilder diagnostic=new StringBuilder();Enumeration<NetworkInterface> interfaces=NetworkInterface.getNetworkInterfaces();
   if(interfaces==null){Diagnostics.interfaces="系统未提供接口列表";return candidates;}
   while(interfaces.hasMoreElements()){
    NetworkInterface ni=interfaces.nextElement();String name=ni.getName().toLowerCase(Locale.ROOT);String learned=context.getSharedPreferences("usb",Context.MODE_PRIVATE).getString("interface","");
    boolean allowed=ni.isUp()&&!ni.isLoopback()&&!managed.contains(ni.getName())&&(name.startsWith("usb")||name.startsWith("rndis")||name.startsWith("ncm")||ni.getName().equals(learned));diagnostic.append(ni.getName()).append(" up=").append(ni.isUp()).append(" managed=").append(managed.contains(ni.getName())).append(" nameOK=").append(name.startsWith("usb")||name.startsWith("rndis")||name.startsWith("ncm"));for(InterfaceAddress ia:ni.getInterfaceAddresses())if(ia.getAddress() instanceof Inet4Address)diagnostic.append(" ").append(ia.getAddress().getHostAddress()).append("/").append(ia.getNetworkPrefixLength());diagnostic.append("\n");if(!allowed)continue;
    for(InterfaceAddress ia:ni.getInterfaceAddresses())if(ia.getAddress() instanceof Inet4Address&&ia.getAddress().isSiteLocalAddress()&&ia.getNetworkPrefixLength()>=24&&ia.getNetworkPrefixLength()<=30){candidates.add(ia);if(ni.getName().equals(learned))selected.add(ia);}
   }
    Diagnostics.interfaces=diagnostic.toString()+"候选数="+candidates.size()+" 已选="+selected.size();return !preferLearned||selected.isEmpty()?candidates:selected;
  }
  private UsbSource uniqueSource()throws Exception{
   List<UsbSource> values=new ArrayList<>();
   for(InterfaceAddress address:candidates(false)){NetworkInterface ni=NetworkInterface.getByInetAddress(address.getAddress());if(ni==null)return null;values.add(new UsbSource(ni.getName(),ni.getIndex(),address.getAddress(),address.getNetworkPrefixLength()));}
   return UsbSource.unique(values);
  }
  boolean sourceCurrent(Endpoint endpoint){try{return scanSource!=null&&scanSource.address.equals(endpoint.source)&&scanSource.peer(endpoint.address)&&scanSource.same(uniqueSource());}catch(Exception e){return false;}}
  boolean sourceCurrent(){try{return scanSource!=null&&scanSource.same(uniqueSource());}catch(Exception e){return false;}}
  boolean hadSource(){return scanSource!=null;}
 InetAddress sourceFor(InetAddress address){try{List<InterfaceAddress> list=candidates();if(list.size()!=1||!(address instanceof Inet4Address))return null;InterfaceAddress ia=list.get(0);byte[] a=ia.getAddress().getAddress(), b=address.getAddress();int x=((a[0]&255)<<24)|((a[1]&255)<<16)|((a[2]&255)<<8)|(a[3]&255);int y=((b[0]&255)<<24)|((b[1]&255)<<16)|((b[2]&255)<<8)|(b[3]&255);int mask=-1<<(32-ia.getNetworkPrefixLength());return (x&mask)==(y&mask)?ia.getAddress():null;}catch(Exception e){Diagnostics.interfaces="接口读取失败 "+e.getClass().getSimpleName();return null;}}
 boolean learn(InetAddress source){try{NetworkInterface ni=NetworkInterface.getByInetAddress(source);if(ni==null||ni.isLoopback()||!ni.isUp()){Diagnostics.manual="接口不可见，不能自动记住";return false;}String lname=ni.getName().toLowerCase(Locale.ROOT);if(lname.startsWith("wlan")||lname.startsWith("wifi")||lname.startsWith("ap")||lname.startsWith("rmnet")||lname.startsWith("tun")){Diagnostics.manual="非USB接口，不保存";return false;}ConnectivityManager cm=(ConnectivityManager)context.getSystemService(Context.CONNECTIVITY_SERVICE);for(Network n:cm.getAllNetworks()){LinkProperties lp=cm.getLinkProperties(n);NetworkCapabilities c=cm.getNetworkCapabilities(n);if(lp!=null&&ni.getName().equals(lp.getInterfaceName())&&c!=null&&(c.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)||c.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)||c.hasTransport(NetworkCapabilities.TRANSPORT_VPN))){Diagnostics.manual="来源是Wi-Fi/蜂窝/VPN，不保存接口";return false;}}boolean valid=false;for(InterfaceAddress a:ni.getInterfaceAddresses())if(source.equals(a.getAddress())&&a.getNetworkPrefixLength()>=24&&a.getNetworkPrefixLength()<=30)valid=true;if(!valid){Diagnostics.manual="源接口前缀不支持自动发现";return false;}context.getSharedPreferences("usb",Context.MODE_PRIVATE).edit().putString("interface",ni.getName()).apply();Diagnostics.manual="已记住USB接口 "+ni.getName()+"，未保存设备IP";return true;}catch(Exception e){Diagnostics.manual="学习接口失败 "+e.getClass().getSimpleName();return false;}}
  ScanResult scan(Endpoint hint,BooleanSupplier cancelled,Consumer<Endpoint> progress){
   try{
    scanSource=uniqueSource();if(scanSource==null)return new ScanResult(Collections.emptyList(),false,"USB 网络接口缺失或不唯一，请检查共享设置");
    UsbSource selected=scanSource;byte[] a=selected.address.getAddress();int ip=((a[0]&255)<<24)|((a[1]&255)<<16)|((a[2]&255)<<8)|(a[3]&255);int prefix=selected.prefix, mask=-1<<(32-prefix), start=ip&mask, end=start|~mask;
    List<InetAddress> addresses=new ArrayList<>();
    // DNS-SD only prioritizes a peer on the single qualified USB subnet. It
    // never supplies the source/network or bypasses the identity probe.
    if(hint!=null&&selected.peer(hint.address))addresses.add(hint.address);
    for(int x=start+1;x<end;x++){if(x==ip)continue;byte[] bytes={(byte)(x>>>24),(byte)(x>>>16),(byte)(x>>>8),(byte)x};InetAddress addr=InetAddress.getByAddress(bytes);if(!addresses.contains(addr))addresses.add(addr);}
    List<Callable<Endpoint>> probes=new ArrayList<>();
    for(InetAddress addr:addresses)probes.add(()->{if(cancelled.getAsBoolean())return null;String id=IdentityProbe.identify(addr,selected.address,null);if(id==null||cancelled.getAsBoolean())return null;Endpoint e=new Endpoint(id,addr,8765,null,selected.address);return sourceCurrent(e)?e:null;});
    return ScanCollector.collect(probes,15000,cancelled,progress);
   }catch(Exception e){Diagnostics.interfaces="扫描失败 "+e.getClass().getSimpleName();return new ScanResult(Collections.emptyList(),false,"接口读取失败，请重试");}
  }
}
