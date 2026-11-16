package org.headring.usbentry;
public final class PolicyTest {
 static void check(boolean x){if(!x)throw new AssertionError();}
 public static void main(String[] args){
  check(DeviceIdentity.valid("cp_0018"));check(DeviceIdentity.valid("cp_0021"));check(!DeviceIdentity.valid("cp_18"));check(!DeviceIdentity.valid("cp_00x8"));
   check(DeviceSelection.autoConnect(new String[]{"cp_0018"},""));check(DeviceSelection.autoConnect(new String[]{"cp_0018"},"cp_0018"));check(!DeviceSelection.autoConnect(new String[]{"cp_0018","cp_0021"},""));check(DeviceSelection.autoConnect(new String[]{"cp_0021"},"cp_0018"));
   String a="GET /phone HTTP/1.1\r\nHost: cp-0018.localhost:"+EntryAddress.PORT+"\r\nOrigin: http://cp-0018.localhost:"+EntryAddress.PORT+"\r\n\r\n";
   check(OriginPolicy.valid(a,"cp_0018"));check(!OriginPolicy.valid(a,"cp_0021"));check(!OriginPolicy.valid(a,"bad"));check(OriginPolicy.valid("GET / HTTP/1.1\r\nHost: localhost:"+EntryAddress.PORT+"\r\n\r\n",null));
  System.out.println("identity, candidate and origin policy passed");
 }
}
