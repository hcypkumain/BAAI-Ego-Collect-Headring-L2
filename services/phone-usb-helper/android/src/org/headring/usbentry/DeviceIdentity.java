package org.headring.usbentry;
final class DeviceIdentity {
 static boolean valid(String id){return id!=null&&id.matches("cp_[0-9]{4}");}
 static String serviceName(String id){if(!valid(id))throw new IllegalArgumentException();return "HeadRing CP"+Integer.parseInt(id.substring(3));}
}
