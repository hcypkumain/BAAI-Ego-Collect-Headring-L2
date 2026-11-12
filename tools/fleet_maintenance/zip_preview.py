"""Bounded ZIP validation with no extraction or filesystem writes."""
import hashlib,io,re,stat,struct,zipfile,unicodedata
from PIL import Image
MAX_ZIP=256*1024*1024
MAX_EXPANDED=1024*1024*1024
MAX_IMAGE=12*1024*1024
MAX_PIXELS=20000000
MAX_SCENE=2*1024*1024

def member_name(info):
    name=getattr(info,'orig_filename',info.filename)
    if not info.flag_bits & 0x800:
        raw=name.encode('cp437')
        try:name=raw.decode('utf-8')
        except UnicodeDecodeError:name=raw.decode('gb18030')
    name=name.replace('\\','/')
    if not name or name.startswith('/') or re.match(r'^[A-Za-z]:',name) or any(ord(c)<32 or ord(c)==127 for c in name) or any(p in ('','..','.') for p in name.rstrip('/').split('/')):raise ValueError('unsafe_path')
    return name

def preview_file(stream):
    stream.seek(0,2);size=stream.tell()
    if not 22<=size<=MAX_ZIP:raise ValueError('zip_size')
    stream.seek(max(0,size-65557));tail=stream.read(65557);end=tail.rfind(b'PK\x05\x06')
    if end<0 or len(tail)-end<22:raise ValueError('invalid_directory')
    _,disk,start,here,count,length,offset,comment=struct.unpack('<4s4H2LH',tail[end:end+22])
    if end+22+comment!=len(tail) or disk or start or here!=count or count>=65535 or count>10000 or length>4*1024*1024 or offset+length>size-len(tail)+end:raise ValueError('zip64_or_directory')
    stream.seek(0);source_hash=hashlib.sha256()
    for chunk in iter(lambda:stream.read(262144),b''):source_hash.update(chunk)
    stream.seek(0);names=set();basenames=set();images=[];scenes=[];expanded=0
    with zipfile.ZipFile(stream) as archive:
        for info in archive.infolist():
            name=member_name(info); normalized=name.rstrip('/')
            key=unicodedata.normalize('NFC',normalized).casefold()
            if key in names:raise ValueError('duplicate_path')
            names.add(key)
            mode=(info.external_attr>>16)&0o170000
            if info.flag_bits&1 or info.compress_type not in (0,8) or mode==stat.S_IFLNK:raise ValueError('unsafe_member')
            if info.is_dir():continue
            expanded+=info.file_size
            if expanded>MAX_EXPANDED or (info.file_size>1024*1024 and info.file_size>max(1,info.compress_size)*500):raise ValueError('expansion_limit')
            if any(p.startswith('.') or p=='__MACOSX' for p in name.split('/')):continue
            base=name.rsplit('/',1)[-1];lower=base.lower()
            kind='scene' if lower.endswith(('_placements_overview.png','.yaml','.yml','.csv')) else 'image' if lower.endswith(('.png','.jpg','.jpeg','.webp')) else None
            if kind is None:continue
            basename_key=unicodedata.normalize('NFC',base).casefold()
            if basename_key in basenames:raise ValueError('duplicate_basename')
            basenames.add(basename_key); limit=MAX_IMAGE if kind=='image' else MAX_SCENE
            if not 0<info.file_size<=limit or len(base.encode('utf-8'))>384:raise ValueError('member_size')
            with archive.open(info) as source: raw=source.read(limit+1)
            if len(raw)!=info.file_size:raise ValueError('size_or_crc')
            item={'name':base,'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw)}
            if kind=='image':
                with Image.open(io.BytesIO(raw)) as im:
                    if im.format not in ('PNG','JPEG','WEBP') or im.width*im.height>MAX_PIXELS or max(im.size)>12000:raise ValueError('pixel_limit')
                    im.load();item['width'],item['height']=im.size
                images.append(item)
            else:scenes.append(item)
    if not images or len(images)>5000 or len(scenes)>4:raise ValueError('image_or_scene_count')
    return {'sha256':source_hash.hexdigest(),'size':size,'expanded_bytes':expanded,'images':images,'scenes':scenes}
