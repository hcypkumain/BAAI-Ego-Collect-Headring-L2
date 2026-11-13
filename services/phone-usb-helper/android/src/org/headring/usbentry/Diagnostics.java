package org.headring.usbentry;
final class Diagnostics {
 static volatile String interfaces="尚未扫描", nsd="尚未开始", probe="尚未探测", manual="未使用", route="未知";
 static String report(){return "接口：\n"+interfaces+"\n\n服务发现："+nsd+"\n\n探针："+probe+"\n\n手动诊断："+manual+"\n\n实际源地址："+route;}
}
