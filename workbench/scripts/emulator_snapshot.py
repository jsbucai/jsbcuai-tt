"""Save an observed Android UI hierarchy and screenshot for manual test evidence."""
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

adb = str(Path.home()/'Library/Android/sdk/platform-tools/adb')
base = [adb, '-s', 'emulator-5554']
name = sys.argv[1] if len(sys.argv)>1 else 'current'
if not name.replace('-', '').replace('_', '').replace('.', '').isalnum():
    raise ValueError('Use a simple evidence filename')
out = Path(__file__).resolve().parents[1]/'outputs/evidence/emulator-20261007'
out.mkdir(parents=True, exist_ok=True)
subprocess.run(base+['shell','rm','-f','/sdcard/ul-window.xml'], check=True)
dump = subprocess.run(base+['shell','uiautomator','dump','/sdcard/ul-window.xml'], capture_output=True, check=True)
if b'UI hierchary dumped to:' not in dump.stdout:
    raise RuntimeError('Android hierarchy is not ready; do not reuse an old snapshot')
xml = subprocess.check_output(base+['shell','cat','/sdcard/ul-window.xml'])
(out/(name+'.xml')).write_bytes(xml)
(out/(name+'.png')).write_bytes(subprocess.check_output(base+['exec-out','screencap','-p']))
for node in ET.fromstring(xml).iter('node'):
    text=node.get('text') or node.get('content-desc')
    if text or node.get('class')=='android.widget.EditText':
        print(text, node.get('class'), node.get('clickable'), node.get('bounds'))
print('Screenshot:',out/(name+'.png'))
