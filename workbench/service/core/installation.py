"""LAN install page; verifies the atomically published current release record."""
import hashlib
import json
from pathlib import Path
import re
from threading import Lock
import zipfile

from flask import Blueprint, jsonify, render_template, send_file


RELEASE_DIR = Path(__file__).resolve().parents[2] / 'releases'


def install_routes():
    routes = Blueprint('installation', __name__)
    lock, cached = Lock(), {}

    def release():
        with lock:
            try:
                directory = RELEASE_DIR.resolve(strict=True)
                manifest_path = (directory / 'current.json').resolve(strict=True)
                if manifest_path != directory / 'current.json' or manifest_path.stat().st_size > 16384:
                    return None
                # Read on every request: a newly published manifest needs no restart.
                manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
                if not isinstance(manifest, dict):
                    return None
                filename, version = manifest.get('file'), manifest.get('version')
                expected_hash, version_code = manifest.get('sha256'), manifest.get('versionCode')
                if not isinstance(filename, str) or not re.fullmatch(r'UL-Workbench-[0-9A-Za-z.-]+\.apk', filename):
                    return None
                if Path(filename).name != filename:
                    return None
                if not isinstance(version, str) or not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z.-]{0,63}', version):
                    return None
                if not isinstance(expected_hash, str) or not re.fullmatch(r'[0-9A-Fa-f]{64}', expected_hash):
                    return None
                if type(version_code) is not int or version_code < 1:
                    return None
                # Resolve symlinks/junctions too; no release may escape this directory.
                path = (directory / filename).resolve(strict=True)
                if path != directory / filename:
                    return None
                before = path.stat()
                if not path.is_file() or not before.st_size:
                    return None
                identity = (str(path), before.st_mtime_ns, before.st_ctime_ns, before.st_size, before.st_ino)
                if cached.get('identity') != identity:
                    # An incomplete copy must not become an install button.
                    with zipfile.ZipFile(path) as archive:
                        if not {'AndroidManifest.xml', 'classes.dex'}.issubset(archive.namelist()):
                            return None
                    digest = hashlib.sha256()
                    with path.open('rb') as source:
                        for block in iter(lambda: source.read(1024 * 1024), b''):
                            digest.update(block)
                    after = path.stat()
                    if identity != (str(path), after.st_mtime_ns, after.st_ctime_ns, after.st_size, after.st_ino):
                        return None
                    cached.update(identity=identity, sha256=digest.hexdigest())
                if cached['sha256'] != expected_hash.lower():
                    return None
                return {'path': path, 'filename': filename, 'version': version,
                          'version_code': version_code, 'bytes': before.st_size,
                          'size': f'{before.st_size / (1024 * 1024):.1f} MB',
                          'sha256': cached['sha256']}
            except (OSError, ValueError, UnicodeError, zipfile.BadZipFile):
                return None

    @routes.get('/app')
    def page():
        return render_template('install.html', release=release())

    @routes.get('/app/download')
    def download():
        current = release()
        if not current:
            return jsonify(ok=False, error='app_building', message='安装包正在构建，请稍后刷新安装页'), 404
        response = send_file(current['path'], mimetype='application/vnd.android.package-archive',
                             as_attachment=True, download_name=current['filename'],
                             conditional=True, etag=current['sha256'], max_age=0)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-APK-SHA256'] = current['sha256']
        return response

    return routes
