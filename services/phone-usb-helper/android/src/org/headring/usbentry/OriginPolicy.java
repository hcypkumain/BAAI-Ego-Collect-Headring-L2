package org.headring.usbentry;
import java.util.Locale;
final class OriginPolicy {
 static boolean valid(String header,String deviceId){
  String host=null,origin=null;String[] lines=header.split("\r\n");
  for(int i=1;i<lines.length;i++){
   int colon=lines[i].indexOf(':');if(colon<1)continue;
   String name=lines[i].substring(0,colon).trim().toLowerCase(Locale.ROOT),value=lines[i].substring(colon+1).trim().toLowerCase(Locale.ROOT);
   if(name.equals("host")){if(host!=null)return false;host=value;}
   if(name.equals("origin")){if(origin!=null)return false;origin=value;}
  }
  if(deviceId!=null&&!DeviceIdentity.valid(deviceId))return false;
  String expected=(deviceId==null?"localhost":"cp-"+deviceId.substring(3)+".localhost")+":"+EntryAddress.PORT;
  return expected.equals(host)&&(origin==null||origin.equals("http://"+host));
 }
}
