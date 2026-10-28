# 销售网页上传候选配置

`sales_web_enablement.py` 只准备配置：不会上传订单、启用执行器、写入 App
数据库或修改 ERP。它使用私有 `sales_web_host_preflight_v1` 报告中的已核验
模板、账套、制单人、经手人及仓库；一次完整只读 `erp.btype.list` 建立客户
精确映射。重名、停用、身份或状态不明的客户不进入映射。

App 数据库以 `mode=ro` 打开。李瑄、刘锋各须存在唯一启用账号并具有销售
岗位，当前已发布账套须与报告一致。工具不读取员工密码或会话令牌。

```sh
python service/tools/sales_web_enablement.py \
  --report /private/path/host-preflight.json \
  --staff-db /private/path/workbench.sqlite3 \
  --file-validation /private/path/file-validation.json \
  --output-dir /private/path/new-enablement-candidate
```

输出目录必须尚不存在，父目录须已存在且不能经符号链接跳转。生成目录权限
0700、三个 JSON 权限 0600：`host-config.candidate.json`、
`policy.candidate.json`、`review.json`。配置始终 `enabled=false`；没有
测试客户豁免，没有 `trial_authorization`。安装、浏览器目录权限及开启由
部署步骤在备份、审核后另行处理。

`--file-validation` 可省略，此时 `acceptance_complete=false`，审核报告提示
补齐文件验收。明确文件验收只证明样表质量，**不证明已上传或已审核**。
文件验收 JSON 必须私有，并使用以下结构；账套、账号和模板 SHA256 必须与
预检报告完全一致。所有检查由实际文件 QA 后记录，不填写未验收的结论。

```json
{
  "schema": "sales_web_file_validation_v1",
  "profile_id": "来自预检报告",
  "account_id": "来自预检报告的64位小写SHA256",
  "template_sha256": "原模板的64位小写SHA256",
  "table_verified": true,
  "completion_basis": "web_import_confirmation",
  "erp_upload_performed": false,
  "erp_readback_performed": false,
  "validated_xls_path": "/private/path/review-order.xls",
  "validated_xls_sha256": "样表实文件的64位小写SHA256",
  "expected_rows": 2,
  "checks": {
    "duplicate_sku_rows_preserved": true,
    "tax_rounding": true,
    "summary_and_note": true,
    "unit_piece": true,
    "all_lines_present": true
  }
}
```

工具重新读取 XLS，核对完整行数、重复商品行、模板表头、单位支、13% 税率、
三位单价及两位行金额、制单人、经手人、仓库和摘要／附加说明长度。样表通过
后候选配置才会有 `acceptance_complete=true`，仍不会开启执行器。

客户映射保存稳定名称与 ERP 身份。每次首次上传仍实时查询完整客户目录，
核对精确名称、唯一身份、启用状态与绑定摘要；目录变化或 API 不可用时阻断
本次上传。恢复未知结果只读取本地网页确认记录，不查 ERP，不再次上传。
