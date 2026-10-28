"""Download only documented Weixin CDN media; never forward Bot credentials."""
import base64
from pathlib import Path
import re
import urllib.parse
import urllib.request
import urllib.error
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.padding import PKCS7

MAX_BYTES=10*1024*1024
CDN='https://novac2c.cdn.weixin.qq.com/c2c'


class MediaError(ValueError): pass


def media_url(media):
    url=media.get('full_url')
    if not url:
        query=media.get('encrypt_query_param')
        if not isinstance(query,str) or not query or len(query)>16384: raise MediaError('微信附件缺少下载引用，请重发原文件。')
        url=CDN+'/download?'+urllib.parse.urlencode({'encrypted_query_param':query})
    if not isinstance(url,str) or len(url)>20000: raise MediaError('微信附件地址无效。')
    parsed=urllib.parse.urlsplit(url)
    if (parsed.scheme!='https' or parsed.hostname!='novac2c.cdn.weixin.qq.com' or parsed.port not in (None,443)
            or parsed.username or parsed.password or parsed.path!='/c2c/download' or parsed.fragment):
        raise MediaError('附件来源不是允许的微信文件通道，请在微信直接发送图片或原文件。')
    return url


def aes_key(media,image_hex=''):
    try:
        if image_hex:
            if not isinstance(image_hex,str) or not re.fullmatch('[0-9a-fA-F]{32}',image_hex): raise ValueError()
            return bytes.fromhex(image_hex)
        encoded=media.get('aes_key','')
        if not isinstance(encoded,str) or len(encoded)>128: raise ValueError()
        raw=base64.b64decode(encoded,validate=True)
        if len(raw)==16:return raw
        if len(raw)==32 and re.fullmatch(b'[0-9a-fA-F]{32}',raw):return bytes.fromhex(raw.decode())
    except (ValueError,TypeError): pass
    raise MediaError('微信附件解密信息不完整，请重发原文件。')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): raise MediaError('微信附件下载发生异常跳转，请重发文件。')


def fetch(url):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(urllib.request.Request(url),timeout=20) as response:
            if response.status!=200: raise MediaError('微信附件尚未下载成功，请稍后重发。')
            raw=response.read(MAX_BYTES+33)
    except (OSError,TimeoutError,urllib.error.URLError):
        raise MediaError('微信附件下载未完成，原消息已保留；请稍后重发文件，不会因此提交订单。') from None
    if not raw or len(raw)>MAX_BYTES+16:raise MediaError('附件为空或超过10MiB，请拆分后发送。')
    return raw


def download(item,fetcher=fetch):
    kind=item.get('type');info=item.get('image_item' if kind==2 else 'file_item',{})
    if kind not in (2,4) or not isinstance(info,dict):raise MediaError('当前录单支持文字、图片和Excel，请发送这些格式。')
    media=info.get('media')
    if not isinstance(media,dict):raise MediaError('微信附件缺少原文件，请重新发送。')
    key=None
    if kind==4 or info.get('aeskey') or media.get('aes_key'):
        key=aes_key(media,info.get('aeskey','') if kind==2 else '')
    raw=fetcher(media_url(media))
    if len(raw)>MAX_BYTES+16:raise MediaError('附件超过10MiB。')
    if key:
        try:
            decryptor=Cipher(algorithms.AES(key),modes.ECB()).decryptor()
            padded=decryptor.update(raw)+decryptor.finalize()
            unpadder=PKCS7(128).unpadder();raw=unpadder.update(padded)+unpadder.finalize()
        except ValueError:raise MediaError('微信附件解密失败，请重发原文件。') from None
    if not raw or len(raw)>MAX_BYTES:raise MediaError('附件为空或超过10MiB。')
    if kind==2:
        if raw.startswith(b'\x89PNG\r\n\x1a\n'): suffix='.png'
        elif raw.startswith(b'\xff\xd8\xff'):suffix='.jpg'
        elif raw[:4]==b'RIFF' and raw[8:12]==b'WEBP':suffix='.webp'
        else:raise MediaError('图片格式暂不支持，请发JPEG或PNG原图。')
        name='微信图片'+suffix
    else:
        name=Path(str(info.get('file_name','')).replace('\\','/')).name
        if Path(name).suffix.lower() not in ('.xls','.xlsx','.png','.jpg','.jpeg','.webp'):
            raise MediaError('当前微信录单支持图片和XLS/XLSX；请将其他资料转为这些格式。')
        expected=info.get('len')
        if expected not in (None,''):
            if not str(expected).isdigit() or int(expected)!=len(raw):raise MediaError('附件长度与微信记录不符，请重发完整文件。')
    if not name or len(name)>160 or any(ord(c)<32 for c in name):raise MediaError('附件名称无效。')
    # Content validation and Excel decompression limits remain owned by SalesAssistant.
    return name,raw
