import json
from pathlib import Path
import sys

import pytest

from workbench.tools import sales_order_trial as trial
from core.wsgjp_gateway import ApiError


def test_unknown_write_result_cannot_send_second_order(monkeypatch, tmp_path):
    config = tmp_path / 'config.json'
    config.write_text('{}', encoding='utf-8')
    calls = []

    class FakeClient:
        def __init__(self, _vault):
            pass

        class Lock:
            def __enter__(self): return self
            def __exit__(self, *_): return None

        lock = Lock()

        def _ready(self):
            return {'credentials': {'appkey': 'a', 'signkey': 's'},
                    'token': {'auth_token': 't'}}

    def attempted_write(_path, payload):
        calls.append(payload['method'])
        raise TimeoutError('unknown after send')

    monkeypatch.setattr(trial, 'LEDGER', tmp_path / 'ledger')
    monkeypatch.setattr(trial, 'ErpClient', FakeClient)
    monkeypatch.setattr(trial, '_build', lambda _client, _config: ({'number': 'ULTEST1'}, {}))
    monkeypatch.setattr(trial, 'post', attempted_write)
    monkeypatch.setattr(sys, 'argv', ['sales_order_trial.py', '--submit', str(config)])

    with pytest.raises(TimeoutError):
        trial.main()
    assert calls == ['erp.saleorder.add']
    entry = tmp_path / 'ledger' / 'ULTEST1.json'
    assert json.loads(entry.read_text(encoding='utf-8'))['status'] == 'intent_committed'
    with pytest.raises(FileExistsError):
        trial.main()
    assert calls == ['erp.saleorder.add']


def test_quote_conversion_and_delivery_suffix():
    assert tuple(map(str, trial._price('1.13', 'tax_inclusive', '1'))) == ('1.000', '1.13')
    assert tuple(map(str, trial._price('1', 'tax_exclusive', '1'))) == ('1.000', '1.13')
    summary = trial._summary({'line_no': 1, 'product_name': '内六角', 'spec': 'M6*20',
                              'grade': '8.8', 'surface': '-', 'quantity': 1,
                              'product_code': 'LS0001'}, 'ULTEST1', '2026-10-15')
    assert summary.endswith('#交期=2026-10-15')
    assert len(summary.split('#')) == 11


def test_vendor_rejection_persists_bounded_error_without_retry(monkeypatch, tmp_path):
    config = tmp_path / 'config.json'
    config.write_text('{}', encoding='utf-8')
    calls = []

    class FakeClient:
        def __init__(self, _vault):
            pass

        class Lock:
            def __enter__(self): return self
            def __exit__(self, *_): return None

        lock = Lock()

        def _ready(self):
            return {'credentials': {'appkey': 'a', 'signkey': 's'},
                    'token': {'auth_token': 't'}}

    def rejected(_path, payload):
        calls.append(payload['method'])
        raise ApiError('vendor_rejected', 18)

    monkeypatch.setattr(trial, 'LEDGER', tmp_path / 'ledger')
    monkeypatch.setattr(trial, 'ErpClient', FakeClient)
    monkeypatch.setattr(trial, '_build', lambda _client, _config: ({'number': 'ULTEST2'}, {}))
    monkeypatch.setattr(trial, 'post', rejected)
    monkeypatch.setattr(sys, 'argv', ['sales_order_trial.py', '--submit', str(config)])

    with pytest.raises(ApiError):
        trial.main()
    entry = json.loads((tmp_path / 'ledger' / 'ULTEST2.json').read_text())
    assert entry['status'] == 'vendor_rejected'
    assert entry['error_category'] == 'vendor_rejected'
    assert entry['error_code'] == 18
    with pytest.raises(FileExistsError):
        trial.main()
    assert calls == ['erp.saleorder.add']
