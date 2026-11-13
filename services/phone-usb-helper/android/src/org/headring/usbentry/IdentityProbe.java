package org.headring.usbentry;
import android.net.Network;
import java.net.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;
final class IdentityProbe {
 static volatile InetAddress lastSource;
 static boolean verify(InetAddress address,InetAddress source,Network network){
  try(Socket s=network==null?new Socket():network.getSocketFactory().createSocket()){
   if(source!=null)s.bind(new InetSocketAddress(source,0));s.connect(new InetSocketAddress(address,8766),350);lastSource=s.getLocalAddress();Diagnostics.route=lastSource.getHostAddress();s.setSoTimeout(800);
   s.getOutputStream().write("GET /.well-known/headring HTTP/1.1\r\nHost: device\r\nConnection: close\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
   ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] b=new byte[512];int n;long deadline=System.nanoTime()+1200000000L;while(out.size()<=2048){long remaining=deadline-System.nanoTime();if(remaining<=0)return false;s.setSoTimeout((int)Math.max(1,remaining/1000000L));n=s.getInputStream().read(b);if(n==-1)break;out.write(b,0,n);}
   if(out.size()>2048)return false;String text=new String(out.toByteArray(),StandardCharsets.UTF_8);int p=text.indexOf("\r\n\r\n");if(!text.startsWith("HTTP/1.1 200 ")||p<0)return false;JSONObject v=new JSONObject(text.substring(p+4));boolean valid=v.optInt("schema")==1&&ConfiguredDevice.ID.equals(v.optString("device_id"))&&v.optInt("http_port")==8765;Diagnostics.probe=valid?"设备身份探针通过":"身份不匹配或端口不符";return valid;
  }catch(Exception e){Diagnostics.probe=address.getHostAddress()+" "+e.getClass().getSimpleName();return false;}
 }
}
