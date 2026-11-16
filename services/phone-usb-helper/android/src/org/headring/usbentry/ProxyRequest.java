package org.headring.usbentry;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/** A single bounded HTTP request. Never tunnel a second unvalidated request. */
final class ProxyRequest {
 final String path;
 final long bodyLength;
 final boolean control,stream;
 final byte[] header;
 private ProxyRequest(String p,long size,byte[] h){path=p;bodyLength=size;header=h;control=p.equals("/api/action")||p.equals("/api/display-state")||p.equals("/api/status");stream=p.startsWith("/api/phone/stream/")||p.endsWith("-stream")||p.endsWith("/stream");}
 static ProxyRequest parse(String raw,String device)throws IOException{
  String withoutLines=raw.replace("\r\n","");
  if(raw.length()>16384||!raw.endsWith("\r\n\r\n")||raw.indexOf('\0')>=0||withoutLines.indexOf('\r')>=0||withoutLines.indexOf('\n')>=0||!OriginPolicy.valid(raw,device))throw new IOException("invalid origin");
  String[] lines=raw.split("\r\n");
  String[] first=lines[0].split(" ",-1);
  if(first.length!=3||!first[0].matches("GET|HEAD|POST")||!first[1].startsWith("/")||first[1].startsWith("//")||first[1].length()>4096||!first[2].matches("HTTP/1\\.[01]"))throw new IOException("invalid request");
  long length=0;boolean seenLength=false;StringBuilder forward=new StringBuilder(lines[0]).append("\r\n");
  for(int i=1;i<lines.length;i++){
   int colon=lines[i].indexOf(':');if(colon<1||!lines[i].substring(0,colon).matches("[A-Za-z0-9!#$%&'*+.^_`|~-]+"))throw new IOException("invalid header");
   String name=lines[i].substring(0,colon),value=lines[i].substring(colon+1).trim();
   if(name.equalsIgnoreCase("Transfer-Encoding")||name.equalsIgnoreCase("Upgrade"))throw new IOException("unsupported transfer");
   if(name.equalsIgnoreCase("Content-Length")){
    if(seenLength||!value.matches("[0-9]{1,10}"))throw new IOException("invalid length");
    seenLength=true;length=Long.parseLong(value);if(length>4L*1024*1024*1024)throw new IOException("body too large");
   }
   if(!name.equalsIgnoreCase("Connection")&&!name.equalsIgnoreCase("Proxy-Connection")&&!name.equalsIgnoreCase("Keep-Alive"))forward.append(lines[i]).append("\r\n");
  }
  if(!first[0].equals("POST")&&length!=0)throw new IOException("unexpected body");
  forward.append("Connection: close\r\n\r\n");
  return new ProxyRequest(first[1].split("\\?",2)[0],length,forward.toString().getBytes(StandardCharsets.ISO_8859_1));
 }
}
