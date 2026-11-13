package org.headring.usbentry;
import android.net.Network;
import java.net.InetAddress;
final class Endpoint {
 final String deviceId; final InetAddress address; final int port; final Network network; final InetAddress source;
 Endpoint(String id, InetAddress a, int p, Network n, InetAddress s){deviceId=id;address=a;port=p;network=n;source=s;}
}
