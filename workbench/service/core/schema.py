"""Existing Excel field contracts, copied as data constants."""
TASK_HEADERS = [
    "任务ID", "日期", "时间", "部门", "事项", "备注", "已完成", "完成时间", "更新时间",
    "紧急", "重要", "反馈状态", "反馈内容", "反馈时间", "反馈处理时间",
]

FIELDS = {
    "order_no": "订单编号",
    "shipping_id": "发货识别号",
    "buyer": "买家账号",
    "product_name": "商品名称",
    "model": "型号",
    "sku": "商品编号",
    "quantity": "数量",
    "system_note": "系统备注",
    "status": "处理状态",
}

SALES_ORDER_FIELD_ALIASES = {
    "order_no": {"订单编号"},
    "buyer": {"单位名称", "客户名称", "买家账号"},
    "sku": {"商品编号", "商品编码"},
    "product_name": {"商品名称", "线上宝贝名称"},
    "quantity": {"订货数量", "数量", "商品总数量"},
    "summary": {"摘要", "订单摘要"},
    "legacy_order_note": {"备注", "订单备注", "系统备注"},
    "extra_note": {"附加说明"},
    "detail_note": {"明细备注", "行备注"},
}

INVENTORY_FIELD_ALIASES = {
    "sku": {"商品编号", "商品编码", "编号"},
    "product_name": {"商品名称", "品名"},
    "spec": {"规格"},
    "model": {"型号", "型号/材质/表面处理", "等级"},
    "material": {"材质"},
    "stock_quantity": {"库存数量", "库存"},
    "available_stock": {"可销售库存", "可销库存"},
    "auxiliary_unit": {"辅助单位"},
    "auxiliary_quantity": {"辅助数量"},
    "location": {"货位"},
    "retail_price": {"零售价"},
    "preset_price": {"预设售价"},
    "status": {"状态"},
    "product_note": {"商品备注", "备注"},
    "standard_no": {"标准号"},
}

PURCHASE_FIELD_ALIASES = {
    "line_no": {"行号"},
    "document_no": {"单据编号"},
    "document_type": {"单据类型"},
    "product_name": {"商品名称"},
    "short_name": {"商品简名"},
    "sku": {"商品编号", "商品编码"},
    "spec": {"规格"},
    "model": {"型号"},
    "quantity": {"数量"},
    "amount": {"金额"},
    "tax_rate": {"税率(%)", "税率"},
    "tax_amount": {"税额"},
    "tax_price": {"税后单价"},
    "tax_total": {"税后金额"},
    "note": {"备注"},
    "warehouse": {"仓库"},
    "handler": {"经手人"},
    "creator": {"制单人"},
    "department": {"部门"},
    "party": {"往来单位", "客户", "供应商"},
    "summary": {"摘要"},
    "extra_note": {"附加说明"},
    "document_date": {"单据日期", "日期"},
}
