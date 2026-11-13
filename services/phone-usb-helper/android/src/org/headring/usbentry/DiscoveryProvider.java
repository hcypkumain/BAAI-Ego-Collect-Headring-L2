package org.headring.usbentry;
interface DiscoveryProvider {
 interface Listener { void found(Endpoint endpoint); void lost(); void failed(); }
 void start(Listener listener); void stop();
}
