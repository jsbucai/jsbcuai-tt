"""Offline tests from the 2026-10-07 sales-order page observation."""
import unittest
from datetime import date

from core.sales_web_readback import (ALL_FILTER, AUDIT_FILTERS, SalesReadbackIncomplete,
    SalesReadbackScope, WisejSalesReadback, is_editor_line_candidate,
    parse_detail_cells, parse_detail_lines,
    parse_list_cells, parse_pager)


NUMBER = "UL26100713370454EE75"
LIST_CELLS = {2: "未审核", 6: "未提交", 8: "2026-10-07", 9: NUMBER,
              10: "备用零售客户", 13: "江苏优螺", 14: "李瑄",
              16: "2026-10-15", 18: "1", 19: "1.13"}
DETAIL_CELLS = {1: "LS3073", 32: "1", 41: "1", 42: "1",
                43: "13", 44: "0.13", 45: "1.13", 46: "1.13"}


class NoteField:
    def __init__(self, *, identity='', label_for='', text='', value='', count=1,
                 visible=True, on_screen=True):
        self.identity, self.label_for, self.text, self.value = identity, label_for, text, value
        self.number, self.visible, self.on_screen = count, visible, on_screen

    def count(self):
        return self.number

    def is_visible(self):
        return self.visible

    def get_attribute(self, name):
        return self.identity if name == 'id' else self.label_for if name == 'for' else None

    def inner_text(self):
        return self.text

    def input_value(self):
        return self.value

    def evaluate(self, _script):
        return self.on_screen


class NoteReadbackTests(unittest.TestCase):
    def reader(self, value=''):
        field = NoteField(identity='$editor$edComment', value=value)
        label = NoteField(identity='$editor$edComment_label', label_for=field.identity, text='附加说明')

        class Page:
            def locator(self, selector):
                if selector == 'input[id$="$edComment"]:visible':
                    return field
                if selector == 'label[id$="$edComment_label"]:visible':
                    return label
                raise AssertionError(selector)

        reader = WisejSalesReadback(Page(), SalesReadbackScope(
            'profile', 'account', 'tenant', 'operator', date(2026, 10, 7)))
        return reader, field, label

    def test_observed_label_and_empty_actual_note_are_readable(self):
        reader, field, _ = self.reader()
        self.assertEqual(reader._read_additional_note(), '')
        field.value = '用户实际附加说明'
        self.assertEqual(reader._read_additional_note(), '用户实际附加说明')

    def test_absent_duplicate_and_offscreen_note_never_become_empty(self):
        for number in (0, 2):
            reader, field, _ = self.reader()
            field.number = number
            with self.assertRaisesRegex(SalesReadbackIncomplete, 'sales_ui_control_ambiguous'):
                reader._read_additional_note()
        reader, field, _ = self.reader()
        field.on_screen = False
        with self.assertRaisesRegex(SalesReadbackIncomplete, 'sales_note_field_identity_unverified'):
            reader._read_additional_note()

    def test_label_text_for_and_value_have_to_be_observed(self):
        reader, field, label = self.reader()
        label.label_for = '$management$comment'
        with self.assertRaisesRegex(SalesReadbackIncomplete, 'sales_note_field_identity_unverified'):
            reader._read_additional_note()
        label.label_for = field.identity; label.text = '订单摘要'
        with self.assertRaisesRegex(SalesReadbackIncomplete, 'sales_note_field_identity_unverified'):
            reader._read_additional_note()
        label.text = '附加说明'; field.value = None
        with self.assertRaisesRegex(SalesReadbackIncomplete, 'sales_note_value_unavailable'):
            reader._read_additional_note()


class FakeWisej(WisejSalesReadback):
    def __init__(self, by_status=None, *, complete=True):
        scope = SalesReadbackScope("profile", "account", "tenant", "operator", date(2026, 10, 7))
        super().__init__(None, scope)
        self.by_status = by_status or {}
        self.complete = complete
        self.visited = []

    def _ensure_sales_page(self):
        pass

    def _range_includes_order(self):
        return self.complete

    def _audit_options(self):
        return set(AUDIT_FILTERS)

    def _query_status(self, number, audit_filter):
        self.visited.append(audit_filter)
        rows = self.by_status.get(audit_filter, [])
        return len(rows), rows


class ReadbackTests(unittest.TestCase):
    def test_captured_list_and_detail_cell_positions(self):
        listing = parse_list_cells(LIST_CELLS)
        detail = parse_detail_cells(DETAIL_CELLS)
        self.assertEqual(listing["number"], NUMBER)
        self.assertEqual(listing["audit_status"], "未审核")
        self.assertEqual(listing["taxed_total"], "1.13")
        self.assertEqual(detail["sku"], "LS3073")
        self.assertEqual(detail["tax_rate_percent"], "13")
        self.assertEqual(detail["taxed_line_amount"], "1.13")

    def test_missing_column_and_non_numeric_pager_fail_closed(self):
        with self.assertRaises(SalesReadbackIncomplete):
            parse_list_cells({key: value for key, value in LIST_CELLS.items() if key != 19})
        with self.assertRaises(SalesReadbackIncomplete):
            parse_detail_cells({key: value for key, value in DETAIL_CELLS.items() if key != 43})
        self.assertEqual(parse_pager("(1条记录)"), 1)
        with self.assertRaises(SalesReadbackIncomplete):
            parse_pager("第1页")

    def test_repeated_sku_detail_rows_keep_independent_note_identity(self):
        one = {**DETAIL_CELLS, 50: "PO-123#项目甲#1#图号甲#六角头螺栓#M20X80#8.8级#镀锌#1#LS3073"}
        two = {**DETAIL_CELLS, 50: "PO-123#项目甲#2#图号甲#六角头螺栓#M20X80#8.8级#镀锌#1#LS3073"}
        lines = parse_detail_lines([two, one])
        self.assertEqual([(line["line_id"], line["sku"]) for line in lines],
                         [("2", "LS3073"), ("1", "LS3073")])
        self.assertEqual([(line["unit_raw"], line["unit_source"]) for line in lines],
                         [("", "column_absent"), ("", "column_absent")])
        with self.assertRaises(SalesReadbackIncomplete):
            parse_detail_lines([one, one])
        with self.assertRaises(SalesReadbackIncomplete):
            parse_detail_lines([one, DETAIL_CELLS])
        with self.assertRaisesRegex(SalesReadbackIncomplete, 'sales_line_note_invalid'):
            parse_detail_lines([DETAIL_CELLS])

    def test_underlying_management_row_is_not_an_editor_line(self):
        class Count:
            def __init__(self, value):
                self.value = value

            def count(self):
                return self.value

        class Row:
            def __init__(self, *, modify, amount_cell):
                self.modify, self.amount_cell = modify, amount_cell

            def is_visible(self):
                return True

            def locator(self, selector):
                if selector == 'a[_buttonvalue="modify"]':
                    return Count(self.modify)
                if selector == 'td[_columnindex="46"]':
                    return Count(self.amount_cell)
                raise AssertionError(selector)

        self.assertFalse(is_editor_line_candidate(Row(modify=1, amount_cell=1)))
        self.assertTrue(is_editor_line_candidate(Row(modify=0, amount_cell=1)))

    def test_full_number_search_includes_audited_status(self):
        audited = {**parse_list_cells(LIST_CELLS), "audit_status": "审核通过"}
        reader = FakeWisej({"审核通过订单": [audited]})
        result = reader.find_by_full_number(NUMBER)
        self.assertTrue(result.complete)
        self.assertEqual(result.rows, (audited,))
        self.assertEqual(tuple(reader.visited), AUDIT_FILTERS)

    def test_same_record_across_audit_and_shipping_tabs_is_one_order(self):
        listing = parse_list_cells(LIST_CELLS)
        reader = FakeWisej({"未审核订单": [listing],
                            "未提交发货订单": [dict(listing)]})
        result = reader.find_by_full_number(NUMBER)
        self.assertTrue(result.complete)
        self.assertEqual(result.rows, (listing,))
        # Two equal records in one tab might be duplicate business documents;
        # they must remain visible to the transport's ambiguity check.
        reader = FakeWisej({"未审核订单": [listing, dict(listing)]})
        self.assertEqual(len(reader.find_by_full_number(NUMBER).rows), 2)

    def test_all_filter_is_queried_when_present_but_unknown_filter_fails_closed(self):
        listing = parse_list_cells(LIST_CELLS)
        reader = FakeWisej({"未审核订单": [listing], ALL_FILTER: [dict(listing)]})
        reader._audit_options = lambda: set(AUDIT_FILTERS) | {ALL_FILTER}
        result = reader.find_by_full_number(NUMBER)
        self.assertTrue(result.complete)
        self.assertEqual(result.rows, (listing,))
        self.assertEqual(tuple(reader.visited), (*AUDIT_FILTERS, ALL_FILTER))
        reader._audit_options = lambda: set(AUDIT_FILTERS) | {ALL_FILTER, "其他业务状态"}
        self.assertFalse(reader.find_by_full_number(NUMBER).complete)

    def test_first_sales_page_navigation_waits_then_checks_unique_input(self):
        class Item:
            def __init__(self, text):
                self.text = text

            def is_visible(self):
                return True

            def inner_text(self):
                return self.text

            def evaluate(self, _script):
                return True

            def click(self):
                pass

        class Menu:
            def __init__(self, item):
                self.item = item

            def all(self):
                return [self.item]

        class Field:
            ready = False
            count_after_wait = 1
            waited = False
            timeout = False

            def count(self):
                return self.count_after_wait if self.ready else 0

            def is_visible(self):
                return self.ready

            def wait_for(self, *, state, timeout):
                self.waited = True
                if self.timeout:
                    raise TimeoutError('navigation did not finish')
                self.ready = True
                assert state == 'visible' and 0 < timeout <= 10000

        class Page:
            def __init__(self, field):
                self.field = field

            def locator(self, selector):
                if selector == 'input[id$="$edNumber"]:visible':
                    return MenuEmpty()
                if selector == 'input[id$="$number"]:visible':
                    return self.field
                if selector == '.MenuRootCaption':
                    return Menu(Item('销售'))
                if selector == '.MenuCaption':
                    return Menu(Item('销售订单管理'))
                raise AssertionError(selector)

        class MenuEmpty:
            def count(self):
                return 0

        reader = WisejSalesReadback(None, SalesReadbackScope(
            'profile', 'account', 'tenant', 'operator', date(2026, 10, 7)))
        reader.account_identity = lambda: ('profile', 'account')
        field = Field()
        reader.page = Page(field)
        reader._ensure_sales_page()
        self.assertTrue(field.waited)
        field = Field()
        field.count_after_wait = 2
        reader.page = Page(field)
        with self.assertRaises(SalesReadbackIncomplete):
            reader._ensure_sales_page()
        field = Field()
        field.timeout = True
        reader.page = Page(field)
        with self.assertRaisesRegex(SalesReadbackIncomplete, 'sales_page_not_ready'):
            reader._ensure_sales_page()

    def test_status_inventory_starts_from_last_selected_tab(self):
        class Field:
            index = len(AUDIT_FILTERS) - 1

            def input_value(self):
                return AUDIT_FILTERS[self.index]

            def click(self):
                pass

            def press(self, key):
                offset = -1 if key == "ArrowUp" else 1
                self.index = min(max(self.index + offset, 0), len(AUDIT_FILTERS) - 1)

        reader = FakeWisej()
        field = Field()
        reader._one = lambda _selector: field
        self.assertEqual(reader._audit_options(), set(AUDIT_FILTERS))

    def test_incomplete_range_or_missing_status_is_never_zero_match(self):
        reader = FakeWisej(complete=False)
        result = reader.find_by_full_number(NUMBER)
        self.assertFalse(result.complete)
        self.assertEqual(result.rows, ())
        self.assertEqual(reader.visited, [])
        reader = FakeWisej()
        reader._audit_options = lambda: {"未审核订单"}
        self.assertFalse(reader.find_by_full_number(NUMBER).complete)

    def test_editor_close_uses_tab_x_and_allows_repeat_lookup(self):
        reader = FakeWisej()

        class Element:
            def __init__(self, value="", clicked=None):
                self.value, self.clicked = value, clicked

            def count(self):
                return 1

            def inner_text(self):
                return self.value

            def click(self):
                self.clicked()

        class Tab:
            def is_visible(self):
                return True

            def locator(self, selector):
                if selector == ".TabCaptionText":
                    return Element("销售订单")
                return Element(clicked=lambda: setattr(page, "closing", True))

        class Collection:
            def __init__(self, value):
                self.value = value

            def all(self):
                return self.value

            def count(self):
                return 1 if page.open else 0

            def wait_for(self, *, state, timeout):
                assert state == 'hidden' and 0 < timeout <= 10000
                assert page.closing
                page.open = False

        class Page:
            open = True
            closing = False

            def locator(self, selector):
                return Collection([Tab()] if selector == ".MDITabCaption" else [])

            def wait_for_timeout(self, _ms):
                pass

        page = Page()
        reader.page = page
        reader._close_editor()
        self.assertFalse(page.open)
        self.assertTrue(reader.find_by_full_number(NUMBER).complete)

    def test_editor_close_discard_requires_exact_modal_and_never_saves(self):
        class Element:
            def __init__(self, *, text='', clicked=None):
                self.text, self.clicked = text, clicked

            def count(self):
                return 1

            def inner_text(self):
                return self.text

            def is_visible(self):
                return True

            def evaluate(self, _script):
                return True

            def click(self):
                if self.clicked:
                    self.clicked()

            def locator(self, selector):
                if selector == '.TabCaptionText':
                    return Element(text='销售订单')
                return Element(clicked=lambda: None)

        class Collection:
            def __init__(self, items=()):
                self.items = items

            def all(self):
                return list(self.items)

        class Editor:
            def __init__(self, page):
                self.page = page

            def wait_for(self, *, state, timeout):
                assert state == 'hidden' and 0 < timeout <= 10000
                if self.page.editor_open:
                    raise TimeoutError('close did not finish')

            def count(self):
                return int(self.page.editor_open)

        class Page:
            def __init__(self):
                self.prompt = 'none'
                self.editor_open = True
                self.save_clicks = 0
                self.discard_clicks = 0

            def locator(self, selector):
                if selector == '.MDITabCaption':
                    return Collection([Element()])
                if selector == 'input[id$="$edNumber"]:visible':
                    return Editor(self)
                raise AssertionError(selector)

            def get_by_text(self, label, *, exact):
                labels = ('保存提示', '请选择对本单据的处理，按《ESC》键放弃本次处理！',
                          '保存单据', '废弃退出')
                if self.prompt == 'none' or label not in labels:
                    return Collection()
                if self.prompt == 'partial' and label != '保存提示':
                    return Collection()
                if label == '保存单据':
                    clicked = lambda: setattr(self, 'save_clicks', self.save_clicks + 1)
                elif label == '废弃退出':
                    def clicked():
                        self.discard_clicks += 1
                        self.editor_open = False
                else:
                    clicked = None
                return Collection([Element(text=label, clicked=clicked)])

        page = Page()
        reader = WisejSalesReadback(page, SalesReadbackScope(
            'profile', 'account', 'tenant', 'operator', date(2026, 10, 7)))
        with self.assertRaisesRegex(SalesReadbackIncomplete, 'sales_editor_not_closed'):
            reader._close_editor()
        page.prompt = 'partial'
        with self.assertRaisesRegex(SalesReadbackIncomplete, 'sales_editor_close_confirmation_required'):
            reader._close_editor()
        page.prompt = 'verified'
        reader._close_editor()
        self.assertEqual((page.discard_clicks, page.save_clicks), (1, 0))


if __name__ == "__main__":
    unittest.main()
