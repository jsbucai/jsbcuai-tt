import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import zipfile

from flask import Flask
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import installation


@pytest.fixture
def release_app(monkeypatch):
    with tempfile.TemporaryDirectory(prefix='install-test-', dir=ROOT / 'runtime') as temp:
        directory = Path(temp)
        apk = directory / 'UL-Workbench-0.1.0-dev.apk'
        monkeypatch.setattr(installation, 'RELEASE_DIR', directory)
        app = Flask(__name__, template_folder=str(ROOT / 'templates'))
        app.register_blueprint(installation.install_routes())
        yield app.test_client(), apk


def package(content):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w') as archive:
        archive.writestr('AndroidManifest.xml', b'fixture manifest')
        archive.writestr('classes.dex', content)
    return result.getvalue()


def publish(apk, contents, **changes):
    apk.write_bytes(contents)
    record = {'version': '0.1.0-dev', 'file': apk.name,
              'sha256': hashlib.sha256(contents).hexdigest(), 'versionCode': 1}
    record.update(changes)
    (apk.parent / 'current.json').write_text(json.dumps(record), encoding='utf-8')
    return record


def test_missing_or_partial_apk_never_offers_download(release_app):
    client, apk = release_app
    for data in [None, b'', b'PK-partial-copy']:
        if data is not None:
            publish(apk, data)
        page = client.get('/app')
        assert page.status_code == 200
        assert '正在构建' in page.get_data(as_text=True)
        assert 'href="/app/download"' not in page.get_data(as_text=True)
        assert client.get('/app/download').status_code == 404


def test_ready_apk_is_current_attachment_with_actual_hash_and_hot_reload(release_app):
    client, apk = release_app
    for index, contents in enumerate([package(b'first'), package(b'second-complete-release')]):
        apk = apk.with_name(f'UL-Workbench-0.1.{index}-dev.apk')
        record = publish(apk, contents, version=f'0.1.{index}-dev', versionCode=index+1)
        digest = record['sha256']
        html = client.get('/app').get_data(as_text=True)
        assert record['version'] in html and digest in html and '已就绪' in html
        assert 'href="/app/download"' in html
        assert '业务数据需登录后才能访问' in html
        response = client.get('/app/download?file=../../app.py')
        assert response.status_code == 200 and response.data == contents
        assert response.headers['Content-Disposition'].startswith('attachment;')
        assert apk.name in response.headers['Content-Disposition']
        assert response.headers['X-APK-SHA256'] == digest
        assert response.mimetype == 'application/vnd.android.package-archive'
        response.close()
    assert client.get('/app/download/app.py').status_code == 404


def test_invalid_manifest_paths_and_hashes_disable_download(release_app):
    client, apk = release_app
    record = publish(apk, package(b'complete'))
    # Warm checksum cache too: invalid manifest updates must take effect immediately.
    assert client.get('/app').status_code == 200
    invalid = [None, {}, {**record, 'file':'../UL-Workbench-0.1.0-dev.apk'},
               {**record, 'file':'..\\UL-Workbench-0.1.0-dev.apk'},
               {**record, 'file':str(apk.resolve())}, {**record, 'file':'other.apk'},
               {**record, 'file':'UL-Workbench-0.1.0-dev.apk:secret'},
               {**record, 'sha256':'0'*64}, {**record, 'sha256':'invalid'},
               {**record, 'versionCode':True}, {**record, 'versionCode':0},
               {**record, 'version':'<script>alert(1)</script>'}]
    for value in invalid:
        (apk.parent / 'current.json').write_text(json.dumps(value), encoding='utf-8')
        assert client.get('/app/download').status_code == 404
        assert 'href="/app/download"' not in client.get('/app').get_data(as_text=True)
    (apk.parent / 'current.json').write_text('{"version":', encoding='utf-8')
    assert client.get('/app/download').status_code == 404
    (apk.parent / 'current.json').unlink()
    assert client.get('/app/download').status_code == 404  # No legacy-file fallback.


def test_release_symlink_escape_is_rejected(release_app, monkeypatch):
    client, apk = release_app
    publish(apk, package(b'complete'))
    original_resolve = Path.resolve
    def escaped(path, *args, **kwargs):
        if path == apk:
            return apk.parent.parent / apk.name
        return original_resolve(path, *args, **kwargs)
    # Simulate a symlink/junction resolution on Windows without elevated link creation.
    monkeypatch.setattr(Path, 'resolve', escaped)
    assert client.get('/app/download').status_code == 404
