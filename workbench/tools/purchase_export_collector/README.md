# 采购未交订单浏览器采集器（独立试验模块）

这个模块只通过管家婆毕方 HTTPS 网页查询和下载「进货订单统计列表」。它不调用业务写入接口、不修改订单，也不把试验快照接入现有后端或 Android。**2026-10-06 的 N150 Linux 隔离试验已成功下载并校验全日期范围的 91 行，与网页 91 行一致；独立试验 timer 已启用，正式数据发布仍关闭。** 定时试验说明见 [trial/README.md](trial/README.md)。

## 取数口径

网页操作顺序：登录 → 进货统计 → 单位方式 → 指定日期 → 详情 → 全部列表 → 筛选「未完成数量 ≥ 0.1」→ 打印下拉的「导出数据」→ 在二级 `.PrintErrMsg` 弹窗再次点击导出。后一个按钮才触发真实下载，这是此前下载等待超时的原因，现已在 N150 验证。采集使用 Playwright 的下载完成事件接收原始 `.xlsx`，不依赖打印插件、Windows 或其保存窗口。必须显式指定开始和结束日期；程序不会默认「近一个月」。早于开始日期的未交订单不会出现在结果中，首次全面覆盖范围要另外核实。

下载后校验工作表名称、页内查询日期、第 5 行附近表头、每行订单和商品身份、日期、未完成数量下限、合计行、网页记录数与文件行数。任何校验失败、零行、登录验证或页面变化都会中止发布。每次成功生成不可变的 `snapshots/<批次>/purchase-unfinished.xlsx` 和 `manifest.json`，最后原子更新 `latest.json`。失败不会覆盖上次成功快照。快照保留原始行，重复供应商/订单/商品/仓库组合只标记候选重复，不静默合并。

## 离线验证

在仓库根目录执行（以现有本机 Python 环境为例）：

```bash
PYTHONPATH=workbench/tools .audit-venv/bin/python -m purchase_export_collector.collector validate \
  --xlsx /Users/apple/Downloads/2026_10_06-00_35_44.xlsx \
  --start 2025-09-01 --end 2026-10-06 --expected-rows 90

PYTHONPATH=workbench/tools .audit-venv/bin/python -m unittest discover \
  -s workbench/tools/purchase_export_collector/tests
```

## N150 隔离试验与后续验收

N150 已使用 Chromium、Playwright 和现有 `ulcollector` 账号执行真实下载。凭据复用 `/etc/ul-purchase/credentials.json`，不需要另索凭据；JSON 字段为 `login_url`、`company`、`username`、`password`，文件权限为 `0600`。`login_url` 保留旧配置兼容，必须是官方 `https://` 域名；程序实际从 `https://www.wsgjp.com.cn/` 顶栏登录链接读取 `https://www.mygjp.com.cn/ngpLogin.aspx`，待 `https://passport.mygjp.com.cn/erp/ngploginNew` iframe 中的密码表单出现后才填入凭据。不要把密码放进命令行、代码、日志或仓库。输出目录限制为 `0700`。每轮采用新的浏览器会话；遇人工验证时可保留该轮会话，避免另起登录丢失现场。

```bash
# N150 上人工试跑也走同一 service/包装器及其 :97 图形环境。
sudo systemctl start ul-purchase-trial.service
```

包装器使用 Xvfb `:97`、`--headed --wait-for-human`。页面需要验证码、短信或扫码时状态为 `needs_human`，进程与原浏览器保持存活，等待人工完成；默认 CLI 不加 `--wait-for-human` 时仍快速退出，不尝试绕过验证。人工查看可按需打开只监听本机回环地址的 noVNC，具体恢复步骤见 trial 说明。真实验证码尚未遇到；目前会话等待逻辑通过了 3 个模拟单元测试，不能当作真实验证码验收。

2026-10-06 的实测证据：

| 查询范围 | 文件/网页行数 | 订单数 | 供应商数 | 结果 |
| --- | --- | --- | --- | --- |
| 2025-09-01 至 2026-10-06 | 90 / 90 | 38 | 11 | 成功下载；范围较短漏掉一张更早订单 |
| 1900-01-01 至 2026-10-06 | 91 / 91 | 39 | 12 | 成功下载并校验；保留为隔离试验快照 |

全范围首次成功快照位于 `/var/lib/ul-purchase-collector/trial/snapshots/20261005T200545Z-66a1b7a0ff8c-b3b80b70`，工作簿 SHA-256 为 `66a1b7a0ff8c18a822b08186a9bea6370cceadacf1a6f1abe57b56810478c482`。随后真实 systemd service 在中国时间 04:13:21 启动、04:14:24 完成，同样得到 91 行，清单位于 `trial/snapshots/20261005T201424Z-d4f977205ebd-c77f82eb/manifest.json`。这些夜间手动试验不计入三个固定时段连续三天验收。

display、manual-view、trial 的 systemd 单元已通过 `systemd-analyze verify`，三个上海时区的 `OnCalendar` 已核对；独立采购试验 timer 已启用。两项 API timer 仍禁用，正式采购 `latest.json` 不存在。此状态记录截至 2026-10-06 本次操作，后续应以运行记录和现场状态为准。正式发布前仍需验证单位口径、早期未结采购单和账号权限覆盖、重启/会话失效/验证码/页面变化/不完整下载的恢复，以及三个固定时段连续三天的实际结果。

数据是带采集时间的**快照**，不是实时 API。原始导出没有稳定的订单明细 ID，也未显式给出计量单位；即使程序校验通过，`units_verified=false`、`coverage_complete=false` 仍保持，不能直接用于对外催货或按单位抵扣在途库存。
