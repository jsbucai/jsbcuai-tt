"""Enqueue one idempotent server-side read-only refresh job."""
import argparse
import json
import os
from core.store import ROOT, Store
from core.update_jobs import UpdateJobs
from core.freight_write_policy import FreightWritePolicy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('refresh', 'reconcile', 'workday'))
    args = parser.parse_args()
    policy = FreightWritePolicy.from_config(os.environ.get('UL_LOGISTICS_WRITE_MAX_PER_JOB'),
        os.environ.get('UL_LOGISTICS_WRITE_ALLOWLIST'), os.environ.get('UL_LOGISTICS_WRITE_SCOPE')) \
        if os.environ.get('UL_LOGISTICS_WRITE_ENABLED') == '1' else FreightWritePolicy.deny()
    jobs = UpdateJobs(Store(ROOT / 'runtime' / 'data'), None, write_policy=policy)
    job = jobs.enqueue_workday() if args.mode == 'workday' else jobs.enqueue_automatic(args.mode)
    if job is None:
        print(json.dumps({'status': 'outside_working_window'}, ensure_ascii=False))
        return
    print(json.dumps({key: job[key] for key in ('id', 'mode', 'status', 'stage')},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
