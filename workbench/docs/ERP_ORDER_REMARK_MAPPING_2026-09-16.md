# 订单备注：奇门结构及同单跨接口核验

核验时间：2026-09-16 21:22。只读，未改变 ERP、5088、5191 或 5192 的业务数据与服务。

## 当前结论

已找到此前“原备注 137 字符、奇门 remark 为 null”的同一订单备注：`erp.ewms.stockoutbill.list` 返回的 `orders[].comment` 为 137 字符，与本地销售源备注将普通空格替换为下划线后的结果完全一致。同一条 WMS 数据的 `number`、`tradeid` 都匹配该源订单号，商品编码也匹配。

备注仍在可读数据中。尚不能断言奇门接口不支持备注，也不能保证换一个参数就能从奇门取出它。事实是同一订单的 WMS 响应带有系统备注，奇门响应没有带出对应文本。字段映射、业务数据来源、接口配置/权限差异等仍需证据，未认定根因。

## 1781 页结构

[奇门查询订单官方文档](http://d7safe.mygjp.com.cn:8020/web/#/erpapi/1781) 简述查询对象是发货单。“订单详情”是通用响应模型名称，并不承诺完整返回原销售订单。

```text
请求
  顶层：pageNo / pageSize 等通用奇门参数
  extendProps：ExtendPropsRequest 的 JSON 字符串
    profile_id / trade_id / query_time_type / start_time / end_time / bill_status

响应 trade_orders[]
  trade_order                  表头
    extendProps                表头扩展 JSON
      details[]                实测还有商品扩展明细
  order_info[]                 商品行，与 trade_order 并列
    remark                     文档定义的行备注
    extendProps                商品扩展 JSON
  promotion_details[]          促销信息
```

实测集合直接返回数组，文档示例还有同名包装对象，解析需兼容两者。请求 `bill_status=5`、返回扩展 `process_state=400` 同为待发货；`trade_status` 是另一套线上交易状态，不能混用。`query_time_type=1` 为发货时间，`2` 为发货单创建时间，都不是交期。`trade_id`、`trade_no`、`raw_no` 与 `vchcode` 不互换，零 ID 不用于关联查询。

## 同一笔 137 字符备注

| 位置 | 实测结果 |
|---|---|
| 本地源表单据备注 | 137 字符，有内容 |
| `trade_orders[0].order_info[0].remark` | null |
| `trade_order` 的备注字段 | 没有对应文本 |
| 解码头部 `extendProps` 后的 `details[]` | 1 行，只有商品、数量、价格、单位等字段，没有备注字段 |
| 解码商品行 `extendProps` | 与头扩展内这条 details 的对象内容完全相等 |
| 递归解码所有可能的 JSON 字符串 | 无源备注原文或空格转下划线文本；已排除只解一层的遗漏 |
| WMS 范围查询的同号同商品 `comment` | 137 字符，与源备注空格转下划线后逐字符一致 |

WMS 按目标订单源订货日期所在日查询，首个 100 条页面找到目标；窗口总数 312，找到后停止，不宣称该日全量完成。

系统备注可能由现行自动化写入。不能据此保证新建、尚未处理或刚修改的订单也有最新内容；下划线不可无条件替换回空格冒充原备注。

## 查询覆盖与异常

第一阶段 48 次只读请求、11 个文档明确的接口。用户进一步要求追查空值后，第二阶段 4 次同单验证（上限 12 次）。原始响应、实际单号、备注原文、客户资料和凭据均未保存到报告。

| 方向 | 结果 |
|---|---|
| 奇门 | 创建/发货时间、状态 1～7、无状态/全状态、店铺、顶层日期、单号查询等均已尝试；已抽样响应的备注为空 |
| 销售出库草稿 | showfullsalebill=true/false，所查范围均为 0 条 |
| 两种 beefun 发货单 | 时间类型、全处理状态、含删除等条件，所查范围为 0 条 |
| WMS 范围查询 | 创建/修改时间均有数据；找到系统备注，随后完成上述同单核验 |
| 管金所销售表头/商品详情 | 返回已过账信息，未补出本次需要的备注/交期 |
| 销售出库列表与明细 | 固定 dlytype=-1；3 单 details[].comment 再次与源备注原文一致 |

不同条件样本重叠，不能简单相加为独立订单覆盖。没有合法取值说明的 tradeArc、tradeStatus、timeType、customerId 等未猜测。交期仍未找到经核对的独立字段。

1. 奇门文档说 trade_id 可逗号分隔多个单号；两个订单各自查询均为 1 条，逗号合并查询为 0 条。不能将批量空响应解释为订单不存在。
2. WMS 精确查询未通过。小写 vchcodes 传 1 个从范围查询取得的大于零 ID，返回 1,109 条，且没有目标 ID。此前其中 986 条能与本地订单对应、985 条系统备注一致，只能证明数据可读，不能证明精确过滤正确或全量完整。可靠定位证据来自范围查询后在内存按同号同商品筛选。

## 接入建议及厂商排查材料

原销售备注、WMS 系统备注、奇门通用备注分开保存来源与采集时间。奇门 null 不覆盖已有有效备注。WMS 范围查询是补充读取候选，正式接入前须验证分页、日期语义、状态、稳定标识、更新延迟与整批对账。原销售交期、采购未到量、最新原备注及物流写入回读仍待核对，本轮不切换生产数据源。

以下材料可交给厂商，尚未发送：

> 同一账套同一订单，奇门单号查询成功，但表头备注及 order_info[].remark 为 null。头部 extendProps.details 与行 extendProps 已完整解码且内容相同，没有备注字段。同号同商品通过 erp.ewms.stockoutbill.list 可取得 137 字符 comment，与源销售备注空格转下划线后完全一致。请确认奇门备注字段实际映射哪个 ERP 字段，普通销售单提交网店发货后是否支持、是否需额外配置/权限，并核实逗号多单查询及 WMS vchcodes 精确查询的契约。

证据：`runtime/wsgjp-connect/order-query-matrix.json`、`runtime/wsgjp-connect/same-order-mapping.json`。
脚本：`tools/wsgjp_connect/audit_query_matrix.py`、`audit_same_order_mapping.py`。
递归解码、未知集合与空集合区分、空/零 ID 拦截已做离线检查。
