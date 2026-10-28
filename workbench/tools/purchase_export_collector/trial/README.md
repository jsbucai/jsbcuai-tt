# 独立采购采集试验排程

截至 2026-10-07，N150 的独立试验 service 已真实执行成功，`ul-purchase-trial.timer` 已启用。使用现有 `ulcollector` 账号、`/etc/ul-purchase/credentials.json`、`/opt/ul-purchase-collector/venv` 和 `/opt/ul-purchase-collector/src`。`run.sh` 安装位置为 `/opt/ul-purchase-collector/bin/run-trial.sh`；依赖单独的 `ul-purchase-display.service` 提供 `:97` 图形会话，以及 collector 的 `--wait-for-human` 支持。采购仍是隔离试验，两项 API timer 已启用，正式采购 `latest.json` 尚未发布。

API 更新在中国时区每天 07:30、11:30、16:00 入队。为避免 2 GB N150 上浏览器采集与 API 查询同时启动，采购试验改在每天 07:40、11:40、16:10 触发，仍位于原三个 ±15 分钟验收窗口内。采购计时器不补跑错过的时段，覆盖统计会如实显示缺失，避免重启后在夜间启动无人值守的浏览器。包装器启动浏览器前只读检查 API `update_jobs` 是否有 `queued/running`，每 5 秒检查一次，最多等待 30 分钟；数据库不可读或超时就记录本轮失败状态，不启动浏览器，也不覆盖上次成功快照。`run.json` 分别记录 timer 触发时间和真正开始采集的时间；等待导致实际采集晚于验收窗口时，统计如实不计该时段成功。查询开始日期固定为 1900-01-01，结束日期在每轮启动时按中国日期计算；范围过大、ERP 不接受日期或文件校验不通过时记录失败，不能静默缩短范围。

只更新 `/var/lib/ul-purchase-collector/trial` 的试验快照。正式目录只允许更新 `collector-status.json`，不会写正式 `latest.json`、工作簿或业务数据库。每轮的结果、错误和脱敏下载诊断分别存入 `trial/runs/<时间>-<随机值>`，`run.json` 标记是否成功、跳过或待人工。已有试验快照仍位于同一个 trial 根。

通过 `flock` 防止与其他本包装器进程重叠。手动测试也必须从这个包装器进入，不能绕过锁直接启动 collector。进程正在等待验证码时保持原浏览器，后续时点不新建登录；出现 `manual-review-required.json` 后，后续任务只记录等待，不再登录。机器重启后检测到上轮 `running/needs_human` 也会保留人工检查状态。

人工恢复分两种：

1. 原进程仍在运行：在 `:97` 的受控图形会话里手动完成验证码。采集成功后自动解除本轮等待，不需要重登。
2. 原进程已退出或需要重新开始：先停止 `ul-purchase-trial.service`，确认原浏览器进程已停止、图形会话可由人工接管，然后执行 `sudo -u ulcollector /opt/ul-purchase-collector/bin/run-trial.sh --release-human-hold`。该命令仅存档解除记录和更新状态，不登录。随后由操作人启动一次 `ul-purchase-trial.service`；出现验证码继续在同一会话手工处理。

不要仅删除状态文件以触发重试。检查成功应以快照清单、文件/网页计数和 `run.json` 为准；systemd 服务执行成功也可能表示本轮安全跳过。

## 已验证的运行与剩余验收

全日期范围 `1900-01-01..2026-10-06` 已在 N150 成功下载 91 行，与网页记录数一致，包含 39 张订单、12 家供应商。较短范围 `2025-09-01..2026-10-06` 为 90 行、38 张订单、11 家供应商，说明只查近期会漏掉仍未交的老单。首次全范围快照和文件 SHA-256 见上级 [README](../README.md)。实际下载需要打印菜单之后的 `.PrintErrMsg` 二级导出按钮，已由 Linux Chromium 完成。

中国时间 04:13:21 启动的真实 service 在 04:14:24 成功完成，生成 `snapshots/20261005T201424Z-d4f977205ebd-c77f82eb/manifest.json`；这是夜间手动试跑，不算当天 07:30、11:30、16:00 的验收成功。systemd 单元与三个上海时区调度表达式已验证；首次预定触发为 2026-10-06 07:30，是否成功必须查实际记录。

Xvfb `:97` 已保持运行。manual-view 曾启动验证 HTTP 200，VNC `5901` 与 noVNC `6081` 仅监听 loopback，验证后 viewer 已关闭；需要人工处理时才按需打开受控查看通道。真实验证码尚未出现，3 个模拟会话等待测试不替代现场验收。

## 只读统计连续三天

`summary.py` 只读取指定目录内现有 `**/run.json`，不启动浏览器、不改运行记录、不读凭据、不写任何快照或正式文件。在 N150 可执行：

```bash
sudo -u ulcollector /opt/ul-purchase-collector/venv/bin/python \
  /opt/ul-purchase-collector/bin/trial-summary.py
```

本机复核已复制的试验记录：

```bash
PYTHONPATH=workbench/tools .audit-venv/bin/python \
  -m purchase_export_collector.trial.summary --runs-dir /path/to/copied/trial/runs
```

统计以 `started_at_utc` 转换到 `Asia/Shanghai` 后的自然日归属为准。三个启动窗口分别为 07:15–07:45、11:15–11:45、15:45–16:15（含边界），仅 `status=succeeded` 计为成功。每个窗口最多贡献一次成功；重复运行不会补足别的窗口，失败、阻塞和跳过不能计入成功，纯解除人工等待的记录不算采集。窗口外的夜间试验被排除。现有 `run.json` 没有定时/人工来源字段，因此窗口内人工启动也会按此规则计入；要验收“定时触发”还需结合 systemd 日志。

JSON 输出提供 `consecutive_complete_days`、`streak_through_date` 和包括今天在内的最近三个自然日 `recent_days`。各 slot 标为 `succeeded/failed/missing`，同时保留实际尝试状态和窗口是否已结束；正在执行但尚无最终结果的时段仍不算成功。当天未到 16:15 且未全部成功时，连续天数截至昨天；当天全部成功或已到 16:15 后纳入当天。连续性按日倒推，缺失一天就中断，不回跳寻找更早的三次成功。无记录的日期和时段明确保持缺失。`--as-of` 可指定带时区的统计截点，晚于截点启动或更新的记录会被排除。

即使 `three_consecutive_scheduled_days=true`，输出的正式 `gate` 仍固定为 `false`。单位口径 `units_verified`、完整覆盖 `coverage_complete` 和异常恢复 `exception_tests_accepted` 均需另行验收；91 行与网页一致只证明该次所选查询范围内的一致性。至少还要核实单位、账号可见范围与早期未结订单、真实人工验证、断网/中断/重启恢复、页面结构变化以及损坏/不完整文件不覆盖上次成功快照。统计脚本不自动修改这些验收结果或开放正式发布。
