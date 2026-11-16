"""Compile unchanged production Java logic for host-JVM regressions.

The sole Android stub supplies Endpoint's Network type; there are no fake
implementations of the production state/scan/proxy/export/recovery classes.
"""
from pathlib import Path
import os
import subprocess
import sys

app = Path(__file__).resolve().parents[1]
work = Path(sys.argv[1]).resolve()
work.mkdir(parents=True, exist_ok=True)
stub = work / 'android/net/Network.java'
stub.parent.mkdir(parents=True, exist_ok=True)
stub.write_text('package android.net; public class Network {}\n')
classes = work / 'classes'
classes.mkdir(exist_ok=True)
source = app / 'src/org/headring/usbentry'
names = ('DeviceIdentity', 'DeviceSelection', 'OriginPolicy', 'Endpoint', 'ConnectionState',
         'ScanResult', 'ScanCollector', 'ProxyRequest', 'TransferBudget', 'ExportStore', 'PageRecovery', 'EntryAddress', 'UsbSource')
java = os.environ.get('TEST_JAVA', '/usr/lib/jvm/java-25-openjdk-amd64/bin/java')
commands = [
    [java, 'com.sun.tools.javac.Main', '-source', '8', '-target', '8', '-proc:none',
     '-d', str(classes), str(stub), *[str(source / (name + '.java')) for name in names],
     *map(str, sorted((app / 'tests/org').rglob('*.java')))],
    [java, '-cp', str(classes), 'org.headring.usbentry.AppRegressionTest', str(work / 'files')],
]
for parallel in [False, True]:
    selected = list(commands[0])
    if parallel:
        profile = work / 'EntryAddress.java'
        profile.write_text((source / 'EntryAddress.java').read_text().replace('PORT=8765;', 'PORT=18765;'))
        selected = [str(profile) if value == str(source / 'EntryAddress.java') else value for value in selected]
    print('PROFILE', 'parallel18765' if parallel else 'upgrade8765', flush=True)
    for command in [selected, [*commands[1][:-1], str(work / ('parallel-files' if parallel else 'files'))]]:
        result = subprocess.run(command, timeout=30)
        if result.returncode:
            raise SystemExit(result.returncode)
