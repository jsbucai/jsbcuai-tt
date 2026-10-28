"""Authenticated N150 adjustment intake; no browser runs in the API process."""
import json
import time
from .adjustments import AdjustmentJobs, AdjustmentConflict, _payload
from .store import dumps, timestamp


class WebAdjustmentJobs(AdjustmentJobs):
    def __init__(self, store, queries):
        super().__init__(store, queries)
        with store.connect(write=True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS adjustment_web_bindings(
                batch_id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,binding TEXT NOT NULL)''')

    def config(self):
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='adjustment_web_config'").fetchone()
        return json.loads(row[0]) if row else {}

    def enabled(self):
        return self.config().get('enabled') is True

    def status(self):
        result = super().status()
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='adjustment_web_heartbeat'").fetchone()
        alive = bool(row and time.time() - float(row[0]) < 180)
        return dict(result, enabled=self.enabled(), execution_host='N150', worker_running=alive)

    def authorize(self, identity, actor):
        if not actor or not actor.get('id'):
            raise AdjustmentConflict('请登录员工账号后处理库存清单')
        with self.store.connect() as db:
            row = db.execute('SELECT actor_id FROM adjustment_web_bindings WHERE batch_id=?', (identity,)).fetchone()
        if row and row[0] != actor['id']:
            raise AdjustmentConflict('这份报损报溢清单属于另一位员工，请使用原账号')

    def enqueue(self, payload, actor=None):
        normalized = _payload(payload)
        self.authorize(normalized['id'], actor)
        frozen = dumps(normalized)
        config = self.config()
        if config.get('enabled') is not True:
            raise AdjustmentConflict('N150 报损报溢执行器尚未启用，手机清单保留')
        with self.store.connect(write=True) as db:
            previous = db.execute('SELECT * FROM adjustment_batches WHERE id=?', (normalized['id'],)).fetchone()
            if previous:
                owner = db.execute('SELECT actor_id FROM adjustment_web_bindings WHERE batch_id=?', (normalized['id'],)).fetchone()
                if not owner or owner[0] != actor['id']:
                    raise AdjustmentConflict('清单属于另一位员工或旧执行器')
                if previous['payload'] != frozen:
                    raise AdjustmentConflict('该批次标识已用于其他内容，不能重复上传')
                return self._result(previous)
            identities = []
            originals = {str(item.get('sku', '')).strip().upper(): item for item in payload['items']}
            for item in normalized['items']:
                found = self.queries.inventory_lookup(item['sku'])
                if not found.get('found'):
                    raise ValueError(f"商品 {item['sku']} 库存身份不唯一或已缺失，请重新查询")
                if found.get('profile_id') != config['profile_id'] or found.get('warehouse_id') != config['warehouse_id']:
                    raise ValueError('报损报溢仅支持当前江苏优螺仓库')
                identity = {key: str(found.get(key) or '') for key in
                            ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')}
                if not all(identity.values()):
                    raise ValueError(f"商品 {item['sku']} 缺少完整库存身份，请更新商品")
                original = originals[item['sku']]
                for key, value in identity.items():
                    supplied = str(original.get('unit_id' if key == 'base_unit' else key) or '')
                    if supplied and supplied != value:
                        raise AdjustmentConflict(f"商品 {item['sku']} 身份已变化，请重新选择")
                identities.append(dict(identity, sku=item['sku'], product_name=found.get('product_name', '')))
            now = timestamp()
            db.execute('''INSERT INTO adjustment_batches
                (id,client_id,payload,status,message,created_at,updated_at)
                VALUES (?,?,?,'queued',?,?,?)''', (normalized['id'], normalized['client_id'], frozen,
                '已保存至 N150，等待网页上传；如需验证码将通过微信通知负责人', now, now))
            binding = dict(config, items=identities, actor_name=actor.get('display_name', actor['id']))
            db.execute('INSERT INTO adjustment_web_bindings VALUES (?,?,?)',
                       (normalized['id'], actor['id'], dumps(binding)))
            return self._result(self._owned(db, normalized['id'], normalized['client_id']))

    def list_for(self, client_id, actor):
        return [value for value in self.list(client_id) if self._actor_matches(value['id'], actor)]

    def _actor_matches(self, identity, actor):
        try:
            self.authorize(identity, actor)
            return True
        except AdjustmentConflict:
            return False
