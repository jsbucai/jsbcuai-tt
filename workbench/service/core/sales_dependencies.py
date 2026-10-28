"""Relevant catalog evidence for sales; stock counts are not order inputs."""
import json
from hashlib import sha256
from .store import dumps
from .daily_work import _setting

FIELDS = ('profile_id','warehouse_id','product_id','sku_id','base_unit','sku',
          'product_name','spec','model','standard','standard_no','grade','material','surface',
          'warehouse','warehouse_name','unit','unit_name','base_unit_name',
          'is_deleted','is_stopped','isstop','deleted','status')


def product_dependencies(db, profile, codes):
    """Stable business attributes, independent of snapshot row IDs/stock."""
    from .sales_skill_matching import _cached_inactive_products
    inactive = _cached_inactive_products(db, profile)
    result = {}
    for code in sorted(set(codes) - {''}):
        products = []
        for row in db.execute("SELECT payload FROM records WHERE kind='inventory' AND sku=?", (code,)):
            product = json.loads(row['payload'])
            if str(product.get('profile_id', '')) == profile:
                products.append({**{k: product.get(k) for k in FIELDS},
                                 'cached_inactive': str(product.get('product_id', '')) in inactive})
        result[code] = sha256(dumps(sorted(products, key=dumps)).encode()).hexdigest()
    return result


def changed_products(db, job, session):
    expected = json.loads(job['catalog_dependencies']) if 'catalog_dependencies' in job.keys() else {}
    actual = product_dependencies(db, session['profile_id'], expected)
    return [code for code in expected if expected[code] != actual.get(code)]


def catalog_fingerprint(db, profile):
    hasher = sha256()
    # Stable ordering and explicit fields: stock, images, logistics and receipt
    # timestamps must not invalidate an analysis in progress.
    for row in db.execute("SELECT id,payload FROM records WHERE kind='inventory' ORDER BY id"):
        product = json.loads(row['payload'])
        if str(product.get('profile_id','')) == profile:
            hasher.update(dumps([row['id'], {k: product.get(k) for k in FIELDS}]).encode())
    return hasher.hexdigest()


def job_catalog_current(db, job, session):
    snapshot = json.loads(job['input_json'])
    if snapshot.get('catalog_dependency_contract') == 2:
        # Analysis reads the current catalogue, then finish pins only the codes
        # it actually proposes. Inputs/ownership are checked by the pipeline.
        return not changed_products(db, job, session)
    expected = snapshot.get('catalog_fingerprint')
    if expected:
        return expected == catalog_fingerprint(db, session['profile_id'])
    # Existing frozen jobs retain their original contract.
    return _setting(db,'data_version','') == job['data_version']
