"""Read JPEG dimensions without decoding or resizing the capture payload."""
SOF=frozenset((0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf))

def jpeg_size(raw):
    if not isinstance(raw,bytes) or raw[:2]!=b'\xff\xd8':raise ValueError('Not a JPEG frame')
    pos=2;end=min(len(raw),65536)
    while pos<end:
        if raw[pos]!=0xff:raise ValueError('Invalid JPEG marker')
        while pos<end and raw[pos]==0xff:pos+=1
        if pos>=end:break
        marker=raw[pos];pos+=1
        if marker in (0xd9,0xda):break
        if marker==0x01 or 0xd0<=marker<=0xd8:continue
        if pos+2>end:break
        size=int.from_bytes(raw[pos:pos+2],'big')
        if size<2 or pos+size>len(raw):raise ValueError('Truncated JPEG segment')
        if marker in SOF:
            if size<8 or pos+7>end:raise ValueError('Invalid JPEG dimensions')
            height=int.from_bytes(raw[pos+3:pos+5],'big');width=int.from_bytes(raw[pos+5:pos+7],'big')
            if min(width,height)<=0:raise ValueError('Empty JPEG dimensions')
            return width,height
        pos+=size
    raise ValueError('JPEG dimensions not found within bounded header')

def verify_resolution(actual,expected,label='camera'):
    actual=tuple(int(v) for v in actual);expected=tuple(int(v) for v in expected)
    if actual!=expected:raise ValueError('%s: capture resolution mismatch: expected %dx%d, received %dx%d'%((label,)+expected+actual))
    return actual
