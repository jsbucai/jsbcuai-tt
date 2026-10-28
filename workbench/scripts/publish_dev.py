"""Publish an already-tested, signed APK atomically to the local installation page."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'app/build/outputs/apk/debug/app-debug.apk'

def main():
    if not SOURCE.is_file(): raise SystemExit('Build the APK before publishing.')
    metadata = json.loads((SOURCE.parent / 'output-metadata.json').read_text('utf-8'))
    if metadata['applicationId'] != 'com.ul.workbench.dev': raise SystemExit('Unexpected application ID')
    element = metadata['elements'][0]
    with zipfile.ZipFile(SOURCE) as apk:
        if apk.testzip() is not None: raise SystemExit('Corrupt APK')
    sdk = os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT')
    if not sdk or not os.environ.get('JAVA_HOME'):
        raise SystemExit('Load runtime/toolchain/local-env.sh or set JAVA_HOME and ANDROID_HOME first.')
    executable = 'apksigner.bat' if os.name == 'nt' else 'apksigner'
    signers = sorted((Path(sdk) / 'build-tools').glob('*/' + executable))
    if not signers:
        raise SystemExit('Android SDK apksigner is missing.')
    verified = subprocess.run([str(signers[-1]), 'verify', '--print-certs', str(SOURCE)],
                              env=os.environ.copy(), check=True, capture_output=True, text=True)
    expected = '1061d4910cea45072a51600e921bf58d3314f2607ba2395c161ccb7bb27c30ac'
    if f'Signer #1 certificate SHA-256 digest: {expected}' not in verified.stdout:
        raise SystemExit('APK is not signed with the original UL development certificate.')
    releases = ROOT / 'releases'
    releases.mkdir(exist_ok=True)
    name = f"UL-Workbench-{element['versionName']}.apk"
    temp = releases / (name + '.tmp')
    shutil.copyfile(SOURCE, temp)
    digest = hashlib.sha256(temp.read_bytes()).hexdigest()
    os.replace(temp, releases / name)
    record = dict(version=element['versionName'], versionCode=element['versionCode'],
                  applicationId=metadata['applicationId'], file=name, sha256=digest,
                  bytes=SOURCE.stat().st_size)
    current = releases / 'current.json.tmp'
    current.write_text(json.dumps(record, ensure_ascii=False, indent=2), 'utf-8')
    os.replace(current, releases / 'current.json')
    print(json.dumps(record, ensure_ascii=False))

if __name__ == '__main__': main()
