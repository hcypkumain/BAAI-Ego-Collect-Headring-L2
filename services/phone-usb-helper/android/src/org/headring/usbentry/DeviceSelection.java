package org.headring.usbentry;
final class DeviceSelection {
  // The remembered ID is a convenience, not an authorization requirement.
  // Actual automatic installation also requires current, unique USB proof.
  static boolean autoConnect(String[] ids,String last){return ids!=null&&ids.length==1&&DeviceIdentity.valid(ids[0]);}
}
