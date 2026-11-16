package org.headring.usbentry;
import java.net.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

/** Production state + production Collector: no fake duplicate connect algorithm. */
public final class FastConnectTest {
 static int checks;
 static void check(boolean value,String label){checks++;if(!value)throw new AssertionError(label);}
 static Endpoint peer(String id)throws Exception{return new Endpoint(id,InetAddress.getByName("192.168.9.17"),8765,null,InetAddress.getByName("192.168.9.1"));}
 static ConnectionState state(){ConnectionState s=new ConnectionState();s.listener(false,true);s.listener(true,true);return s;}
 public static void main(String[] args)throws Exception{
  Endpoint a=peer("cp_0017"),b=peer("cp_0023");ConnectionState s=state();
  long scan=s.beginScan(),choice=s.choiceRevision(),before=s.snapshot(1).connectionRevision;
  check(s.autoChooseVerified(scan,choice,b,2),"verified new ID connects immediately");
  check(s.snapshot(3).target==b&&!s.snapshot(3).scanning,"success ends scan atomically");
  check(s.snapshot(3).connectionRevision==before+1,"one revision for successful install");
  check(!s.finishScan(scan,new ScanResult(Collections.emptyList(),false,"超时"),4),"cancelled scan cannot replace success with timeout");
  check(!s.autoChooseVerified(scan,choice,a,5)&&s.snapshot(5).target==b,"second peer cannot replace target");
  scan=s.beginScan();check(!s.autoChooseVerified(scan,s.choiceRevision(),a,6),"manual discovery cannot replace existing target");
  s.listener(false,false);check(!s.autoChooseVerified(scan,s.choiceRevision(),a,7),"listener failure does not authorize target replacement");
  s=state();scan=s.beginScan();choice=s.choiceRevision();long explicit=s.beginChoice();
  check(!s.autoChooseVerified(scan,choice,a,1),"explicit choice invalidates old auto result");
  check(s.beginScan()==-1&&s.snapshot(2).choosing,"timer cannot compete while explicit verification pending");
  long newer=s.beginChoice();s.choiceError(explicit,"old failure");s.finishChoice(explicit);
  check(s.snapshot(2).choosing,"old failure/finally cannot release new choice");
  check(s.choose(newer,b)&&!s.snapshot(2).choosing,"latest explicit choice completes normally");
  s=state();explicit=s.beginChoice();s.choiceError(explicit,"failed");
  check(!s.snapshot(1).choosing&&s.beginScan()>=0,"failure permits automatic retry");
  scan=s.beginScan();choice=s.choiceRevision();long current=s.beginScan();
  check(!s.autoChooseVerified(scan,choice,a,1)&&s.scanCurrent(current),"old scan cannot steal newer scan");
  s.close();check(!s.autoChooseVerified(current,choice,a,1),"closed service cannot revive");
  s=state();scan=s.beginScan();
  check(!s.autoChooseVerified(scan,s.choiceRevision(),new Endpoint("bad",a.address,8765,null,a.source),1),"unverified ID format rejected");
  check(!s.autoChooseVerified(scan,s.choiceRevision(),new Endpoint("cp_0017",a.address,80,null,a.source),1),"wrong target port rejected");
  UsbSource usb=new UsbSource("usb0",7,a.source,24);
  check(usb.same(new UsbSource("usb0",7,a.source,24)),"stable source fingerprint");
  check(!usb.same(new UsbSource("usb0",8,a.source,24)),"recreated interface invalidates source");
  check(usb.peer(a.address)&&!usb.peer(a.source),"only a different host on source subnet");
  check(!usb.peer(InetAddress.getByName("192.168.10.17")),"other subnet excluded");
  check(!usb.peer(InetAddress.getByName("192.168.9.0"))&&!usb.peer(InetAddress.getByName("192.168.9.255")),"network and broadcast excluded");
  check(UsbSource.unique(Arrays.asList(usb,usb))==null&&UsbSource.unique(Collections.emptyList())==null,"ambiguous or missing source rejected");
  check(UsbSource.unique(Collections.singletonList(usb))==usb,"one qualified source accepted");
  ConnectionState deduplicated=state();long same=deduplicated.beginChoice("select:cp_0017@192.168.9.17");
  check(deduplicated.beginChoice("select:cp_0017@192.168.9.17")==-1&&deduplicated.choiceCurrent(same),"repeated taps share the in-flight verification");
  long different=deduplicated.beginChoice("select:cp_0023@192.168.9.23");
  check(different>same&&!deduplicated.choiceCurrent(same),"different explicit choice still supersedes old request");
  deduplicated.choiceError(different,"failed");
  check(deduplicated.beginChoice("select:cp_0023@192.168.9.23")>different,"genuine failed verification may retry immediately");
  final ConnectionState fast=state();final long ticket=fast.beginScan(),intent=fast.choiceRevision();
  CountDownLatch slowStarted=new CountDownLatch(1),releaseSlow=new CountDownLatch(1),installed=new CountDownLatch(1);AtomicBoolean slowReturned=new AtomicBoolean();
  ExecutorService owner=Executors.newSingleThreadExecutor();long start=System.nanoTime();
  try{
   Future<ScanResult> result=owner.submit(()->ScanCollector.collect(Arrays.asList(
    ()->{slowStarted.countDown();boolean done=false;while(!done){try{releaseSlow.await();done=true;}catch(InterruptedException ignored){}}slowReturned.set(true);return null;},
    ()->{if(!slowStarted.await(2,TimeUnit.SECONDS))throw new AssertionError("slow probe did not start");return a;}
   ),15000,()->!fast.scanCurrent(ticket),endpoint->{fast.found(ticket,endpoint,System.nanoTime());if(fast.autoChooseVerified(ticket,intent,endpoint,System.nanoTime()))installed.countDown();}));
   check(installed.await(2,TimeUnit.SECONDS),"fast peer installed before slow peer is released");
   ScanResult partial=result.get(2,TimeUnit.SECONDS);
   check(!slowReturned.get()&&!partial.complete&&fast.snapshot(System.nanoTime()).target==a,"collector returns without waiting for slow peer");
   check(!fast.finishScan(ticket,partial,System.nanoTime()),"late partial result cannot undo installed device");
   System.out.println("Fast-connect simulated verification-to-install/return: "+TimeUnit.NANOSECONDS.toMillis(System.nanoTime()-start)+"ms; slow peer still blocked");
  }finally{releaseSlow.countDown();owner.shutdownNow();owner.awaitTermination(2,TimeUnit.SECONDS);}
  System.out.println("Fast-connect regressions: "+checks+" checks passed");
 }
}
