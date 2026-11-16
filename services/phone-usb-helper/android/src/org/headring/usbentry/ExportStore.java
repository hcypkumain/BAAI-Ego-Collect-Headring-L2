package org.headring.usbentry;

import java.io.*;
import java.nio.file.*;
import java.util.*;

/** App-private, bounded exports survive Activity recreation and picker cancellation. */
final class ExportStore {
 static final long LIMIT=32L*1024*1024;
 static final class Pending {
  final File file;final String name,mime;
  Pending(File f,String n,String m){file=f;name=n;mime=m;}
 }
 private final File directory,manifest;
 private FileOutputStream output;
 private File active;
 private String name,mime;
 private long expected,written;
 private Pending pending;
 ExportStore(File root)throws IOException{
  directory=new File(root,"headring-exports");if(!directory.isDirectory()&&!directory.mkdirs())throw new IOException("export directory unavailable");
  manifest=new File(directory,"pending.properties");restore();
  File[] files=directory.listFiles();if(files!=null)for(File file:files)if(file.getName().matches("download-[a-f0-9-]{36}\\.tmp")&&(pending==null||!file.equals(pending.file))&&System.currentTimeMillis()-file.lastModified()>24L*60*60*1000)file.delete();
 }
 synchronized boolean begin(String filename,String contentType,long size)throws IOException{
  if(output!=null||pending!=null||size<0||size>LIMIT)return false;
  active=new File(directory,"download-"+UUID.randomUUID()+".tmp");output=new FileOutputStream(active);
  name=safeName(filename);mime=contentType!=null&&contentType.matches("[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")?contentType:"application/octet-stream";
  expected=size;written=0;return true;
 }
 synchronized boolean write(byte[] bytes)throws IOException{
  if(output==null||bytes==null||written+bytes.length>expected){abortActive();return false;}
  output.write(bytes);written+=bytes.length;return true;
 }
 synchronized Pending finish()throws IOException{
  if(output==null||written!=expected){abortActive();throw new IOException("incomplete export");}
  output.getFD().sync();output.close();output=null;
  Pending ready=new Pending(active,name,mime);Properties values=new Properties();values.setProperty("file",active.getName());values.setProperty("name",name);values.setProperty("mime",mime);values.setProperty("size",Long.toString(written));
  File temporary=new File(directory,"pending.new");
  try(FileOutputStream file=new FileOutputStream(temporary)){values.store(file,"HeadRing pending export");file.getFD().sync();}
  try{Files.move(temporary.toPath(),manifest.toPath(),StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING);}
  catch(AtomicMoveNotSupportedException e){Files.move(temporary.toPath(),manifest.toPath(),StandardCopyOption.REPLACE_EXISTING);}
  pending=ready;active=null;return ready;
 }
 synchronized Pending pending(){return pending;}
 synchronized boolean active(){return output!=null;}
 synchronized void complete(Pending saved)throws IOException{
  if(pending!=saved)return;
  // Keep the manifest/file for retry if destination copying failed. Only call
  // this after the destination stream has successfully closed.
  Files.deleteIfExists(manifest.toPath());Files.deleteIfExists(saved.file.toPath());pending=null;
 }
 synchronized void abortActive(){
  if(output!=null)try{output.close();}catch(IOException ignored){}
  output=null;if(active!=null)active.delete();active=null;
 }
 private void restore(){
  try{
   if(!manifest.isFile()||manifest.length()>8192||Files.isSymbolicLink(manifest.toPath()))return;
   Properties values=new Properties();try(InputStream input=new FileInputStream(manifest)){values.load(input);}
   String basename=values.getProperty("file","");if(!basename.matches("download-[a-f0-9-]{36}\\.tmp"))throw new IOException();
   File file=new File(directory,basename);long size=Long.parseLong(values.getProperty("size","-1"));
   if(Files.isSymbolicLink(file.toPath())||!file.isFile()||size<0||size>LIMIT||file.length()!=size)throw new IOException();
   pending=new Pending(file,safeName(values.getProperty("name")),values.getProperty("mime","application/octet-stream"));
  }catch(Exception e){manifest.delete();}
 }
 static String safeName(String filename){String value=filename==null?"下载文件":filename.replaceAll("[^A-Za-z0-9._\\-\\u4e00-\\u9fff]","_");return value.isEmpty()?"下载文件":value.substring(0,Math.min(100,value.length()));}
}
