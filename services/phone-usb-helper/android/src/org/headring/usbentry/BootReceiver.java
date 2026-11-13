package org.headring.usbentry;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
public final class BootReceiver extends BroadcastReceiver {
 public void onReceive(Context c,Intent i){ if(Intent.ACTION_BOOT_COMPLETED.equals(i.getAction())){ try{c.startForegroundService(new Intent(c,EntryService.class));}catch(RuntimeException ignored){} } }
}
