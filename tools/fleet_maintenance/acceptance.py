"""Explicit read-only real-device acceptance. Only targets listed by the operator."""
import argparse,hashlib,io,json,re,sys,time,urllib.request
from PIL import Image

def main():
 p=argparse.ArgumentParser();p.add_argument('--base-url',required=True);p.add_argument('--devices',nargs='+',required=True);p.add_argument('--readonly',action='store_true',required=True);a=p.parse_args()
 if not re.fullmatch(r'http://127\.0\.0\.1:\d{1,5}/?',a.base_url): p.error('base URL must be local IPv4')
 base=a.base_url.rstrip('/')
 def get(path):
  with urllib.request.urlopen(base+path,timeout=10) as r:return json.load(r)
 def job(path,obj):
  token=get('/api/state')['csrf']; req=urllib.request.Request(base+path,json.dumps(obj).encode(),headers={'Origin':base,'X-CSRF-Token':token,'Content-Type':'application/json'},method='POST')
  with urllib.request.urlopen(req,timeout=10) as r: ident=json.load(r)['job']
  for _ in range(160):
   s=get('/api/state'); status=s['jobs'][ident]['status']
   if status!='running':
    if status!='done': raise RuntimeError('job '+path+' '+status)
    return s
   time.sleep(0.5)
  raise RuntimeError('job timeout')
 def image(meta):
  with urllib.request.urlopen(base+'/api/assets/'+meta['key'],timeout=15) as r: raw=r.read(8388609)
  if hashlib.sha256(raw).hexdigest()!=meta['sha256']: raise RuntimeError('asset sha mismatch')
  with Image.open(io.BytesIO(raw)) as im:
   if im.size!=(meta['width'],meta['height']) or im.format not in ('JPEG','PNG'): raise RuntimeError('image invalid')
   im.verify()
  return {k:meta[k] for k in ('sha256','width','height','format','kind')}
 output={}
 for name in a.devices:
  m=re.fullmatch(r'cp_?(\d{4})',name)
  if not m:p.error('invalid device')
  device='cp_'+m.group(1);s=job('/api/refresh',{'device':device});r=s['cache'][device]
  if r.get('schema')!=2 or not r.get('online'):raise RuntimeError(device+' refresh failed')
  profiles=r['profiles'];ref=r['reference'];active=r['profile_context']['active_mode']; catalog=profiles[active]['catalog']
  if any(profiles.get(m,{}).get('root_status')!='resolved' for m in ('emg','eeg')): raise RuntimeError(device+' profile root unresolved')
  if ref['revision']!=catalog['revision']: raise RuntimeError(device+' catalog revision changed')
  if catalog.get('count')!=ref.get('total_unique'): raise RuntimeError(device+' catalog and visible count differ; inspect scope')
  for mode in ('emg','eeg'):
   storage=profiles[mode]['storage']
   if storage.get('status')!='mounted' or not storage.get('expected_uuid') or storage['expected_uuid']!=storage.get('actual_uuid') or not storage.get('data_root','').startswith(storage.get('mount','')+'/'): raise RuntimeError(device+' storage profile mismatch')
  lists=[x for x in ref.get('lists',[]) if x.get('count_visible',0)>0]
  if not lists or ref.get('membership_status')!='catalog_revision_matched_visible_items': raise RuntimeError(device+' list membership unavailable')
  filtered=[x for x in ref['items'] if lists[0]['id'] in x.get('lists',[])]
  if not filtered: raise RuntimeError(device+' list filter empty')
  for mode in ('emg','eeg'):
   st=profiles[mode]['storage']
   if not any(b.get('majmin')==st.get('majmin') and b.get('mounted') for b in r.get('blocks',[])): raise RuntimeError(device+' storage volume not bound to block')
  original_context=dict(r['profile_context'])
  samples=[]; tasks=set()
  for item in ref['items']:
   task=re.sub(r'[_-]\d+$','',item['name'])
   if task not in tasks: samples.append(item);tasks.add(task)
   if len(samples)==3: break
  if len(samples)<3: raise RuntimeError(device+' insufficient distinct task images')
  ids=[x['id'] for x in samples]; s=job('/api/thumbnails',{'device':device,'ids':ids}); thumbnails=s['cache'][device]['thumbnails']
  images=[]
  for item in samples:
   ident=item['id']; thumb=image(thumbnails[ident]);s=job('/api/original',{'device':device,'ids':[ident]}); original=image(s['cache'][device]['originals'][ident]);images.append({'id':ident,'name':item['name'],'thumbnail':thumb,'original':original})
  if filtered[0]['id'] not in ids:
   s=job('/api/original',{'device':device,'ids':[filtered[0]['id']]}); filtered_original=image(s['cache'][device]['originals'][filtered[0]['id']])
  else: filtered_original=image(s['cache'][device]['originals'][filtered[0]['id']])
  s=job('/api/audit',{'device':device}); audit=s['cache'][device]['history_audit']
  s=job('/api/operations/plans',{'device':device});r=s['cache'][device]
  report=get('/api/reports/'+device)
  report_text=json.dumps(report).lower()
  if any(x in report_text for x in ('dashboard_token','access_code','password')) or report['plan']['plan_sha256']!=r['review_plan']['plan_sha256']: raise RuntimeError('report invalid or secret')
  s=job('/api/refresh',{'device':device}); r=s['cache'][device]
  if r['profile_context']!=original_context: raise RuntimeError(device+' profile context changed')
  count_before=len(s['jobs']); s=get('/api/state'); s=get('/api/state')
  if len(s['jobs'])!=count_before: raise RuntimeError('GET triggered remote job')
  output[device]={'context':r['profile_context'],'profiles':profiles,'reference':{k:ref.get(k) for k in ('revision','total_unique','completed_unique','partial','reason')},'images':images,'list_filter':{'id':lists[0]['id'],'name':lists[0]['name'],'count':len(filtered),'sample_id':filtered[0]['id'],'original':filtered_original},'history':{'records':len(r['history']['items']),'associated':sum(bool(x.get('asset_id')) for x in r['history']['items']),'audit':audit},'blocks':r['blocks'],'plan':r['review_plan'],'report_checked':True,'context_unchanged':True}
 print(json.dumps(output,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
