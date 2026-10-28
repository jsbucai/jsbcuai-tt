"""New-product plans cannot invent identity or retry an uncertain ERP write."""
import copy
import json
import zlib
from pathlib import Path

import pytest

from core.erp_api import ErpClient, ErpUnavailable
from core.sales_codex_pipeline import digest
from core.sales_new_product import (
    ProductCreationClient, classification_choices, execute_confirmed_product_creation,
    fetch_new_product_context, new_product_plan_hash, prepare_new_product_plan,
    validate_confirmed_plan, verify_created_product,
)
from core.sales_skill_matching import load_skill_rules
from core.wsgjp_gateway import ApiError
from tools.sales_new_product_worker import ReadOnlyProductClient, readonly_preflight


RULES_DIR = Path(__file__).resolve().parents[2] / 'deploy/skills/ul-sales-order/references/rules'


def product(code='LS0001', spec='M6*30', **fields):
    return {'ptypeid': str(zlib.crc32(code.encode())), 'usercode': code,
        'fullname': '六角头螺栓GB/T5783', 'name': '六角头螺栓',
        'standard': spec, 'type': '8.8级 发黑', 'area': '碳钢',
        'unit1': '支', 'parptypeid': 'class-child', 'skus': [{'skuid': 'sku-' + code}],
        **fields}


def classes():
    return [
        {'id': 'class-root', 'typeid': 'type-root', 'parid': '0', 'fullname': '螺栓', 'profileid': 'P'},
        {'id': 'class-child', 'typeid': 'type-child', 'parid': 'class-root', 'fullname': '外六角', 'profileid': 'P'},
    ]


def suggestion(**fields):
    return {'product_name': '六角头螺栓', 'spec': 'M6×40', 'standard': 'GB/T5783',
        'grade': '8.8', 'surface': '发黑', 'material': '碳钢', 'base_unit': '支',
        'source_ids': ['S1'], 'source_quote': '六角头螺栓GB/T5783 M6×40 8.8级 发黑 碳钢', **fields}


def prepare(products=None, category_rows=None, source=None, **kwargs):
    products = products if products is not None else [product()]
    values = {'profile_id': 'P', 'data_version': 'V1', 'products': products,
        'classes': category_rows if category_rows is not None else classes(),
        'completeness': {'profile_id': 'P', 'products_complete': True, 'classes_complete': True,
            'includes_inactive_deleted': True, 'product_count': len(products)},
        'rules': load_skill_rules(RULES_DIR)}
    values.update(kwargs)
    return prepare_new_product_plan(source if source is not None else suggestion(), **values)


def frozen_plan():
    plan = prepare()
    plan.update(source_result_hash='a' * 64, suggestion_id='SUG1', session_id='SESSION1',
        base_version=3, data_version='V1')
    assert plan['status'] == 'ready', plan['issues']
    return plan


class FakeClient:
    def __init__(self, *, result=None, created=None):
        self.products = [product()]
        self.class_rows = classes()
        self.profile = 'P'
        self.calls = []
        self.created = created or product('LS0002', 'M6*40')
        self.result = result
        self.write_error = None
        self.lookup_error = False
        self.lookup_override = None
        self.stocks = []

    def account(self):
        self.calls.append('account')
        return self.profile

    def pages(self, method, params, collection, identity, **kwargs):
        if method == 'erp.goodsstocks.list':
            self.calls.append(method)
            assert params['ptypeids'] == [int(self.created['ptypeid'])]
            return copy.deepcopy(self.stocks)
        assert method == 'erp.ptype.list' and collection == 'ptypes'
        assert params['isall'] is True and params['begintime'].startswith('1900')
        self.calls.append(method)
        return copy.deepcopy(self.products)

    def call(self, method, params):
        self.calls.append(method)
        if method == 'erp.ptype.classlist':
            # The official sample has a false zero total; the complete list is
            # the contract, unlike the paginated product list.
            return {'ptypeclasslist': copy.deepcopy(self.class_rows), 'totalresults': 0}
        assert method == 'erp.ptype.listbykey'
        if self.lookup_error:
            raise ErpUnavailable('failed')
        if self.lookup_override is not None:
            return {'ptypes': self.lookup_override}
        return {'ptypes': [{'ptypeid': row['ptypeid'], 'usercode': row['usercode']} for row in self.products
                           if row['usercode'].upper() == params['keyword'].upper()]}

    def create_product(self, plan, confirmed_plan_hash):
        self.calls.append('erp.ptype.add')
        assert confirmed_plan_hash == digest(plan)
        if self.write_error:
            raise self.write_error
        if self.result is not None:
            return self.result
        self.products.append(self.created)
        return {'ptypes': [{'id': self.created['ptypeid'], 'skuid': self.created['skus'][0]['skuid'],
            'usercode': self.created['usercode'], 'fullname': self.created['fullname']}], 'errorinfos': []}


def execute(client, plan=None, check=lambda: True):
    plan = plan or frozen_plan()
    return execute_confirmed_product_creation(client, plan,
        confirmed_plan_hash=digest(plan), pre_submit_check=check)


def test_plan_reuses_verified_template_and_ignores_model_code_class_type():
    data = [product()]; original = copy.deepcopy(data)
    plan = prepare(data, source=suggestion(code='HACK0001', classification_id='unknown', model='12.9 镀锌'))
    assert data == original
    assert plan['status'] == 'ready' and plan['code'] == 'LS0002'
    assert plan['payload'] == {'ParTypeId': 'type-child', 'FullName': '六角头螺栓GB/T5783',
        'Name': '六角头螺栓', 'UserCode': 'LS0002', 'Standard': 'M6×40',
        'Type': '8.8级 发黑', 'Area': '碳钢', 'Unit': '支', 'BarCode': 'LS0002'}
    assert plan['classification_path'] == '螺栓 / 外六角'
    assert 'IniQty' not in plan['payload'] and len(plan['identity_hash']) == 64


def test_next_code_uses_inactive_deleted_and_reserved_codes_no_hole_fill():
    data = [product(), product('LS0008', isstop=True), product('LS0012', deleted=True), product('LS00020')]
    assert prepare(data, reserved_codes=['LS00023'])['code'] == 'LS00024'


def test_reference_fullname_with_old_dimensions_cannot_be_copied_to_new_spec():
    data = [product(fullname='六角头螺栓GB/T5783 M6*30')]
    plan = prepare(data)
    assert plan['status'] == 'blocked' and 'new_product_reference_name_contains_spec' in plan['issues']


def test_duplicate_same_attributes_found_beyond_top5():
    data = [product(f'LS{i:04}', spec=f'M6*{i}') for i in range(1, 9)] + [product('LS0099', spec='M6*40')]
    plan = prepare(data)
    assert plan['status'] == 'blocked'
    assert plan['existing_codes'] == ['LS0099']
    assert 'new_product_identity_already_exists' in plan['issues']


@pytest.mark.parametrize('proof', [
    {}, {'products_complete': True},
    {'products_complete': True, 'classes_complete': True, 'includes_inactive_deleted': False, 'profile_id': 'P', 'product_count': 1},
    {'products_complete': True, 'classes_complete': True, 'includes_inactive_deleted': True, 'profile_id': 'OTHER', 'product_count': 1},
    {'products_complete': True, 'classes_complete': True, 'includes_inactive_deleted': True, 'profile_id': 'P', 'product_count': 0},
])
def test_partial_or_other_profile_catalog_cannot_allocate_code(proof):
    with pytest.raises(ValueError, match='catalog_not_complete'):
        prepare(completeness=proof)


@pytest.mark.parametrize('field,value', [('surface', ''), ('grade', ''), ('standard', ''), ('material', ''),
    ('grade', '10.9'), ('standard', 'DIN933'), ('surface', '镀锌')])
def test_missing_or_conflicting_attributes_cannot_copy_reference_defaults(field, value):
    plan = prepare(source=suggestion(**{field: value}))
    assert plan['status'] == 'blocked' and not plan['payload']


def test_unique_category_path_with_cycle_stopped_or_missing_parent_is_rejected():
    rows = classes() + [dict(classes()[1], id='class-duplicate', typeid='other-type')]
    assert not any(row['classification_path'] == '螺栓 / 外六角' for row in classification_choices(rows, 'P'))
    assert prepare(category_rows=rows)['status'] == 'blocked'
    for changes in ({'parid': 'missing'}, {'parid': 'class-child'}, {'isstop': True}):
        assert prepare(category_rows=[classes()[0], {**classes()[1], **changes}])['status'] == 'blocked'


def test_real_classlist_zero_profile_sentinel_is_scoped_by_authenticated_context():
    zero = [{**row, 'profileid': '0'} for row in classes()]
    assert prepare(category_rows=zero)['status'] == 'ready'
    with pytest.raises(ValueError, match='class_profile_conflict'):
        prepare(category_rows=[{**row, 'profileid': 'OTHER'} for row in classes()])


def test_reference_choice_resolves_patterns_without_accepting_arbitrary_category():
    data = [product(), product('FB0007', type='8.8 发黑')]
    assert prepare(data)['status'] == 'blocked'
    assert prepare(data, reference_code='FB0007')['code'] == 'FB0008'
    assert prepare(data, reference_code='NOTFOUND')['status'] == 'blocked'


def test_confirmation_digest_includes_parent_bindings_and_full_payload():
    plan = frozen_plan(); agreed = digest(plan)
    assert agreed == new_product_plan_hash(plan)
    validate_confirmed_plan(plan, agreed)
    changed = copy.deepcopy(plan); changed['base_version'] += 1
    with pytest.raises(ValueError, match='confirmation_conflict'):
        validate_confirmed_plan(changed, agreed)
    changed = copy.deepcopy(plan); changed['payload']['IniQty'] = 1
    with pytest.raises(ValueError, match='payload_invalid'):
        validate_confirmed_plan(changed, digest(changed))


def test_one_add_and_real_unique_product_sku_unit_verified():
    client = FakeClient(); output = execute(client)
    assert output['status'] == 'created'
    assert client.calls.count('erp.ptype.add') == 1
    row = output['result']['verified_inventory_row']
    assert (row['sku'], row['profile_id'], row['base_unit'], row['unit_name']) == ('LS0002', 'P', '1', '支')
    assert row['product_id'] == client.created['ptypeid'] and row['sku_id'] == 'sku-LS0002'
    assert row['stock_quantity'] == '0' and output['result']['created_code'] == 'LS0002'


def test_verified_new_product_retains_actual_warehouse_stock_not_synthetic_zero():
    client = FakeClient()
    client.stocks = [{'profileid': 'P', 'ktypeid': '29871293290508390',
        'ptypeid': client.created['ptypeid'], 'skuid': 'sku-LS0002', 'qty': '12.00', 'saleqty': '10'}]
    output = execute(client)
    assert output['status'] == 'created'
    row = output['result']['verified_inventory_row']
    assert row['stock_quantity'] == '12' and row['available_stock'] == '10'
    assert 'raw_product' not in output['result']


@pytest.mark.parametrize('change', [{'profileid': 'OTHER'}, {'skuid': 'other-sku'}, {'qty': 'NaN'}])
def test_wrong_identity_or_nonfinite_live_stock_never_activates_new_product(change):
    client = FakeClient()
    client.stocks = [{'profileid': 'P', 'ktypeid': '29871293290508390',
        'ptypeid': client.created['ptypeid'], 'skuid': 'sku-LS0002', 'qty': '12', 'saleqty': '10', **change}]
    assert execute(client)['status'] == 'unknown'
    assert client.calls.count('erp.ptype.add') == 1


def test_final_permission_recheck_after_reads_can_cancel_without_post():
    client = FakeClient()
    def changed():
        assert 'erp.ptype.list' in client.calls and 'erp.ptype.listbykey' in client.calls
        return False
    output = execute(client, check=changed)
    assert output['status'] == 'blocked' and 'erp.ptype.add' not in client.calls
    output = execute(client, check=None)
    assert output['status'] == 'blocked' and 'erp.ptype.add' not in client.calls


def test_external_code_or_identical_product_added_since_confirmation_blocks_before_post():
    client = FakeClient(); client.products.append(product('LS0002', 'M6*60'))
    assert execute(client)['status'] == 'blocked' and 'erp.ptype.add' not in client.calls
    client = FakeClient(); client.products.append(product('LS0077', 'M6*40'))
    assert execute(client)['status'] == 'blocked' and 'erp.ptype.add' not in client.calls


def test_live_classification_or_profile_changed_blocks_before_post():
    client = FakeClient(); client.class_rows[1]['typeid'] = 'changed'
    assert execute(client)['status'] == 'blocked' and 'erp.ptype.add' not in client.calls
    client = FakeClient(); client.profile = 'OTHER'
    assert execute(client)['status'] == 'blocked' and 'erp.ptype.add' not in client.calls


@pytest.mark.parametrize('response', [None, {}, {'ptypes': [], 'errorinfos': []},
    {'ptypes': [], 'errorinfos': [{'code': -1, 'message': 'secret vendor body'}]},
    {'ptypes': [{'id': 'wrong', 'skuid': 'sku-wrong', 'usercode': 'WRONG'}], 'errorinfos': []},
    {'ptypes': [{'id': 'product-LS0002', 'skuid': 'sku-LS0002', 'usercode': 'LS0002'}], 'errorinfos': [{'code': -1}]},
])
def test_partial_or_unclear_add_never_counts_created_or_retries(response):
    client = FakeClient(result=response if response is not None else 'unexpected-body')
    output = execute(client)
    assert output['status'] == 'unknown' and client.calls.count('erp.ptype.add') == 1
    assert 'secret' not in json.dumps(output)


def test_mutation_exception_is_unknown_and_not_retried():
    client = FakeClient(); client.write_error = TimeoutError('secret vendor text')
    output = execute(client)
    assert output['status'] == 'unknown' and client.calls.count('erp.ptype.add') == 1
    assert 'secret' not in json.dumps(output)


@pytest.mark.parametrize('changes,reason', [
    ({'unit1': ''}, 'unit_not_verified'),
    ({'unit1': '箱'}, 'unit_not_verified'),
    ({'skus': []}, 'sku_not_unique'),
    ({'skus': [{'skuid': 'a'}, {'skuid': 'b'}]}, 'sku_not_unique'),
    ({'type': '10.9级 发黑'}, 'attributes_conflict'),
    ({'deleted': True}, 'attributes_conflict'),
])
def test_add_receipt_never_activates_missing_or_conflicting_actual_identity(changes, reason):
    client = FakeClient(); client.products.append(product('LS0002', 'M6*40', **changes))
    output = verify_created_product(client, frozen_plan())
    assert output['status'] == 'unknown' and reason in output['result']['reason']
    assert 'erp.ptype.add' not in client.calls


def test_unknown_recovery_is_verify_only_and_receipt_ids_must_match():
    client = FakeClient(); client.products.append(product('LS0002', 'M6*40'))
    output = verify_created_product(client, frozen_plan(), receipt={'id': 'OTHER', 'skuid': 'sku-LS0002'})
    assert output['status'] == 'unknown'
    assert verify_created_product(client, frozen_plan())['status'] == 'created'
    assert 'erp.ptype.add' not in client.calls


def test_whole_read_contract_not_broken_by_official_false_zero_class_total():
    client = FakeClient(); result = fetch_new_product_context(client, 'P')
    assert result['completeness']['products_complete'] is True and len(result['classes']) == 2
    client = FakeClient()
    output = readonly_preflight(client, 'P', suggestion=suggestion(), rules=load_skill_rules(RULES_DIR))
    assert output['status'] == 'read_contract_verified' and output['plan_status'] == 'ready'
    assert output['external_submit'] is False and 'erp.ptype.add' not in client.calls


def test_readonly_probe_cannot_refresh_credentials_or_create():
    client = ReadOnlyProductClient('/not-read')
    with pytest.raises(ErpUnavailable, match='refresh|expired'):
        client._refresh({})
    with pytest.raises(ErpUnavailable, match='write_forbidden'):
        client.create_product({}, '')


def test_general_erp_client_does_not_gain_product_write_permission():
    client = ErpClient('/not-read')
    with pytest.raises(ErpUnavailable):
        client.call('erp.ptype.add', {'ptypes': []})


def test_restricted_write_adapter_sends_one_ptypes_and_never_retries_auth_error(monkeypatch):
    calls = []
    def transport(path, payload):
        calls.append((path, payload))
        raise ApiError('account', 13)
    client = ProductCreationClient('/not-read', transport=transport)
    record = {'credentials': {'appkey': 'k', 'signkey': 's'},
              'token': {'auth_token': 'private', 'profileid': 'P'}}
    monkeypatch.setattr(client, '_ready', lambda: record)
    monkeypatch.setattr(client, '_refresh', lambda _: pytest.fail('write refreshed/retried'))
    plan = frozen_plan()
    with pytest.raises(ErpUnavailable):
        client.create_product(plan, digest(plan))
    assert len(calls) == 1 and json.loads(calls[0][1]['ptypes']) == [plan['payload']]
    assert calls[0][1]['method'] == 'erp.ptype.add'
