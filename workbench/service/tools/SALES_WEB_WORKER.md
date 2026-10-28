# 销售网页执行器：隔离预检与开通门槛

此执行器默认关闭。API 只把已确认版本写入 `sales_order_import` 队列；N150 的单一 browser lane 依次处理采购采集与销售导入。销售子进程才读取 N150 私有网页登录凭据、ERP API vault 和原始空白 XLS 模板。API 镜像与 APK 不持有这些文件。

在 N150 以 `ulcollector` 运行 `sales_web_host_preflight.py` 生成只读预检报告。传入已验收的 ERP `Main.gspx` URL、最近一次只读网页登录验收 JSON、原始空白模板的受限副本，以及以下 N150 私有路径：

```
--erp-main-url https://bfuned5main.wsgjp.com.cn/Main.gspx
--account-evidence /var/lib/ul-purchase-collector/readback/acceptance-20261007/evidence.json
--erp-vault /opt/ul-workbench/service/runtime/data/secrets/erp.json
--credentials /etc/ul-purchase/credentials.json
--collector-source /opt/ul-purchase-collector/src
--runner-python /opt/ul-sales-runner/venv/bin/python
--browser-profile /var/lib/ul-sales-runner/browser-profile
--chromium /usr/bin/chromium
--dictionary-evidence /opt/ul-sales-runner/private/sales-erp-dictionary-live-probe-20261007.json
--output /opt/ul-sales-runner/private/sales-web-host-preflight-20261007.json
```

上述 Main.gspx 域名与已有只读验收的 account scope 在 N150 重新计算后相符；这项核对未重新登录，也未写 ERP。

报告在指定 `--output` 路径以 `0600` 新建。程序只返回报告路径与 SHA-256，不打印密码、Token 或原始 ERP 响应。报告含 API profile、网页登录 account scope、客户/职员/仓库精确唯一 ID、空白模板 SHA-256 及 **`enabled=false`、`acceptance_complete=false`** 的 host config 候选。客户 binding 带 24 小时有效期；真实提交前会再调用 ERP 客户目录只读 API，严格核对同一客户名称与 ID。任何查无、重名、停用、账套不符或分页不完整都会在上传前中止。

预检使用不续期的只读 ERP 客户端；授权即将过期时会停止，不改私有 vault。它只创建指定的候选报告文件。

三名职员共用同一次独立双分页扫描，报告保留 `reported_total`、实际 `catalog_count`、`completeness_basis` 与共同 `snapshot_hash`。当前 ERP 员工列表报告 13、实际稳定 14；该差异会原样留档，扫描必须在不同页大小、终止页和重复首页间一致。制单人 ID 还必须等于私有 API vault 的 `token.employee_id`。可选的 `--dictionary-evidence` 只引用此前只读探针的私有文件路径与哈希，不把它当作启用配置。

原 NAS 模板 `/mnt/nas/开发项目/Fastener_AI/Fastener_AI_N150_20261006-001631/01_原始数据/业务单据导入模板 (录单使用).xls` 当前为 `root:root 0600`，`ulcollector` 不能直接读取。开通前需复制至专用私有模板路径并核对原始 SHA-256 `a24e8a1f34d781b8719840def86bf02452ecc53f8bfe681e5a629c48c30fa2e2`；不得把已填写样单改头当作模板。

正式写入只在 host config 同时明确 `enabled=true`、`acceptance_complete=true` 且模板/账号/仓库/经手人/制单人/冻结稿一致时开放。首次现场验收可保持这两个开关均为 `false`，另在 N150 私有 host config 加入**仅一张单**的 `trial_authorization`，格式如下；缺字段、多字段、类型不符或冻结稿不一致均会在打开 ERP 前阻断。`taxed_total` 必须是准确到分的整单含税金额字符串，执行器还会从实际生成的每行未税金额与 13% 税额重新计算并逐字比较，才允许唯一上传尝试。此配置不代表正式验收完成，预检报告默认不生成它。

```json
"trial_authorization": {
  "schema": "sales_web_trial_v1",
  "session_id": "<已冻结会话 ID>",
  "confirmed_hash": "<64 位确认稿 SHA-256>",
  "order_number": "<唯一完整单号>",
  "customer_name": "<精确客户全称>",
  "line_count": 2,
  "taxed_total": "3.39"
}
```

子进程先做实时客户 ID 预检，再由 `SalesWebImportTransport` 持久化唯一 intent 与上传前 `attempt.json`。进程中断或上传结果未知后，只能针对原 intent 与完整单号回查，不能重新上传。完整 ERP 列表、逐行明细、税额、摘要与已授权的单位归一证据均通过后，`SalesAssistant.finish_web_import` 才在同一 SQLite 事务把队列标记成功、会话标记已提交。共享队列的人工 `resolve()` 与普通 `finish()` 不接受销售单成功结案；销售单只能走上述专用只读回查与原子结案。

普通未知结果保留为 `uncertain`，不会自动重试。操作员可在同一执行器使用 `reconcile-sales --operation-id <原队列32位ID>` 明确触发一次只读回查；该命令先检查原 `attempt.json`，即使原始 writer 开关已关闭，也不提供上传路径。回查不完整继续保留 `uncertain`，仍不能重新入队或改用新单号补发。

**当前不得启用正式 writer 或发布。** 精确 trial 授权只覆盖其绑定的一张样单，并须另有该张样单的用户授权。正式开通仍需完成真实多行、重复 SKU 样单的网页回查与现场 App 验收，并审核 host config、API policy 的客户绑定及执行器依赖。N150 现有 `/usr/bin/python3` 和采购 Playwright venv 缺少销售服务依赖，因此销售 child 应使用独立 `/opt/ul-sales-runner/venv/bin/python`。
