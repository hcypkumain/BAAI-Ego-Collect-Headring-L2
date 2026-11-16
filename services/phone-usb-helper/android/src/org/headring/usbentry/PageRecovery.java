package org.headring.usbentry;
/** Only failed main-frame GETs may be retried; never replay a control POST. */
final class PageRecovery {
 private int failures;
 private boolean failed;
 void started(){failed=false;}
 void success(){if(!failed)failures=0;}
 long failure(){failed=true;if(failures>=5)return -1;return new long[]{750,1500,3000,6000,10000}[failures++];}
 boolean failed(){return failed;}
 void manual(){failures=0;failed=false;}
}
