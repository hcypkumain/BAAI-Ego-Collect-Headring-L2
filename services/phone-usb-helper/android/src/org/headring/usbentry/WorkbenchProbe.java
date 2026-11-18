package org.headring.usbentry;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
/** Read-only reachability probe; never sends or retries capture controls. */
final class WorkbenchProbe {
 static boolean reachable(InetAddress address,InetAddress source,int port){
  try(Socket s=new Socket()){
   if(source!=null)s.bind(new InetSocketAddress(source,0));
   s.connect(new InetSocketAddress(address,port),1000);s.setSoTimeout(1200);
   s.getOutputStream().write("GET /api/display-state HTTP/1.0\r\nHost: device\r\nConnection: close\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
   InputStream in=s.getInputStream();ByteArrayOutputStream line=new ByteArrayOutputStream();
   for(int i=0;i<128;i++){int b=in.read();if(b<0)return false;if(b=='\n')break;line.write(b);}
   String status=new String(line.toByteArray(),StandardCharsets.US_ASCII).trim();
   return status.matches("HTTP/1\\.[01] [1-5][0-9][0-9]( .*)?");
  }catch(IOException|IllegalArgumentException e){return false;}
 }
}
