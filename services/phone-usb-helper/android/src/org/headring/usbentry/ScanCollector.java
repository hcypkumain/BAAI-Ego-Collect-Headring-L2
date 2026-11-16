package org.headring.usbentry;
import java.util.*;
import java.util.concurrent.*;
import java.util.function.*;

/** Completion order, partial results and cancellation are independent of Android. */
final class ScanCollector {
 static ScanResult collect(List<Callable<Endpoint>> probes,long budgetMs,BooleanSupplier cancelled,Consumer<Endpoint> progress){
  ExecutorService workers=Executors.newFixedThreadPool(8);
  CompletionService<Endpoint> completed=new ExecutorCompletionService<>(workers);
  List<Future<Endpoint>> futures=new ArrayList<>();List<Endpoint> matches=new ArrayList<>();
  int remaining=probes.size();long deadline=System.nanoTime()+TimeUnit.MILLISECONDS.toNanos(budgetMs);
  try{
   for(Callable<Endpoint> probe:probes){if(cancelled.getAsBoolean())break;futures.add(completed.submit(probe));}
   remaining=futures.size();
   while(remaining>0&&!cancelled.getAsBoolean()){
    long time=deadline-System.nanoTime();if(time<=0)break;
    Future<Endpoint> next=completed.poll(Math.min(time,TimeUnit.MILLISECONDS.toNanos(100)),TimeUnit.NANOSECONDS);
    if(next==null)continue;remaining--;
    try{Endpoint e=next.get();if(e!=null&&!cancelled.getAsBoolean()){matches.add(e);progress.accept(e);if(cancelled.getAsBoolean())break;}}
    catch(ExecutionException ignored){} // A failed peer never erases verified peers.
   }
   boolean all=remaining==0&&!cancelled.getAsBoolean();
   return new ScanResult(matches,all,all?"未发现头环，请确认 USB 网络共享已开启":"搜索中断或超时，请重试");
  }catch(InterruptedException e){Thread.currentThread().interrupt();return new ScanResult(matches,false,"搜索已取消");}
  finally{for(Future<Endpoint> f:futures)f.cancel(true);workers.shutdownNow();}
 }
}
