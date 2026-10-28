"""Read adapter: real envelope conventions, no fabricated allocation facts."""
import copy
import sys
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.erp_source import LiveSource, QIMEN
from core.api_source import validate_snapshot, SourceIncomplete, publish
from core.classification_pipeline import calculate_query_snapshot
from core.erp_api import ErpUnavailable
from core.erp_cache import ReferenceCache
from core.rules import note_delivery_date, parse_note
from core.store import Store
from core.wms_cache import WmsCache
from core.order_lifecycle import OrderLifecycle, import_delivery_dates

class Client:
    def __init__(self):
        self.calls = []
        self.orders = [{'trade_order': {'trade_id': 'A#2', 'trade_time': '2026-09-24',
            'warehouse_id': '20', 'warehouse_name': '主仓', 'extendProps': {'customer_shop_account':'买家'}},
            'order_info': {'order_info': [{'recid':'40','spec_code':'LS01','goods_name':'螺栓', 'sell_count':'5'}]}}]
        self.shipped_orders = []
        self.exact_orders = {}
        self.fail_details = False
        self.stock_duplicate = False
        self.receipt_bills = []
        self.stock_qty = 10
        self.stock_saleable = 17
        self.stock_extra = []
    def account(self): return '10'
    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'erp.stock.list':
            return {'stocks':[{'id':'20','profileid':'10','fullname':'主仓'},
                              {'id':'99','profileid':'10','fullname':'忽略仓'}], 'totalresults':2}
        if method == 'erp.ptype.list':
            return {'ptypes':[
                {'ptypeid':'30','usercode':'LS01','fullname':'螺栓','unit1':'',
                 'standard':'M10','type':'8.8级发黑','area':'碳钢','comment':'A01',
                 'isclass':False,'skus':[{'skuid':'31'}]},
                {'ptypeid':'32','usercode':'LS02','fullname':'零库存螺栓','unit1':'',
                 'standard':'M12','type':'10.9级发黑','area':'40Cr','comment':'B02',
                 'isclass':False,'skus':[{'skuid':'33'}]},
                {'ptypeid':'34','usercode':'BASE1','fullname':'无SKU商品','unit1':'',
                 'standard':'M14','type':'发黑','area':'碳钢','comment':'C03',
                 'isclass':False,'skus':[]},
            ], 'totalresults':3}
        if method == QIMEN:
            props = params['extendProps']
            if props.get('trade_id'):
                source = self.exact_orders.get(props['trade_id'], [])
            elif props.get('bill_status') == '6':
                source = self.shipped_orders
            else:
                source = self.orders
            begin = (params['pageNo']-1)*100
            page = source[begin:begin+100]
            return {'code':'0','TotalResults':len(source),'totalResults':str(len(page)),
                    'trade_orders':{'trade_orders':copy.deepcopy(page)}}
        if method == 'erp.freight.list':
            return {'freights':[]}
        if method == 'erp.ewms.stockoutbill.list':
            assert params['timetype'] in {0, 1} and params['pagesize'] == 200
            if self.fail_details: raise ErpUnavailable('unavailable')
            return {'orders':[{'vchcode':'50','tradeid':'A#2','ktypeid':'20','summary':'PO#P#2#D#螺栓#M10#8.8#锌#5#LS01#交期=2026-10-02',
                'receiveraddress':'客户甲有限公司','receivername':'不作为客户','freightbillno':'@10/02#A#2','goodslist':[{'id':'40','usercode':'LS01','ptypeid':'30',
                    'skuid':'31','unit':1,'unitrate':1,'qty':5,'assqty':5}]}], 'totalresults':1}
        if method == 'erp.ewms.stockoutbill.query':
            return {'orders':[]}
        if method == 'erp.goodsstocks.list':
            row = {'profileid':'10','ktypeid':'20','ptypeid':'30','skuid':'31',
                   'qty':self.stock_qty,'saleqty':self.stock_saleable}
            rows = ([row, row] if self.stock_duplicate else [row]) + self.stock_extra
            return {'goodsstocks':rows, 'totalresults':len(rows)}
        if method == 'erp.bill.billcenterlist':
            begin = (params['pageindex']-1)*100
            return {'details':copy.deepcopy(self.receipt_bills[begin:begin+100])}
        raise AssertionError(method)


def test_summary_delivery_suffix_is_strict_and_keeps_legacy_label_fields():
    summary = 'PO#P#2#D#螺栓#M10#8.8#锌#5#LS01#交期=2026-10-02'
    assert note_delivery_date(summary) == '2026-10-02'
    assert parse_note(summary)['sku'] == 'LS01'
    assert parse_note(summary)['deliveryDate'] == '2026-10-02'
    with pytest.raises(ValueError, match='交期待确认'):
        note_delivery_date(summary.replace('2026-10-02', '10/02'))


def test_complete_query_uses_confirmed_remaining_quantity_and_summary_delivery_date():
    client = Client(); result = validate_snapshot(LiveSource(client, warehouse_id='20').collect())
    assert result['orders'][0]['delivery_date'] == '2026-10-02'
    assert result['orders'][0]['lines'][0]['delivery_date'] == '2026-10-02'
    assert result['orders'][0]['lines'][0]['open_qty'] == '5'
    assert result['orders'][0]['lines'][0]['base_unit'] == '1'
    assert result['records']['orders'][0]['quantity'] == '5'
    assert result['records']['orders'][0]['buyer'] == '客户甲有限公司'
    assert result['records']['orders'][0]['customer_name'] == '客户甲有限公司'
    assert result['records']['orders'][0]['customer_source'] == 'wms_receiveraddress'
    assert result['records']['orders'][0]['summary'].endswith('#交期=2026-10-02')
    assert result['records']['inventory'][0]['purchase_pending'] == '12'
    assert result['records']['inventory'][0]['inferred_other_reserved'] == '0'
    assert result['records']['inventory'][0]['effective_physical'] == '10'
    assert len(result['records']['inventory']) == 3
    zero = next(row for row in result['records']['inventory'] if row['sku'] == 'LS02')
    assert zero['stock_quantity'] == zero['available_stock'] == '0'
    assert zero['material'] == '40Cr' and zero['location'] == 'B02'
    assert zero['print_blocked'] is False
    base = next(row for row in result['records']['inventory'] if row['sku'] == 'BASE1')
    assert base['sku_id'] == '' and base['stock_quantity'] == '0'
    assert result['source_counts']['inventory'] == 3
    assert result['source_counts']['inventory_stock_rows'] == 1
    assert result['source_counts']['warehouses'] == 1
    assert result['source_counts']['warehouses_discovered'] == 2
    assert result['records']['inventory'][0]['unit_name'] == ''
    assert result['receipt_events_complete'] is True
    assert result['receipt_events'] == []
    assert result['purchase_batches_complete'] is False
    assert result['query_scope']['quantity_basis'] == 'remaining_unshipped_quantity_user_confirmed_2026_09_29'
    assert not any(i['code'].startswith('delivery_date_') for i in result['calculation_issues'])
    assert not any(i['code'] == 'remaining_demand_unverified' for i in result['calculation_issues'])
    assert all('update' not in method for method, _ in client.calls)


def test_inventory_record_exposes_negative_residual_as_other_draft_reservation():
    client = Client(); client.stock_saleable = 4
    result = validate_snapshot(LiveSource(client, warehouse_id='20').collect())
    stock = next(row for row in result['records']['inventory'] if row['sku'] == 'LS01')
    assert stock['purchase_pending'] == '0'
    assert stock['inferred_other_reserved'] == '1'
    assert stock['effective_physical'] == '9'


def test_negative_physical_without_demand_remains_queryable_and_has_no_usable_stock():
    client = Client()
    client.stock_extra = [{
        'profileid':'10','ktypeid':'20','ptypeid':'32','skuid':'33',
        'qty':'-443','saleqty':'-443',
    }]
    result = validate_snapshot(LiveSource(client, warehouse_id='20').collect())
    stock = next(row for row in result['records']['inventory'] if row['sku'] == 'LS02')
    assert stock['stock_quantity'] == '-443'
    assert stock['available_stock'] == '-443'
    assert stock['purchase_pending'] == '0'
    assert stock['inferred_other_reserved'] == '0'
    assert stock['effective_physical'] == '0'
    state = {'version':1, 'profile_id':'10', 'events':{}, 'balances':{},
             'orders':{}, 'scope_periods':{}}
    calculated = calculate_query_snapshot(result, state, today=date.today())
    assert calculated['blocked_orders'] == []
    assert [plan['tradeid'] for plan in calculated['plans']] == ['A#2']


def test_qimen_upper_total_counts_all_pages_and_identical_duplicate_is_safe():
    client = Client(); client.orders *= 101
    progress = []
    source = LiveSource(client); source.progress = progress.append
    assert len(source._orders('10')) == 1
    assert len(client.calls) == 2
    assert progress[-1]['current'] == 101


def test_conflicting_duplicate_order_rejects_snapshot():
    client = Client(); second = copy.deepcopy(client.orders[0])
    second['order_info']['order_info'][0]['sell_count'] = '6'
    client.orders.append(second)
    with pytest.raises(SourceIncomplete, match='不同内容'):
        LiveSource(client)._orders('10')


def test_duplicate_stock_fails_instead_of_last_row_winning():
    client = Client(); client.stock_duplicate = True
    with pytest.raises(SourceIncomplete, match='重复身份'):
        LiveSource(client, warehouse_id='20').collect()


def test_configured_legacy_order_is_excluded_from_query_and_allocation():
    client = Client()
    current = copy.deepcopy(client.orders[0])
    current['trade_order']['trade_id'] = 'B#3'
    current['order_info']['order_info'][0]['recid'] = '41'
    client.orders.append(current)
    original_call = client.call

    def call(method, params):
        if method == 'erp.ewms.stockoutbill.list':
            return {'orders':[{'vchcode':'51','tradeid':'B#3','ktypeid':'20',
                'summary':'PO#P#3#D#螺栓#M10#8.8#锌#5#LS01#交期=2026-10-02',
                'receiveraddress':'客户乙有限公司','goodslist':[{'id':'41','usercode':'LS01',
                    'ptypeid':'30','skuid':'31','unit':1,'unitrate':1,'qty':5,'assqty':5}]}],
                'totalresults':1}
        return original_call(method, params)

    client.call = call
    facts = validate_snapshot(LiveSource(
        client, warehouse_id='20', ignored_order_ids={'A#2'}).collect())
    assert [row['order_no'] for row in facts['orders']] == ['B#3']
    assert [row['order_no'] for row in facts['records']['orders']] == ['B#3']
    assert facts['calculation_issues'] == []
    assert facts['query_scope']['ignored_order_ids'] == ['A#2']
    assert facts['source_counts']['order_documents_raw'] == 2
    assert facts['source_counts']['order_documents'] == 1
    assert facts['source_counts']['ignored_order_documents'] == 1
    state = {'version':1, 'profile_id':'10', 'events':{}, 'balances':{},
             'orders':{}, 'scope_periods':{}}
    calculated = calculate_query_snapshot(facts, state, today=date.today())
    assert calculated['blocked_orders'] == []
    assert [row['tradeid'] for row in calculated['plans']] == ['B#3']
    assert not any(method == 'erp.ewms.stockoutbill.query' for method, _ in client.calls)


def test_complete_status6_set_is_staged_as_shadow_and_reported_in_scope():
    client = Client()
    shipped = copy.deepcopy(client.orders[0])
    shipped['trade_order']['trade_id'] = 'OLD#500'
    shipped['trade_order']['extendProps'] = {'process_state':500}
    shipped['order_info']['order_info'][0]['recid'] = '500-line'
    client.shipped_orders = [shipped]
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runtime') as temp:
        lifecycle = OrderLifecycle(Store(temp))
        facts = validate_snapshot(LiveSource(client, warehouse_id='20',
            order_lifecycle=lifecycle).collect())
    assert {order['order_no'] for order in facts['orders']} == {'A#2','OLD#500'}
    shadow = next(order for order in facts['orders'] if order['order_no'] == 'OLD#500')
    assert shadow['shadow_demand'] is True
    assert shadow['lifecycle_state'] == 'candidate'
    display = next(row for row in facts['records']['orders'] if row['order_no'] == 'OLD#500')
    assert display['print_blocked'] is True and display['printable'] is False
    assert facts['source_counts']['shipped_unposted_documents'] == 1
    assert facts['source_counts']['lifecycle_shadow_orders'] == 1
    assert facts['query_scope']['lifecycle'] == 'qimen_bill_status_6_with_exact_disappearance_check'
    status6 = [params for method, params in client.calls if method == QIMEN and
               params['extendProps']['bill_status'] == '6']
    assert len(status6) == 2 and all('trade_id' not in row['extendProps'] for row in status6)


def test_historical_status500_uses_verified_delivery_cache_not_order_date():
    client = Client()
    shipped = copy.deepcopy(client.orders[0])
    shipped['trade_order']['trade_id'] = 'OLD#500'
    shipped['trade_order']['extendProps'] = {'process_state':500}
    shipped['order_info']['order_info'][0]['recid'] = ''
    client.shipped_orders = [shipped]
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runtime') as temp:
        store = Store(temp)
        import_delivery_dates(store, '10', {'OLD#500':'2026-10-15'},
            observed_at='2026-10-01T08:00:00+08:00')
        facts = validate_snapshot(LiveSource(client, warehouse_id='20',
            order_lifecycle=OrderLifecycle(store)).collect())
    shadow = next(order for order in facts['orders'] if order['order_no'] == 'OLD#500')
    assert shadow['order_date'] == '2026-09-24'
    assert shadow['delivery_date'] == '2026-10-15'
    assert shadow['lines'][0]['delivery_date'] == '2026-10-15'
    display = next(row for row in facts['records']['orders']
                   if row['order_no'] == 'OLD#500')
    assert display['delivery_date'] == '2026-10-15'


def test_status6_first_page_change_rereads_only_that_source():
    client = Client()
    shipped = copy.deepcopy(client.orders[0])
    shipped['trade_order']['extendProps'] = {'process_state':500}
    client.shipped_orders = [shipped]
    original = client.call
    reads = 0

    def call(method, params):
        nonlocal reads
        if method == QIMEN and params['extendProps'].get('bill_status') == '6':
            reads += 1
            if reads == 2:
                return {'code':'0', 'TotalResults':0,
                        'trade_orders':{'trade_orders':[]}}
        return original(method, params)

    client.call = call
    assert LiveSource(client)._orders('10', bill_status='6') == client.shipped_orders
    assert reads == 4


def test_status6_reordering_does_not_fail_or_drop_documents():
    client = Client()
    client.shipped_orders = [copy.deepcopy(client.orders[0]), copy.deepcopy(client.orders[0])]
    client.shipped_orders[1]['trade_order']['trade_id'] = 'B#2'
    original = client.call
    reads = 0
    def call(method, params):
        nonlocal reads
        result = original(method, params)
        if method == QIMEN:
            reads += 1
            if reads % 2 == 0:
                result['trade_orders']['trade_orders'].reverse()
        return result
    client.call = call
    assert len(LiveSource(client)._orders('10', bill_status='6')) == 2
    assert reads == 2


def test_persistent_status6_mutation_is_bounded_and_never_published():
    from core.api_source import SourceChanged
    client = Client()
    client.shipped_orders = [copy.deepcopy(client.orders[0])]
    original = client.call
    reads = 0
    def call(method, params):
        nonlocal reads
        result = original(method, params)
        if method == QIMEN:
            reads += 1
            result['trade_orders']['trade_orders'][0]['trade_order']['modified'] = str(reads)
        return result
    client.call = call
    with pytest.raises(SourceChanged):
        LiveSource(client)._orders('10', bill_status='6')
    assert reads == 6


def test_missing_wms_summary_is_visible_and_not_printable():
    client = Client(); client.fail_details = True
    facts = LiveSource(client, warehouse_id='20').collect()
    row = facts['records']['orders'][0]
    assert row['summary'] == '' and row['print_blocked']
    assert row['warehouse_id'] == '20' and row['base_unit'] == '1'
    assert any(i['code'] == 'summary_details_unavailable' for i in facts['calculation_issues'])
    assert any(i['code'] == 'delivery_date_missing' for i in facts['calculation_issues'])
    assert any(i['code'] == 'order_detail_unverified' and i['order_no'] == 'A#2'
               for i in facts['calculation_issues'])


def test_bill_center_builds_signed_receipt_events_with_stable_api_identity():
    client = Client()
    detail = {'dlyorder':'1','ktypeid':'20','ptypeid':'30','skuid':'31',
              'usercode':'LS01','pfullname':'螺栓','qty':5}
    client.receipt_bills = [
        {'vchcode':'501','number':'IN1','vchtypename':'进货入库单','date':'2026-09-24',
         'overtime':'2026-09-24 10:00:00','details':{'details':[detail]}},
        {'vchcode':'502','number':'RT1','vchtypename':'进货退货单','date':'2026-09-25',
         'overtime':'2026-09-25 10:00:00','details':{'details':[dict(detail,qty=-2)]}},
    ]
    facts = LiveSource(client, warehouse_id='20').collect()
    assert facts['receipt_events_complete'] is True
    assert {row['quantity'] for row in facts['receipt_events']} == {'5','-2'}
    assert {row['type'] for row in facts['receipt_events']} == {'进货入库单','进货退货单'}
    assert all(row['profile_id']=='10' and row['warehouse_id']=='20' and
               row['product_id']=='30' and row['sku_id']=='31' for row in facts['receipt_events'])


def test_unverified_exchange_document_blocks_receipt_classification_only():
    client = Client()
    client.receipt_bills = [{
        'vchcode':'503','number':'EX1','vchtypename':'进货换货单','date':'2026-09-25',
        'details':{'details':[{'dlyorder':'1','ktypeid':'20','ptypeid':'30',
            'skuid':'31','usercode':'LS01','pfullname':'螺栓','qty':1}]},
    }]
    facts = LiveSource(client, warehouse_id='20').collect()
    assert facts['receipt_events_complete'] is False
    assert any(item['code']=='receipt_exchange_unverified' for item in facts['calculation_issues'])


def test_published_query_marks_blocked_and_never_creates_success_cache():
    temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "runtime")
    store = Store(temp.name)
    facts = LiveSource(Client(), warehouse_id='20').collect()
    calculated = calculate_query_snapshot(facts)
    assert calculated['plans'] == [] and calculated['blocked_orders']
    publish(store, facts, calculated)
    record = store.rows('orders')[0]
    assert record['calculation_status'] == 'blocked'
    assert record['shipping_id'] == '@10/02#A#2'
    assert record['source_shipping_id'] == '@10/02#A#2'
    assert next(row for row in store.rows('inventory') if row['sku'] == 'LS01')['stock_quantity'] == '10'
    temp.cleanup()


def test_request_progress_and_inventory_explicit_scope():
    client = Client(); progress = []
    LiveSource(client, warehouse_id='20').collect(progress.append)
    call = next(p for m,p in client.calls if m == 'erp.goodsstocks.list')
    assert call['ptypeids'] == [30, 32, 34] and call['ktypeids'] == [20]
    assert call['pagesize'] == 500
    receipt = next(p for m,p in client.calls if m == 'erp.bill.billcenterlist')
    assert receipt['pagesize'] == 500
    assert {'orders','products','inventory_products'} <= {p['source'] for p in progress}


def test_reference_cache_merges_incremental_catalog_without_losing_unchanged_products():
    client = Client()
    original_call = client.call
    def changed_call(method, params):
        if method == 'erp.ptype.list' and params['begintime'] != '1900-01-01 00:00:00':
            client.calls.append((method,params))
            return {'ptypes':[{'ptypeid':'30','usercode':'LS01','fullname':'螺栓',
                'unit1':'','picurl':'http://erp.qiniu.wsgjp.com/new.png',
                'isclass':False,'skus':[{'skuid':'31'}]}], 'totalresults':1}
        return original_call(method,params)
    client.call=changed_call
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runtime') as temp:
        cache = ReferenceCache(Store(temp))
        first = LiveSource(client, warehouse_id='20', reference_cache=cache).collect()
        first_catalog_calls = [method for method, _ in client.calls
                               if method in {'erp.stock.list', 'erp.ptype.list'}]
        client.calls.clear()
        second = LiveSource(client, warehouse_id='20', reference_cache=cache).collect()
        merged = cache.get('catalog-v1','10')['products']
    assert first_catalog_calls == ['erp.stock.list', 'erp.ptype.list']
    assert [method for method,_ in client.calls if method in {'erp.stock.list','erp.ptype.list'}] == [
        'erp.ptype.list','erp.stock.list']
    assert len(merged)==3 and next(row for row in merged if row['ptypeid']=='30')['picurl'].endswith('/new.png')
    assert first['source_counts']['reference_cache_used'] is False
    assert second['source_counts']['reference_cache_used'] is True
    assert len(second['records']['inventory']) == len(first['records']['inventory'])
    assert len(second['product_images']) == 1


def test_full_reconcile_refreshes_reference_data_and_wms_year():
    client = Client()
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runtime') as temp:
        store = Store(temp); references = ReferenceCache(store); wms = WmsCache(store)
        source = LiveSource(client, warehouse_id='20', reference_cache=references, wms_cache=wms)
        source.collect()
        client.calls.clear()
        normal = source.collect()
        normal_wms = next(params for method, params in client.calls
                          if method == 'erp.ewms.stockoutbill.list')
        assert normal_wms['timetype'] == 1
        assert normal['source_counts']['reference_cache_used'] is True
        client.calls.clear()
        reconciled = source.collect(full_reconcile=True)
        full_wms = next(params for method, params in client.calls
                        if method == 'erp.ewms.stockoutbill.list')
        assert full_wms['timetype'] == 0
        assert reconciled['source_counts']['reference_cache_used'] is False
        assert reconciled['source_counts']['full_reconcile'] is True


def test_wms_cache_is_sanitized_incremental_and_profile_scoped():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runtime') as temp:
        cache = WmsCache(Store(temp))
        now = datetime(2026, 9, 29, 10, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
        rows = [
            {'vchcode':'1','tradeid':'A','summary':'one','receiveraddress':'客户甲有限公司','receiverphone':'secret',
             'goodslist':[{'id':'11','usercode':'LS01','qty':1,'receivername':'secret'}]},
            {'vchcode':'2','tradeid':'B','summary':'two','goodslist':[]},
            {'vchcode':'3','tradeid':'outside','summary':'ignored','goodslist':[]},
            {'vchcode':'4','tradeid':'pending','deliverstatus':'0','summary':'kept','goodslist':[]},
        ]
        cache.commit('10', {'A','B'}, rows, now, full=True)
        saved = cache.get('10', {'A','B','pending'})
        assert set(saved) == {'A','B','pending'}
        assert saved['A']['receiveraddress'] == '客户甲有限公司'
        assert 'receiverphone' not in saved['A']
        assert 'receivername' not in saved['A']['goodslist'][0]
        assert cache.get('other', {'A'}) == {}
        initial = cache.metadata('10')
        assert initial['full_year'] == 2026 and initial['last_full'] == now.isoformat()
        later = now + timedelta(hours=1)
        cache.commit('10', {'A','C'}, [dict(rows[0],summary='changed')], later, full=False)
        saved = cache.get('10', {'A','B','C'})
        assert set(saved) == {'A','B'} and saved['A']['summary'] == 'changed'
        assert saved['A']['_observed_at'] == later.isoformat()
        assert saved['B']['_observed_at'] == now.isoformat()
        metadata = cache.metadata('10')
        assert metadata['last_full'] == initial['last_full']
        assert metadata['last_sync'] == later.isoformat()


def test_order_date_probe_skips_old_uncached_rows_and_resolves_current_cached_row():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runtime') as temp:
        cache = WmsCache(Store(temp))
        started = datetime(2026, 9, 29, 10, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
        cache.commit('10', {'CURRENT'}, [{
            'vchcode':'1', 'tradeid':'CURRENT', 'deliverstatus':'0', 'goodslist':[],
        }], started, full=True)
        source = LiveSource(Client(), warehouse_id='20', wms_cache=cache)
        queried = []
        def pages(method, params, *args, **kwargs):
            queried.append(params['begintime'][:10])
            return [{'vchcode':'1','tradeid':'CURRENT'}] \
                if params['begintime'].startswith('2026-09-23') else []
        source._pages = pages
        orders = [
            {'trade_order': {'trade_id':'CURRENT','pay_time':'2026-09-24'}},
            {'trade_order': {'trade_id':'OLD','pay_time':'2020-06-16'}},
        ]
        values, issues, counts = source._order_dates('10', orders, started)
        assert values == {'CURRENT':'2026-09-23'}
        assert len(queried) == 8 and all(value.startswith('2026-09-') for value in queried)
        assert counts == {'order_dates_cached':0, 'order_dates_probed':1}
        assert [item['order_no'] for item in issues] == ['OLD']


def test_posted_internal_outputs_and_exact_red_pair_do_not_count_raw_materials():
    client=Client()
    base={'vchcode':'ASM1','number':'ZZ1','vchtype':16,'vchtypename':'生产组装','date':'2026-09-24','overtime':'2026-09-24T10:00:00','ktypeid':'20',
          'details':[{'dlyorder':'in1','ptypeid':'30','skuid':'31','qty':5,'inout':0},
                     {'dlyorder':'out1','ptypeid':'32','skuid':'33','qty':-10,'inout':1}]}
    client.receipt_bills=[copy.deepcopy(base)]
    source=LiveSource(client,warehouse_id='20')
    products={'30':{'unit1':'支'},'32':{'unit1':'支'}}
    rows,events,_,_=source._purchases('10',datetime(2026,10,4,tzinfo=ZoneInfo('Asia/Shanghai')),products,'20')
    assert len(rows)==len(events)==1 and events[0]['quantity']=='5'
    assert events[0]['type']=='内部组装／拆卸' and events[0]['supplier_id']=='internal'
    original_id=events[0]['event_id']
    base['redold']=True
    red=copy.deepcopy(base);red.update(vchcode='RED1',redword=True)
    for line in red['details']:line.update(qty=-line['qty'],inout=1-line['inout'],dlyorder='r'+line['dlyorder'])
    client.receipt_bills=[red,base]
    _,events,_,_=source._purchases('10',datetime(2026,10,4,tzinfo=ZoneInfo('Asia/Shanghai')),products,'20')
    assert events[0]['event_id']==original_id and events[0]['quantity']=='0' and len(events)==1
    red['details'][0]['qty']=-6
    with pytest.raises(SourceIncomplete,match='红冲无法唯一'):source._purchases('10',datetime(2026,10,4,tzinfo=ZoneInfo('Asia/Shanghai')),products,'20')


def test_large_receipt_response_restarts_paging_without_duplicate_or_missing_lines():
    client=Client();calls=[]
    bills=[{'vchcode':str(i),'number':f'IN{i}','vchtypename':'进货入库单','date':'2026-10-01',
        'details':[{'dlyorder':'1','ktypeid':'20','ptypeid':'30','skuid':'31','qty':1}]} for i in range(501)]
    def call(method,params):
        size,page=params['pagesize'],params['pageindex'];calls.append((size,page))
        if size==500 and page==2:raise ErpUnavailable('too large','response_too_large')
        return {'details':bills[(page-1)*size:page*size]}
    client.call=call
    rows,events,_,_=LiveSource(client)._purchases('10',datetime(2026,10,14),{'30':{'unit1':'支'}},'20')
    assert len(rows)==len(events)==501 and len({x['id'] for x in rows})==501
    assert calls==[(500,1),(500,2),(250,1),(250,2),(250,3),(250,1)]


def test_receipt_response_limit_is_bounded_and_does_not_retry_other_errors():
    for category,expected in [('response_too_large',[500,250,125,62,31,15,7,3,1]),('signature',[500])]:
        client=Client();sizes=[]
        def call(method,params):
            sizes.append(params['pagesize']);raise ErpUnavailable('unavailable',category)
        client.call=call
        with pytest.raises(ErpUnavailable):LiveSource(client)._purchases('10',datetime(2026,10,14),{},'20')
        assert sizes==expected
