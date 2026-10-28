"""Offline Wisej upload-control checks. No ERP session is opened."""
import tempfile
import unittest
from pathlib import Path

from core.sales_web_transport import PlaywrightSalesWebBrowser, PlaywrightUploadSelectors


class Control:
    def __init__(self):
        self.clicks = 0

    def click(self, **_kwargs):
        self.clicks += 1

    def wait_for(self, **_kwargs):
        pass

    def is_visible(self):
        return True


class DocumentType(Control):
    def __init__(self):
        super().__init__()
        self.value = ""
        self.arrows = 0

    def input_value(self):
        return self.value

    def press(self, key):
        if key == "ArrowDown":
            self.arrows += 1
            self.value = "销售订单" if self.arrows == 2 else "进货订单"


class FileInput(Control):
    def __init__(self):
        super().__init__()
        self.path = None

    def set_input_files(self, path):
        self.path = Path(path)

    def evaluate(self, _script):
        return self.path.name


class Page:
    def __init__(self):
        self.confirm_prompt = Control()

    def get_by_text(self, _text, **_kwargs):
        return self.confirm_prompt

    def goto(self, *_args, **_kwargs):
        raise AssertionError("Wisej import has no separate URL")


class UploadUiTests(unittest.TestCase):
    def browser(self):
        page = Page()
        browser = PlaywrightSalesWebBrowser(page, PlaywrightUploadSelectors(), None,
                                            lambda: ("profile", "account"))
        browser._session = lambda: None
        return browser

    def test_stage_uses_menus_custom_type_and_file_without_navigation(self):
        browser = self.browser()
        doc, file_input, upload = DocumentType(), FileInput(), Control()
        menus = []
        browser._menu = lambda selector, text: menus.append((selector, text))
        browser._one = lambda selector, visible=True: {
            browser.selectors.document_type_selector: doc,
            browser.selectors.file_selector: file_input,
            browser.selectors.upload_selector: upload,
        }[selector]
        browser._progress_snapshot = lambda: ""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "one.xls"
            path.write_bytes(b"offline-test")
            browser.stage_sales_import(path)
            self.assertEqual(file_input.path, path)
        self.assertEqual([text for _, text in menus], ["系统", "导入导出", "业务单据导入"])
        self.assertEqual(doc.input_value(), "销售订单")
        self.assertEqual(doc.arrows, 2)

    def test_upload_and_confirmation_are_each_clicked_once(self):
        browser = self.browser()
        upload, confirm = Control(), Control()
        browser._one = lambda _selector: upload
        browser._visible_text = lambda text: confirm if text == "确定" else None
        browser.click_upload_once()
        self.assertEqual(upload.clicks, 1)
        self.assertEqual(confirm.clicks, 1)

    def test_processing_window_is_never_closed_early(self):
        browser = self.browser()
        title = Control()
        title.all = lambda: [title]
        browser.page.get_by_text = lambda _text, **_kwargs: title
        browser._on_screen = lambda _item: True
        browser._progress_snapshot = lambda: "业务单据导入进度\n1,开始导入..."
        with self.assertRaisesRegex(RuntimeError, "still processing"):
            browser._close_import_dialogs()
        self.assertEqual(title.clicks, 0)

    def test_progress_waits_for_terminal_receipt(self):
        browser = self.browser()
        reports = iter(["业务单据导入进度", "1,开始导入...",
                        "业务单据导入进度\n4,----完成----"])
        browser._progress_snapshot = lambda: next(reports)
        browser.page.wait_for_timeout = lambda _ms: None
        self.assertIn("完成", browser.visible_import_progress())
        self.assertFalse(browser._terminal_progress("业务单据导入进度\n1,开始导入..."))

    def test_upload_browser_does_not_require_an_erp_readback_port(self):
        browser = PlaywrightSalesWebBrowser(Page(), PlaywrightUploadSelectors(),
                                            identity=lambda: ("profile", "account"))
        browser._session = lambda: None
        self.assertIsNone(browser.readback)
        self.assertEqual(browser.account_identity(), ("profile", "account"))

    def test_progress_snapshot_rejects_multiple_frame_windows(self):
        browser = self.browser()
        class Frame:
            def evaluate(self, _script):
                return "业务单据导入进度\n1,开始导入..."
        browser.page.frames = [Frame(), Frame()]
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            browser._progress_snapshot()

    def test_progress_polling_stops_on_expired_session(self):
        browser = self.browser()
        def expired():
            raise RuntimeError("login expired")
        browser._session = expired
        browser._progress_snapshot = lambda: "业务单据导入进度\n4,----完成----"
        with self.assertRaisesRegex(RuntimeError, "login expired"):
            browser.visible_import_progress()


if __name__ == "__main__":
    unittest.main()
