"""Read-only Wisej sales-order lookup for the observed 网上管家婆 UI.

Selectors and column numbers were captured on 2026-10-07 against the
江苏优螺 ERP ``Main.gspx`` sales-order list and editor. This adapter makes no
business-data writes: opening ``编辑`` is solely for reading the existing order.
Any missing control, incomplete status search, or pagination mismatch yields
an incomplete list, so the import transport cannot treat a false zero as safe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Mapping

from .sales_web_transport import SalesListResult


AUDIT_FILTERS = (
    "未审核订单", "审核通过订单", "未提交发货订单", "已提交发货订单",
)
ALL_FILTER = "全部显示"
FILTER_FOR_STATUS = {"未审核": "未审核订单", "审核通过": "审核通过订单",
                     "未提交发货": "未提交发货订单", "已提交发货": "已提交发货订单"}
LIST_COLUMNS = {
    "audit_status": 2, "shipping_status": 6, "date": 8, "number": 9,
    "customer": 10, "warehouse": 13, "salesperson": 14,
    "delivery_date": 16, "untaxed_total": 18, "taxed_total": 19,
}
DETAIL_COLUMNS = {
    "sku": 1, "quantity": 32, "untaxed_unit_price": 41,
    "untaxed_line_amount": 42, "tax_rate_percent": 43,
    "tax_amount": 44, "taxed_unit_price": 45, "taxed_line_amount": 46,
}
DETAIL_NOTE_COLUMN = 50
PAGER = re.compile(r"^\(\s*(\d+)\s*条记录\s*\)$")
ON_SCREEN = ("e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0"
             "&&r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight}")
NAVIGATION_WAIT_MS = 10000


class SalesReadbackIncomplete(RuntimeError):
    """The ERP page did not prove a complete read-only lookup."""


@dataclass(frozen=True)
class SalesReadbackScope:
    profile_id: str
    account_id: str
    tenant_name: str
    operator_name: str
    order_date: date


def parse_pager(value: str) -> int:
    matched = PAGER.fullmatch(str(value).strip())
    if not matched:
        raise SalesReadbackIncomplete("sales_pager_unrecognized")
    return int(matched.group(1))


def parse_list_cells(cells: Mapping[int, str]) -> dict[str, str]:
    try:
        result = {name: str(cells[index]).strip() for name, index in LIST_COLUMNS.items()}
    except (KeyError, TypeError):
        raise SalesReadbackIncomplete("sales_list_columns_missing") from None
    if not all(result.values()):
        raise SalesReadbackIncomplete("sales_list_value_missing")
    return result


def parse_detail_cells(cells: Mapping[int, str]) -> dict[str, str]:
    try:
        result = {name: str(cells[index]).strip() for name, index in DETAIL_COLUMNS.items()}
    except (KeyError, TypeError):
        raise SalesReadbackIncomplete("sales_detail_columns_missing") from None
    if not all(result.values()):
        raise SalesReadbackIncomplete("sales_detail_value_missing")
    return result


def parse_detail_line(cells: Mapping[int, str]) -> dict[str, str]:
    """Read a grid row using the observed 10-field note as line identity."""
    result = parse_detail_cells(cells)
    note = str(cells.get(DETAIL_NOTE_COLUMN, '')).strip()
    parts = [part.strip() for part in note.split("#")]
    if (len(parts) != 10 or not all(parts) or not parts[2].isdigit()
            or int(parts[2]) <= 0 or parts[9] != result["sku"]):
        raise SalesReadbackIncomplete("sales_line_note_invalid")
    result["line_id"], result["detail_note"] = str(int(parts[2])), note
    # The observed editor has no independent unit column. Preserve that fact
    # for the transport, which may apply the owner's blank-unit rule only if
    # the frozen product identity says ERP base unit ID 1.
    result["unit_raw"], result["unit_source"] = "", "column_absent"
    return result


def parse_detail_lines(rows: list[Mapping[int, str]]) -> tuple[dict[str, str], ...]:
    lines = tuple(parse_detail_line(cells) for cells in rows)
    if not lines:
        raise SalesReadbackIncomplete("sales_detail_line_ambiguous")
    if len(lines) > 1:
        identities = [(line.get("line_id"), line["sku"]) for line in lines]
        if any(not line_id for line_id, _ in identities) or len(set(identities)) != len(lines):
            raise SalesReadbackIncomplete("sales_detail_line_identity_missing")
    return lines


def is_editor_line_candidate(row) -> bool:
    """Exclude the sales-management row that remains mounted behind editor."""
    return (row.is_visible() and row.locator('a[_buttonvalue="modify"]').count() == 0
            and row.locator('td[_columnindex="46"]').count() == 1)


class WisejSalesReadback:
    """Site-specific ``SalesWebReadback`` over an already logged-in page.

    ``account_identity`` may be supplied as the transport's identity reader.
    The ERP UI reveals tenant and operator names, but not its numeric profile
    ID, so those IDs must be bound to the same verified login configuration.
    """

    def __init__(self, page, scope: SalesReadbackScope, *, settle_ms: int = 1800):
        if not 0 <= settle_ms <= 10000:
            raise ValueError("invalid Wisej settle time")
        self.page, self.scope, self.settle_ms = page, scope, settle_ms

    @staticmethod
    def _exact_on_screen(locator, text: str):
        matches = [item for item in locator.all()
                   if item.is_visible() and item.inner_text().strip() == text
                   and item.evaluate(ON_SCREEN)]
        if len(matches) != 1:
            raise SalesReadbackIncomplete("sales_ui_target_ambiguous")
        return matches[0]

    def _one(self, selector: str):
        locator = self.page.locator(selector)
        if locator.count() != 1 or not locator.is_visible():
            raise SalesReadbackIncomplete("sales_ui_control_ambiguous")
        return locator

    def _read_additional_note(self) -> str:
        """Read the observed editor field; absence is not an empty note.

        Sales management also keeps an off-screen ``$comment`` query field.
        The sample editor's separate ``$edComment`` field was observed on
        N150 on 2026-10-07, with a label-for link and text 附加说明.
        """
        field = self._one('input[id$="$edComment"]:visible')
        label = self._one('label[id$="$edComment_label"]:visible')
        identity = field.get_attribute('id')
        if (not identity or label.get_attribute('for') != identity or
                label.inner_text().strip() != '附加说明' or
                not field.evaluate(ON_SCREEN) or not label.evaluate(ON_SCREEN)):
            raise SalesReadbackIncomplete('sales_note_field_identity_unverified')
        value = field.input_value()
        if not isinstance(value, str):
            raise SalesReadbackIncomplete('sales_note_value_unavailable')
        return value

    def account_identity(self) -> tuple[str, str]:
        if not self.page.url.lower().startswith("https://") or not self.page.url.lower().split("?", 1)[0].endswith("/main.gspx"):
            raise SalesReadbackIncomplete("erp_main_page_missing")
        if self.page.title().strip() != f"网上管家婆 –【{self.scope.tenant_name}】":
            raise SalesReadbackIncomplete("erp_tenant_mismatch")
        operators = [item for item in self.page.locator("span.Label.ellipsis").all()
                     if item.is_visible() and item.inner_text().strip() == self.scope.operator_name
                     and item.evaluate(ON_SCREEN)]
        if len(operators) != 1:
            raise SalesReadbackIncomplete("erp_operator_mismatch")
        if "您的账号已经在其他地方登录" in self.page.locator("body").inner_text():
            raise SalesReadbackIncomplete("erp_session_conflict")
        return self.scope.profile_id, self.scope.account_id

    def _ensure_sales_page(self) -> None:
        self.account_identity()
        if self.page.locator('input[id$="$edNumber"]:visible').count():
            raise SalesReadbackIncomplete("sales_editor_still_open")
        if self.page.locator('input[id$="$number"]:visible').count() == 1:
            return
        self._exact_on_screen(self.page.locator(".MenuRootCaption"), "销售").click()
        self._exact_on_screen(self.page.locator(".MenuCaption"), "销售订单管理").click()
        # Wisej mounts the page asynchronously after the menu click. A wait
        # only establishes readiness; _one still rejects duplicate controls.
        try:
            self.page.locator('input[id$="$number"]:visible').wait_for(
                state="visible", timeout=NAVIGATION_WAIT_MS)
        except Exception:
            raise SalesReadbackIncomplete("sales_page_not_ready") from None
        self._one('input[id$="$number"]:visible')

    def _audit_options(self) -> set[str]:
        field = self._one('input[id$="$drpIsAuditFlag"]:visible')
        # Start from either end safely. Wisej may stop at the last entry or
        # wrap, and a recovery read can begin on any previously selected tab.
        seen_up = {field.input_value().strip()}
        for _ in range(10):
            before = field.input_value().strip()
            field.click()
            field.press("ArrowUp")
            value = field.input_value().strip()
            if value == before or value in seen_up:
                break
            seen_up.add(value)
        else:
            raise SalesReadbackIncomplete("sales_audit_options_unbounded")
        options = [field.input_value().strip()]
        for _ in range(10):
            field.click()
            field.press("ArrowDown")
            value = field.input_value().strip()
            if value in options:
                break
            options.append(value)
        else:
            raise SalesReadbackIncomplete("sales_audit_options_unbounded")
        return set(options)

    def _select_audit(self, target: str) -> None:
        field = self._one('input[id$="$drpIsAuditFlag"]:visible')
        if target not in (*AUDIT_FILTERS, ALL_FILTER):
            raise SalesReadbackIncomplete("sales_audit_filter_unknown")
        for direction in ("ArrowDown", "ArrowUp"):
            for _ in range(5):
                if field.input_value().strip() == target:
                    break
                before = field.input_value()
                field.click()
                field.press(direction)
                if field.input_value() == before:
                    break
        if field.input_value().strip() != target:
            raise SalesReadbackIncomplete("sales_audit_filter_not_selected")
        self._one('button[id$="$button1"]:visible').click()
        self.page.wait_for_timeout(self.settle_ms)

    def _search_exact(self, number: str) -> None:
        button = self._one('button[id$="$button3"]:visible')
        if not button.evaluate(ON_SCREEN):
            self._one('button[id$="$heightQueryBtn"]:visible').click()
        field = self._one('input[id$="$number"]:visible')
        field.fill(number)
        if not button.evaluate(ON_SCREEN):
            self._one('button[id$="$heightQueryBtn"]:visible').click()
        if not button.evaluate(ON_SCREEN):
            raise SalesReadbackIncomplete("sales_advanced_query_offscreen")
        button.click()
        self.page.wait_for_timeout(self.settle_ms)
        if field.input_value() != number:
            raise SalesReadbackIncomplete("sales_number_filter_not_retained")

    def _range_includes_order(self) -> bool:
        try:
            start = date.fromisoformat(self._one('input[id$="$createBeginDate"]:visible').input_value())
            end = date.fromisoformat(self._one('input[id$="$createEndDate"]:visible').input_value())
            return start <= self.scope.order_date <= end
        except ValueError:
            return False

    def _main_rows(self):
        return [row for row in self.page.locator("tr.GridBodyRow").all()
                if row.is_visible() and row.locator('a[_buttonvalue="modify"]').count() == 1]

    @staticmethod
    def _cells(row, columns: Mapping[str, int]) -> dict[int, str]:
        cells = {}
        for index in set(columns.values()):
            locator = row.locator(f'td[_columnindex="{index}"]')
            if locator.count() != 1:
                raise SalesReadbackIncomplete("sales_grid_column_ambiguous")
            cells[index] = locator.inner_text().strip()
        return cells

    def _query_status(self, number: str, audit_filter: str) -> tuple[int, list[dict[str, str]]]:
        self._select_audit(audit_filter)
        self._search_exact(number)
        pagers = [text.strip() for text in self.page.locator(".PagerSummaryText:visible").all_inner_texts()]
        if len(pagers) != 1:
            raise SalesReadbackIncomplete("sales_pager_ambiguous")
        total = parse_pager(pagers[0])
        rows = [parse_list_cells(self._cells(row, LIST_COLUMNS)) for row in self._main_rows()]
        if total != len(rows) or any(row["number"] != number for row in rows):
            raise SalesReadbackIncomplete("sales_pagination_or_filter_incomplete")
        return total, rows

    def find_by_full_number(self, order_number: str) -> SalesListResult:
        """Search every observed audit/shipping tab; never turn an error into 0."""
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", order_number):
                raise SalesReadbackIncomplete("sales_number_invalid")
            self._ensure_sales_page()
            options = self._audit_options()
            known = set(AUDIT_FILTERS)
            if not self._range_includes_order() or options not in (known, known | {ALL_FILTER}):
                raise SalesReadbackIncomplete("sales_range_or_status_incomplete")
            found = []
            filters = (*AUDIT_FILTERS, ALL_FILTER) if ALL_FILTER in options else AUDIT_FILTERS
            for audit_filter in filters:
                _, rows = self._query_status(order_number, audit_filter)
                # The shipping tabs can overlap an audit tab. Collapse only
                # the same complete row seen in an earlier tab; duplicates
                # within one tab remain ambiguous business records.
                earlier = tuple(found)
                for row in rows:
                    if row not in earlier:
                        found.append(row)
            return SalesListResult(True, tuple(found))
        except Exception:
            return SalesListResult(False, ())

    def open_detail_readonly(self, row: Mapping[str, str]) -> Mapping[str, object]:
        """Open the matching editor to read it; never click save or audit."""
        number = str(row.get("number", ""))
        audit_filter = FILTER_FOR_STATUS.get(str(row.get("audit_status", "")))
        if not audit_filter or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", number):
            raise SalesReadbackIncomplete("sales_row_identity_invalid")
        self._ensure_sales_page()
        _, rows = self._query_status(number, audit_filter)
        if len(rows) != 1 or rows[0] != dict(row):
            raise SalesReadbackIncomplete("sales_detail_row_changed")
        matching = [item for item in self._main_rows()
                    if self._cells(item, {"number": 9})[9] == number]
        if len(matching) != 1:
            raise SalesReadbackIncomplete("sales_detail_row_ambiguous")
        matching[0].locator('a[_buttonvalue="modify"]').click()
        self.page.wait_for_timeout(self.settle_ms)
        try:
            if self._one('input[id$="$edNumber"]:visible').input_value() != number:
                raise SalesReadbackIncomplete("sales_editor_number_mismatch")
            summary = self._one('input[id$="$edSummary"]:visible').input_value()
            note = self._read_additional_note()
            maker = self._one('[id$="$lblInput"]:visible').inner_text().strip()
            if not maker:
                raise SalesReadbackIncomplete("sales_maker_missing")
            row_cells = []
            for item in self.page.locator("tr.GridBodyRow").all():
                # Sales management remains mounted behind the editor. Its
                # order row has the edit action and shares numeric column
                # indexes; exclude it before parsing editor product rows.
                if not is_editor_line_candidate(item):
                    continue
                cells = self._cells(item, {**DETAIL_COLUMNS, "detail_note": DETAIL_NOTE_COLUMN})
                if cells[DETAIL_COLUMNS["sku"]]:
                    row_cells.append(cells)
            lines = parse_detail_lines(row_cells)
            return {"number": number, "maker": maker, "summary": summary, "note": note,
                    **lines[0], "lines": tuple(lines)}
        finally:
            self._close_editor()

    def _close_editor(self) -> None:
        tabs = [item for item in self.page.locator(".MDITabCaption").all()
                if item.is_visible() and item.locator(".TabCaptionText").count() == 1
                and item.locator(".TabCaptionText").inner_text().strip() == "销售订单"]
        if len(tabs) != 1 or tabs[0].locator(".TabCloseButton").count() != 1:
            raise SalesReadbackIncomplete("sales_editor_tab_ambiguous")
        tabs[0].locator(".TabCloseButton").click()
        editor = self.page.locator('input[id$="$edNumber"]:visible')
        try:
            editor.wait_for(state="hidden", timeout=1000)
        except Exception:
            discard = self._verified_discard_control()
            if discard is None:
                try:
                    editor.wait_for(state="hidden", timeout=NAVIGATION_WAIT_MS - 1000)
                except Exception:
                    discard = self._verified_discard_control()
            if discard is not None:
                # The observed Wisej modal explicitly distinguishes saving
                # from discarding the editor state. A read-only lookup may
                # choose only this exact, uniquely visible discard action.
                discard.click()
                try:
                    editor.wait_for(state="hidden", timeout=NAVIGATION_WAIT_MS)
                except Exception:
                    raise SalesReadbackIncomplete("sales_editor_not_closed") from None
            elif editor.count():
                raise SalesReadbackIncomplete("sales_editor_not_closed")
        if editor.count():
            raise SalesReadbackIncomplete("sales_editor_not_closed")

    def _verified_discard_control(self):
        labels = ("保存提示", "请选择对本单据的处理，按《ESC》键放弃本次处理！",
                  "保存单据", "废弃退出")
        try:
            matches = [[item for item in self.page.get_by_text(label, exact=True).all()
                        if item.is_visible() and item.evaluate(ON_SCREEN)] for label in labels]
        except Exception:
            raise SalesReadbackIncomplete("sales_editor_close_confirmation_required") from None
        if not any(matches):
            return None
        if any(len(items) != 1 for items in matches):
            raise SalesReadbackIncomplete("sales_editor_close_confirmation_required")
        return matches[-1][0]
