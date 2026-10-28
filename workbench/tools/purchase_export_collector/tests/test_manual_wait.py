import unittest
from unittest.mock import MagicMock, patch

from purchase_export_collector.collector import CollectorError, _wait_manual_login


class ManualLoginWaitTests(unittest.TestCase):
    def page(self):
        page = MagicMock()
        page.url = "https://www.mygjp.com.cn/ngpLogin.aspx"
        page.is_closed.return_value = False
        page.context.pages = [page]
        return page

    def test_pending_session_stays_open_until_operator_completes_login(self):
        page = self.page()
        ready = MagicMock()
        with patch("purchase_export_collector.collector._ready_erp_page", side_effect=[None, None, ready]), \
                patch("purchase_export_collector.collector._write_status") as status:
            self.assertIs(_wait_manual_login(page, [], "start"), ready)
        self.assertEqual([call.args[1] for call in status.call_args_list], ["needs_human", "running"])
        self.assertEqual(page.wait_for_timeout.call_count, 2)
        page.close.assert_not_called()
        page.context.close.assert_not_called()
        page.goto.assert_not_called()
        page.locator.assert_not_called()

    def test_closed_session_is_not_reported_as_success(self):
        page = self.page()
        page.is_closed.return_value = True
        with patch("purchase_export_collector.collector._ready_erp_page", return_value=None), \
                patch("purchase_export_collector.collector._write_status") as status:
            with self.assertRaisesRegex(CollectorError, "manual_login_session_closed"):
                _wait_manual_login(page, [], "start")
        self.assertEqual([call.args[1] for call in status.call_args_list], ["needs_human"])

    def test_insecure_redirect_stops_without_resubmitting_credentials(self):
        page = self.page()
        page.url = "http://www.wsgjp.com.cn/"
        with patch("purchase_export_collector.collector._ready_erp_page", return_value=None), \
                patch("purchase_export_collector.collector._write_status"):
            with self.assertRaisesRegex(CollectorError, "login_redirect_not_official_https"):
                _wait_manual_login(page, [], "start")
        page.goto.assert_not_called()


if __name__ == "__main__":
    unittest.main()
