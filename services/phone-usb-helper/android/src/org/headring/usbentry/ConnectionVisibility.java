package org.headring.usbentry;
/** Local-only visibility decision: disconnected screens never show stale controls. */
final class ConnectionVisibility {
 static boolean lost(boolean hasPage,String previous,String current){return hasPage&&previous!=null&&!previous.isEmpty()&&(current==null||current.isEmpty());}
 static boolean revealAfterLoad(boolean recovering,boolean failed,String current,boolean userRequestedSettings){return recovering&&!failed&&current!=null&&!current.isEmpty()&&!userRequestedSettings;}
}
