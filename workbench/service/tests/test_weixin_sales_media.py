import base64
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.padding import PKCS7
from tools.weixin_sales_media import download,media_url,MediaError,MAX_BYTES


def encrypted(raw,key):
    pad=PKCS7(128).padder();data=pad.update(raw)+pad.finalize()
    crypt=Cipher(algorithms.AES(key),modes.ECB()).encryptor()
    return crypt.update(data)+crypt.finalize()


@pytest.mark.parametrize('hex_ascii',[False,True])
def test_official_file_key_formats_and_plaintext_length(hex_ascii):
    key=b'0123456789abcdef';raw=b'xlsx-binary-file'
    item={'type':4,'file_item':{'file_name':'订单.xlsx','len':str(len(raw)),
          'media':{'encrypt_query_param':'encrypted-secret','aes_key':base64.b64encode(key.hex().encode() if hex_ascii else key).decode()}}}
    urls=[]
    def fetch(url):urls.append(url);return encrypted(raw,key)
    assert download(item,fetch)==('订单.xlsx',raw)
    assert urls[0]=='https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=encrypted-secret'
    item['file_item']['len']='99'
    with pytest.raises(MediaError,match='长度'):download(item,fetch)


def test_image_hex_key_and_no_secret_in_error():
    key=b'0123456789abcdef';raw=b'\x89PNG\r\n\x1a\nimage'
    item={'type':2,'image_item':{'aeskey':key.hex(),'media':{'encrypt_query_param':'secret'}}}
    assert download(item,lambda _:encrypted(raw,key))==('微信图片.png',raw)
    with pytest.raises(MediaError,match='解密失败'):download(item,lambda _:b'invalid-padding')


@pytest.mark.parametrize('url',[
    'http://novac2c.cdn.weixin.qq.com/c2c/download','https://evil.example/c2c/download',
    'https://novac2c.cdn.weixin.qq.com.evil.example/c2c/download',
    'https://user@novac2c.cdn.weixin.qq.com/c2c/download',
    'https://novac2c.cdn.weixin.qq.com:8443/c2c/download',
    'https://novac2c.cdn.weixin.qq.com/c2c/other'])
def test_media_reference_cannot_fetch_arbitrary_host(url):
    with pytest.raises(MediaError):media_url({'full_url':url})


def test_large_or_unsupported_files_fail_before_business_pipeline():
    item={'type':2,'image_item':{'media':{'encrypt_query_param':'x'}}}
    with pytest.raises(MediaError,match='10MiB'):download(item,lambda _:b'a'*(MAX_BYTES+17))

