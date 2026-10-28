# 管家婆官方 API 独立连接验证

## 订单数据只读核查

`audit_order_remark_trace.py --run` 对真实已返回订单递归检查奇门表头/明细/扩展，再以相同单号核查两个发货单接口，并从近期销售出库记录中选最多 3 张源表同号单核查行 `comment` 及单据中心扩展。最多 12 次标准查询和 2 次奇门查询；仅输出字段路径/统计。实测销售出库明细的 3 个 `details[].comment` 与原订单备注完全一致；未出库备注尚未接通。报告 `runtime/wsgjp-connect/order-remark-trace.json` 无业务原文。

`audit_qimen_orders.py --run` 固定调用官方 `qimen.taobao.erp.order.sync`（文档定义为查询），默认 5 条样本；`--verify` 最多 3 次查询；`--full` 最多 25 页、2,500 条及一次已返回单号查询。使用独立受限探针，不扩大 5192 的通用读取白名单。实际已命中当前订单，但 2026-09-16 查询的 1,868 条记录含 52 条完全重复，去重 1,816 个订单号；备注、物流及有效内部 ID 尚缺，不能直接替换现有更新。指定查询窗读完不等于全部业务订单同步完成。报告为 `runtime/wsgjp-connect/qimen-order-audit.json`，只保存字段/统计。

`audit_sales_all.py --run` 固定使用 `dlytype=-1`（用户业务为普通订单借用网店发货），分页读取近 7 天销售出库表头，再取两张单据的明细样本。报告为 `runtime/wsgjp-connect/sales-all-types-audit.json`，不含实际单号或业务原文。它验证的是销售出库记录，不能当成完整未完成销售订单同步。

`audit_order_sources.py --run` 是单独手动运行的诊断入口，不改变 5192 页面及其三个查询方法白名单，也不修改 5088/5191。诊断读取器仅允许物流字典、单据中心、完整/非敏感发货单、销售出库表头/明细、网店列表、管金所进货详情八个已核对文档的查询方法，每个读取器最多 12 次请求；不会执行 ERP 写入或修改业务表格。默认 `--run` 执行来源对照；其他诊断各自使用固定查询入口。

`audit_purchase_source.py --run` 只调用一次 `beefun.gjsstockbilldetail.list`，查询近 7 天首 20 条并与本地进货明细、采购订单做单据号+商品编号存在性对照。实测 20 条均匹配进货明细，0 条匹配采购订单；返回字段没有未完成数量或仓库，不能替代采购未到量源表。报告为 `runtime/wsgjp-connect/purchase-source-audit.json`，不保存业务原文。

输出 `runtime/wsgjp-connect/order-source-audit.json`，不保存密钥、实际订单号、客户资料或原始响应。核查结论见 `docs/ERP_API_READONLY_AUDIT_2026-09-16.md`。截至 2026-09-16，入库样本可对上，现有销售订单未被上述发货单接口返回，不能据此认定数据不存在，也不能宣布可以完整替代当前自动化。

2026-09-16。该工具用于首次账套授权和少量只读查询，不替换线上数据源。

## 启动与使用

在工作台项目目录执行：

```powershell
& .\tools\wsgjp_connect\start.ps1
```

在运行服务的本机打开 `http://127.0.0.1:5192/`。从管家婆应用管理页面复制 App Key、App Secret、Sign Key 到本机表单；本机表单不要求 ERP 登录密码。

点击官方授权后，在 `https://authcentral.wsgjp.com` 登录并检查授权的公司/账套。授权码兑换成功后，输入已知商品编号，最多核对一个商品、20 个仓库及 50 条库存记录。超过范围会停止并提示，不把部分数据当完整结果。不调用写入接口。

本次授权请求的回跳地址是 `http://192.168.1.5:5192/integrations/wsgjp/callback`。官方文档要求回跳主域名与注册时一致，本机使用原登记的 IP，但测试端口不同；平台是否接受仍须以实际官方授权页面为准。5191 登记的两个预留地址没有被改动，仍为未启用状态。没有开放公网、修改 DNS 或添加防火墙规则。

配置页、查询动作仅接受 loopback 地址和主机名；回调接受随机、限时、单次使用的 keyword，并核对 appkey，拒绝重复参数和重放。授权码使用后立即跳到无查询参数的本机页面，使用 no-referrer/no-store。请不要分享含授权码的浏览器地址。

凭据与 Token 使用当前 Windows 用户的 DPAPI 加密，放在 `%LOCALAPPDATA%\ULWorkbench\wsgjp`，目录 ACL 仅授权当前用户与 SYSTEM。不进入源码、APK、工作台数据库或普通项目备份。不打印密钥、令牌或 ERP 原始响应，不输出请求访问日志。

## 与正式工作台的边界

- 只运行独立 5192 进程，不加载 5191 的自动更新或报损报溢 worker。
- 5088、5191、ERP 原始 XLS、打印模板及 App 数据来源不变。
- 只允许 `erp.ptype.list`、`erp.stock.list`、`erp.goodsstocks.list` 三个查询方法；“只读”是本工具的调用限制，不代表官方授予的 Token 本身具有只读权限。
- 查询结果只在进程内存显示，不写入工作台数据。
- 强制请求正式 HTTPS 网关，拒绝 HTTP 和重定向转发；不自动重试授权码兑换。
- Token 距到期 24 小时内在页面访问或只读查询前自动续期，并继续用 DPAPI 原子保存；刷新失败不覆盖旧 Token。工具不接收通知、不定时同步，也不开放写方法。

## 已验证与待验证

已验证：正式 HTTPS 网关可响应；官方 Token SHA256 和业务 MD5 签名示例匹配；22 项独立测试通过（包括状态、CSRF、重放、只读白名单、DPAPI、自动续期及刷新失败保留旧 Token）；本机配置页 200、局域网配置页 403、无效授权回调 400；原 5088 与 5191 可响应。应用配置已从用户明确提供的本地文档导入 DPAPI。

2026-09-16 实际验证完成：用户在官方页面完成账套授权，经同一内网 IP 的 5192 端口回跳后，Token 兑换及 DPAPI 加密保存成功；已获得授权令牌与刷新令牌。成功调用正式商品、仓库和库存查询。此局域网授权/主动查询流程不依赖公网入站。

抽样核对：LS3073（六角头螺栓，M20*80，8.8级特殊热浸锌）返回 1 条库存，Qty=16、SaleQty=16，与当前 5191 从 XLS 导入的该商品名称、规格、型号和两项数量一致。用户先查的 LS0123 返回商品但库存记录为空；5191 对应商品数量均为 0，但空记录不能普遍当作库存为 0 的契约保证。对账只覆盖这些样本，未证明所有订单、仓库或商品均一致。

未在 5191 实施：生产数据源切换、后台定时同步、订单与采购等其他 API。Fastener_AI 已另行复用同一 DPAPI 授权完成商品全量/增量同步，但不改变 5191；其健康接口的 `wsgjp_api:false` 仍正确。

浏览器兼容性：本地操作页使用 `Referrer-Policy: same-origin`，避免 Chromium 表单提交在 `no-referrer` 下发送 `Origin:null`；仍拒绝不可信/不透明 Origin，并保留会话 CSRF 校验。带授权码的回调仍固定 `no-referrer`，不将授权码传播到后续页面。配置更新没有修改现有服务。

如需停止，仅停止 `runtime/wsgjp-connect/server.json` 指向、且命令行确认为本目录 `server.py` 的进程；不删除 Token、不停止现有工作台。没有安装自启动任务。

## 开发检查

2026-09-16 订单补查工具：`audit_query_matrix.py --run` 为固定只读方法的 48 次有界诊断；`audit_same_order_mapping.py --run` 为最多 12 次同单备注映射核验。它们不扩展服务 Client 的读取白名单，不修改 ERP 或 App 数据源。结论及边界见 `docs/ERP_ORDER_REMARK_MAPPING_2026-09-16.md`。报告只含结构、状态与比较统计。

```powershell
& .\service\.venv\Scripts\python.exe -m pytest .\tools\wsgjp_connect\test_connection.py -q
```

官方依据：
- 授权：http://d7safe.mygjp.com.cn:8020/web/#/erpapi/298
- 公共参数：http://d7safe.mygjp.com.cn:8020/web/#/erpapi/299
- 商品：http://d7safe.mygjp.com.cn:8020/web/#/erpapi/409
- 仓库：http://d7safe.mygjp.com.cn:8020/web/#/erpapi/399
- 库存：http://d7safe.mygjp.com.cn:8020/web/#/erpapi/400
