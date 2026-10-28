from datetime import date
from decimal import Decimal
from pathlib import Path
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from purchase_export_collector.collector import (
    CollectorError, _account_scope, _allowed_https, _click_exact, _login,
    _official_login_entry, _official_password_frame, _read_credentials,
    _web_row_count, publish_snapshot,
)
from purchase_export_collector.validator import ExportValidationError, validate_export


FIXTURES = (
    Path("/Users/apple/Downloads/2026_10_04-21_54_16.xlsx"),
    Path("/Users/apple/Downloads/2026_10_06-00_35_44.xlsx"),
)
START = date(2025, 9, 1)
END = date(2026, 10, 6)


@unittest.skipUnless(all(path.is_file() for path in FIXTURES), "local exported workbooks unavailable")
class ExportFixtureTests(unittest.TestCase):
    def test_both_real_exports_match_observed_business_shape(self):
        first = validate_export(FIXTURES[0], start=START, end=date(2026, 10, 4),
                                min_unfinished=Decimal("0.1"), expected_rows=90)
        second = validate_export(FIXTURES[1], start=START, end=END,
                                 min_unfinished=Decimal("0.1"), expected_rows=90)
        for result in (first, second):
            self.assertEqual(result["header_row"], 5)
            self.assertEqual(result["row_count"], 90)
            self.assertEqual(result["order_count"], 38)
            self.assertEqual(result["supplier_count"], 11)
            self.assertEqual(result["unfinished_sum"], "14853")
            self.assertFalse(result["units_verified"])
            self.assertFalse(result["coverage_complete"])
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_fail_closed_on_wrong_scope_or_page_count(self):
        for kwargs, reason in (
            ({"start": date(2025, 8, 31), "end": END}, "start_date_mismatch"),
            ({"start": START, "end": date(2026, 10, 5)}, "end_date_mismatch"),
            ({"start": START, "end": END, "expected_rows": 89}, "web_file_count_mismatch"),
            ({"start": START, "end": END, "min_unfinished": Decimal("2")}, "unfinished_below_filter"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(ExportValidationError, reason):
                validate_export(FIXTURES[1], **kwargs)

    def test_failed_validation_keeps_last_success_pointer(self):
        metadata = validate_export(FIXTURES[1], start=START, end=END, expected_rows=90)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            scope = _account_scope("C", "https://bfuned5main.wsgjp.com.cn/Main.gspx")
            manifest = publish_snapshot(FIXTURES[1], output, metadata, web_rows=90,
                                        account_scope=scope)
            self.assertTrue(manifest.is_file())
            pointer_before = (output / "latest.json").read_bytes()
            with self.assertRaisesRegex(ExportValidationError, "web_file_count_mismatch"):
                validate_export(FIXTURES[1], start=START, end=END, expected_rows=91)
            self.assertEqual((output / "latest.json").read_bytes(), pointer_before)
            pointer = json.loads(pointer_before)
            self.assertEqual(pointer["manifest"], str(manifest.relative_to(output)))
            published = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(published["account_scope"], scope)
            self.assertEqual(published["account_scope_basis"],
                             "login_company_and_verified_erp_host_v1")


class CredentialBoundaryTests(unittest.TestCase):
    def test_login_scope_changes_with_company_or_verified_erp_host(self):
        main = "https://bfuned5main.wsgjp.com.cn/Main.gspx"
        scope = _account_scope("公司甲", main)
        self.assertEqual(len(scope), 64)
        self.assertEqual(scope, _account_scope(" 公司甲 ", main))
        self.assertNotEqual(scope, _account_scope("公司乙", main))
        self.assertNotEqual(scope, _account_scope(
            "公司甲", "https://bfuned6main.wsgjp.com.cn/Main.gspx"))
        for url in ("http://bfuned5main.wsgjp.com.cn/Main.gspx",
                    "https://bfuned5main.wsgjp.com.cn/other",
                    "https://bfuned5main.wsgjp.com.cn.evil.example/Main.gspx"):
            with self.subTest(url=url), self.assertRaisesRegex(
                    CollectorError, "account_scope_unverified"):
                _account_scope("公司甲", url)

    def test_only_official_https_hosts_allowed(self):
        self.assertTrue(_allowed_https("https://bfuned5main.wsgjp.com.cn/Main.gspx"))
        self.assertFalse(_allowed_https("http://bfuned5main.wsgjp.com.cn/Main.gspx"))
        self.assertFalse(_allowed_https("https://wsgjp.com.cn.evil.example/login"))
        self.assertFalse(_allowed_https("https://user:pass@wsgjp.com.cn/login"))

    def test_login_entry_and_password_frame_are_exact_official_pages(self):
        self.assertTrue(_official_login_entry("https://www.mygjp.com.cn/ngpLogin.aspx"))
        self.assertTrue(_official_password_frame("https://passport.mygjp.com.cn/erp/ngploginNew?cssurl=x"))
        self.assertFalse(_official_login_entry("https://www.mygjp.com.cn/not-login"))
        self.assertFalse(_official_login_entry("https://www.mygjp.com.cn.evil.example/ngpLogin.aspx"))
        self.assertFalse(_official_password_frame("http://passport.mygjp.com.cn/erp/ngploginNew"))

    def test_login_resolves_homepage_link_without_visiting_unauthenticated_main(self):
        page = MagicMock()
        page.url = "about:blank"
        visited = []

        def goto(url, **_kwargs):
            visited.append(url)
            page.url = url

        page.goto.side_effect = goto
        link = MagicMock()
        link.get_attribute.return_value = "https://www.mygjp.com.cn/ngpLogin.aspx"
        page.locator.return_value = link
        frame = MagicMock()
        frame.url = "https://passport.mygjp.com.cn/erp/ngploginNew?cssurl=x"
        controls = {name: MagicMock() for name in ("#company", "#username", "#password", "#btnlogin", "#tb1")}
        for control in controls.values():
            control.count.return_value = 1
            control.is_visible.return_value = True
        frame.locator.side_effect = controls.__getitem__
        page.frames = [frame]
        page.context.pages = [page]
        controls["#btnlogin"].click.side_effect = lambda: setattr(
            page, "url", "https://bfuned5main.wsgjp.com.cn/Main.gspx"
        )

        result = _login(page, {"login_url": "https://bfuned5main.wsgjp.com.cn/Main.gspx",
                               "company": "C", "username": "U", "password": "P"})
        self.assertIs(result, page)
        self.assertEqual(visited, ["https://www.wsgjp.com.cn/",
                                   "https://www.mygjp.com.cn/ngpLogin.aspx"])
        controls["#company"].fill.assert_called_once_with("C")
        controls["#username"].fill.assert_called_once_with("U")
        controls["#password"].fill.assert_called_once_with("P")

    def test_unexpected_homepage_login_link_stops_before_password(self):
        page = MagicMock()
        page.url = "about:blank"
        page.goto.side_effect = lambda url, **_kwargs: setattr(page, "url", url)
        link = MagicMock()
        link.get_attribute.return_value = "http://www.mygjp.com.cn/ngpLogin.aspx"
        page.locator.return_value = link
        with self.assertRaisesRegex(CollectorError, "official_login_link_unexpected"):
            _login(page, {"login_url": "https://bfuned5main.wsgjp.com.cn/Main.gspx",
                          "company": "C", "username": "U", "password": "P"})
        self.assertEqual(page.goto.call_count, 1)

    def test_credentials_must_be_private_and_not_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "login.json"
            file.write_text(json.dumps({"login_url": "https://bfuned5main.wsgjp.com.cn/",
                                        "company": "C", "username": "U", "password": "P"}))
            file.chmod(0o644)
            with self.assertRaisesRegex(CollectorError, "permissions_not_private"):
                _read_credentials(file)
            file.chmod(0o600)
            self.assertEqual(_read_credentials(file)["company"], "C")
            link = Path(temp) / "link.json"
            os.symlink(file, link)
            with self.assertRaisesRegex(CollectorError, "symlink"):
                _read_credentials(link)


class BrowserSelectorTests(unittest.TestCase):
    def test_old_offscreen_report_cannot_supply_click_or_row_count(self):
        chromium = shutil.which("chromium")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright unavailable")
        if not chromium:
            self.skipTest("Chromium unavailable")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chromium, headless=True)
            try:
                page = browser.new_page(viewport={"width": 800, "height": 600})
                page.set_content("""
                    <div style="position:absolute;left:-8000px;top:-8000px">
                      <button onclick="document.body.dataset.clicked='old'">筛选</button>
                      <div>进货订单统计列表</div>
                      <div class="PagerSummaryText">(67条记录)</div>
                    </div>
                    <div style="position:absolute;left:30px;top:30px">
                      <button onclick="document.body.dataset.clicked='new'">筛选</button>
                      <div>进货订单统计列表</div>
                      <div class="PagerSummaryText">(90条记录)</div>
                    </div>
                """)
                _click_exact(page, "筛选")
                self.assertEqual(page.locator("body").get_attribute("data-clicked"), "new")
                self.assertEqual(_web_row_count(page), 90)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
