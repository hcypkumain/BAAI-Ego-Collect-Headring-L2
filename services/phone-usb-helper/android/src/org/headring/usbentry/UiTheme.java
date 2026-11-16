package org.headring.usbentry;

import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.ColorFilter;
import android.graphics.PixelFormat;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Local presentation tokens. Never interprets acquisition readiness. */
final class UiTheme {
 static final int BACKGROUND=Color.rgb(244,246,242),SURFACE=Color.WHITE,SOFT=Color.rgb(237,242,236);
 static final int INK=Color.rgb(25,44,39),MUTED=Color.rgb(83,102,94),LINE=Color.rgb(215,224,216);
 static final int PRIMARY=Color.rgb(23,100,75),SELECTED=Color.rgb(226,239,230),ERROR=Color.rgb(163,50,50);
 private UiTheme(){}
 static int dp(Context context,float value){return Math.round(value*context.getResources().getDisplayMetrics().density);}
 static GradientDrawable background(Context c,int color,int stroke,int radius){
  GradientDrawable shape=new GradientDrawable();shape.setColor(color);shape.setCornerRadius(dp(c,radius));
  if(stroke!=0)shape.setStroke(dp(c,1),stroke);return shape;
 }
 static TextView text(Context c,String value,int size,int color,boolean bold){
  TextView view=new TextView(c);view.setText(value);view.setTextSize(size);view.setTextColor(color);
  view.setTypeface(Typeface.create("sans-serif",bold?Typeface.BOLD:Typeface.NORMAL));view.setIncludeFontPadding(false);
  view.setLineSpacing(dp(c,3),1f);return view;
 }
 static LinearLayout column(Context c){LinearLayout view=new LinearLayout(c);view.setOrientation(LinearLayout.VERTICAL);return view;}
 static LinearLayout surface(Context c){
  LinearLayout view=column(c);view.setPadding(dp(c,10),dp(c,10),dp(c,10),dp(c,10));view.setBackground(background(c,SURFACE,LINE,16));return view;
 }
 static void gap(LinearLayout parent,int height){View spacer=new View(parent.getContext());parent.addView(spacer,new LinearLayout.LayoutParams(1,dp(parent.getContext(),height)));}
 static void button(Button button,boolean primary){
  Context c=button.getContext();button.setAllCaps(false);button.setTextSize(14);button.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));
  button.setMinHeight(dp(c,48));button.setMinimumHeight(dp(c,48));button.setPadding(dp(c,12),dp(c,8),dp(c,12),dp(c,8));
  button.setTextColor(new ColorStateList(new int[][]{new int[]{-android.R.attr.state_enabled},new int[]{}},new int[]{Color.rgb(102,117,108),primary?Color.WHITE:PRIMARY}));
  android.graphics.drawable.StateListDrawable states=new android.graphics.drawable.StateListDrawable();
  states.addState(new int[]{-android.R.attr.state_enabled},background(c,Color.rgb(230,235,230),0,12));
  states.addState(new int[]{},new RippleDrawable(ColorStateList.valueOf(primary?0x33FFFFFF:0x2217644B),background(c,primary?PRIMARY:SURFACE,primary?0:LINE,12),null));
  button.setBackground(states);button.setStateListAnimator(null);
 }
 static Drawable icon(Context c,boolean back,int color){
  Drawable icon=new Drawable(){
   final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);
   public void draw(Canvas canvas){
    canvas.save();canvas.translate(getBounds().left,getBounds().top);canvas.scale(getBounds().width()/24f,getBounds().height()/24f);
    paint.setColor(color);paint.setStyle(Paint.Style.STROKE);paint.setStrokeWidth(1.8f);paint.setStrokeCap(Paint.Cap.ROUND);paint.setStrokeJoin(Paint.Join.ROUND);
    Path path=new Path();
    if(back){path.moveTo(14,5);path.lineTo(7,12);path.lineTo(14,19);path.moveTo(7,12);path.lineTo(21,12);}
    else{path.moveTo(12,3);path.lineTo(12,18);path.moveTo(9,6);path.lineTo(12,3);path.lineTo(15,6);path.moveTo(12,12);path.lineTo(6,9);path.lineTo(6,6);path.moveTo(12,15);path.lineTo(18,12);path.lineTo(18,9);canvas.drawCircle(12,20,2,paint);canvas.drawCircle(6,5,1,paint);canvas.drawRect(17,7,19,9,paint);}
    canvas.drawPath(path,paint);canvas.restore();
   }
   public void setAlpha(int alpha){paint.setAlpha(alpha);}
   public void setColorFilter(ColorFilter filter){paint.setColorFilter(filter);}
   public int getOpacity(){return PixelFormat.TRANSLUCENT;}
  };
  icon.setBounds(0,0,dp(c,22),dp(c,22));return icon;
 }
}
