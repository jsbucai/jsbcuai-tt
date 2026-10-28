# UL 随行 N150/API 迁移实施记录

更新：2026-09-26。开发分支：`codex/n150-api`。状态：**公网入口、独立后端和登录已部署，用户手机登录成功；完整业务接入及生产验收未完成，不替换正在使用的旧版。** 当前配置及验证以 [0.2.2 公网部署记录](N150公网部署与0.2.2交付-20260926.md) 为准；文末保留此前阶段记录。

## 已实现

- 从 NAS 0.1.3 项目创建独立 `workbench` 工作副本；保留 Kotlin/Compose、Flask、SQLite 和原查询、扫码、拆箱、蓝牙打印、调整批次流程。
- API 模式不启动 Windows 导入、Excel 自动刷新或 Windows 报损报溢执行端。旧文件模式保留用于原功能回归；公网模式拒绝文件模式。
- 单管理员密码哈希、30 天可撤销会话、登录限流。Android 会话由 Keystore 加密，按服务器地址隔离；ERP 凭据不进入 App。
- 持久化手动更新任务、同一任务 UUID 幂等、单活动任务、后台工作线程、进程锁、重启中断恢复。没有定时业务同步。
- ERP HTTPS 签名调用、授权续期与受限原子落盘、分页闭合/重复检测。生产默认只开放读取；危险 `beefun.deliver.updatefreightinfo` 不在允许名单中。
- 基于标准输入的完整 FIFO 计算：库存与在途隔离到账套/仓库/商品/SKU/基本单位；大单不被跳过；保留完整订单编号；交货日期生成 `@MM/DD#完整订单编号`；缺证据、负数及混合前缀产生冲突。
- 计算结果、上次成功缓存、逐笔请求分别保存在 `logistics_calculations`、`freight_cache`、`freight_attempts`。只确认成功才推进缓存；未知结果阻止该单自动重试，即使旧成功缓存与本次目标相同也不能绕过核对。
- 数据快照在 SQLite 事务中整体发布，摘要映射至原 `system_note`；失败保留前一版。原打印记录及调整记录不被快照替换。
- Debian Docker Compose 部署文件、非 root 容器、只读根文件系统、Asia/Shanghai 时区、健康检查、HTTPS 入口配置模板。
- SQLite 在线一致性备份，AES-GCM 加密后拷贝 NAS；备份覆盖数据库及后端授权/密码哈希。恢复撤销会话并设置写入暂停，不自动重放旧任务。独立备份密钥不放进备份包。
- Android 增加登录、会话失效、手动更新和任务状态。保持业务页面风格与原功能；0.2.2 新安装地址为 `https://ul.yingyinghome.com:8443`，已保存的旧 UL 443 地址自动迁移，其他自定义地址保留。

## 真实验证与未完成边界

| 项目 | 本轮结果 |
|---|---|
| Python 回归测试 | 139 项已在 N150 实际 Python 3.12 容器通过；测试与生产数据隔离 |
| 新 ERP 适配层真实读取 | 使用 NAS 授权读取 5 个仓库成功；本轮 ERP 业务写入 0 次 |
| 完整业务源 | **未完成**。`WmsSource.collect()` 当前仅验证连通性与近期订单结构，随后主动报 `SourceIncomplete`，不会发布快照 |
| 计算规则 | 原脚本与迁出纯内核均复现 2,395 单/2,403 明细；v2 完整在线输入仍待 API 补齐，公式冲突项保持阻断 |
| 物流写入 | 用户已确认 orderfreight 接口只改物流、不影响发货记账；适配已编写，待可用服务接入。本机此次没有执行 ERP 写入 |
| 报损报溢 | 草稿、批次、任务记录流程保留；API 执行器未接通，提交返回明确不可用，不转交 Windows |
| App 构建 | 0.2.2 Mac 构建成功：121 项通过，Lint 0 error/4 warning，原签名通过，APK 已放 NAS |
| N150 与公网 | VM 103 已部署，2 vCPU/2 GB，独立 local 磁盘实际预留 16 GiB；DNS、8443 Caddy 和证书已接通，用户手机登录成功 |
| 备份恢复 | 目标机每日加密备份任务已安装；首份 NAS 备份及隔离恢复通过，完整性、暂停写入、撤销会话均验证 |

**不得因查询、登录或健康检查通过而宣布可以上线。当前默认配置的同步任务会显示资料不完整，这是有意的上线保护。**

## 待补齐依据

1. 已取得并复现现行分类脚本、全部类别样例和前后入库账本；补充回复的 31 个文件及修复后的基线 37 项校验均通过，不需重复索取。
2. ERP 原始字段到标准合同的可验证映射：完整未完成数量、终止状态、交货日期、单位换算、SKU、仓库、采购批次、到货分配与催货判断。不能把 WMS 总数量当成未完成数量，不能把空采购结果当成零在途。
3. `beefun.orderfreight.updatefreight` 的单笔验收：前后记录订单状态及相关出库/记账单据，证明只有物流字段变化。**禁止改用已造成发货的 deliver 方法。**
4. 官方报损/报溢 API 的完整单据流程与重复提交核对依据。接口未知结果必须可人工核对，不能自动重复记账。
5. N150 基础部署已完成：VM 103 在 local 存储实际预留 16 GiB；DNS、8443、证书和登录均已验证。后续继续监控虚拟机及宿主容量，完成业务验收后再正式切换。
6. Mac 构建工具与原 NAS 签名文件已齐备；仍须真机验证蜂窝网络查询、扫码、拆箱、A300E 打印及登录失效流程。

## 模块位置

- 接口及登录：`service/app.py`、`service/core/auth.py`
- ERP 与标准数据源：`service/core/erp_api.py`、`service/core/api_source.py`
- 业务规则与任务：`service/core/logistics.py`、`service/core/update_jobs.py`
- 数据存储：`service/core/store.py`；保留原打印/调整账本
- 客户端：`app/src/main/java/com/ul/workbench/data/SessionVault.kt`、`WorkbenchRepository.kt`、`WorkbenchViewModel.kt`
- 部署及恢复：`compose.yaml`、`Dockerfile`、`deploy/`、`service/backup.py`

## 标准源数据合同

`API_SOURCE.collect()` 返回以下内容。生产实现必须由完整 API 结果生成，测试 fixture 不能成为生产回退数据源。

- `complete: true`：所有分页、状态、基本单位和来源关联都已验证；不是仅请求成功。
- `profile_id`：单一授权账套。
- `source_updated_at`：包含时区的实际数据时间。
- `orders[]`：完整 `order_no`、`order_date`、`delivery_date`（ISO 日期），及 `lines[]`。
- 明细：稳定 `line_id`、`profile_id/warehouse_id/product_id/sku_id/base_unit`、`product_code`、基本单位 `open_qty`。
- 到货证据：`receipt_evidence_complete: true`、`allocated_receipts[]`（唯一 allocation_id、qty、received_date）。这些数量已经包含在实物库存中，不能再加一次。
- 催货证据：`expedite_evidence_complete: true`、明确布尔 `expedite_required`。
- `inventory[]`：同一库存键的 `physical_qty`、`saleable_qty`；无重复键。
- `records`：完整 `orders/inventory/purchases` 三类现有 App 展示记录，类内稳定唯一 id。订单展示记录的 `order_no/line_id` 与计算明细一一对应，必须提供原始 `summary`。

按用户 2026-09-25 最新更正，采购未到总量 = 库存查询中的订单数量 + 改制单数量 = `physical_qty - saleable_qty`。不再加销售未完成需求。FIFO 分配仍须读取完整销售需求；采购日期/供应商仅是催货批次依据，不能与未到总量来源混为一谈。

## 目标机部署步骤（后续重建参考）

2026-09-26 基础部署已执行，实际配置见 0.2.2 交付记录。以下为重建流程，业务 sender 仍须在完整验收后启用。

1. 核实宿主余量与现有网络，再新建独立 Debian VM。本次实际为 2 vCPU/2 GB/16 GiB，磁盘在 local 目录实际预留；不占用路由已分配资源。安装 Docker Engine/Compose。
2. 将此工作副本复制到 `/opt/ul-workbench`，不复制本机 `runtime`、测试凭据或构建缓存。安装 `service/requirements-api.txt` 到临时 Python venv，用 `scripts/setup_api.py --authorization <受限授权文件路径>` 交互初始化密码、授权与备份密钥。
3. 备份密钥另存受限位置；将 `service/runtime/data` 归属设为 UID/GID 10001，secret 目录 700、文件 600。数据库必须位于 VM 本地磁盘。
4. 先执行 `docker compose config`、镜像构建及容器测试，再启动。默认仅绑定 VM 回环地址 5191。如现有 HTTPS 代理位于另一台主机，必须选定 VM 内网地址并限制来源为该代理，不能映射公网 5191。
5. 现有公网代理已核实为 CT 102 上的 Caddy，部署时为 UL 增加独立域名路由；`deploy/nginx-site.example.conf` 仅供其他 Nginx 环境参考，不能直接用于此主机。用户移动网络实测 8443 可用、443 不可用，因此采用 `https://ul.yingyinghome.com:8443`。先建立 DNS/DDNS、覆盖 UL 域名的证书和实际后端地址，再验证续期；现有家庭站点证书不覆盖 UL。公网 80/443 不可靠时采用 DNS 验证申请证书，不替换既有站点。同步修改 APK 默认地址及已有客户端保存的服务器地址。
6. 待完整源实现和写入验收通过后，才配置物流/调整 sender；当前没有环境变量可以随意开启未经验证的写入。
7. NAS 挂载确认后，在 `/etc/ul-workbench-backup.env` 中配置 `UL_NAS_MOUNT` 和其下的 `UL_BACKUP_DIR`；非默认数据目录同时配置 `UL_DATA_DIR`。安装 `deploy/ul-backup.service` 与 timer，先手动执行备份、恢复演练再启用 timer。只定时备份，不定时更新业务。
8. 原签名放入 `runtime/signing/debug.keystore`（600）。设置 JDK 17 的 `JAVA_HOME` 和含 API 35 的 `ANDROID_HOME`，执行 `bash scripts/build.sh`。脚本运行 Android 测试、lint 和 debug APK 构建；未找到原签名时拒绝生成替代签名。

恢复在停服、保留旧目录后执行 `service/backup.py restore --runtime <全新目录> --file <备份包> --key-file <独立密钥>`。将新目录正确挂载并恢复 UID/GID 后，只允许查询/计算；逐笔核对备份之后的物流、调整和打印操作，再由维护人员解除 `restoration_hold`。不能仅清空 unknown 状态就上线。

## 验收及切换

依次完成真实数据对照、物流单笔/小批量无副作用、报损报溢防重复、蜂窝网络与蓝牙真机测试、目标机断网重启和 NAS 恢复演练。全部通过后停止旧脚本物流写入并切换。任一功能尚依赖 Windows 或未通过，上线保持阻断。

## 后续更新：宿主管理访问已解决（2026-09-25）

用户更正凭据后，通过 SSH 成功登录 `root@192.168.100.2`，以上“SSH 无可用认证”的阻断已解除。凭据未写入项目、报告或部署文件。本次只读检查，未创建或变更虚拟机、网络、存储和网站。

- 宿主：Proxmox VE 9.2.2，Intel N150，4 核；内存约 15.4 GiB，当前可用约 12.0 GiB。
- 现有 VM 100：iStoreOS，2 vCPU、2 GiB RAM；磁盘约 2.38 GiB + 30 GiB。
- 现有 CT 101：tailscale，1 vCPU、1 GiB RAM、约 8 GiB 磁盘；CT 102：hht-call，1 vCPU、512 MiB RAM、约 4 GiB 磁盘。
- 宿主 LAN：vmbr1，192.168.100.2/24；vmbr0 为另一物理网络桥。此处仅记录，不改变桥接或路由。
- 物理盘 sda 111.8 GiB；local-lvm 精简池约 49.3 GiB，当前实际使用约 5.0 GiB、实际空闲约 44.3 GiB。已有虚拟盘逻辑容量合计约 44.4 GiB；再添加 32 GiB 会使总承诺容量超过精简池物理容量，不能把“当前空闲”当成已为新增磁盘预留的容量。
- 结论：2 GiB 内存起始预算可行；独立 Debian VM 的磁盘位置、实际预留与容量监控还需明确。完整 API 业务映射、写入安全验收及 Android 构建等阻断仍存在。

## 后续更新：生产基线已复现，分类内核已迁出（2026-09-25）

NAS 新增 `private-business-baseline/order-classification-20260924-114346` 冻结资料，包含真实输入、完整输出、脚本、测试及入库账本前后状态。本地只读副本放在忽略版本控制的受限 audit 目录，不随源码交付。

- 原始脚本全流程复现：2,395 张订单、2,403 行配货明细，10 类最终分类全部一致；入库账本计算后状态与冻结 `after` 完全一致。
- 已从生产脚本抽取无 Windows/Excel/API I/O 的纯业务函数至 `service/core/classification/`；保留来源 SHA-256 和抽取函数清单。
- 迁出的内核再次复现同批输入：2,395 张订单分类、2,403 行实物/在途/缺口和入库分工证据均匹配，最终入库状态一致。
- 在途公式对照：17,157 个库存键中 17,135 个一致；1,082 个销售需求键中 1,080 个一致；18 个负在途键，其中影响 3 张订单。排除冲突订单后基础分类变化为 0。
- 上述完整原规则复现使用冻结批次的普通采购在途；公式对照是独立验证，不表示含 3 张冲突单的 v2 快照已经可以发布。
- 完整生产规则的 API 标准输入编排已加入 `core/business_rules.py`，保留五维身份，采购日期/供应商催货、持续来货分工、滚动入库窗口、红冲退货处理。当前真实 API 源仍未补齐，不会拿冻结样例充当在线数据。
- 入库账本随展示快照原子提交至 SQLite `settings.receipt_ledger_v1`；首次更新必须完成旧账本的 API 身份映射并明确提供已核验初始化状态，不能自动清零重建。账本纳入现有数据库备份。
- 后端回归：134 项通过，包含新增的混合来源补齐、保持原分工、批次数量消耗、7/8 天边界、催货优先级、委外批次缺口、账套隔离、负数精度和发布回滚。
- 原 Windows 95 项套件在 Mac 收集时受 `msvcrt` 依赖阻断；没有伪造 Windows 锁来宣称它们在 Mac 已通过。纯内核真实批次复现及本地 134 项回归为本轮直接证据。

### v2 标准输入增量

标准快照设置 `rules_contract: v2`，提供 `receipt_events_complete`、`purchase_batches_complete`、完整滚动 `receipt_period`（截至本次业务日期），及以下集合：

- `purchase_batches`：每个稳定 batch_id、完整五维身份、order_no、purchase_date、supplier_id/supplier_name、基本单位 open_qty。普通采购及委外总量须与推导在途闭合，不能将无日期/供应商的差额编造为普通批次。
- `receipt_events`：稳定 event_id、document_no、posted_date、type（进货入库单/进货退货单）、带符号 quantity、完整五维身份。红冲及退货符号必须经 API 对照验证。
- 每条销售明细明确提供自己的 `delivery_date`，用于按交期先后分配本单现货与采购批次；订单头 `delivery_date` 专用于物流号，存在多交期时需要原公式依据。
- 首次初始化提供 `receipt_state_bootstrap_verified: true` 和已映射 `receipt_initial_state`。账本必须带同一 `profile_id`；已有本地账本时始终使用本地状态。

v1 简化计算合同保留给已有迁移测试，不能作为生产 v2 完整入库/催货规则的替代。生产数据源覆盖、接口写入验收、Android 构建与部署仍未完成。


## 后续更新：原电脑补充回复已核验（2026-09-25）

已读取 NAS `给新电脑Codex的补充回复` 的 6 份说明、API 只读核验附件、Android 补丁与报告。31 个交付文件散列全部一致；修复后的基线清单 37 项与现有冻结输入/输出及新版 MANIFEST 全部一致。受限副本保留在忽略版本控制的 audit 目录。

- C 列公式依据已补齐：销售导出 L 列“到货日期”与 G 列完整订单号生成 `@MM/DD#完整订单编号`。这不是程序运行日期。
- 旧上传代码对相同完整订单号保留首行 C 值；冻结样本中 8 组重复订单均无交期冲突。因此该样本不能确立“不同交期取最早/最晚”的新规则；没有可靠主单交期且明细交期冲突时，不得擅自选日期写入。
- 历史奇门 `sell_count` 是销售数量，尚未证明为剩余未完成数量；字段中的状态、单位和交期还不足以实现完整标准合同。该证据来自旧查询时点，不能当成当前实时总数或断言所有 API 都不可用。
- `erp.bill.billcenterlist(vchtype=34)` 与 `beefun.gjsstockbilldetail.list` 有实际进货样本对照，适合到货流水；它们不能直接代表采购未完成量。退货/红冲关系仍需实样验证。
- 普通/委外采购未到批次仍未取得完整可用 API；旧 WMS 入库查询的空结果不能解释成采购未到为零。
- `beefun.orderfreight.updatefreight` 有历史字段写回证据，但没有写前后出库/记账、剩余量证据，继续保持未验收状态。日常物流仍按用户要求比较上次成功缓存；写后业务状态核对仅用于首次接口验收及未知结果处理。
- 报损报溢官方新增、保存/审核/过账、查询回执与幂等接口仍未闭环。暂不启用真实业务写入，不用 Windows 导入替代上线要求。
- 原电脑独立构建提交一行测试修正：保存“相同服务地址”应使用当前 `serverUrl`，不能写死已废弃局域网地址。已审阅并合入；另一个更换服务器清空工作区的测试仍保留。

API 缺口不能由历史说明或冻结样本代填。继续推进前，优先查明完整未完成订单/采购读取合同及报损报溢写入能力；当前资料已收齐的部分不再重复索取。

Mac 工具链与完整 Android 验证现已通过，详见 `Mac-Android构建环境.md`；Android 工具缺失不再是迁移阻断。公网服务与真实 ERP 写入仍未上线。


## 用户口径更正及 APK 连接问题（2026-09-25 晚间）

- 用户明确更正：库存查询的“订单数量”和“改制单数量”都计入采购未到；可销售库存 = 库存数量 - 订单数量 - 改制单数量。这里库存查询的订单数量不得混用为销售未发货需求。
- 两套计算入口统一改为 `physical_qty - saleable_qty`，规则版本更新为 `4-api-1/4-api-2`；完整同维度的 `stock_order_qty/rework_qty` 若提供，校验非负与差额精确闭合。历史冻结批次的旧公式对照保留为历史结果，不能当成本次更正后的验收。
- 只读抽查近期订单涉及的 3 个库存记录：接口返回 `qty/saleqty`，3 个差额均为正；没有单独返回订单/改制数量。该抽查不代替全量字段口径验收，未执行业务写入。
- 未到总量不再以“必须取得采购未到列表 API”为前提；完整 FIFO 仍需销售未完成量与交期，催货规则仍需采购/改制批次日期及供应商信息。
- 用户确认 `beefun.orderfreight.updatefreight` 不影响发货/记账，只修改物流信息。该确认与历史本机试验记录分别记录；本次没有重新写单。曾触发发货的 `beefun.deliver.updatefreightinfo` 继续排除。
- APK 无登录页、无数据的根因：0.2.0 默认指向尚未配置的 `https://ul.yingyinghome.com`，权威 DNS 查询返回 NXDOMAIN，N150 业务服务尚未部署；原 App 只在收到 401 时打开登录页，连接失败落在空工作台。
- 0.2.1 增加首次连接引导，地址不可用时明确显示状态并允许重试/修改地址；服务返回需登录后进入登录页。已加载数据后暂时断网，保留原工作区。未连接时不把扫码输入路由到查询/打印。
- 这次连接引导修正不能代替 N150 后端、域名、HTTPS、管理员和数据同步部署。开发包尚不是可用的公网业务交付。

0.2.1 最终验证：后端 139 项、Android 121 项全部通过；Lint 0 error/4 warning；连接页面截图已核查，原开发签名一致。
