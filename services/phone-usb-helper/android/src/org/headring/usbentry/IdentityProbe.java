package org.headring.usbentry;
import android.net.Network;
import java.net.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;
final class IdentityProbe {
 static volatile InetAddress lastSource;
 static String identify(InetAddress address,InetAddress source,Network network){
  try(Socket s=network==null?new Socket():network.getSocketFactory().createSocket()){
   if(source!=null)s.bind(new InetSocketAddress(source,0));s.connect(new InetSocketAddress(address,8766),350);lastSource=s.getLocalAddress();Diagnostics.route=lastSource.getHostAddress();s.setSoTimeout(800);
   s.getOutputStream().write("GET /.well-known/headring HTTP/1.1\r\nHost: device\r\nConnection: close\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
   ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] b=new byte[512];int n;long deadline=System.nanoTime()+1200000000L;while(out.size()<=2048){long remaining=deadline-System.nanoTime();if(remaining<=0)return null;s.setSoTimeout((int)Math.max(1,remaining/1000000L));n=s.getInputStream().read(b);if(n==-1)break;out.write(b,0,n);}
   if(out.size()>2048)return null;String text=new String(out.toByteArray(),StandardCharsets.UTF_8);int p=text.indexOf("\r\n\r\n");if(!text.startsWith("HTTP/1.1 200 ")||p<0)return null;JSONObject v=new JSONObject(text.substring(p+4));String id=v.optString("device_id","");boolean valid=v.optInt("schema")==1&&DeviceIdentity.valid(id)&&v.optInt("http_port")==8765;Diagnostics.probe=valid?"设备身份探针通过 "+id:"身份格式或端口不符";return valid?id:null;
  }catch(Exception e){Diagnostics.probe=address.getHostAddress()+" "+e.getClass().getSimpleName();return null;}
 }
 static boolean verify(InetAddress address,InetAddress source,Network network,String expected){String id=identify(address,source,network);return id!=null&&id.equals(expected);}
}
