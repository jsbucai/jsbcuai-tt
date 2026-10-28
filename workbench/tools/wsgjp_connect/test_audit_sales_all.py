import unittest
from audit_sales_all import fetch_all_sales
from gateway import ApiError


class Reader:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.params = []

    def read(self, method, params):
        assert method == 'erp.bill.getsalebilllist'
        self.params.append(params)
        return next(self.pages)


class PagingTests(unittest.TestCase):
    def test_all_types_and_all_pages(self):
        reader = Reader([{'bills': [{'vchcode': '1'}, {'vchcode': '2'}], 'totalresults': 3},
                         {'bills': [{'vchcode': '3'}], 'totalresults': 3}])
        rows, result = fetch_all_sales(reader, 'begin', 'end', page_size=2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(result['pages'], 2)
        self.assertEqual([p['pageno'] for p in reader.params], [1, 2])
        self.assertTrue(all(p['dlytype'] == -1 for p in reader.params))

    def test_duplicate_page_not_accepted_as_complete(self):
        reader = Reader([{'bills': [{'vchcode': '1'}], 'totalresults': 2},
                         {'bills': [{'vchcode': '1'}], 'totalresults': 2}])
        with self.assertRaisesRegex(ApiError, 'sales_duplicate_id'):
            fetch_all_sales(reader, 'b', 'e', page_size=1)

    def test_changed_total_not_accepted(self):
        reader = Reader([{'bills': [{'vchcode': '1'}], 'totalresults': 2},
                         {'bills': [{'vchcode': '2'}], 'totalresults': 3}])
        with self.assertRaisesRegex(ApiError, 'sales_total_changed_during_paging'):
            fetch_all_sales(reader, 'b', 'e', page_size=1)

    def test_empty_incomplete_page_not_accepted(self):
        with self.assertRaisesRegex(ApiError, 'sales_page_coverage_incomplete'):
            fetch_all_sales(Reader([{'bills': [], 'totalresults': 1}]), 'b', 'e')


if __name__ == '__main__':
    unittest.main()
