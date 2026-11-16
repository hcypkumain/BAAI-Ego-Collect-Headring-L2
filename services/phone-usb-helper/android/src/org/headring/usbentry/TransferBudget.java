package org.headring.usbentry;
import java.util.concurrent.TimeUnit;
final class TransferBudget {
 private final long started,total,idle;
 private volatile long touched;
 TransferBudget(long now,boolean control,boolean stream){started=touched=now;total=TimeUnit.SECONDS.toNanos(control?15:stream?75:180);idle=TimeUnit.SECONDS.toNanos(control?8:stream?12:30);}
 void touch(long now){touched=now;}
 boolean expired(long now){return now-started>=total||now-touched>=idle;}
}
