package org.headring.usbentry;
import java.net.*;import java.io.*;
final class WorkbenchProbeTest {
 static void test()throws Exception{
  InetAddress a=InetAddress.getByName("127.0.0.1");
  try(ServerSocket server=new ServerSocket(0,1,a)){
   Thread t=new Thread(()->{try(Socket s=server.accept()){s.setSoTimeout(2000);BufferedReader r=new BufferedReader(new InputStreamReader(s.getInputStream()));String request=r.readLine();if(!request.startsWith("GET /api/display-state "))throw new AssertionError("must use read-only endpoint");s.getOutputStream().write("HTTP/1.1 503 Busy\r\n\r\n".getBytes("US-ASCII"));}catch(Exception e){throw new RuntimeException(e);}});t.start();
   if(!WorkbenchProbe.reachable(a,null,server.getLocalPort()))throw new AssertionError("HTTP response is reachable");t.join(3000);
  }
  try(ServerSocket s=new ServerSocket(0,1,a)){int port=s.getLocalPort();s.close();if(WorkbenchProbe.reachable(a,null,port))throw new AssertionError("closed port reported reachable");}
 }
}
