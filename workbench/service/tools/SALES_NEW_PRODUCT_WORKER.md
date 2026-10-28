# Confirmed new product worker

The App creates a proposal through `sales_new_product_jobs`, then the isolated
host worker prepares a plan. The administrator confirms the entire frozen plan
hash. Codex cannot choose a code, category, ERP payload or approve creation.

Preparation reads a fresh complete `erp.ptype.list(isall=true)` catalog, including
stopped/deleted code occupation, and `erp.ptype.classlist`. A unique enabled full
classification path and an existing reference template are required. The NAS
sequence rule takes the maximum current/reserved prefix number plus one, with at
least four digits. The queue separately protects reservations in its transaction.

The host service runs as `ulcollector` and takes the same `.web-browser-lane.lock`
as browser, document and Codex jobs. Its default ExecStart prepares only. Actual
creation additionally requires the explicit `--allow-create` deployment flag,
the server `sales_new_product_policy.enabled` gate, an active administrator's
confirmation, unchanged session/customer/data versions, and the final local
`authorize_submission` check after network preflight.

`--readonly-preflight --profile-id <authorized-profile>` performs no queue claim,
product write, or credential refresh. It reads the private ERP vault directly as
the collector; it never prints credentials. Provide `{}` on stdin, or a synthetic
`suggestion` and `reference_code` for preparation only. Stable output includes
counts, status and issue codes, without the catalog, source order or payload.
Never use a synthetic preparation as a confirmed business job.

The 2026-10-07 N150 read-contract evidence is in the collector's private directory
`/var/lib/ul-sales-product-preflight/20261007-contract/contract-evidence.json`:
18,052 complete products, 824 deleted, 71 stopped, 191 classifications, 141 unique
enabled full paths, and a unique exact-code lookup. The unknown synthetic family
blocked correctly; a synthetic new specification with a current template yielded
a ready plan. No ERP product was created during this test. Real classlist rows
all used `profileid='0'` as a sentinel; authorization remains bound to `account()`
and current parent identities. `parptypeid` joins category `id`; creation passes
that category's `typeid` as `ParTypeId`.

Creation sends a single `ptypes` item once, without `IniQty`. Any transport error,
partial `errorinfos`, missing identity, or unclear response remains unknown. Even
a success receipt must pass exact code → unique ptype → actual unique SKU → unit
支 → actual 江苏优螺 stock verification. The queue alone merges the verified row and
changes the catalog version. It does not replace the directory or reset inventory.

Unknown creation never returns to the submission queue. `verify_created_product`
is a query-only recovery primitive; it cannot call `ptype.add`. Program rollback
must preserve plans, receipts and created product identities. The user's canceled
sales-order readback requirement does not cancel product identity verification.
