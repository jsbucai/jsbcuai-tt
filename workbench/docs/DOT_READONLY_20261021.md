# dot 业务只读接入，2026-10-21

已在现有 0.5.23.2 销售网页基础上发布只读接入；本轮最新为 `ul-workbench:0.5.23.2-dot-procurement-1`。固定销售网页设置、对话移除与恢复、原 App、销售 API 和打印事实均保留。没有提交、修改或审核 ERP 订单，没有恢复或复制业务数据库。

入口：https://yingyinghome.com:8443/dot/ 。使用原独立只读账号。登录后默认仍显示 Dot资料，可切换数据状态、订单、库存、采购流水。采购为已过账流水，不能据此判断采购未到货；源字段缺失保持空白，库存不跨商品或单位汇总。

## 代码与边界

- UL 源码归本项目：`service/app.py` 注册独立身份及路由，`service/core/dot_readonly.py` 提供四个 GET，`service/dot-readonly-manifest.json` 固定兼容源码哈希。
- 传输与网页维护在相邻 N150 项目 `dot-access/`：`ul_client.py`、`business_contract.py`、`gateway.py`、`worker.py`、`index.html`、`app.js`。
- 固定账套 `27056512834386351`、仓库 `29871293290508390`（08390）；不接收覆盖范围、SQL、表名、文件路径或上游 URL。
- 四条路径 `/api/dot-readonly/v1/{sources,orders,inventory,purchases}`。最多每页 100 条，继续分页绑定首个数据版本；版本变化 409。查询在同一个 SQLite 读取事务内，SQL authorizer 禁止写操作及认证等非白名单表。
- 最小字段不包括金额、联系人、联系方式、地址、自由备注、认证字段或原始对象。隐藏字段不参与搜索；网关和客户端同时校验精确响应形状，错误响应也仅投影固定错误码与固定说明。
- 独立机器身份不属于员工，不能访问原 App 或销售接口；员工凭据也不能访问机器接口。机器令牌只在 VM `/etc/dot-access/ul-client.json`，root:dot-reader、0640；UL secrets 策略仅保存哈希，10001:10001、0600。不写日志、浏览器或本地交付文件。
- CT 复用 8443，仅 VM 103 可访问 `/dot/_ul/v1/*` 的四条 GET 映射，未知路径和方法拒绝；公网 UL 域名直接访问机器接口为 403，伪造 X-Forwarded-For 不改变来源判断。CT 到 UL 保留已批准的内网 HTTP。
- dot-reader 继续禁止读取生产 runtime，目录保持 0700，旧直接读库配置仍禁用；网络与 systemd 保护没有放宽。N150 浏览器执行仍关闭。

## 验收证据

已通过：候选真实 Flask 隔离测试 11 项；客户端、契约、网关、NAS 合成测试 27 项；本项目销售工作台及客户归属回归 41 项。候选无网络、无生产挂载，仅临时合成数据库。线上经公网 HTTPS 登录、四类查询、跨页版本一致、过期版本、跨范围、分页上限、写入拒绝、内部路径拒绝、注销撤销均通过。

2026-10-21 00:25:56 CST 启动新容器，更换到健康响应 4.61 秒。不是单次请求中断时长的精确测量。四类经过 dot 网关的读取为 0.316–0.548 秒；这四次样本不构成 p95 性能结论。

验收时来源版本 `1621e8a0aac1025c7b31`，来源时间 2026-10-20 18:45:05 +08:00，订单 2234、库存 18237、采购过账流水 2491 条，采购范围 2026-08-07 至 2026-10-20。数值是本次快照，不保证未来不变。

NAS 75 字节合成验收文件下载 SHA256 仍为 `6cd52f74b1c2054e91d35859449f497ff5aafd3b0a0dad5fa8e4f1aa8796d11a`。dot 账号实测不能读取生产数据库，相关日志未发现机器令牌；VM 可用内存约 1425 MiB。新增业务尚未由 dot 云端独立验收，未进行 Mac 真实关机试验；运行链路本身在 N150，不依赖 Mac 转发。

两条旧 native claimed 打印记录 `ef0d6f8f10f949349b02bb1bcb706ca5`（10月14日）和 `4bc4420454b940458696ca323ec0c4eb`（10月16日）前后状态和更新时间不变，实际出纸仍未知。没有清理、释放或重发。切换时同步、库存上传、销售模型、新商品及销售 API 队列为空。

详细证据：相邻项目 `evidence/2026-10-21-dot-business-https-acceptance.json`、`2026-10-21-dot-ul-cutover.json`、`2026-10-21-dot-ul-final-vm.json`、`2026-10-21-dot-ul-final-ct.json`。

## 维护与回退

后续升级若变动兼容清单内源码或 schema，业务读取将停止。必须重新验收字段、范围和身份隔离，生成新清单并随镜像部署；不能只改哈希跳过验收，也不能回退直接读库。UL 项目正常发布要包含本模块及清单，避免覆盖该集成。

本轮只通过 PVE 192.168.100.2 的 `qm guest exec 103` 管理 VM，未使用 VM 直接 SSH。原始基线与回滚镜像：`ul-workbench:dot-rollback-20261020`；VM/CT 受限备份 `/root/backup-dot-access/ul-readonly-20261020` 和 `/root/backup-dot-access/ul-readonly-20261021-handoff`。含敏感配置的备份仅留服务器，不输出或拷入普通文档。

回退先关闭网关业务能力及机器身份，恢复 files-only 的 dot 程序；撤销本轮内部路由并校验、平滑重载 Caddy；必要时恢复本轮之前的 UL 程序/compose 镜像并重建容器。只回退程序，不恢复业务数据库，不撤销新增销售、打印或人工事实，不加回旧整库 ACL。已执行的部署工具不可盲目重跑，应先读回各阶段状态。

## 需采购产品数：2026-10-21 01:19 CST 已完成

新增 GET /dot/api/procurement，内部对应 /api/dot-readonly/v1/procurement；不接受范围或分页参数。网页新增“需采购产品数”。现有四条接口响应保持不变，dot-ul-readonly/2 增加独立最小响应契约。

公网 HTTPS 实测 **400 项**。来源2026-10-20T18:45:05+08:00，数据版本1621e8a0aac1025c7b31，任务版本572，tasks_current=true、valid=true。口径为采购看板 role=purchase、view=role、status=procurement 的商品事项总数，不是订单行数、支数、负库存或已过账采购流水数。未刷新 ERP、未写业务订单。

core/procurement_projection.py 与 task_board 共用稳定身份归组、缺口合计和 shortage>0 筛选；与 customer_orders 共用订单供需校验和客户整单分组。读取同版本已发布 allocation，不重新执行库存分配、不调用员工看板或代理管理员。内部 SQL 仅投影必要业务字段，未读员工/认证/跟进备注表；只输出指标及版本。缺投影、不同版本、计算缺失或范围/身份冲突返回 valid=false,total=null 和固定原因，未知不显示0。同一读取事务保证版本一致，兼容清单已增加共用计算模块。

生产镜像 ul-workbench:0.5.23.2-dot-procurement-1，ID sha256:a258edebed1b1e91fc1037fa254321ae5b63148b7edc93a04acc698df371b858；2026-10-21 01:18:03 CST 启动，切换到健康3.46秒，重启0。同步、打印、调整及销售队列空闲检查通过后切换；两条旧claimed打印状态、元数据及冻结内容不变。

验证：122项UL回归（包含11项最小投影测试）、30项dot契约/网关/客户端/NAS测试、14项Docker隔离Flask测试。另以修改前正式看板对照9种合成场景一致，审计代码在 outputs/dot-procurement-20261021/test_original_board_comparison.py。首次候选夹具缺first_seen_at，修正夹具后14项全过，失败候选未部署。候选无网络、无生产数据挂载。

公网原四类接口、分页版本、写拒绝、内部路由、伪造来源拦截、注销撤销均通过；采购本次请求0.743秒，仅单次样本，不称p95。原NAS 75字节连接验收文件SHA256仍为6cd52f74b1c2054e91d35859449f497ff5aafd3b0a0dad5fa8e4f1aa8796d11a。runtime目录0700，dot-reader不能直接读库；机器凭据/策略、网关配置和systemd隔离不变，浏览器执行仍关闭。

VM/CT程序配置备份：/root/backup-dot-access/procurement-metric-20261021。本轮回退基线为0.5.23.2-dot-readonly-1，按该目录恢复程序/compose、网关程序与Caddy并重载；不恢复数据库，不撤销新增业务事实，不回到更早丢失dot集成的销售镜像。部署工具不可盲目重跑。

证据：相邻N150项目 evidence/2026-10-21-dot-procurement-https-acceptance.json、cutover.json、final-vm.json、final-ct.json（后三个文件同前缀）。父线程独立云端验收仍待完成；本机通过公网域名的验收不冒充云端结果。未进行Mac真实关机试验。
