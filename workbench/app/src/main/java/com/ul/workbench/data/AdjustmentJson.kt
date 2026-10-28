package com.ul.workbench.data

import org.json.JSONArray
import org.json.JSONObject

/** The frozen request is shared by the local outbox and the idempotent server request. */
object AdjustmentJson {
    fun line(value: AdjustmentLine) = JSONObject().put("sku", value.sku).put("name", value.name)
        .put("spec", value.spec).put("model", value.model).put("quantity", value.quantity).put("note", value.note)
        .put("profile_id", value.profileId).put("warehouse_id", value.warehouseId).put("product_id", value.productId)
        .put("sku_id", value.skuId).put("unit_id", value.unitId)
    fun line(value: JSONObject) = AdjustmentLine(value.getString("sku"), value.getString("name"),
        value.optString("spec"), value.optString("model"), value.getString("quantity"), value.optString("note"),
        value.optString("profile_id"), value.optString("warehouse_id", "29871293290508390"), value.optString("product_id"),
        value.optString("sku_id"), value.optString("unit_id"))
    fun payload(value: AdjustmentBatch) = JSONObject().put("id", value.id).put("client_id", value.clientId)
        .put("type", value.type.wire).put("items", JSONArray(value.items.map { line(it) }))
    fun batch(value: AdjustmentBatch) = payload(value).put("status", value.status).put("message", value.message)
        .put("document_no", value.documentNo).put("retryable", value.retryable)
        .put("confirmation_pending", value.confirmationPending).put("completion_source", value.completionSource)
        .put("upload_server", value.uploadServer)
    fun batch(value: JSONObject): AdjustmentBatch {
        val items = value.getJSONArray("items")
        return AdjustmentBatch(value.getString("id"), value.getString("client_id"), AdjustmentType.fromWire(value.getString("type")),
            (0 until items.length()).map { line(items.getJSONObject(it)) }, value.getString("status"),
            value.optString("message"), value.optString("document_no"), value.optBoolean("retryable"),
            value.optBoolean("confirmation_pending"), value.optString("completion_source"), value.optString("upload_server"))
    }
    fun encode(value: AdjustmentState): String = JSONObject().put("version", 2)
        .put("drafts", JSONArray(value.drafts.map { JSONObject().put("type", it.type.wire).put("line", line(it.line)) }))
        .put("batches", JSONArray(value.batches.map { batch(it) })).toString()
    fun decode(raw: String): AdjustmentState {
        val value = JSONObject(raw)
        require(value.getInt("version") in 1..2) { "库存清单版本不兼容" }
        val drafts = value.getJSONArray("drafts")
        val batches = value.getJSONArray("batches")
        return AdjustmentState(
            (0 until drafts.length()).map { index -> drafts.getJSONObject(index).let { AdjustmentDraft(AdjustmentType.fromWire(it.getString("type")), line(it.getJSONObject("line"))) } },
            (0 until batches.length()).map { batch(batches.getJSONObject(it)) })
    }
}
