# UL Android service contract

N150 public base URL: `https://ul.yingyinghome.com:8443/api/v2`. The fixed LAN adjustment-upload base URL remains `http://192.168.1.5:5191/api/v2`. JSON responses have `ok:true`; errors use `{ok:false,error,message}` and an appropriate HTTP status.

The N150 public service requires `Authorization: Bearer <session>` on every business endpoint. Only `/livez` and `/auth/login` are public. Order, product, inventory and bill-center data are refreshed by the persistent manual update job; normal queries read the last atomically published SQLite snapshot and do not wait for ERP. `source_updated_at` and `data_version` identify that snapshot. The old computer's LAN adjustment service remains a separate, unauthenticated private-LAN endpoint and must not be exposed to the internet.

Installation (outside the API prefix): `GET /app` is a lightweight install page for the independent native app. Every request reads `../releases/current.json`; the current 0.4.0 shape is `{version:"0.4.0-dev",file:"UL-Workbench-0.4.0-dev.apk",sha256:"64 hexadecimal characters",versionCode:13}`. The filename must match `UL-Workbench-[0-9A-Za-z.-]+.apk` and resolve directly inside the releases directory. `GET /app/download` serves only this release as an attachment after its actual SHA-256 matches the record; the checksum is shown on the page and in the `X-APK-SHA256` header. Missing/invalid records, mismatched checksums and incomplete APKs have no download button and download returns 404 `app_building`. Publish the APK atomically first, then atomically replace `current.json`; changes load without restarting the service. No fallback release or caller-supplied path is accepted. The HTML page does not host business screens or a WebView app.

- `GET /health`: service/version, `data_version`, published profile and time, counts, source scope, worker state and explicit capabilities. N150 reports logistics and adjustment writes separately; API connectivity never implies that either write path is enabled.
- `GET /data/sources`: current published source metadata and `data_version`.
- `POST /data/refresh`: compatibility entry for the persistent manual update queue in API mode. New clients use `POST /update-jobs` and poll the returned job.
- `GET /orders?q=DH0019&mode=sku&scope=all&page=1&page_size=50`: `items,count,total,total_quantity,page,page_size,inventory,data_version`. mode: sku/buyer/summary; note remains a compatibility alias. scope: all/stock; stock means exactly LS库存件, LD库存件, LM库存件, DQ库存件, QT库存件. Optional `logistics_company` selects 异常/需采购/待到货/催货/来货件 or one of the five stock companies; non-stock categories use `scope=all`. Items preserve `id,order_no,shipping_id,buyer,product_name,sku,quantity,summary,system_note,status,logistics_company`; `specification` contains the decoded standard/spec note field with spaces, and each item also has `inventory:{found,stock_quantity,available_stock}`. Quantity strings `"0"` are real zeros; missing stock has `found:false` and empty strings.
- `GET /inventory?q=DH0019&page=1&page_size=50`: `items,count,total,page,page_size,total_stock,total_available,data_version`; items contain `id,sku,product_name,spec,model,material,location,standard_no,stock_quantity,available_stock`.

Order items additionally preserve `model` from the order source's own `型号` column, when that column exists. An empty order model stays empty; clients may display the corresponding `inventory.model` as a fallback. Do not substitute the order system note's `grade` for a product model. This addition does not change the standard/specification, QR or CPCL field rules. Existing imported data needs a refresh after the service loads the new mapping.

Inventory supports optional `sku`, `name` (product name), `spec`, `model` (source `型号`, often combining grade and finish), `material`, `status` and `only_available=true`. All supplied fields and `q` are combined with AND. Starting in 0.1.3, `sku` uses trimmed case-insensitive exact equality; other text fields retain containment after space/underscore normalization. The native dedicated code field sends `sku=LS0001` and empty `q`, with no residual filters. General `q` remains a keyword search. For example `/inventory?name=六角&spec=M12&model=8.8&material=40Cr` needs no `q`. Pagination affects returned items only; counts and stock/available totals cover the complete filtered range. `model` is not an exact grade enumeration: `8级` can also match the suffix of `8.8级`.

For inventory all-results printing use `{kind:"inventory",all:true,query:{name:"六角",spec:"M12",model:"8.8",material:"40Cr"},expected_data_version:"…"}`. Send the complete current filter object; the service reuses the same inventory query, disregards client pagination and retains the 100-item limit. At least one effective `q`/supported text filter or an enabled `only_available` filter is required; empty/whitespace-only conditions and unrecognized parameters do not authorize printing the whole inventory. Order all-results printing accepts an empty keyword when a supported structural filter such as `print_state` or a date range is present. Selected-ID printing retains its existing semantics.

- `POST /print/jobs`: `{kind:"order"|"inventory"|"function_codes",item_ids:["…"],scope:"all"|"stock",logistics_company?:"LS库存件",expected_data_version:"version from query",max_per_package?:5}`. Selected IDs are resolved against this service's current data, and stock/company scope is rechecked. `expected_data_version` is required except for function_codes; mismatch returns 409 `data_version_conflict`, requiring a fresh query before printing. Alternatively use `{kind:"order",all:true,query:{q:"DH0019",mode:"sku",scope:"stock",logistics_company?:"LS库存件"},expected_data_version:"…",max_per_package?:5}` for the explicit all-results action. Maximum 100 source rows / 100 resulting labels; never silently truncates all-results printing. In app 0.1.2, order PALL01 prints selected IDs, and PBOX01 opens the same selected-ID split editor as the button; inventory maps both codes to selected-ID printing. `function_codes` generates a label containing both command QR codes. Preparing never sends bytes.
- Preparation returns `job_id,status:"prepared",labels,cpcl_pages:string[],cpcl_base64,cpcl_bytes,bytes,pages:[{index,cpcl_base64,bytes,fields,packageInfo}],data_version,sha256,mobile_render_mode:"text",paper_width_mm:50,paper_height_mm:100`. CPCL uses GB18030, the old mobile TEXT/VTEXT 55 / SETMAG / SETBOLD / QR positions, preserving standard/spec spaces and resolved QR fields. `cpcl_pages` is one complete page per Base64 string.
- `GET /print/jobs/:id`: status/counts/source version/message only; does not resend.
- `POST /print/jobs/:id/claim`: `{transport:"native",idempotency_key:"unique string 16–128 chars",user_initiated:true}`. First response contains the frozen preparation plus `claim_token,status:"claimed",replayed:false`. Same action repeated returns `replayed:true` **without printing bytes or token**; a different action for the same job returns 409. Keep the idempotency key stable across uncertain network responses.
- `POST /print/jobs/:id/result`: `{claim_token,status:"submitted"|"unknown"|"failed"|"cancelled",message,bytes_sent?:1234}`. Only records the app's result. `submitted` means the app handed bytes to the printer connection, not verified paper output. Report unknown after partial writes, timeout or uncertain native result; no automatic retry. No server-side device or print-submit endpoint exists.

The APK owns scanner input, native Bluetooth and its sequential print queue. Use 5088's known pacing initially (180-byte BLE chunks, 40ms between chunks, 350ms between labels where applicable); faster data size is obtained by text CPCL, not by shortening waits without a device test. Prepared jobs preserve their bytes even when source data refreshes. A new copy requires an explicit user action and a new prepared job.

## Reserved WSGJP registration addresses (service 1.2.2)

`/integrations/wsgjp/callback` and `/integrations/wsgjp/notify` reserve paths only. GET/HEAD show a static disabled page; POST/OPTIONS return `ok:false,error:integration_not_enabled`. All these responses use HTTP 503 and never acknowledge authorization or delivery. The application does not parse, echo, persist or log the supplied authorization/query/body values, has no credentials and makes no outbound calls. Responses use no-store and no-referrer. Existing LAN/Origin restrictions still run first and can reject with 403. These are not production integrations; HTTP frameworks can still buffer requests. No public route or DNS was configured, and vendor acceptance of private registration URLs remains unverified. Health reports `wsgjp_api:false,wsgjp_reserved_endpoints:true`.

## Inventory adjustment batches on the fixed LAN service

These endpoints belong to `http://192.168.1.5:5191/api/v2` on the old computer. The N150 public service deliberately returns `capability_unavailable` for adjustment submission.

`POST /adjustment-batches` accepts `{id:<UUID>,client_id:<stable device UUID>,type:"loss"|"overflow",items:[{sku,name,spec,model,quantity,note}]}`. Freeze this payload locally before POST and reuse its UUID after an uncertain response. Each batch contains 1–100 unique inventory SKUs; quantity is positive, at most 100000000 and six fractional digits; note is at most 200 characters. Both types use positive quantities. The app combines same-SKU drafts of the same type before submission; different types never offset one another.

The response is `{ok:true,batch:{id,client_id,type,items,status,message,document_no,retryable,created_at,updated_at}}`. Submission returns quickly after SQLite commit; the worker processes one XLS import at a time. Same UUID and canonical payload returns its existing result; different payload returns 409 and never overwrites. Definite validation rejection (400 `invalid_request`) occurs before insertion; the client can restore that frozen list to editable drafts. A timeout is not a validation rejection.

`GET /adjustment-batches/<id>?client_id=<UUID>` reads a batch, including successful batches; `GET /adjustment-batches?client_id=<UUID>` lists this device's unresolved batches. Device IDs scope views but are not authenticated identities. `POST /adjustment-batches/<id>/retry` with `{client_id:<UUID>}` is accepted only for a failed batch whose retryable flag is true. No automatic ERP retry, unknown-result retry, or batch-deletion endpoint exists.

States are `queued`, `running`, `succeeded`, `failed`, `unknown`. `failed` means the sender knows no submission was attempted or no document was created. Once submission may have occurred, errors are unknown. Service restart conservatively converts interrupted running batches to unknown. A marker in `runtime/data/adjustments/<batch UUID>/submission.json` prevents the adapter from resubmitting an attempted batch. Fresh matching-row-count completion receipts are saved beside it. Only succeeded removes a batch from the phone's active list; upload-time additions to drafts remain untouched.

### Manual ERP reconciliation (0.1.3)

`POST /adjustment-batches/<id>/confirm` accepts `{client_id:<UUID>,confirmed:true}` only after the user reviews the batch and verifies its ERP document and quantities. It changes only an owned `unknown` batch to `succeeded`, returns `completion_source:"user_verified"`, and records previous status/message, device ID, document reference and time in `adjustment_confirmations`. It does not call the ERP sender, change physical stock or remove original import evidence. Already-succeeded repeats return the original result; queued/running/failed confirmations are rejected. Missing or non-boolean confirmation is rejected.

The client persists the confirmation intent separately from the upload intent. A lost response retries only this confirmation endpoint, never creates another import, and only removes the matching local batch after a successful response. Normal ERP success uses `completion_source:"erp_receipt"`; legacy success may have an empty source. Interrupted batches recover their display reference from their own local marker while retaining unknown outcome until reviewed.

## N150 public update API

Public mode requires `Authorization: Bearer <session>` for all business endpoints.
`POST /api/v2/auth/login` accepts `{ "password": "..." }`, returns `token` and `expires_in`.
`POST /api/v2/auth/logout` revokes the current token. `/livez` exposes no business data.

`POST /api/v2/update-jobs` accepts a UUID and `mode` in `preview`, `refresh`, `reconcile`, `sync`, `update`, or `inventory_subset`, and returns HTTP 202 with `job`.
The same UUID and mode return the existing job. `update` requires a verified logistics sender;
otherwise HTTP 409 `capability_unavailable`. Only one queued/running task is accepted.
ERP transport enablement alone never authorizes a write. A `sync` or `update` job freezes
the server-side policy into its durable queue record. `UL_LOGISTICS_WRITE_SCOPE=all`
explicitly selects every eligible difference in that manual job; it rejects a simultaneous
`UL_LOGISTICS_WRITE_MAX_PER_JOB` or `UL_LOGISTICS_WRITE_ALLOWLIST`, so a stale finite
setting cannot silently turn a requested full update into a partial one. A missing scope with
no finite boundary remains disabled and sends no ERP request. The legacy finite policy stays
available: `scope=bounded` (or an empty scope for compatibility) uses the exact allowlist
first and the per-job maximum second. Results distinguish `selected`, `deferred`,
`unchanged`, and their order lists; deferred targets are not counted as unchanged. Full mode
has no arbitrary count cap, while existing validation blocks, unresolved requests and
allocation-scope safety rules still apply.
`GET /api/v2/update-jobs/latest` and `GET /api/v2/update-jobs/<id>` return persistent status.
Statuses: queued, running, succeeded, partial, unknown, blocked, failed.
Unknown requests are not retried automatically. Preview never advances the successful-write cache.

Health capabilities advertise API mode, source contract verification, logistics writes and adjustment
writes separately. API connectivity does not imply full source coverage. Query data can publish while
classification issues remain blocked; clients must inspect the job's `query_status`, `logistics_status`
and per-order issues separately.

## 0.3.0 inventory, images and whole-order printing

All order and inventory business data is scoped to warehouse `08390`
(`29871293290508390`). Public endpoints require the existing bearer session.

`GET /orders` adds `print_state=all|unprinted|reserved|printed|uncertain`,
`order_date_from/to`, `delivery_date_from/to`, and
`sort=default|location|order_date|delivery_date`. It also accepts an exact
`customer` filter. Dates use inclusive Shanghai
calendar days. `mode=summary` is canonical; `mode=note` remains an alias. Items
include `summary`, `order_date`, `delivery_date`, `location`, `buyer`,
`location_sort_key`, `print_state`, `print_state_updated_at`, and
`label_fingerprint`. WMS `receiveraddress` is the business-authoritative source
for `buyer`/customer name; blank values remain blank and receiver person/phone
fields are never used as fallbacks. Legacy snapshots without
`customer_source=wms_receiveraddress` have their old `buyer` value suppressed.
The response includes `customer_options`
and `customer_count`, built from the current result set after date/logistics
filters and before the selected customer is applied. `print_state_counts`
counts distinct whole orders after the customer and other filters and before
the print-state filter.

Order `POST /print/jobs` accepts `device_id` and either legacy `item_ids` or
`order_nos`. Selecting one line expands to every printable line of that order.
New clients also send a stable 16--128 character `prepare_idempotency_key`
which is persisted before the request is sent. Repeating the same key, device
and normalized request while the job remains prepared returns the original
job, frozen pages and `replayed:true`; reusing it for another request returns
HTTP 409 `print_prepare_idempotency_conflict`. This closes the response-loss
window between reserving an order and receiving its server-generated job ID.
Preparation atomically changes the whole order to `reserved`; another device
receives HTTP 409 `order_print_conflict`. Claim and result must use the same
device ID. A fully submitted job becomes `printed`; an uncertain or partially
sent failure becomes `uncertain`; a definite zero-byte cancellation/failure
returns to `unprinted`. `POST /orders/<order_no>/print-state/reset` accepts
`profile_id`, `device_id`, and optional `reason`, and records an audit entry.

`POST /print/jobs/<job_id>/cancel` accepts `device_id`, a distinct stable
`idempotency_key`, and `reason`. It releases an order only while the job is
still `prepared` and has never been claimed or sent.
`POST /print/jobs/<job_id>/recover` accepts the original claim idempotency key after
a claim response is lost; a matching claimed job becomes `unknown` and its
orders become `uncertain`, never `unprinted`. Both operations are transactional
and idempotent. A claimed job must otherwise be completed through `/result`
with its claim token.

`GET /inventory/<record_id>/history` returns `snapshot`, `coverage`,
`movements`, and `pending_adjustments`. It returns newest movements through the
fourth positive inbound, capped at 120. `balance_after` is present only when
the bill window is continuous and all included movement semantics are verified.
The history is physical inventory only; the Android client merges its local,
unposted loss/overflow drafts into the separate pending notice.

`GET /products/<product_id>/images` returns an `items` array containing zero or
one protected image URL. `GET /product-images/<image_id>/content` proxies the
allowlisted ERP image through the N150 cache with private conditional caching.
The phone never receives ERP credentials or the upstream URL.

`POST /update-jobs` also accepts
`{id,mode:"inventory_subset",product_ids:[...]}` (or a `request` object with
`product_ids`/full identities). It atomically refreshes only those published
08390 inventory rows. It never changes physical stock optimistically and never
runs logistics writes. A different active update returns HTTP 409 `update_busy`
so the client can report that the inventory awaits the next complete update.

## 0.4.0 staff accounts and daily work

`POST /auth/login` accepts either the compatible administrator payload
`{password}` or an employee payload `{username,password}`. `GET /me` returns the
session-bound `id`, display name, roles, administrator flag and packing limit.
Administrators manage staff through `GET/POST /staff`, `PATCH /staff/<id>` and
`POST /staff/<id>/password`; password hashes are never returned. Deactivation or
password reset revokes that user's sessions. At least one active administrator
must remain.

Only administrators may enqueue complete ERP/order updates, use the legacy
`/data/refresh` compatibility write, or resolve an uncertain freight attempt.
An authenticated employee may enqueue `inventory_subset` after a LAN adjustment
because that mode only rereads named inventory identities and never writes
freight. Server authorization is authoritative even if an old client still
shows an update control.

`GET /daily-work?role=<role>&view=mine|all` returns the business date, published
and task-projection versions, `tasks_current`, source time, stale flag, role
summary, visible active items, today's human events and separately confirmed
business completions. `view=all`, staff load and all administration endpoints
require an administrator. Roles are `sales`, `purchase`, `stock_packing` and
`arrival_packing`.

`GET /daily-work/version` is the lightweight polling endpoint. It returns only
`business_date`, `data_version`, `source_updated_at`, `task_version`,
`task_data_version` and `tasks_current`. The App polls this endpoint and fetches
the full task list only when one of those version fields or the business date
changes.

Task mutations use the task's integer `version` and a stable `idempotency_key`:

- `POST /daily-work/tasks/<id>/actions` with `action=start|progress|wait|resolve|packed|reopen`, `note`, and optional `next_follow_up_date`.
- `POST /daily-work/tasks/<id>/transfer` with `assignee_id` and required `reason` (administrator only).
- `POST /daily-work/tasks/<id>/urgent` with `urgent` and required `reason` (administrator only).
- `GET/PUT /daily-work/attendance`, `GET/PUT /daily-work/settings`, and assignment-rule endpoints are administrator only.

Automatic assignment is disabled by default. Enabling it explicitly assigns
current safe candidates; packing assignments use a combined per-person cap
across both packing roles. A task projection behind `data_version` returns
`tasks_current:false` and packaging/printing mutations fail with
`task_projection_conflict`. Order print preparation and first claim recheck the
current task owner within the same write transaction. An employee can reset an
uncertain print only for their own active packing task; an administrator may
perform the recovery action while the projection is being repaired.

Mobile task actions are queued locally with the same idempotency key only for
transport failures, timeouts, HTTP 429 or server errors. Deterministic client or
conflict responses are retained as local `requires_review` evidence and are not
automatically retried or allowed to block later independent actions.

## 0.4.1 customer-order groups

`GET /daily-work/groups` accepts `role`, `view=mine|all`,
`status=all|shortage|packing|ready_to_ship|review`, exact `customer`, `keyword`
and one-based `page`. It returns 20 customer-order cards per page, `total`,
`has_more`, status counts, `data_version`, `task_version` and `tasks_current`.
The opaque group ID is derived from profile, warehouse 08390 and the complete
text before the first `#`; a number without `#` forms its own group.

`GET /daily-work/groups/<group_id>` returns the complete unfiltered order
lines, missing products, linked purchase tasks and follow-up history. The
customer field is accepted only from source-marked WMS `receiveraddress`.
Missing or conflicting customer names make the group review-only. A line is
physical-ready only when its demand exactly closes against a matching
profile, warehouse, product, SKU and base-unit FIFO allocation. Printed labels
do not count as packed. `ready_to_ship` requires all active unshipped lines to
be physically ready and explicitly packed, with no review condition.

`POST /daily-work/groups/<group_id>/followups` takes `version`,
`idempotency_key`, `note`, optional `waiting_for` and
`next_follow_up_date`; one sales owner handles the entire customer order.
`POST /daily-work/groups/<group_id>/transfer` takes `version`,
`idempotency_key`, `assignee_id` and `reason` (administrator only). Both
operations are durable and version-checked. A newly added line changes the
existing group card and displays a change reminder; it never creates a
second follow-up identity. Existing per-system-order packing, printing and
purchase endpoints remain authoritative. Purchase cards in `GET /daily-work`
remain grouped by product and include `customer_order_impact` and
`near_complete_customer_orders`; the latter counts only groups with no review
issue whose sole missing product is this purchase item's SKU and has a
positive procurement shortage.

## 0.4.2 完整岗位工作清单

`GET /daily-work/board` 为新客户端的统一列表入口。必填 `role`，
`view=mine|role` 默认为本人；支持 `keyword`、`status`、交期自然日
`due_from/to` 和岗位视图中的 `assignee_id`。每页 50 项，返回 `total`、
`page`、`has_more`、`data_version`、`task_version`、`tasks_current`、
`stale`、`summary`、`today_events` 和 `filter_options.assignees`。
翻页携带首屏数据和任务版本；版本变化返回 409，不返回半批结果。
销售按客户整单、采购按完整商品身份、包装按同品归组。普通等待工作
仍保留在 all 结果中。员工可以只读查看本岗位全部，不能越权处理。

`GET /daily-work/board/<id>?role=...&view=...` 返回 `item`、完整 `lines`、
`related_groups` 与当日处理记录。`lines[].product_lines` 保留组合系统订单
各商品的数量和单位；组合订单只有一个任务和打印锁。

`POST /daily-work/selections` 提交相同的完整筛选条件、数据／任务版本，
以及 `ids`、详情的 `board_id` + `task_ids`，或本人范围的 `all=true`。
服务器保存精确对象及其版本，返回 `selection_id`、`object_count`、
`order_nos`、`profile_id`、版本及 `excluded_count`。全选覆盖全部分页；
不能操作的项目会明确计数，客户端必须显示排除提示。

`POST /daily-work/batches` 提交 `selection_id`、`idempotency_key`、
`action=start|packed|followup`。跟进附加 `result`、选填 `note`、
`waiting_for`、`promised_date`、`next_follow_up_date`。其他原因或需核对
必须附说明。所有对象在同一事务中校验和保存；冲突时整体回滚。
相同请求重放返回原结果，不重复增加完成记录。恢复暂停时只允许
返回已经完成的幂等结果，新增操作保持暂停。

批量选择与操作记录由 SQLite v8 持久化。原逐项任务、客户整单和打印
接口保留；包装限额字段仅用于旧客户端兼容，不再限制新派工或转交。
同品归并不改变 FIFO 库存分配，不替代实物包装或 ERP 发货确认。

## 两栏任务页（兼容旧客户端）

销售与库存件包装的新列表传 `work_state=todo|in_progress`，显示“待处理／处理中”。
省略该参数时保留原状态筛选和原卡片身份。采购与来货件包装拒绝两栏参数并继续
现有行为。每个卡片新增 `work_state`、`has_note`、`latest_note` 和 `work_label`；
`summary.work_state_counts={todo,in_progress}` 按相同关键字、交期、负责人及旧状态
筛选的全部结果计算，忽略本次 `work_state`，且不受分页影响。

`GET /daily-work`、`/daily-work/version` 及岗位清单元数据增加
`print_state_version`：对排序后的完整 `order_print_states` 事实计算哈希；无表与空表
使用同一稳定空哈希。它独立于任务版本，打印任务分段不会因此改变全局 `task_version`。
岗位列表每个响应使用单一 SQLite 读快照；翻页与恢复携带首屏的
`print_state_version` 查询参数，发生任何打印事实变化返回 409
`print_state_version_changed` 并要求重新加载第一页，避免 offset 漏项或重复。
详情支持相同的可选查询核验；选择和导图在冻结之前核验可选
`expected_print_state_version`，选择结果也返回冻结时的打印版本。旧客户端省略
此参数继续原行为。

库存件在商品聚合前按整张系统订单的处理阶段划分；同商品可以分别在两栏出现，
每栏拥有互不重复的订单范围。新版卡片 ID 为 `phase:<work_state>:<范围哈希>`，
哈希包含原商品身份及排序后的完整成员任务 ID；打印或取消导致成员变动时，旧 ID
立即失效，不能在相同任务版本下悄悄换成另一批订单。
详情、明确 ID 选择和导图能识别此范围；本人全选与导图请求另外携带 `work_state`。
旧卡片 ID 继续查询原聚合范围。详情返回的 `lines[].work_state` 对应原订单阶段，
卡片 `print_state` 优先级为 `uncertain > reserved > printed > unprinted`，对应
“打印待核对／打印中／待出库”。打印预留、出纸不确定和已打印进入处理中；
发送前取消且没有人工进度时恢复待处理。打印事实不写成已包装。

`POST /daily-work/groups/<group_id>/actions` 接收 `action=start|reset_to_todo|note`、
`idempotency_key`、`version`（跟进版本）、`expected_data_version` 和
`expected_task_version`。仅 `note` 动作附加非空 `note`（最多 1000 字）；备注不改变
处理阶段，开始和退回不生成备注或日期。成功返回 `replayed`、`version`、
`task_version`、`work_state`、`has_note`、`latest_note`。固定销售、有效临时代办或
管理员可以提交；客户身份／负责人不明、来源范围不完整及过期版本返回冲突。
授权、版本、记录和幂等结果在一个事务内保存；相同请求返回原结果，恢复暂停期间
也只允许返回已保存结果。不同内容复用同一操作标识返回 `idempotency_conflict`。

两栏读取原有人工开始、等待、解决和包装历史；自动创建和派工不算开始。
销售阶段只取销售自己的操作，包装进度不自动改变销售阶段。任务详情新增 `notes`
（`note,actor_id,created_at,event_type`）保存长期人工说明；单纯开始、派工和负责人
变更不点亮备注标识。销售过往事项及其分页详情包含新开始、退回和备注记录。

未出现订单不能推断完成：保留最后整单范围、原阶段、操作者和备注，显示待核对且
禁止提交或打印。库存包装条件变化保留旧库存事项为只读核对，新岗位不能与其重复
执行。只有无待核对标识、至少两次确认的 ERP 500/600 生命周期事实可以结束事项。
新增表 `customer_order_work_actions`、`customer_order_board_snapshots` 由每日任务
schema v3 增量建立，保留全局 SQLite 版本及旧事件表。

## 0.3.2 已出库未结存订单生命周期

后端 1.4.5 先计算 `net_adjustment = saleable_qty - physical_qty + total_sales_open`。`total_sales_open` 必须先按账套、08390仓库、商品、SKU和基本单位汇总全部活动需求及生命周期影子需求，不能逐单重复加入。`net_adjustment >= 0` 时，`purchase_pending=net_adjustment`；`net_adjustment < 0` 时，`purchase_pending=0`、`inferred_other_reserved=-net_adjustment`、`effective_physical=max(physical_qty-inferred_other_reserved,0)`。完整分类使用 `effective_physical` 进行 FIFO，但入库账本仍以原始 `physical_qty` 核对。库存列表和库存子集刷新输出同样的三个字段。`inferred_other_reserved` 只是库存三数推算的其他占用，通常来自草稿，不能据此声称已读取具体单据。规则版本为 `5-api-4`。

每次完整手动更新除活动订单外，还完整分页读取奇门 `bill_status=6`，并在发布前重读首屏及总数检查集合稳定性。该账套实测 `process_state=500` 表示已实际出库但尚未记账；这些订单以 `shadow_demand` 继续参与 08390 库存及采购未到 FIFO，避免把 ERP 尚未扣减的库存再次分给后续订单。

订单记录增加 `lifecycle_state`、`process_state`、`lifecycle_confirmations`、`lifecycle_pending_review`、`lifecycle_release_pending` 和 `shadow_demand`。状态流转为 `active -> candidate -> shipped_unposted -> release_pending -> archived`：500 连续两次成功读取后确认需结存；600 也连续确认后才归档和释放 FIFO 占用。精确查询失败或首个 600 仍保守占用数量并显示“订单状态待核对”，不得写回物流。

`candidate` 和 `shipped_unposted` 在 App 中显示“已完成订单需结存”，但均不得进入包装选择、未打印队列、标签准备或领取。服务端在第一次领取已准备标签时，于同一 SQLite 写事务内重新核对数据版本、当前记录和生命周期；状态已变化时返回 409 `order_lifecycle_conflict`，取消未领取任务并释放打印预留。已经领取的任务继续执行原有不确定出纸处理，不自动重打。

活跃订单摘要中的合法 `#交期=YYYY-MM-DD` 随成功发布保存到持久交期缓存。历史 500 订单只使用已验证缓存或显式审核导入的交期，不以订货日期替代。缺交期时仍可查询并占用 FIFO，但不生成物流单号或写回 ERP。审核后的历史映射先用 `service/import_order_delivery_dates.py` 默认校验模式检查，只有显式 `--apply` 才写入缓存；默认拒绝覆盖不同的已有交期。

“已完成订单需结存”的 ERP 物流编码必须每次从 `erp.freight.list` 中按唯一全名读取，禁止硬编码或猜测。目标物流单号仍为 `@MM/DD#完整订单编号`。全局 `UL_LOGISTICS_WRITE_ENABLED=0` 时，以上同步、计算和本地展示均可运行，但不会调用 ERP 物流更新接口。

## 0.4.11：Codex 录单与受控新商品计划

本节为增量接口，旧草稿、确认、附件及网页进度接口继续保留。会话详细查询返回 `codex_enabled`、`codex_job` 和 `new_product_plans`。处理结果仍是草稿；模型不能确认订单或执行业务写入。

- `POST /api/v2/sales-assistant/sessions/{session_id}/codex-jobs`：请求含 `version`、`data_version`、`text`、`idempotency_key`，可含完整的 `attachment_ids`。服务端冻结当前消息、原附件及客户固定销售范围，持久排队。分析和匹配在 N150 独立进程执行。
- `POST /api/v2/sales-assistant/sessions/{session_id}/codex-jobs/{job_id}/adopt`：请求含版本、数据版本、`result_hash` 和幂等键。只允许明确采用当前可核对结果，整批替换草稿明细，原草稿及来源留档；同商品不同原始行保持独立。
- `POST /api/v2/sales-assistant/sessions/{session_id}/new-products/prepare`：请求含版本、数据版本、`job_id`、`result_hash`、`suggestion_id` 和幂等键，可含当前参考商品 `reference_code`。准备计划不会写 ERP，也不增加会话版本。
- `POST /api/v2/sales-assistant/sessions/{session_id}/new-products/{plan_id}/confirm`：请求含版本、数据版本、`plan_hash` 和幂等键；仅管理员可确认。冻结范围失效不能执行；创建前再次校验权限与版本。

分析阶段保留原始品名、标准规格、等级、表面、材质、数量、报价、客户料号及客户行号。匹配阶段提供当前有效商品、哈希固定的 NAS 规则与有可靠客户身份的历史证据；旧历史价格不替代本次报价。原件与目录对照供用户明确采用，结果不代替订单确认。

新商品执行独立排队，使用完整当前目录查重和占号、明确当前分类与参考模板；创建只发送一次。商品、SKU、基本单位“支”及江苏优螺库存核实成功后才发布本地商品资料。结果不确定保持 `unknown`，不重新创建；新商品成功后旧匹配数据版本失效，须重新整理订单。

版本冲突、断网或失效范围保留原输入与请求；没有自动重新采用或重新提交。销售网页上传仍按本次完整文件的网页确认判断结果，不执行上传后的 ERP 销售订单回查。

新提交冻结为 `sales_web_import_v2`／`document_mode=line_document`：每条客户明细使用独立、稳定的 ERP 单号，同一个 XLS 文件顺序导入。`document_numbers` 与逐行摘要、日期在 payload 和实际文件中共同校验；一行只对应一个 ERP 单据。每行 G 列摘要和 Y 列明细备注相同，包含十段与该行交期，使用当前 App 的 `parse_note`／`note_delivery_date` 口径回读。原客户行号与内部文件行序号分别保存。

完整成功后会话增量返回 `erp_order_numbers`、`erp_document_count`、`erp_reference_scope=first_line_document`；兼容字段 `erp_order_no` 为首行真实 ERP 单号，不代表整批的唯一单号。旧 v1 已冻结参数、文件、单号及回执保持原格式。任何不完整网页回执都不能确认整批成功，不会重新上传全批或余行。
