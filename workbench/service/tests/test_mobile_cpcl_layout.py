"""0.1.2 rollback contract. Synthetic data; no hardware or production writes."""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.mobile_cpcl import order_cpcl, inventory_cpcl, function_codes_cpcl

FIXTURES = Path(__file__).with_name('fixtures') / 'label_012'
# The exact synthetic fields used to create the pre-change review CPCL files.
ORDER = dict(name='HEX BOLT 六角螺栓', orderNo='PO20260910001234', projectNo='JOB-2026-090',
             spec='DIN933  M12*40', grade='8.8', quantity='1200', totalPackages='12',
             packageQuantity='100', lineNo='12', drawingNo='DRW-120034', surface='镀蓝白锌',
             sku='LS0012034', qrValue='SHIP-120034')
INVENTORY = dict(productName='内六角圆柱头螺钉 SOCKET CAP SCREW', spec='M12*100', model='12.9级发黑',
                 material='合金钢', location='A-12-08', standardNo='DIN912', sku='LS0012034', qrValue='LS0012034')


@pytest.mark.parametrize('name,renderer,fields', [
    ('order-before.cpcl', order_cpcl, ORDER),
    ('inventory-before.cpcl', inventory_cpcl, INVENTORY),
])
def test_exact_012_cpcl_bytes(name, renderer, fields):
    assert renderer(fields) == (FIXTURES / name).read_bytes()


def test_order_readability_keeps_original_double_size_and_bold_and_spaces():
    body = order_cpcl(ORDER).decode('gb18030')
    assert '! U1 SETBOLD 4\r\nSETMAG 2 2\r\nVTEXT 55 0 22 604 HEX BOLT 六角螺栓' in body
    for y, value in [(86, ORDER['orderNo']), (132, ORDER['projectNo']),
                     (174, 'DIN933  M12*40'), (220, '8.8'), (282, '1200')]:
        assert f'! U1 SETBOLD 3\r\nSETMAG 2 2\r\nVTEXT 55 0 {y} 604 {value}' in body
    assert '! U1 SETBOLD 4\r\nSETMAG 2 2\r\nVTEXT 55 0 328 536 12' in body
    assert '! U1 SETBOLD 4\r\nSETMAG 2 2\r\nVTEXT 55 0 328 348 100' in body
    assert 'VTEXT 55 0 178 698 TP:' in body and 'VTEXT 55 0 224 698 CT:' in body


def test_inventory_keeps_original_spec_four_times_and_model_three_times():
    body = inventory_cpcl(INVENTORY).decode('gb18030')
    assert '! U1 SETBOLD 5\r\nSETMAG 4 4\r\nVTEXT 55 0 86 654 M12*100' in body
    assert '! U1 SETBOLD 5\r\nSETMAG 3 3\r\nVTEXT 55 0 194 752 12.9级发黑' in body
    assert '! U1 SETBOLD 5\r\nSETMAG 2 2\r\nVTEXT 55 0 326 210 LS0012034' in body
    assert 'VTEXT 55 0 62 780 DIN912' in body


def test_order_and_inventory_qr_and_paper_remain_unchanged():
    order = order_cpcl(ORDER).decode('gb18030')
    inventory = inventory_cpcl(INVENTORY).decode('gb18030')
    assert 'VB QR 204 150 M 2 U 4\r\nMA,SHIP-120034\r\nENDQR' in order
    assert 'VB QR 122 208 M 2 U 5\r\nMA,LS0012034\r\nENDQR' in inventory
    for body in (order, inventory):
        assert body.startswith('! 0 200 200 800 1\r\nPAGE-WIDTH 400\r\nGAP-SENSE\r\n')
        assert 'EG ' not in body and body.endswith('FORM\r\nPRINT\r\n')


def test_function_codes_are_byte_identical_to_the_pre_change_version():
    raw = function_codes_cpcl()
    assert hashlib.sha256(raw).hexdigest() == '1658f8cfb2e0dde085bfb461afadc084834307f57eb72e3d1044753a027e4f8d'
    body = raw.decode('gb18030')
    assert 'B QR 118 92 M 2 U 4\r\nMA,PALL01\r\nENDQR' in body
    assert 'B QR 118 482 M 2 U 4\r\nMA,PBOX01\r\nENDQR' in body


def test_every_text_resets_scale_and_bold_without_state_leakage():
    for raw in (order_cpcl(ORDER), inventory_cpcl(INVENTORY), function_codes_cpcl()):
        lines = raw.decode('gb18030').split('\r\n')
        for index, line in enumerate(lines):
            if line.startswith(('TEXT 55 0 ', 'VTEXT 55 0 ')):
                assert lines[index-2].startswith('! U1 SETBOLD ')
                assert lines[index-1].startswith('SETMAG ')
                assert lines[index+1:index+3] == ['SETMAG 1 1', '! U1 SETBOLD 0']
