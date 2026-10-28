"""HTTPS-only ERP adapter with locked, atomically persisted portable authorization."""
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from .wsgjp_gateway import Client, ApiError, compact, business_signature, post, validate_credentials

READ_METHODS = frozenset({'erp.stock.list', 'erp.ptype.list', 'erp.goodsstocks.list',
    'erp.btype.list', 'erp.employee.listall',
    'erp.freight.list', 'erp.ewms.stockoutbill.list', 'erp.ewms.stockoutbill.query',
    'erp.bill.billcenterlist', 'qimen.taobao.erp.order.sync'})
LOGISTICS_METHOD = 'beefun.orderfreight.updatefreight'


class ErpUnavailable(Exception):
    def __init__(self, message, category='erp_unavailable', code=None):
        self.category, self.code = category, code
        super().__init__(message)


class ErpClient:
    def __init__(self, vault, transport=post, allow_logistics=False):
        self.vault = Path(vault)
        self.transport, self.allow_logistics = transport, allow_logistics
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.last_auth_error = None

    def _record(self):
        if self.vault.is_symlink():
            raise ErpUnavailable('授权文件不得为符号链接')
        try:
            if self.vault.stat().st_mode & 0o077:
                raise ErpUnavailable('授权文件必须仅服务账号可读写（600）')
            record = json.loads(self.vault.read_text(encoding='utf-8-sig'))
            validate_credentials(record['credentials'])
            if record['credentials']['appkey'] != record['token']['appkey'] or not record['token']['profileid']:
                raise ErpUnavailable('授权账套不匹配')
            return record
        except ErpUnavailable:
            raise
        except Exception:
            raise ErpUnavailable('授权文件缺失或格式无效') from None

    def _ready(self):
        record = self._record()
        if float(record['token']['expires_at']) > time.time() + 600:
            return record
        return self._refresh(record)

    def _refresh(self, record):
        c, old = record['credentials'], record['token']
        try:
            new = Client(c, transport=self.transport).refresh(old['refresh_token'])
            if str(new['profileid']) != str(old['profileid']):
                raise ErpUnavailable('续期返回了其他账套')
            # Same seconds-based lifetime contract as the original connection tool.
            if not new.get('refresh_token'):
                new['refresh_token'] = old['refresh_token']
            new['received_at'] = time.time()
            new['expires_at'] = time.time() + float(new['expires_in'])
            record['token'] = new
            temporary = self.vault.with_name(self.vault.name + '.tmp')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(record, f, ensure_ascii=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(temporary, self.vault)
            self.last_auth_error = None
            return record
        except ErpUnavailable:
            raise
        except Exception:
            raise ErpUnavailable('ERP 授权续期失败，请重新授权；未记录凭据内容') from None

    def account(self):
        with self.lock:
            return str(self._ready()['token']['profileid'])

    def call(self, method, params):
        if method not in READ_METHODS and not (method == LOGISTICS_METHOD and self.allow_logistics):
            raise ErpUnavailable('此 ERP 方法未启用或未完成接入验收')
        if {'appkey','token','method','timestamp','sign'} & params.keys():
            raise ValueError('业务参数不能覆盖公共参数')
        # Hold through transport to serialize token use/rotation and bound concurrency.
        with self.lock:
            record = self._ready(); c, t = record['credentials'], record['token']
            def payload_for(active):
                payload = {k:compact(v) if isinstance(v,(dict,list,bool)) else str(v) for k,v in params.items()}
                payload.update(appkey=c['appkey'], token=active['auth_token'], method=method,
                               timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                payload['sign'] = business_signature(payload, c['signkey'])
                return payload
            try:
                return self.transport('/allnewapi', payload_for(t))
            except ApiError as error:
                # Another authorized client may rotate the shared token before
                # its recorded expiry. A read can safely refresh and retry once.
                if method in READ_METHODS and error.code in {13, 15}:
                    try:
                        record = self._refresh(record)
                        return self.transport('/allnewapi', payload_for(record['token']))
                    except Exception:
                        pass
                # A write exception never means the business request failed safely.
                raise ErpUnavailable('ERP 请求未取得可确认结果', error.category, error.code) from None

    def pages(self, method, params, collection, identity, size=100, limit=200):
        output, seen, expected = [], set(), None
        for page in range(1, limit + 1):
            result = self.call(method, dict(params, pageno=page, pagesize=size))
            items = result.get(collection) if isinstance(result, dict) else None
            total = result.get('totalresults') if isinstance(result, dict) else None
            if not isinstance(items, list) or isinstance(total, bool) or not str(total).isdigit():
                raise ErpUnavailable('分页响应结构不完整')
            if expected is not None and expected != int(total):
                raise ErpUnavailable('分页期间总数变化，请重新同步')
            expected = int(total)
            for row in items:
                ident = identity(row)
                if not ident or ident in seen:
                    raise ErpUnavailable('分页存在缺失或重复标识，未发布快照')
                seen.add(ident); output.append(row)
            if len(output) == expected:
                return output
            if len(output) > expected or len(items) != size:
                raise ErpUnavailable('分页数量不闭合，未发布快照')
        raise ErpUnavailable('分页超过上限，未发布部分数据')

    def update_logistics(self, target):
        result = self.call(LOGISTICS_METHOD, {'freightinfos':[{
            k:target[k] for k in ('tradeid','freightname','freightcode','freightnumber')}]})
        items = result.get('result') if isinstance(result, dict) else None
        if not isinstance(items, list) or len(items) != 1 or items[0].get('tradeid') != target['tradeid']:
            raise ErpUnavailable('物流响应未匹配该订单')
        if items[0].get('issuccess') is True:
            return True
        if items[0].get('issuccess') is False:
            return False
        raise ErpUnavailable('物流单笔结果不明确')

    def start_auth_maintenance(self):
        def loop():
            while not self.stop_event.is_set():
                try:
                    with self.lock: self._ready()
                except Exception:
                    self.last_auth_error = 'ERP 授权不可用，请检查服务端授权配置'
                self.stop_event.wait(60)
        threading.Thread(target=loop, name='erp-authorization', daemon=True).start()
