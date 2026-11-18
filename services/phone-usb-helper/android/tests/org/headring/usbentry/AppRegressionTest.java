package org.headring.usbentry;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

/** Host-JVM tests of production state, scan collector, HTTP policy and exports. */
public final class AppRegressionTest {
 static int checks;
 static void check(boolean value,String label){checks++;if(!value)throw new AssertionError(label);}
 static Endpoint endpoint(String id,String address)throws Exception{return new Endpoint(id,InetAddress.getByName(address),8765,null,InetAddress.getByName("192.168.9.1"));}
 static ConnectionState ready(){ConnectionState state=new ConnectionState();state.listener(false,true);state.listener(true,true);return state;}
 static void stateTests()throws Exception{
  Endpoint a=endpoint("cp_0017","192.168.9.17"),b=endpoint("cp_0023","192.168.9.23");
  ConnectionState state=ready();long first=state.beginScan();
  check(state.snapshot(1).scanning,"scan publishes loading");state.found(first,a,1);
  check(state.snapshot(2).candidates.size()==1&&!state.snapshot(2).stale,"verified candidate selectable before scan finishes");
  ConnectionState.Snapshot old=state.snapshot(2);boolean immutable=false;try{old.candidates.clear();}catch(UnsupportedOperationException expected){immutable=true;}check(immutable,"immutable candidate snapshot");
  state.finishScan(first,new ScanResult(Collections.singletonList(a),true,""),3);
  long empty=state.beginScan();state.finishScan(empty,new ScanResult(Collections.emptyList(),false,"超时"),4);
  check(state.snapshot(4).candidates.size()==1&&state.snapshot(4).stale,"empty scan retains candidate as stale");
  check(state.snapshot(ConnectionState.CANDIDATE_TTL+4).candidates.isEmpty(),"candidate expires rather than living forever");
  first=state.beginScan();long newer=state.beginScan();state.found(newer,b,10);
  check(!state.finishScan(first,new ScanResult(Collections.emptyList(),true,""),11),"old scan cannot commit");
  check(state.snapshot(11).candidates.get(0)==b,"old scan cannot erase newer peer");
  check(old.candidates.size()==1&&old.candidates.get(0)==a,"old UI snapshot not mutated");
  long choice=state.beginChoice(),latest=state.beginChoice();
  check(!state.choose(choice,a),"old connection choice rejected");check(state.choose(latest,b),"latest choice installed");
  check(state.snapshot(12).current().equals("cp_0023"),"current reflects latest device");
  check(!state.invalidate(a)&&state.snapshot(12).target==b,"late health failure cannot remove replacement");
  check(state.invalidate(b)&&state.snapshot(12).current().isEmpty(),"current cleared on actual invalidation");
  first=state.beginScan();choice=state.choiceRevision();state.finishScan(first,new ScanResult(Collections.singletonList(a),true,""),20);
  state.beginChoice();check(!state.autoChooseVerified(first,choice,a,20),"auto-connect cannot overtake explicit choice");
  ConnectionState unavailable=new ConnectionState();choice=unavailable.beginChoice();unavailable.choose(choice,a);
  check(unavailable.snapshot(1).current().isEmpty(),"target alone is not connected");unavailable.listener(false,true);
  check(unavailable.snapshot(1).current().isEmpty(),"one address family is insufficient");unavailable.listener(true,true);
  check(unavailable.snapshot(1).current().equals("cp_0017"),"both loopback listeners required");unavailable.listener(false,false);
  check(unavailable.snapshot(1).current().isEmpty(),"listener loss removes connected claim");unavailable.listener(false,true);
  check(unavailable.snapshot(1).current().equals("cp_0017"),"listener recovery restores state");
  state.close();check(!state.choose(latest,b)&&state.beginScan()==-1,"closed service cannot revive from late work");
 }
 static void scanTests()throws Exception{
  Endpoint a=endpoint("cp_0017","192.168.9.17");CountDownLatch blocked=new CountDownLatch(1);AtomicInteger seen=new AtomicInteger();
  List<Callable<Endpoint>> work=Arrays.asList(()->{blocked.await();return null;},()->a);
  ScanResult result=ScanCollector.collect(work,80,()->false,e->seen.incrementAndGet());
  check(!result.complete&&result.matches.size()==1&&seen.get()==1,"slow first peer does not hide fast verified peer");
  result=ScanCollector.collect(Arrays.asList(()->{throw new IOException();},()->a),1000,()->false,e->{});
  check(result.complete&&result.matches.size()==1,"failed peer does not erase verified match");
  AtomicBoolean cancel=new AtomicBoolean();result=ScanCollector.collect(Collections.singletonList(()->a),1000,cancel::get,e->cancel.set(true));
  check(!result.complete&&result.matches.size()==1,"cancel returns verified partial result");
  result=ScanCollector.collect(Collections.singletonList(()->a),1000,()->true,e->{throw new AssertionError();});
  check(!result.complete&&result.matches.isEmpty(),"cancelled scan cannot publish peer");
 }
 static String request(String method,String path,String extra){return method+" "+path+" HTTP/1.1\r\nHost: cp-0017.localhost:"+EntryAddress.PORT+"\r\n"+extra+"\r\n";}
 static void rejected(String raw,String name)throws Exception{boolean rejected=false;try{ProxyRequest.parse(raw,"cp_0017");}catch(IOException e){rejected=true;}check(rejected,name);}
 static void proxyTests()throws Exception{
  ProxyRequest control=ProxyRequest.parse(request("POST","/api/action","Content-Length: 3\r\n"),"cp_0017");
  check(control.control&&control.bodyLength==3,"control gets independent admission class");
  check(ProxyRequest.parse(request("GET","/api/display-state?client_id=x",""),"cp_0017").control,"privacy reads retain control capacity");
  ProxyRequest video=ProxyRequest.parse(request("GET","/api/phone/stream/cam_ros_left.jpg","Connection: keep-alive\r\n"),"cp_0017");
  check(!video.control&&video.stream,"preview does not take control slots");
  String forwarded=new String(video.header,StandardCharsets.ISO_8859_1);check(forwarded.contains("Connection: close\r\n")&&!forwarded.contains("keep-alive"),"one request per validated connection");
  rejected(request("GET","http://example.test/",""),"absolute-form URL rejected");
  rejected(request("GET","//example.test/",""),"network-path URL rejected");
  rejected(request("POST","/api/action","Content-Length: 1\r\nContent-Length: 2\r\n"),"duplicate content length rejected");
  rejected(request("POST","/api/action","Transfer-Encoding: chunked\r\n"),"unsupported chunked framing rejected");
  rejected(request("GET","/phone","Content-Length: 1\r\n"),"GET body rejected");
  rejected(request("GET","/phone","Origin: http://evil.test\r\n"),"cross-origin request rejected");
  rejected(request("GET","/phone","Host: localhost:8765\r\n"),"duplicate Host rejected");
  rejected(request("GET","/phone","X-Test: value\nInjected: yes\r\n"),"bare newline rejected");
  rejected(request("GET","/phone","").replace("cp-0017","cp-0023"),"old device origin rejected");
  rejected(request("GET","/phone","").replace(":"+EntryAddress.PORT+"\r\n",":"+(EntryAddress.PORT==8765?18765:8765)+"\r\n"),"other App profile port rejected");
  TransferBudget budget=new TransferBudget(0,true,false);
  check(!budget.expired(TimeUnit.SECONDS.toNanos(7)),"control initially within idle budget");
  check(budget.expired(TimeUnit.SECONDS.toNanos(8)),"silent control closes at idle budget");
  budget=new TransferBudget(0,false,false);budget.touch(TimeUnit.SECONDS.toNanos(29));
  check(!budget.expired(TimeUnit.SECONDS.toNanos(40)),"active upload progress extends idle allowance");
  check(budget.expired(TimeUnit.SECONDS.toNanos(180)),"total request lifetime bounded despite activity");
  budget=new TransferBudget(0,false,true);check(budget.expired(TimeUnit.SECONDS.toNanos(12)),"silent continuous stream bounded");
 }
 static void exportTests(File root)throws Exception{
  ExportStore store=new ExportStore(root);byte[] bytes={1,2,3};
  check(!store.begin("big","text/plain",ExportStore.LIMIT+1),"oversize export rejected before allocation");
  check(store.begin("../../报告.txt","text/plain",3),"bounded export starts");
  check(!store.begin("other","text/plain",1),"concurrent blob cannot replace active export");
  check(store.write(bytes),"blob bytes accepted");ExportStore.Pending pending=store.finish();
  check(pending.file.isFile()&&!pending.name.contains("/"),"export name is sanitized");
  ExportStore recreated=new ExportStore(root);ExportStore.Pending restored=recreated.pending();
  check(restored!=null&&Arrays.equals(Files.readAllBytes(restored.file.toPath()),bytes),"completed blob survives recreation");
  check(!recreated.begin("new","text/plain",0),"pending picker export not overwritten");
  recreated.abortActive();check(recreated.pending()!=null&&restored.file.exists(),"navigation/teardown cannot remove ready export");
  recreated.complete(new ExportStore.Pending(restored.file,"wrong","text/plain"));check(recreated.pending()!=null,"unrelated completion cannot delete pending export");
  recreated.complete(restored);check(recreated.pending()==null&&!restored.file.exists(),"successful destination copy releases export");
  check(recreated.begin("partial","text/plain",3)&&!recreated.write(new byte[4])&&!recreated.active(),"overflow aborts incomplete blob");
  recreated.begin("short","text/plain",3);boolean incomplete=false;try{recreated.finish();}catch(IOException expected){incomplete=true;}check(incomplete&&!recreated.active(),"incomplete blob never opens picker");
  recreated.begin("ok","text/plain",0);recreated.finish();File manifest=new File(root,"headring-exports/pending.properties");Properties values=new Properties();values.setProperty("file","../../outside");values.setProperty("size","0");try(FileOutputStream file=new FileOutputStream(manifest)){values.store(file,"");}
  check(new ExportStore(root).pending()==null,"manifest cannot restore arbitrary file path");
 }
 static void recoveryTests(){
  PageRecovery page=new PageRecovery();check(!page.failed(),"initial page not failed");
  for(int i=0;i<5;i++){page.started();check(page.failure()>=0,"bounded GET retry "+i);page.success();}
  page.started();check(page.failure()==-1,"retry budget terminates rather than looping forever");
  page.manual();check(!page.failed()&&page.failure()==750,"manual retry starts fresh budget");
  page.started();page.success();check(page.failure()==750,"successful page resets next failure budget");
 }
 public static void main(String[] args)throws Exception{
  ConnectionVisibilityTest.test(); WorkbenchProbeTest.test();
  PolicyTest.main(new String[0]);stateTests();scanTests();proxyTests();exportTests(new File(args[0]));recoveryTests();FastConnectTest.main(new String[0]);
  System.out.println("App regressions: "+checks+" checks passed");
 }
}
