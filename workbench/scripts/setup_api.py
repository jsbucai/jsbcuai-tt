"""Prepare restricted Linux runtime; accepts password through a terminal, never argv."""
import argparse
import getpass
import json
import os
import shutil
import sys
from pathlib import Path
from werkzeug.security import generate_password_hash

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'service'))
from core.wsgjp_gateway import validate_credentials


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--authorization',type=Path,required=True)
    args=parser.parse_args()
    record=json.loads(args.authorization.read_text(encoding='utf-8-sig'))
    validate_credentials(record['credentials'])
    if record['credentials']['appkey'] != record['token']['appkey']:
        raise ValueError('授权不匹配')
    destination=ROOT/'service/runtime/data/secrets'
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    if any((destination/name).exists() for name in ('erp.json','admin-password.hash','backup.key')):
        raise ValueError('已有配置，不自动覆盖授权、密码或备份密钥')
    password=getpass.getpass('设置管理员密码（至少 12 位）：')
    if len(password)<12 or password!=getpass.getpass('再次输入：'):
        raise ValueError('密码过短或不一致')
    values={'erp.json':json.dumps(record,ensure_ascii=False).encode(),
            'admin-password.hash':generate_password_hash(password).encode(),'backup.key':os.urandom(32)}
    for name,data in values.items():
        fd=os.open(destination/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as file:file.write(data)
    print('已创建受限配置。Linux 部署时将 runtime/data 归属设为容器 UID 10001；备份密钥须另存受限位置。')

if __name__=='__main__': main()
