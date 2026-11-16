package org.headring.usbentry;

import android.content.Context;
import android.net.Uri;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import org.json.JSONObject;

/** Trusted-page, fail-open appearance layer, bundled with the APK for offline use. */
final class WebTheme {
 static final String CONNECTION_PATH="/__headring/ui/connection";
 private final String css,script;
 WebTheme(Context context)throws IOException{css=read(context,"ui/headring-theme.css");script=read(context,"ui/headring-presentation.js");}
 static boolean page(Uri uri){if(uri==null)return false;String p=uri.getPath();return "/phone".equals(p)||"/phone/".equals(p)||"/".equals(p);}
 String injection(String nonce,String originAndPath){
  String destination=CONNECTION_PATH+"?nonce="+Uri.encode(nonce);
  return "(function(){try{if(location.origin+location.pathname!=="+JSONObject.quote(originAndPath)+")return;window.__headringUi={version:6,css:"+JSONObject.quote(css)+",connection:"+JSONObject.quote(destination)+"};"+script+"}catch(_error){}})();";
 }
 private static String read(Context context,String path)throws IOException{
  try(InputStream input=context.getAssets().open(path);ByteArrayOutputStream output=new ByteArrayOutputStream()){
   byte[] buffer=new byte[8192];int count;while((count=input.read(buffer))!=-1){if(output.size()+count>262144)throw new IOException("UI asset too large");output.write(buffer,0,count);}return output.toString("UTF-8");
  }
 }
}
