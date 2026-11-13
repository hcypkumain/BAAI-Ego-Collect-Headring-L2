package org.headring.usbentry;
import java.util.Locale;
final class OriginPolicy {
 static boolean valid(String h,String deviceId){String host=null,origin=null;String[] lines=h.split("\\r\\n");for(int i=1;i<lines.length;i++){int p=lines[i].indexOf(':');if(p<1)continue;String k=lines[i].substring(0,p).trim().toLowerCase(Locale.ROOT),v=lines[i].substring(p+1).trim().toLowerCase(Locale.ROOT);if(k.equals("host")){if(host!=null)return false;host=v;}if(k.equals("origin")){if(origin!=null)return false;origin=v;}}if(deviceId!=null&&!DeviceIdentity.valid(deviceId))return false;String expected=deviceId==null?"localhost:8765":("cp-"+deviceId.substring(3)+".localhost:8765");return expected.equals(host)&&(origin==null||origin.equals("http://"+host));}
}
