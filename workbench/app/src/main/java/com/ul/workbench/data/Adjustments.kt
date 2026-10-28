package com.ul.workbench.data

import java.math.BigDecimal

const val LAN_ADJUSTMENT_SERVER = "http://192.168.1.5:5191"

enum class AdjustmentType(val wire: String, val label: String) {
    Loss("loss", "报损"), Overflow("overflow", "报溢");
    companion object { fun fromWire(value: String) = entries.first { it.wire == value } }
}

data class AdjustmentLine(
    val sku: String, val name: String, val spec: String = "", val model: String = "",
    val quantity: String, val note: String = "",
    val profileId: String = "", val warehouseId: String = "29871293290508390",
    val productId: String = "", val skuId: String = "", val unitId: String = ""
)

fun AdjustmentLine.identityKey(): String = if (profileId.isNotBlank() && warehouseId.isNotBlank() &&
    productId.isNotBlank() && skuId.isNotBlank() && unitId.isNotBlank())
    listOf(profileId, warehouseId, productId, skuId, unitId).joinToString("|") else sku.uppercase()
data class AdjustmentDraft(val type: AdjustmentType, val line: AdjustmentLine)
/** Acknowledges the local disk commit, independently of when the UI collects state. */
data class AdjustmentSaveResult(val saved: Boolean, val message: String = "")
data class AdjustmentBatch(
    val id: String, val clientId: String, val type: AdjustmentType, val items: List<AdjustmentLine>,
    val status: String = "submitting", val message: String = "等待确认提交结果", val documentNo: String = "",
    val retryable: Boolean = false, val confirmationPending: Boolean = false, val completionSource: String = "",
    val uploadServer: String = ""
) {
    val active get() = confirmationPending || status in setOf("submitting", "queued", "running")
    val statusLabel get() = if (confirmationPending) "正在确认" else when (status) {
        "submitting" -> "确认电脑接收中"; "queued" -> "已提交电脑 · 等待执行"; "running" -> if (message.contains("验证码") || message.contains("等待人工")) "等待人工处理" else "电脑正在执行"
        "legacy_unknown" -> "原通道待核对"
        "succeeded" -> "上传成功"; "failed" -> "未上传"; "rejected" -> "需修改"; else -> "结果待核对"
    }
}
data class AdjustmentState(
    val drafts: List<AdjustmentDraft> = emptyList(), val batches: List<AdjustmentBatch> = emptyList(),
    val syncing: Boolean = false, val message: String? = null, val storageError: Boolean = false
) {
    val count get() = drafts.size + batches.sumOf { it.items.size }
}

fun adjustmentQuantity(value: String): String {
    require(value.trim().matches(Regex("[0-9]+(\\.[0-9]{1,6})?"))) { "请输入大于 0 的数量，最多 6 位小数" }
    val number = BigDecimal(value.trim())
    require(number > BigDecimal.ZERO && number <= BigDecimal("100000000")) { "数量需大于 0，且不超过一亿" }
    return number.stripTrailingZeros().toPlainString()
}
