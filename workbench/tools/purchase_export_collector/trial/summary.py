"""Read existing trial run records; never start a collector or publish data."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo


CHINA = ZoneInfo("Asia/Shanghai")
SLOTS = ("07:30", "11:30", "16:00")
WINDOW = timedelta(minutes=15)
DEFAULT_RUNS = Path("/var/lib/ul-purchase-collector/trial/runs")
COLLECTION_STATUSES = {
    "running", "succeeded", "failed", "needs_human", "blocked_needs_human",
    "blocked_previous_run", "skipped_busy",
}


def parse_timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamp_requires_timezone")
    return stamp


def slot_center(day: date, slot: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(slot), tzinfo=CHINA)


def summarize(runs_dir: Path, *, as_of: datetime | None = None) -> dict:
    """Count unique successful scheduled slots using their actual launch times.

    An unfinished current day does not break yesterday's streak until the last
    launch window closes. Missing calendar dates always break the streak.
    """
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("as_of_requires_timezone")
    local_now = now.astimezone(CHINA)
    today = local_now.date()
    grouped = defaultdict(list)
    ignored = defaultdict(int)
    invalid_records = []
    files_seen = 0
    for path in sorted(runs_dir.rglob("run.json")):
        files_seen += 1
        if path.is_symlink():
            ignored["symlink"] += 1
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError("record_must_be_object")
            if record.get("mode") != "isolated_trial" or record.get("formal_business_publish") is not False:
                ignored["outside_isolated_trial"] += 1
                continue
            status = record.get("status")
            if status == "human_hold_released_no_collection_started":
                ignored["manual_hold_release"] += 1
                continue
            if status not in COLLECTION_STATUSES:
                raise ValueError("unknown_status")
            started = parse_timestamp(record["started_at_utc"])
            updated = parse_timestamp(record["updated_at_utc"]) if record.get("updated_at_utc") else started
            if started > now or updated > now:
                ignored["after_as_of"] += 1
                continue
            local_start = started.astimezone(CHINA)
            day = local_start.date()
            slot = next((candidate for candidate in SLOTS
                         if abs(local_start - slot_center(day, candidate)) <= WINDOW), None)
            if slot is None:
                ignored["outside_start_window"] += 1
                continue
            grouped[(day, slot)].append({
                "run": str(path.relative_to(runs_dir)),
                "status": status,
                "started_at_local": local_start.isoformat(timespec="seconds"),
            })
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            # Report only the filename; never echo arbitrary record contents.
            invalid_records.append(str(path.relative_to(runs_dir)))

    def day_summary(day: date) -> dict:
        slots = []
        for slot in SLOTS:
            attempts = grouped.get((day, slot), [])
            successes = sum(attempt["status"] == "succeeded" for attempt in attempts)
            terminal_attempts = any(attempt["status"] != "running" for attempt in attempts)
            state = "succeeded" if successes else "failed" if terminal_attempts else "missing"
            slots.append({
                "slot": slot,
                "status": state,
                "window_closed": local_now >= slot_center(day, slot) + WINDOW,
                "in_progress": any(attempt["status"] == "running" for attempt in attempts),
                "successful_attempts": successes,
                "attempts": attempts,
            })
        successful_slots = sum(slot["status"] == "succeeded" for slot in slots)
        return {"date": day.isoformat(), "complete": successful_slots == len(SLOTS),
                "successful_slots": successful_slots, "slots": slots}

    current_complete = day_summary(today)["complete"]
    current_day_closed = local_now >= slot_center(today, SLOTS[-1]) + WINDOW
    through_day = today if current_complete or current_day_closed else today - timedelta(days=1)
    consecutive = 0
    cursor = through_day
    while day_summary(cursor)["complete"]:
        consecutive += 1
        cursor -= timedelta(days=1)

    return {
        "schema": 1,
        "as_of": local_now.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "start_window_minutes": 15,
        "runs_directory": str(runs_dir),
        "run_records_seen": files_seen,
        "ignored_records": dict(sorted(ignored.items())),
        "invalid_records": invalid_records,
        "consecutive_complete_days": consecutive,
        "streak_through_date": through_day.isoformat(),
        "current_day_in_progress": not current_complete and not current_day_closed,
        "recent_days": [day_summary(today - timedelta(days=offset)) for offset in (2, 1, 0)],
        "gate": False,
        "acceptance": {
            "three_consecutive_scheduled_days": consecutive >= 3,
            "units_verified": False,
            "coverage_complete": False,
            "exception_tests_accepted": False,
        },
        "notes": [
            "仅使用实际启动时间处于三个窗口内且 status=succeeded 的运行；同一窗口最多计一次成功。",
            "无记录的日期和时段保留缺失；窗口外的夜间手工测试不计入验收天数。",
            "run.json 未区分定时或人工启动；窗口内手工启动会按同一规则计入，需结合 systemd 日志确认定时来源。",
            "当天未到最后窗口结束且未全部成功时，连续天数截至昨天；当天完成或窗口结束后纳入当天。",
            "本统计仅证明运行时间与结果；单位、全量覆盖及验证码/中断/失败恢复仍需单独验收，正式 gate 始终为 false。",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--as-of", type=parse_timestamp, help="Explicit timezone-aware report cutoff (ISO 8601).")
    args = parser.parse_args()
    print(json.dumps(summarize(args.runs_dir, as_of=args.as_of), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
