"""One-off delivery confirmations never become a future inference rule."""
import copy
import json
import tempfile
from pathlib import Path

import pytest
from tests.test_erp_source import Client
from core.erp_source import LiveSource
from core.order_lifecycle import OrderLifecycle
from core.delivery_confirmations import install, load, resolve, document_identity
from core.api_source import validate_snapshot
from core.store import Store
from core.wms_cache import WmsCache
from core.fields import order_fields


@pytest.fixture
def prepared():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runtime') as path:
        store = Store(path)
        client = Client()
        original = client.call
        details = original('erp.ewms.stockoutbill.list', {'timetype':0, 'pagesize':200})['orders'][0]
        details['summary'] = '普通摘要，无交期'
        def call(method, params):
            if method == 'erp.ewms.stockoutbill.list':
                return {'orders': [copy.deepcopy(details)], 'totalresults':1}
            return original(method, params)
        client.call = call
        lifecycle = OrderLifecycle(store)
        source = LiveSource(client, warehouse_id='20', order_lifecycle=lifecycle)
        facts = validate_snapshot(source.collect())
        with store.connect(write=True) as db:
            lifecycle.persist(db, facts['order_lifecycle'])
        WmsCache(store).commit('10', ['A#2'], [details], '2026-10-23T14:00:00+08:00')
        row = {'order_no':'A#2', 'document_id':'50', 'order_date':'2026-09-24',
               'delivery_date':'2026-10-09', 'identity_hash': document_identity(details)}
        yield store, client, source, details, row


def test_scoped_date_survives_restart_without_mutating_summary_or_historical_cache(prepared):
    store, client, source, details, row = prepared
    assert install(store, '10', [row], 'approved-18', '用户本次确认')['installed'] == 1
    assert install(store, '10', [row], 'approved-18', '用户本次确认')['installed'] == 0
    for _ in range(2):
        source.order_lifecycle = OrderLifecycle(Store(store.runtime))
        facts = validate_snapshot(source.collect())
        json.dumps(facts)
        assert facts['orders'][0]['delivery_date'] == '2026-10-09'
        assert facts['orders'][0]['lines'][0]['delivery_date'] == '2026-10-09'
        display = facts['records']['orders'][0]
        assert display['summary'] == details['summary']
        assert display['delivery_date_status'] == 'known'
        assert '交期待确认' not in display['print_warnings']
        assert order_fields(display)['deliveryDate'] == '2026-10-09'
        assert not any(i['code'].startswith('delivery_date_') for i in facts['calculation_issues'])
        with store.connect(write=True) as db:
            OrderLifecycle.persist(db, facts['order_lifecycle'])
            assert db.execute('SELECT count(*) FROM erp_order_delivery_cache').fetchone()[0] == 0
    assert install(store, '10', [row], 'approved-18', '用户本次确认')['installed'] == 0


@pytest.mark.parametrize('change', ['number', 'document', 'date', 'product', 'customer', 'warehouse'])
def test_recreated_or_other_orders_cannot_inherit_date(prepared, change):
    store, _, _, details, row = prepared
    install(store, '10', [row], 'approved-18', '用户本次确认')
    number, order_date = row['order_no'], row['order_date']
    details = copy.deepcopy(details)
    if change == 'number': number = 'A#3'
    if change == 'document': details['vchcode'] = '51'
    if change == 'date': order_date = '2026-09-25'
    if change == 'product': details['goodslist'][0]['ptypeid'] = '99'
    if change == 'customer': details['receiveraddress'] = '另一客户'
    if change == 'warehouse': details['ktypeid'] = '99'
    assert resolve(load(store, '10'), number, order_date, details) is None
    assert resolve(load(store, 'other-profile'), row['order_no'], row['order_date'], details) is None


@pytest.mark.parametrize('suffix,expected', [
    ('#交期=2026-11-20', '2026-11-20'), ('#交期=2026-02-30', None)])
def test_explicit_erp_date_has_priority_and_invalid_date_is_not_hidden(prepared, suffix, expected):
    store, _, source, details, row = prepared
    install(store, '10', [row], 'approved-18', '用户本次确认')
    details['summary'] += suffix
    facts = validate_snapshot(source.collect())
    assert facts['orders'][0]['delivery_date'] == expected
    assert not facts['records']['orders'][0].get('delivery_date_confirmation_id')


def test_unconfirmed_active_order_stays_missing_even_with_historical_cache(prepared):
    store, _, source, _, row = prepared
    from core.order_lifecycle import import_delivery_dates
    import_delivery_dates(store, '10', {row['order_no']: row['delivery_date']})
    facts = source.collect()
    assert facts['orders'][0]['delivery_date'] is None
    assert any(i['code'] == 'delivery_date_missing' for i in facts['calculation_issues'])


def test_install_is_atomic_and_rejects_scope_changes(prepared):
    store, _, _, _, row = prepared
    with pytest.raises(ValueError, match='当前未交'):
        install(store, '10', [row, {**row, 'order_no':'missing'}], 'approved-18', '用户本次确认')
    assert load(store, '10') == {}
    install(store, '10', [row], 'approved-18', '用户本次确认')
    with pytest.raises(ValueError, match='拒绝覆盖'):
        install(store, '10', [{**row, 'delivery_date':'2026-11-01'}], 'approved-18', '用户本次确认')
