package org.headring.usbentry;
import java.util.*;
final class ScanResult {
 final List<Endpoint> matches;
 final boolean complete;
 final String message;
 ScanResult(List<Endpoint> values,boolean all,String text){matches=Collections.unmodifiableList(new ArrayList<>(values));complete=all;message=text;}
}
