"""Owner-only verification bridge, independent of the browser/Codex lane."""
import base64
import hashlib
import secrets
import urllib.parse
import urllib.request
try:
    from tools.erp_human_challenge import Challenge, ROOT
except ImportError:
    from erp_human_challenge import Challenge, ROOT


def ingest(message, fallback, root=ROOT):
    text = ''.join(item.get('text_item', {}).get('text', '') for item in message.get('item_list', [])
                   if item.get('type') == 1)
    if root.exists() and Challenge(root).receive(text, message.get('message_id', '')):
        return
    # Never route verification material to sales analysis even with no worker.
    if text.strip().startswith('验证码'): return
    if fallback: fallback(message)


def upload_image(account, path, request, no_redirect):
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError('verification_image_invalid')
    raw = path.read_bytes()
    if not raw.startswith(b'\x89PNG'): raise ValueError('verification_image_invalid')
    key, filekey = secrets.token_bytes(16), secrets.token_hex(16)
    pad = padding.PKCS7(128).padder(); padded = pad.update(raw) + pad.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    value = request(account['base'], '/ilink/bot/getuploadurl', {
        'filekey': filekey, 'media_type': 1, 'to_user_id': account['ilink_user_id'],
        'rawsize': len(raw), 'rawfilemd5': hashlib.md5(raw).hexdigest(),
        'filesize': len(encrypted), 'no_need_thumb': True, 'aeskey': key.hex()}, account['bot_token'])
    url = value.get('upload_full_url') or ('https://novac2c.cdn.weixin.qq.com/c2c/upload?' +
        urllib.parse.urlencode({'encrypted_query_param': value.get('upload_param', ''), 'filekey': filekey}))
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'novac2c.cdn.weixin.qq.com' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('verification_cdn_not_allowed')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), no_redirect())
    with opener.open(urllib.request.Request(url, data=encrypted,
            headers={'Content-Type': 'application/octet-stream'}), timeout=15) as response:
        download = response.headers.get('x-encrypted-param')
        if response.status != 200 or not download:
            raise ValueError('verification_cdn_receipt_missing')
    return {'type': 2, 'image_item': {'media': {'encrypt_query_param': download,
        'aes_key': base64.b64encode(key.hex().encode()).decode(), 'encrypt_type': 1}, 'mid_size': len(encrypted)}}


def notify(account, state, at, state_path, send, request, no_redirect, root=ROOT):
    if not root.exists(): return
    value = Challenge(root).current()
    if not value: return
    identity = value['id']
    message = ('N150 报损／报溢正在等待 ERP 登录验证，尚未上传单据。\n' +
        ('请查看验证码图片，' if value['has_image'] else '') +
        (f'请回复：验证码 {identity} 验证码内容\n例如“验证码 {identity} 123456”。10分钟内有效，仅用于本次登录。'
         if value['kind'] in {'image', 'code'} else
         '当前是滑块或无法识别的验证，需在原浏览器会话人工完成；不会自动尝试绕过。'))
    event = {'id': 'erp-verification-' + identity, 'kind': 'sales', 'target': {}, 'text': message}
    result = send(account, state, event, at, state_path)
    if value['has_image'] and result['status'] in {'accepted', 'already_accepted'}:
        image_id = event['id'] + '-image'
        if state.get('outbox', {}).get(image_id, {}).get('status') in {'accepted', 'dispatching', 'unknown'}: return
        # Record a bounded preparation attempt too; CDN failures must not create
        # an unbounded request loop while a verification session is waiting.
        attempted = state.setdefault('verification_media_attempts', {})
        if attempted.get(identity, 0) >= 2: return
        attempted[identity] = attempted.get(identity, 0) + 1
        item = upload_image(account, root / (identity + '.png'), request, no_redirect)
        send(account, state, dict(event, id=image_id, item_list=[item]), at, state_path)
