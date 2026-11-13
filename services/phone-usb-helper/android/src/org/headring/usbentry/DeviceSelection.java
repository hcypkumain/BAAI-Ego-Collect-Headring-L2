package org.headring.usbentry;
final class DeviceSelection {
 static boolean autoConnect(String[] ids,String last){return ids!=null&&ids.length==1&&DeviceIdentity.valid(ids[0])&&(last==null||last.isEmpty()||last.equals(ids[0]));}
}
