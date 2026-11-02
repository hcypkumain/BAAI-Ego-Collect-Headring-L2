"""Report possible secret literals without printing any matched secret value."""
import collections,json,re,sys,zipfile
from pathlib import Path
ROOT=Path(sys.argv[1]).resolve();findings=[];files=0;fixtures=[]
patterns={
    'github-token':re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'aliyun-access-key':re.compile(r'LTAI[A-Za-z0-9]{16,}'),
    'aws-access-key':re.compile(r'(?:AKIA|ASIA)[A-Z0-9]{16}'),
    'private-key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'credential-in-url':re.compile(r'https?://[^\s/@:]+:[^\s/@]+@'),
}
literal=re.compile(r'''(?im)\b(access_key_id|access_key_secret|api_key|secret_key|password|access_token|client_secret|ak|sk)\b["']?\s*[:=]\s*["']([^"'\r\n]{8,})["']''')
def inspect(name,raw):
    global files
    if b'\x00' in raw:return
    try:text=raw.decode('utf-8-sig')
    except UnicodeDecodeError:return
    files+=1
    for kind,pattern in patterns.items():
        for match in pattern.finditer(text):findings.append(dict(file=name,line=text.count('\n',0,match.start())+1,kind=kind))
    for match in literal.finditer(text):
        value=match.group(2).casefold()
        if name=='orangepi/tests/test_device_diagnostics.py' and value=='secret_qc':
            fixtures.append(dict(file=name,line=text.count('\n',0,match.start())+1,reason='synthetic diagnostic-redaction fixture'));continue
        if any(part in value for part in ('example','dummy','test','placeholder','replace','redacted','your_','${','<','not-a-','invalid','fake')):continue
        findings.append(dict(file=name,line=text.count('\n',0,match.start())+1,kind='review-secret-literal',key=match.group(1)))
for path in ROOT.rglob('*'):
    if not path.is_file() or '.git' in path.relative_to(ROOT).parts:continue
    relative=path.relative_to(ROOT).as_posix()
    if path.suffix=='.whl':
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(('.py','.json','.toml','.cfg','METADATA')):inspect(relative+'!'+name,archive.read(name))
    elif path.stat().st_size<=5*1024*1024:inspect(relative,path.read_bytes())
result=dict(scanned_text_files=files,possible_secret_locations=findings,
    reviewed_synthetic_fixtures=fixtures,
    file_count=sum(1 for path in ROOT.rglob('*') if path.is_file() and '.git' not in path.relative_to(ROOT).parts))
print(json.dumps(result,ensure_ascii=False,indent=2))
sys.exit(2 if findings else 0)
