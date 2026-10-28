"""Loopback-only build cache using Python's verified HTTPS transport.

Uses official Maven hosts by default, with optional process-scoped mirror URLs.
No TLS verification is disabled.
Not an application service and not reachable from the LAN.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import hashlib
import shutil
import threading
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1] / 'runtime' / 'maven-cache'
GRADLE_CACHE = Path(os.environ.get('GRADLE_USER_HOME', str(Path.home() / '.gradle'))) / 'caches/modules-2/files-2.1'
HOSTS = {'google': 'https://dl.google.com/dl/android/maven2/',
         'central': 'https://maven-central.storage-download.googleapis.com/maven2/',
         'plugins': 'https://plugins.gradle.org/m2/'}
# Opt-in download mirrors for build environments with blocked upstream access.
# Defaults remain official endpoints; transport always validates HTTPS.
for name in HOSTS:
    override = os.environ.get('UL_MAVEN_' + name.upper() + '_URL')
    if override:
        if not override.startswith('https://'):
            raise ValueError('Maven mirror must use HTTPS')
        HOSTS[name] = override.rstrip('/') + '/'
locks = {}
guard = threading.Lock()

class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self): self.serve(False)
    def do_GET(self): self.serve(True)
    def serve(self, body):
        if self.path == '/health':
            self.send_response(200); self.send_header('Content-Type', 'text/plain; charset=utf-8'); self.end_headers()
            if body: self.wfile.write(b'ul-build-maven-cache')
            return
        parts = self.path.lstrip('/').split('/', 1)
        if len(parts) != 2 or parts[0] not in HOSTS or any(x in parts[1] for x in ('..', '?', '\\', '%')):
            self.send_error(400); return
        key = '/'.join(parts)
        target = ROOT / key
        with guard: lock = locks.setdefault(key, threading.Lock())
        try:
            with lock:
                if not target.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    coordinate = parts[1].split('/')
                    if len(coordinate) >= 4:
                        group = '.'.join(coordinate[:-3])
                        artifact, version, name = coordinate[-3:]
                        checksum = name.endswith('.sha1') or name.endswith('.sha256')
                        source_name = name.rsplit('.', 1)[0] if checksum else name
                        candidates = list((GRADLE_CACHE / group / artifact / version).glob('*/' + source_name))
                        if candidates:
                            if checksum:
                                digest = hashlib.sha1() if name.endswith('.sha1') else hashlib.sha256()
                                with candidates[0].open('rb') as cached:
                                    while chunk := cached.read(1024 * 256): digest.update(chunk)
                                target.write_text(digest.hexdigest(), encoding='ascii')
                            else: shutil.copyfile(candidates[0], target)
                if not target.is_file():
                    temp = target.with_name(target.name + '.download')
                    upstream = HOSTS[parts[0]]
                    # Gradle binds an artifact to the repository that supplied its POM.
                    # A locally cached Central POM must resolve its JAR at Central too.
                    coordinate = parts[1].split('/')
                    group = '.'.join(coordinate[:-3]) if len(coordinate) >= 4 else ''
                    if parts[0] == 'google' and not (group == 'com.android' or group.startswith(('androidx.', 'com.android.', 'com.google.android.', 'com.google.testing.platform'))):
                        upstream = HOSTS['central']
                    for attempt in range(3):
                        try:
                            offset = temp.stat().st_size if temp.exists() else 0
                            request = urllib.request.Request(upstream + parts[1])
                            if offset:
                                request.add_header('Range', f'bytes={offset}-')
                            with urllib.request.urlopen(request, timeout=90) as source:
                                # Resume only when the server confirms the exact offset.
                                # A server ignoring Range returns a fresh complete body.
                                resume = source.status == 206
                                if resume and not source.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                                    raise ValueError('Unexpected Maven download range')
                                received = 0
                                with temp.open('ab' if resume else 'wb') as output:
                                    while chunk := source.read(1024 * 256):
                                        output.write(chunk)
                                        received += len(chunk)
                                expected = source.headers.get('Content-Length')
                                if expected is not None and received != int(expected):
                                    raise OSError('Incomplete Maven download')
                            os.replace(temp, target)
                            break
                        except urllib.error.HTTPError as exc:
                            if exc.code == 416 and attempt < 2:
                                temp.unlink(missing_ok=True)
                                continue
                            raise
                        except Exception:
                            if attempt == 2: raise
            self.send_response(200)
            self.send_header('Content-Length', str(target.stat().st_size))
            self.send_header('Content-Type', 'application/octet-stream')
            self.end_headers()
            if body:
                with target.open('rb') as source:
                    while chunk := source.read(1024 * 256): self.wfile.write(chunk)
        except urllib.error.HTTPError as exc: self.send_error(exc.code)
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as exc:
            print(type(exc).__name__, key, flush=True)
            self.send_error(502)
    def log_message(self, *_): pass

if __name__ == '__main__':
    ROOT.mkdir(parents=True, exist_ok=True)
    print('Verified-TLS Maven cache on 127.0.0.1:5192', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 5192), Handler).serve_forever()
