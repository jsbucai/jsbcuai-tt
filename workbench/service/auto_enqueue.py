"""Enqueue one idempotent server-side read-only refresh job."""
import argparse
import json
import os
from core.store import ROOT, Store
from core.update_jobs import UpdateJobs
from core.freight_write_policy import FreightWritePolicy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('refresh', 'reconcile', 'workday', 'purchase-dawn'))
    args = parser.parse_args()
    policy = FreightWritePolicy.from_config(os.environ.get('UL_LOGISTICS_WRITE_MAX_PER_JOB'),
        os.environ.get('UL_LOGISTICS_WRITE_ALLOWLIST'), os.environ.get('UL_LOGISTICS_WRITE_SCOPE')) \
        if os.environ.get('UL_LOGISTICS_WRITE_ENABLED') == '1' else FreightWritePolicy.deny()
    jobs = UpdateJobs(Store(ROOT / 'runtime' / 'data'), None, write_policy=policy)
    if args.mode == 'purchase-dawn':
        job = jobs.enqueue_purchase_dawn()
    elif args.mode == 'workday':
        job = jobs.enqueue_workday()
    else:
        job = jobs.enqueue_automatic(args.mode)
    if job is None:
        status = 'disabled' if args.mode == 'purchase-dawn' else 'outside_working_window'
        print(json.dumps({'status': status, 'reason': '设置 UL_PURCHASE_DAWN_FULL=1 后才会入队凌晨全量校正' if args.mode == 'purchase-dawn' else ''}, ensure_ascii=False))
        return
    print(json.dumps({key: job[key] for key in ('id', 'mode', 'status', 'stage')},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
