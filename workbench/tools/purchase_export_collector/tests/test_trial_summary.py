"""Calendar/window acceptance must not manufacture successful trial days."""

import json
from pathlib import Path
import tempfile
import unittest

from purchase_export_collector.trial.summary import parse_timestamp, summarize


class TrialSummaryTests(unittest.TestCase):
    def write_run(self, root: Path, name: str, started: str, status="succeeded"):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "run.json").write_text(json.dumps({
            "mode": "isolated_trial", "formal_business_publish": False,
            "started_at_utc": started, "updated_at_utc": started, "status": status,
        }), encoding="utf-8")

    def complete_day(self, root: Path, day: str):
        for slot in ("07:30", "11:30", "16:00"):
            self.write_run(root, f"{day}-{slot}", f"{day}T{slot}:00+08:00")

    def test_utc_date_rollover_and_inclusive_start_windows_exclude_night_tests(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.write_run(root, "morning", "2026-10-05T23:15:00Z")
            self.write_run(root, "noon", "2026-10-06T03:30:00Z")
            self.write_run(root, "afternoon", "2026-10-06T08:15:00Z")
            self.write_run(root, "night", "2026-10-05T20:05:45Z")
            self.write_run(root, "too-late", "2026-10-06T08:15:01Z")
            result = summarize(root, as_of=parse_timestamp("2026-10-06T09:00:00Z"))
            day = result["recent_days"][-1]
            self.assertEqual(day["date"], "2026-10-06")
            self.assertEqual(day["successful_slots"], 3)
            self.assertTrue(day["complete"])
            self.assertEqual(result["consecutive_complete_days"], 1)
            self.assertEqual(result["ignored_records"]["outside_start_window"], 2)
            self.assertFalse(result["gate"])

    def test_duplicate_slot_cannot_replace_failed_or_missing_other_slots(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for number, minute in enumerate((25, 30, 35)):
                self.write_run(root, f"morning-{number}", f"2026-10-06T07:{minute}:00+08:00")
            self.write_run(root, "noon-failed", "2026-10-06T11:30:00+08:00", "needs_human")
            result = summarize(root, as_of=parse_timestamp("2026-10-06T17:00:00+08:00"))
            day = result["recent_days"][-1]
            self.assertEqual([slot["status"] for slot in day["slots"]], ["succeeded", "failed", "missing"])
            self.assertEqual(day["slots"][0]["successful_attempts"], 3)
            self.assertEqual(day["successful_slots"], 1)
            self.assertEqual(result["consecutive_complete_days"], 0)
            self.assertFalse(result["acceptance"]["three_consecutive_scheduled_days"])

    def test_api_wait_uses_actual_collection_start_for_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / 'delayed' / 'run.json'
            run.parent.mkdir()
            run.write_text(json.dumps({
                'mode': 'isolated_trial', 'formal_business_publish': False,
                'triggered_at_utc': '2026-10-06T23:40:00Z',
                'started_at_utc': '2026-10-07T00:10:00Z',
                'collection_started_at_utc': '2026-10-07T00:10:00Z',
                'updated_at_utc': '2026-10-07T00:11:10Z',
                'status': 'succeeded',
            }), encoding='utf-8')
            result = summarize(root, as_of=parse_timestamp('2026-10-07T09:00:00Z'))
            self.assertEqual(result['ignored_records']['outside_start_window'], 1)
            self.assertEqual(result['recent_days'][-1]['successful_slots'], 0)

    def test_missing_calendar_day_breaks_streak_and_current_future_slots_stay_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-05"):
                self.complete_day(root, day)
            result = summarize(root, as_of=parse_timestamp("2026-10-06T06:00:00+08:00"))
            self.assertEqual(result["consecutive_complete_days"], 1)
            self.assertEqual(result["streak_through_date"], "2026-10-05")
            self.assertEqual([day["complete"] for day in result["recent_days"]], [False, True, False])
            self.assertTrue(result["current_day_in_progress"])
            self.assertTrue(all(slot["status"] == "missing" and not slot["window_closed"]
                                for slot in result["recent_days"][-1]["slots"]))
            ended = summarize(root, as_of=parse_timestamp("2026-10-06T17:00:00+08:00"))
            self.assertEqual(ended["consecutive_complete_days"], 0)
            self.assertEqual(ended["streak_through_date"], "2026-10-06")
            three_days = summarize(root, as_of=parse_timestamp("2026-10-03T17:00:00+08:00"))
            self.assertEqual(three_days["consecutive_complete_days"], 3)
            self.assertTrue(three_days["acceptance"]["three_consecutive_scheduled_days"])
            self.assertFalse(three_days["gate"])


if __name__ == "__main__":
    unittest.main()
