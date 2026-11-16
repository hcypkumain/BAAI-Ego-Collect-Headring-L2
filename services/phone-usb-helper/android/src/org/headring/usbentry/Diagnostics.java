package org.headring.usbentry;
import java.util.*;
final class Diagnostics {
 static volatile String interfaces="尚未扫描", nsd="尚未开始", probe="尚未探测", manual="未使用", route="未知";
 static volatile ConnectionState connection=new ConnectionState();
 static ConnectionState.Snapshot snapshot(){return connection.snapshot(System.nanoTime());}
 static String report(){ConnectionState.Snapshot s=snapshot();return "连接状态："+s.message+"\n可选设备数："+s.candidates.size()+(s.stale?"（需重新验证）":"")+(s.scanning?" · 搜索中":"")+"\n\n接口：\n"+interfaces+"\n\n服务发现："+nsd+"\n\n探针："+probe+"\n\n手动诊断："+manual+"\n\n实际源地址："+route;}
}
