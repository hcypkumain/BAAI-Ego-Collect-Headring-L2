package org.headring.usbentry;
import java.util.*;
final class Diagnostics {
 static volatile String interfaces="尚未扫描", nsd="尚未开始", probe="尚未探测", manual="未使用", route="未知", current="";
 static volatile List<Endpoint> candidates=Collections.emptyList();
 static String report(){return "当前设备："+(current.isEmpty()?"等待头环连接":current)+"\n\n接口：\n"+interfaces+"\n\n服务发现："+nsd+"\n\n探针："+probe+"\n\n手动诊断："+manual+"\n\n实际源地址："+route+"\n\n可选设备数："+candidates.size();}
}
