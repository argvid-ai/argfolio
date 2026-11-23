#!/usr/bin/env python3
"""Reproducible preview APK build using Google's aapt2/D8/apksig. JDK 11+."""
import argparse
import os
from ipaddress import IPv4Address
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument('--tools', type=Path, required=True, help='directory with aapt2.jar, r8.jar, apksig.jar, platform35.zip')
ap.add_argument('--output', type=Path, required=True)
ap.add_argument('--lan-address', type=IPv4Address, help='IPv4 address of the paired HTTPS workbench')
ap.add_argument('--lan-certificate', type=Path, help='Public CA certificate for this workbench')
ap.add_argument('--workbench-url', help='Default public HTTPS origin for the Android app')
a = ap.parse_args()
password = os.environ.get('PAPERSCAN_PREVIEW_KEYSTORE_PASSWORD')
if not password:
    ap.error('Set PAPERSCAN_PREVIEW_KEYSTORE_PASSWORD in the build environment')
if bool(a.lan_address) != bool(a.lan_certificate):
    ap.error('--lan-address and --lan-certificate must be used together')
if a.workbench_url:
    try:
        origin = urlsplit(a.workbench_url)
        if (origin.scheme != 'https' or not origin.hostname or origin.username is not None
                or origin.path not in ('', '/') or origin.query or origin.fragment
                or origin.port == 0):
            raise ValueError()
    except ValueError:
        ap.error('--workbench-url must be an HTTPS origin without a path or credentials')
t = a.tools.resolve()
out = a.output.resolve()
build = t / 'build'
build.mkdir(exist_ok=True)
out.parent.mkdir(parents=True, exist_ok=True)

def run(*args):
    subprocess.run([str(x) for x in args], check=True)

with zipfile.ZipFile(t / 'aapt2.jar') as z:
    (t / 'aapt2').write_bytes(z.read('aapt2'))
(t / 'aapt2').chmod(0o755)
with zipfile.ZipFile(t / 'platform35.zip') as z:
    name = next(n for n in z.namelist() if n.endswith('/android.jar'))
    (t / 'android35.jar').write_bytes(z.read(name))
res = build / 'resources'
if res.exists():
    shutil.rmtree(res)
shutil.copytree(ROOT / 'android/app/src/main/res', res)
if a.lan_address:
    raw = res / 'raw'
    raw.mkdir()
    shutil.copyfile(a.lan_certificate, raw / 'workbench_ca.pem')
    config = ET.parse(res / 'xml/network_security_config.xml')
    domain = ET.SubElement(config.getroot(), 'domain-config', cleartextTrafficPermitted='false')
    ET.SubElement(domain, 'domain', includeSubdomains='false').text = str(a.lan_address)
    anchors = ET.SubElement(domain, 'trust-anchors')
    ET.SubElement(anchors, 'certificates', src='@raw/workbench_ca')
    config.write(res / 'xml/network_security_config.xml')
values = ET.parse(res / 'values/connection.xml')
lan_url = f'https://{a.lan_address}:8443' if a.lan_address else ''
values.getroot().find("string[@name='default_workbench']").text = a.workbench_url or lan_url
values.getroot().find("string[@name='previous_lan_workbench']").text = lan_url
values.write(res / 'values/connection.xml')
run(t/'aapt2','compile','--dir',res,'-o',build/'resources.zip')
run(t/'aapt2','link','-o',build/'unsigned.apk','--manifest',ROOT/'android/app/src/main/AndroidManifest.xml',
    '-I',t/'android35.jar','--min-sdk-version','24','--target-sdk-version','35',
    '--java',build/'generated',build/'resources.zip')
if (build/'classes').exists():
    shutil.rmtree(build/'classes')
(build/'classes').mkdir()
# Compile against the same platform used to link resources.
run('javac','--release','8','-encoding','UTF-8','-classpath',t/'android35.jar','-d',build/'classes',
    ROOT/'android/app/src/main/java/com/cix/paperscan/MainActivity.java',
    build/'generated/com/cix/paperscan/R.java')
with zipfile.ZipFile(build/'classes.jar','w') as z:
    for p in (build/'classes').rglob('*.class'):
        z.write(p,p.relative_to(build/'classes'))
(build/'dex').mkdir(exist_ok=True)
run('java','-cp',t/'r8.jar','com.android.tools.r8.D8','--min-api','24','--lib',t/'android35.jar','--output',build/'dex',build/'classes.jar')
with zipfile.ZipFile(build/'unsigned.apk','a') as z:
    z.write(build/'dex/classes.dex','classes.dex')
key = t/'preview-signing.jks'
signing_env = os.environ.copy()
signing_env['PAPERSCAN_PREVIEW_KEYSTORE_PASSWORD'] = password
if not key.exists():
    subprocess.run(['keytool','-genkeypair','-keystore',str(key),'-storetype','JKS',
        '-storepass:env','PAPERSCAN_PREVIEW_KEYSTORE_PASSWORD',
        '-keypass:env','PAPERSCAN_PREVIEW_KEYSTORE_PASSWORD',
        '-alias','preview','-keyalg','RSA','-keysize','2048','-validity','3650',
        '-dname','CN=PaperScan Local Preview, O=CIX PaperScan, C=CN'],check=True,env=signing_env)
(build/'signer').mkdir(exist_ok=True)
run('javac','-cp',t/'apksig.jar','-d',build/'signer',ROOT/'tools/SignApk.java')
subprocess.run(['java','-cp',str(t/'apksig.jar')+':'+str(build/'signer'),'SignApk',
    str(key),str(build/'unsigned.apk'),str(out)],check=True,env=signing_env)
run(t/'aapt2','dump','badging',out)
print('Created',out)
