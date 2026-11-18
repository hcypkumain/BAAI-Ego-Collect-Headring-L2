package org.headring.usbentry;
final class ConnectionVisibilityTest {
 static void test(){
  if(!ConnectionVisibility.lost(true,"cp_0023",""))throw new AssertionError("lost connection not shown");
  if(ConnectionVisibility.lost(false,"cp_0023","")||ConnectionVisibility.lost(true,"",""))throw new AssertionError("false disconnect");
  if(ConnectionVisibility.revealAfterLoad(true,true,"cp_0023",false)||ConnectionVisibility.revealAfterLoad(true,false,"",false)||ConnectionVisibility.revealAfterLoad(true,false,"cp_0023",true))throw new AssertionError("unsafe reveal");
  if(!ConnectionVisibility.revealAfterLoad(true,false,"cp_0023",false))throw new AssertionError("reconnect not restored");
 }
}
