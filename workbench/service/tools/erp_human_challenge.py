"""One-use owner-supplied login verification. Never send codes to a model."""
import json
import os
from pathlib import Path
import re
import time
import uuid

ROOT = Path('/var/lib/ul-erp-human')


def save(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    with temporary.open('x') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False); stream.flush(); os.fsync(stream.fileno())
    if os.geteuid() == 0: os.chown(temporary, 10001, 10001)
    os.replace(temporary, path)


class Challenge:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise ValueError('verification_directory_not_private')

    def current(self):
        try:
            value = json.loads((self.root / 'request.json').read_text())
        except (OSError, ValueError): return None
        return value if value.get('status') == 'waiting' and value.get('expires', 0) > time.time() else None

    def create(self, job_id, kind, image=None):
        identity = uuid.uuid4().hex[:10]
        value = {'id': identity, 'job_id': str(uuid.UUID(job_id)), 'kind': kind,
                 'status': 'waiting', 'expires': time.time() + 600, 'has_image': image is not None}
        (self.root / 'answer.json').unlink(missing_ok=True)
        if image is not None:
            if not image.startswith(b'\x89PNG') or len(image) > 1024 * 1024:
                raise ValueError('verification_image_invalid')
            image_path = self.root / (identity + '.png')
            with image_path.open('xb') as stream:
                os.chmod(image_path, 0o600); stream.write(image)
        save(self.root / 'request.json', value)
        return value

    def receive(self, text, message_id):
        # Consume even stale/malformed verification commands; never fall through
        # to the sales/Codex inbox. An explicit challenge id prevents late replay.
        if not text.strip().startswith('验证码'): return False
        request = self.current()
        match = re.fullmatch(r'验证码\s+([0-9a-f]{10})\s+([A-Za-z0-9]{4,10})', text.strip())
        if request and request['kind'] in {'image', 'code'} and match and match[1] == request['id']:
            path = self.root / 'answer.json'
            if not path.exists(): save(path, {'id': match[1], 'code': match[2], 'message_id': str(message_id)})
        return True

    def answer(self, identity):
        path = self.root / 'answer.json'
        try: value = json.loads(path.read_text())
        except (OSError, ValueError): return None
        path.unlink(missing_ok=True)
        request = self.current()
        if request and request['id'] == identity == value.get('id'):
            return value['code']
        return None

    def close(self, identity, status):
        path = self.root / 'request.json'
        try: value = json.loads(path.read_text())
        except (OSError, ValueError): return
        if value.get('id') == identity:
            value['status'] = status; save(path, value)
            (self.root / 'answer.json').unlink(missing_ok=True)
            (self.root / (identity + '.png')).unlink(missing_ok=True)


def controls(page):
    """Only explicit visible verification fields on official HTTPS frames."""
    from purchase_export_collector.collector import _allowed_https
    found = []
    for candidate in page.context.pages:
        for frame in candidate.frames:
            if not _allowed_https(frame.url): continue
            locator = frame.locator('input:not([type="password"]):not([type="hidden"])')
            for item in locator.all():
                if not item.is_visible(): continue
                hint = ' '.join(item.get_attribute(name) or '' for name in ('placeholder', 'aria-label', 'name', 'id'))
                if re.search(r'验证码|captcha|verifycode|validatecode|checkcode|vcode', hint, re.I):
                    found.append((frame, item))
    return found[0] if len(found) == 1 else (None, None)


def wait_login(page, job_id, progress, root=ROOT):
    from purchase_export_collector.collector import _ready_erp_page, CollectorError
    challenge = Challenge(root)
    deadline = time.monotonic() + 600
    frame, field = controls(page)
    if frame is None:
        hints = ' '.join(candidate.locator('body').inner_text(timeout=2000)[:10000]
                         for candidate in page.context.pages if not candidate.is_closed())
        if not any(word in hints for word in ('滑块', '验证', '验证码')):
            raise CollectorError('login_not_completed_without_verification')
    image = None
    if frame is not None:
        images = [item for item in frame.locator('img').all() if item.is_visible() and
                  re.search(r'captcha|verify|validate|checkcode|vcode|验证码',
                    (item.get_attribute('src') or '') + (item.get_attribute('id') or '') +
                    (item.get_attribute('alt') or ''), re.I)]
        if len(images) == 1: image = images[0].screenshot(type='png')
    value = challenge.create(job_id, 'image' if image else 'code' if field is not None else 'manual', image)
    progress('等待验证码，已请求微信通知负责人；保留当前登录会话（10分钟）')
    submitted = False
    try:
        while time.monotonic() < deadline:
            ready = _ready_erp_page(page)
            if ready is not None:
                challenge.close(value['id'], 'completed')
                return ready
            code = challenge.answer(value['id'])
            if code and not submitted:
                current_frame, current_field = controls(page)
                if current_frame is None or current_frame != frame:
                    raise CollectorError('verification_page_changed')
                buttons = [button for name in ('确定', '验证', '登录', '登 录', '提交验证')
                           for button in frame.get_by_role('button', name=name, exact=True).all()
                           if button.is_visible()]
                if len(buttons) != 1:
                    raise CollectorError('verification_submit_ambiguous')
                current_field.fill(code)
                code = None
                submitted = True
                challenge.close(value['id'], 'submitted')
                buttons[0].click()
                progress('验证码已填写，等待 ERP 登录结果')
                # One attempt only. Wrong/expired code becomes a known pre-upload
                # failure; no repeated guessing or stale code replay.
                deadline = min(deadline, time.monotonic() + 45)
            page.wait_for_timeout(1000)
        raise CollectorError('manual_verification_expired_or_failed')
    finally:
        if challenge.current(): challenge.close(value['id'], 'expired')
