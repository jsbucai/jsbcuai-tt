"""Record isolated OCR dependency hashes and unchanged global package absence."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys


def main():
    base=Path(sys.argv[1]).resolve()
    if str(base)!='/opt/ul-sales-runner/deps/ocr-20261007':
        raise SystemExit('Evidence path is not the isolated OCR validation directory')
    files=[]
    for path in sorted((base/'packages').glob('*.deb')):
        metadata=subprocess.run(['dpkg-deb','-f',str(path),'Package','Version','Architecture'],
            capture_output=True,timeout=10,check=True).stdout.decode().strip()
        files.append({'file':path.name,'sha256':sha256(path.read_bytes()).hexdigest(),
                      'metadata':metadata,'size':path.stat().st_size})
    identities=[]
    paths=[base/'root/usr/bin/tesseract',base/'root/usr/lib/x86_64-linux-gnu/libtesseract.so.5',
           base/'root/usr/lib/x86_64-linux-gnu/liblept.so.5',base/'tesseract-isolated',
           base/'service/core/sales_document_extract.py',base/'service/core/sales_document_pipeline.py']
    paths.extend(sorted((base/'root/usr/share/tesseract-ocr/5/tessdata').glob('*.traineddata')))
    for path in paths:
        identities.append({'path':str(path),'resolved_path':str(path.resolve()),
                           'sha256':sha256(path.read_bytes()).hexdigest(),'size':path.stat().st_size})
    global_packages={}
    for package in ('tesseract-ocr','tesseract-ocr-chi-sim','tesseract-ocr-eng','libtesseract5','liblept5'):
        status=subprocess.run(['dpkg-query','-W','-f=${Status} ${Version}',package],
                              capture_output=True,timeout=10)
        global_packages[package]=status.stdout.decode().strip() if status.returncode==0 else None
    python_version=subprocess.run([str(base/'document-venv/bin/python'),'--version'],
                                  capture_output=True,timeout=10,check=True).stdout.decode().strip()
    pypdf=subprocess.run([str(base/'document-venv/bin/python'),'-c',
                          'import pypdf; print(pypdf.__version__)'],
                         capture_output=True,timeout=10,check=True).stdout.decode().strip()
    evidence={'isolated_directory':str(base),'os_release':Path('/etc/os-release').read_text(),
        'python':python_version,'pypdf':pypdf,'packages':files,'file_identities':identities,
        'global_package_status_after':global_packages,'global_packages_still_absent':
            all(value is None for value in global_packages.values()),
        'system_install_performed':False,'active_collector_venv_modified':False,
        'persistent_worker_enabled':False,'service_restart_performed':False}
    target=base/'evidence/environment.json'
    target.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n');target.chmod(0o600)
    print(json.dumps({'evidence':str(target),'packages':len(files),
        'global_packages_still_absent':evidence['global_packages_still_absent'],'pypdf':pypdf}))


if __name__=='__main__':
    main()
