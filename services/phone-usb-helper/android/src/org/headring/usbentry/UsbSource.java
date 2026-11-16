package org.headring.usbentry;
import java.net.InetAddress;
import java.net.Inet4Address;
import java.util.List;

/** A source fingerprint, not a claim that a device identity has been verified. */
final class UsbSource {
 final String name;final int index,prefix;final InetAddress address;
 UsbSource(String n,int i,InetAddress a,int p){name=n;index=i;address=a;prefix=p;}
 static UsbSource unique(List<UsbSource> values){return values.size()==1?values.get(0):null;}
 boolean same(UsbSource other){return other!=null&&index==other.index&&prefix==other.prefix&&name.equals(other.name)&&address.equals(other.address);}
 boolean peer(InetAddress other){
  if(!(address instanceof Inet4Address)||!(other instanceof Inet4Address)||prefix<24||prefix>30||address.equals(other))return false;
  int own=number(address),remote=number(other),mask=-1<<(32-prefix),start=own&mask,end=start|~mask;
  return remote>start&&remote<end;
 }
 private static int number(InetAddress a){byte[] v=a.getAddress();return ((v[0]&255)<<24)|((v[1]&255)<<16)|((v[2]&255)<<8)|(v[3]&255);}
}
