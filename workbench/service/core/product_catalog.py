"""Current catalog membership is determined by ERP identity and deletion flag."""


def is_deleted(product):
    # ERP uses `deleted`; published inventory uses `is_deleted`.  In particular,
    # the string 'false' must not hide a valid product.
    return any(str(product.get(key, '')).strip().lower() in {'true', '1'}
               for key in ('deleted', 'is_deleted'))


def current_products(products):
    # Do not merge by usercode: ERP can reuse it after deleting a product.
    # Stopped products remain visible with their existing stopped status.
    return [product for product in products if not is_deleted(product)]
