"""ERP code reuse must never make a deleted product current inventory."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.erp_source import LiveSource, START
from core.erp_cache import ReferenceCache
from core.store import Store
from core.queries import Queries
from core.print_jobs import print_readiness
from test_erp_source import Client
from test_api_query_contract import app, insert, stock, IDENTITY


class ReusedCodeClient(Client):
    def __init__(self):
        super().__init__()
        self.increment = []

    def call(self, method, params):
        if method == 'erp.ptype.list':
            if params['begintime'] != START:
                self.calls.append((method, params))
                rows = copy.deepcopy(self.increment)
            else:
                rows = super().call(method, params)['ptypes'] + [
                    {'ptypeid': '999', 'usercode': 'LS01', 'fullname': '已删除旧规格',
                     'standard': 'M42', 'deleted': True, 'skus': [],
                     'picurl': 'https://erp.qiniu.wsgjp.com/deleted.png'},
                    {'ptypeid': '998', 'usercode': 'DELETED-ONLY', 'deleted': '1',
                     'fullname': '已删除有SKU', 'skus': [{'skuid': '997'}]}]
            return {'ptypes': rows, 'totalresults': len(rows)}
        return super().call(method, params)


def test_full_incremental_restart_and_reconcile_exclude_tombstones(app):
    store = app.extensions['service']['store']
    cache = ReferenceCache(store)
    client = ReusedCodeClient()
    for mode in ('full', 'incremental', 'restart', 'reconcile'):
        source = LiveSource(client, warehouse_id='20', reference_cache=ReferenceCache(store))
        snapshot = source.collect(full_reconcile=mode == 'reconcile')
        rows = snapshot['records']['inventory']
        assert {r['product_id'] for r in rows} == {'30', '32', '34'}
        assert next(r for r in rows if r['sku'] == 'LS01')['spec'] == 'M10'
        assert snapshot['product_images'] == []
        assert len(snapshot['inventory']) == 2
        assert all(998 not in p.get('ptypeids', []) and 999 not in p.get('ptypeids', [])
                   for method, p in client.calls if method == 'erp.goodsstocks.list')
        # The raw ERP identity evidence is retained, not rewritten as the new SKU.
        assert len(cache.get_entry('catalog-v1', '10')['payload']['products']) == 5


def test_incremental_delete_is_retained_as_evidence_but_not_projected(app):
    store = app.extensions['service']['store']; client = ReusedCodeClient()
    source = LiveSource(client, warehouse_id='20', reference_cache=ReferenceCache(store))
    source._references('10')
    client.increment = [{'ptypeid':'32', 'usercode':'LS02', 'deleted':' true ', 'skus':[]}]
    _, current, _ = source._references('10')
    assert {p['ptypeid'] for p in current} == {'30', '34'}
    cached = ReferenceCache(store).get_entry('catalog-v1', '10')['payload']['products']
    assert next(p for p in cached if p['ptypeid'] == '32')['deleted'] == ' true '


@pytest.mark.parametrize('flag', [True, 1, 'true', ' TRUE ', '1'])
@pytest.mark.parametrize('field', ['is_deleted', 'deleted'])
def test_deleted_products_are_excluded_before_query_pagination_and_totals(app, field, flag):
    active = stock('active', spec='M60*2')
    old = {**stock('old', spec='M42*1.5'), 'product_id':'old-product',
           'sku_id':'', field:flag, 'stock_quantity':'99', 'available_stock':'99'}
    insert(app, 'inventory', old, active)
    response = app.test_client().get('/api/v2/inventory?sku=LS001&page_size=1').get_json()
    assert response['total'] == 1 and response['items'][0]['id'] == 'active'
    assert response['total_stock'] == 12 and response['total_available'] == 2
    queries = app.extensions['service']['queries']
    assert queries.inventory_lookup('LS001')['id'] == 'active'
    assert queries.inventory_lookup('LS001', {**IDENTITY, 'product_id':'old-product'})['found'] is False
    fallback = Queries(app.extensions['service']['store']).inventory({'sku':'LS001'})
    assert fallback['total'] == 1 and fallback['total_stock'] == 12
    assert print_readiness('inventory', old)[0] is False


@pytest.mark.parametrize('flag', [False, 0, '', 'false', '0', None])
def test_valid_zero_stock_and_stopped_products_are_not_mistaken_for_deleted(app, flag):
    valid = {**stock('valid', is_deleted=flag, status='停用'), 'stock_quantity':'0', 'available_stock':'0'}
    insert(app, 'inventory', valid)
    response = app.test_client().get('/api/v2/inventory?sku=LS001').get_json()
    assert response['total'] == 1 and response['items'][0]['status'] == '停用'


def test_two_current_identities_with_same_code_remain_ambiguous(app):
    insert(app, 'inventory', stock('a'), {**stock('b'), 'product_id':'other'})
    queries = app.extensions['service']['queries']
    assert queries.inventory_lookup('LS001')['ambiguous'] is True
    assert app.test_client().get('/api/v2/inventory?sku=LS001').get_json()['total'] == 2
