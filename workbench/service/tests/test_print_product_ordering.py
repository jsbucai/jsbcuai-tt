import json
import pytest
from core.print_ordering import label_key
from core.print_jobs import JobConflict
from core.store import dumps
from tests.test_print_previews import app, preview, prepare, report
from tests.test_order_print_state import order


def insert(app, identity, number, name, spec, quantity='1'):
    value = order(identity, number, identity, identity, '', '', '共享摘要')
    value.update(product_name=name, spec=spec, quantity=quantity)
    with app.extensions['service']['store'].connect(write=True) as db:
        db.execute('INSERT INTO records(kind,id,payload,sku) VALUES (?,?,?,?)', ('orders', identity, dumps(value), identity))
    return value


def test_names_then_natural_specs_not_selection_or_shared_summary(app):
    rows = [insert(app,'x1','n1','螺钉','M10×80'), insert(app,'x2','n2','螺钉','M6*100'),
            insert(app,'x3','n3','螺钉','M6x20'), insert(app,'x4','n4','垫圈','M30'),
            insert(app,'x5','n5','螺钉','M6×20', '3')]
    value = preview(app, ['n1','n2','n3','n4','n5'], max_per_package=2)
    job = prepare(app, value)
    assert [p['line_ref']['id'] for p in job['pages']] == ['x4','x3','x5','x5','x2','x1']
    assert job['labels'] == 6


def test_segments_keep_global_spec_order_and_packaging_contiguous(app):
    numbers = []
    for i in range(105,0,-1):
        n = f'test-{i:03}'
        insert(app,n,n,'螺钉',f'M6x{i}')
        numbers.append(n)
    value = preview(app, numbers)
    assert [s['label_count'] for s in value['segments']] == [100,5]
    labels=[]
    for segment in value['segments']:
        job = prepare(app, value, segment['index'])
        labels += [p['line_ref']['id'] for p in job['pages']]
        report(app, job)
    assert labels == sorted(numbers)


def test_multiline_interleaving_keeps_whole_claim_and_sort(app):
    insert(app,'aa1','mixed','螺钉','M6x10')
    insert(app,'aa2','mixed','螺钉','M6x100')
    insert(app,'bb1','middle','螺钉','M6x20')
    value = preview(app, ['mixed','middle'])
    job = prepare(app, value)
    assert [p['line_ref']['id'] for p in job['pages']] == ['aa1','bb1','aa2']
    assert job['order_count'] == 2


def test_changed_sort_fields_invalidate_frozen_preview(app):
    value = preview(app,['ORDER-B'])
    store=app.extensions['service']['store']
    with store.connect(write=True) as db:
        item=json.loads(db.execute("SELECT payload FROM records WHERE id='line-b1'").fetchone()[0])
        item['spec']='M99'
        db.execute("UPDATE records SET payload=? WHERE id='line-b1'",(dumps(item),))
    with pytest.raises(JobConflict,match='规格已变化'): prepare(app,value)

def test_catalog_spec_fallback_matches_task_card_and_is_frozen(app):
    store=app.extensions['service']['store']
    with store.connect(write=True) as db:
        for ident,spec in [('stock-a','M10x90'),('stock-b','M6x20'),('stock-c','M6x9')]:
            item=json.loads(db.execute('SELECT payload FROM records WHERE id=?',(ident,)).fetchone()[0]);item['spec']=spec
            db.execute('UPDATE records SET payload=? WHERE id=?',(dumps(item),ident))
    value=preview(app,['ORDER-A','ORDER-B']);job=prepare(app,value)
    assert [p['line_ref']['id'] for p in job['pages']]==['line-b1','line-a2','line-a1']
