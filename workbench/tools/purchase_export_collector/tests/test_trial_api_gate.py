"""The browser trial must wait for the API queue and fail closed on uncertainty."""

from pathlib import Path
import sqlite3
import tempfile
import unittest

from purchase_export_collector.trial.api_gate import api_updates_idle, wait_for_api_idle


class TrialApiGateTests(unittest.TestCase):
    def test_waits_until_update_queue_drains(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'workbench.sqlite3'
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE update_jobs(id TEXT, status TEXT)')
                db.execute("INSERT INTO update_jobs VALUES ('a','running')")
            ticks = [0.0]

            def sleep(seconds):
                ticks[0] += seconds
                with sqlite3.connect(database) as db:
                    db.execute("UPDATE update_jobs SET status='succeeded' WHERE id='a'")

            self.assertFalse(api_updates_idle(database))
            self.assertTrue(wait_for_api_idle(database, 30, 5,
                clock=lambda: ticks[0], sleep=sleep))
            self.assertEqual(ticks[0], 5)
            self.assertTrue(api_updates_idle(database))

    def test_timeout_and_unreadable_queue_do_not_approve_browser_start(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'workbench.sqlite3'
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE update_jobs(id TEXT, status TEXT)')
                db.execute("INSERT INTO update_jobs VALUES ('a','queued')")
            ticks = [0.0]

            def sleep(seconds):
                ticks[0] += seconds

            self.assertFalse(wait_for_api_idle(database, 12, 5,
                clock=lambda: ticks[0], sleep=sleep))
            self.assertEqual(ticks[0], 12)
            with self.assertRaises(sqlite3.OperationalError):
                api_updates_idle(Path(folder) / 'missing.sqlite3')


if __name__ == '__main__':
    unittest.main()
