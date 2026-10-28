"""Offline audit guard and comparison tests. No vault reads or network requests."""
import unittest
from audit_order_sources import AuditReader, compare_receipts
from gateway import ApiError


class AuditTests(unittest.TestCase):
    def reader(self):
        return AuditReader.__new__(AuditReader)

    def test_business_write_rejected_before_credentials_or_transport(self):
        with self.assertRaisesRegex(ApiError, 'audit_read_only_method_not_allowed'):
            self.reader().read('beefun.orderfreight.updatefreight', {})

    def test_reserved_method_cannot_be_injected(self):
        with self.assertRaisesRegex(ApiError, 'reserved_parameters'):
            self.reader().read('erp.freight.list', {'method': 'write'})

    def test_request_budget_rejected_before_credentials(self):
        reader = self.reader()
        reader.calls = 12
        with self.assertRaisesRegex(ApiError, 'audit_call_limit'):
            reader.read('erp.freight.list', {})

    def test_receipt_join_uses_document_product_warehouse_date(self):
        local = [{'单据编号': 'TEST1', '商品编号': 'LS0001', '仓库': 'W1',
                  '单据日期': '2026-09-15', '单据类型': '进货入库单', '数量': '0.3'}]
        bills = [{'number': 'TEST1', 'date': '2026-09-15T00:00:00', 'kfullname': 'W1',
                  'details': [{'usercode': 'LS0001', 'qty': '0.1'}, {'usercode': 'LS0001', 'qty': '0.2'},
                              {'usercode': 'LS0001', 'qty': '0.3', 'kfullname': 'W2'}]}]
        counts = compare_receipts(bills, local, 0, '2026-09-16T15:00:00')['counts']
        self.assertEqual(counts['quantity_equal'], 1)
        self.assertEqual(counts['document_exists_but_line_key_differs'], 1)
        self.assertNotIn('quantity_different', counts)

    def test_post_export_document_is_distinguished_from_quantity_mismatch(self):
        bills = [{'number': 'TEST2', 'date': '2026-09-16T00:00:00', 'kfullname': 'W1',
                  'overtime': '2026-09-16T16:00:00', 'details': [{'usercode': 'LS0001', 'qty': 1}]}]
        counts = compare_receipts(bills, [], 0, '2026-09-16T15:00:00')['counts']
        self.assertEqual(counts['missing_document_posted_after_export'], 1)
        self.assertNotIn('quantity_different', counts)


if __name__ == '__main__':
    unittest.main()
