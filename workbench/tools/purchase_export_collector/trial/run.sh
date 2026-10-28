#!/usr/bin/env bash
# Isolated browser-export trial. Never use the application's snapshot root as output.
set -euo pipefail
umask 0077
export TZ=Asia/Shanghai

readonly TRIAL_ROOT=/var/lib/ul-purchase-collector/trial
readonly CODE_ROOT=/opt/ul-purchase-collector/src
readonly PYTHON=/opt/ul-purchase-collector/venv/bin/python
readonly CREDENTIALS=/etc/ul-purchase/credentials.json
readonly FORMAL_STATUS=/opt/ul-workbench/service/runtime/data/purchase-snapshots/collector-status.json
readonly API_DATABASE=/opt/ul-workbench/service/runtime/data/workbench.sqlite3
readonly QUERY_START=1900-01-01
readonly QUERY_END="$(date +%F)"
readonly STATUS_FILE="$TRIAL_ROOT/collector-status.json"
readonly HOLD_FILE="$TRIAL_ROOT/manual-review-required.json"

if [[ $# -gt 1 || ($# -eq 1 && "$1" != --release-human-hold) ]]; then
    printf '%s\n' 'Usage: run-trial.sh [--release-human-hold]' >&2
    exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
    printf '%s\n' 'Trial Python environment is unavailable.' >&2
    exit 3
fi
mkdir -p "$TRIAL_ROOT/runs"
chmod 700 "$TRIAL_ROOT" "$TRIAL_ROOT/runs"
readonly RUN_DIR="$(mktemp -d "$TRIAL_ROOT/runs/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
readonly TRIGGERED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STARTED_AT="$TRIGGERED_AT"
COLLECTION_STARTED_AT=
FAILURE_REASON=

# This helper only writes safe run metadata and the explicitly allowed status file.
# It never opens credentials or a formal latest.json/workbook.
state_tool() {
    "$PYTHON" - "$STATUS_FILE" "$HOLD_FILE" "$FORMAL_STATUS" "$RUN_DIR" \
        "$TRIGGERED_AT" "$STARTED_AT" "$COLLECTION_STARTED_AT" \
        "$QUERY_START" "$QUERY_END" "$@" <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import json, os, sys, uuid

state, hold, formal, run = map(Path, sys.argv[1:5])
triggered, started, collection_started, begin, end, action, *args = sys.argv[5:]
now = datetime.now(timezone.utc).isoformat(timespec="seconds")

def read_state():
    if not state.exists():
        return {"status": "absent"}
    try:
        value = json.loads(state.read_text(encoding="utf-8"))
        if value.get("status") not in {"running", "needs_human", "failed", "succeeded"}:
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, AttributeError):
        return {"status": "invalid"}

def write(path, value):
    if path.is_symlink():
        raise RuntimeError("metadata_symlink")
    staged = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    staged.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    staged.chmod(0o600)
    os.replace(staged, path)

def publish_state(status, reason):
    previous = read_state()
    payload = {"status": status, "reason": reason,
               "started_at_utc": previous.get("started_at_utc", started),
               "updated_at_utc": now}
    write(state, payload)
    write(formal, payload)

if action == "read":
    print(read_state()["status"])
elif action == "run":
    write(run / "run.json", {"schema": 1, "mode": "isolated_trial",
        "status": args[0], "exit_code": int(args[1]),
        "triggered_at_utc": triggered,
        "started_at_utc": started, "updated_at_utc": now,
        "collection_started_at_utc": collection_started or None,
        "reason": args[2] if len(args) > 2 and args[2] else None,
        "query_start": begin, "query_end": end,
        "output_root": str(state.parent), "formal_business_publish": False})
elif action == "gate_failed":
    payload = {"status": "failed", "reason": args[0],
               "started_at_utc": triggered, "updated_at_utc": now}
    write(state, payload)
    write(formal, payload)
elif action in {"hold", "mark_hold"}:
    if not hold.exists():
        write(hold, {"status": "needs_human", "reason": args[0],
                     "detected_at_utc": now, "run_directory": str(run)})
    if action == "hold":
        publish_state("needs_human", args[0])
elif action == "release":
    if hold.exists():
        os.replace(hold, run / "released-human-hold.json")
    publish_state("failed", "manual_review_released_for_supervised_retry")
elif action == "success":
    # Only a completed successful collection clears the hold automatically.
    if hold.exists():
        os.replace(hold, run / "resolved-human-hold.json")
PY
}

FINAL_STATUS=failed
CHILD_PID=
HAS_LOCK=0
finish() {
    local result=$?
    trap - EXIT INT TERM
    if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -TERM "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
    if [[ "$HAS_LOCK" == 1 && "$result" != 0 ]]; then
        local state
        state="$(state_tool read)" || state=invalid
        if [[ "$state" == needs_human || "$state" == running || "$state" == invalid ]]; then
            state_tool hold previous_trial_interrupted || true
        fi
    fi
    state_tool run "$FINAL_STATUS" "$result" "$FAILURE_REASON" || true
    if [[ -f /opt/ul-purchase-collector/bin/trial-summary.py ]]; then
        local summary_tmp
        summary_tmp="$(mktemp "$TRIAL_ROOT/.summary-XXXXXX")"
        if "$PYTHON" /opt/ul-purchase-collector/bin/trial-summary.py \
            --runs-dir "$TRIAL_ROOT/runs" > "$summary_tmp"; then
            chmod 600 "$summary_tmp"
            mv -f "$summary_tmp" "$TRIAL_ROOT/acceptance-summary.json"
        else
            rm -f "$summary_tmp"
        fi
    fi
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

exec 9>"$TRIAL_ROOT/.collector.lock"
if ! flock -n 9; then
    FINAL_STATUS=skipped_busy
    if [[ "${1:-}" == --release-human-hold ]]; then
        printf '%s\n' 'A trial is still running; stop it before releasing the hold.' >&2
        exit 75
    fi
    exit 0
fi
HAS_LOCK=1
if [[ "${1:-}" == --release-human-hold ]]; then
    state_tool release
    FINAL_STATUS=human_hold_released_no_collection_started
    exit 0
fi
previous_state="$(state_tool read)"
if [[ -f "$HOLD_FILE" || "$previous_state" == needs_human ]]; then
    state_tool hold manual_review_required_before_retry
    FINAL_STATUS=blocked_needs_human
    exit 0
fi
if [[ "$previous_state" == running || "$previous_state" == invalid ]]; then
    state_tool hold previous_trial_interrupted
    FINAL_STATUS=blocked_previous_run
    exit 0
fi

if PYTHONPATH="$CODE_ROOT" "$PYTHON" -m purchase_export_collector.trial.api_gate \
    --database "$API_DATABASE" --max-wait-sec 1800 --poll-sec 5; then
    :
else
    gate_result=$?
    FAILURE_REASON=api_update_busy_timeout
    if [[ "$gate_result" != 75 ]]; then
        FAILURE_REASON=api_update_state_unavailable
    fi
    state_tool gate_failed "$FAILURE_REASON"
    exit "$gate_result"
fi

# The acceptance summary uses started_at_utc. Keep it at the actual browser
# collection start, so waiting past a scheduled window never counts as on time.
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
COLLECTION_STARTED_AT="$STARTED_AT"
state_tool run running -1
# DISPLAY=:97 and XAUTHORITY are supplied by the service. CAPTCHA remains
# visible in that same display; --wait-for-human never solves the challenge.
PYTHONPATH="$CODE_ROOT" "$PYTHON" -m purchase_export_collector.collector collect \
    --credentials-file "$CREDENTIALS" \
    --output-dir "$TRIAL_ROOT" \
    --start "$QUERY_START" --end "$QUERY_END" \
    --chromium-executable /usr/bin/chromium \
    --headed --wait-for-human \
    --diagnostics-file "$RUN_DIR/download-diagnostics.json" \
    --status-file "$FORMAL_STATUS" --retain-rejected \
    >"$RUN_DIR/result.json" 2>"$RUN_DIR/stderr.log" &
CHILD_PID=$!
while kill -0 "$CHILD_PID" 2>/dev/null; do
    state="$(state_tool read)"
    if [[ "$state" == needs_human ]]; then
        # Do not overwrite the live collector state: it may have just resumed.
        state_tool mark_hold manual_login_verification_required
    fi
    sleep 2
done
if wait "$CHILD_PID"; then
    result=0
else
    result=$?
fi
CHILD_PID=
if [[ "$result" == 0 && "$(state_tool read)" == succeeded ]]; then
    state_tool success
    FINAL_STATUS=succeeded
elif [[ -f "$HOLD_FILE" ]]; then
    state_tool hold manual_review_required_before_retry
    FINAL_STATUS=needs_human
    if [[ "$result" == 0 ]]; then result=3; fi
else
    FINAL_STATUS=failed
    if [[ "$result" == 0 ]]; then result=3; fi
fi
exit "$result"
