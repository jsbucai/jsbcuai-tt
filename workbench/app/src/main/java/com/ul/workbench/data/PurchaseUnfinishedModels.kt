package com.ul.workbench.data

import org.json.JSONArray
import org.json.JSONObject

/** Independent browser-export snapshot; never a purchase-receipt or ERP-write model. */
data class PurchaseSnapshotMeta(
    val status: String = "unavailable", val reason: String = "", val snapshotId: String = "",
    val capturedAt: String = "", val queryStart: String = "", val queryEnd: String = "",
    val rowCount: Int = 0, val supplierCount: Int = 0, val orderCount: Int = 0,
    val stale: Boolean = true, val accountScope: String = "", val accountScopeVerified: Boolean = false,
    val unitsVerified: Boolean = false, val coverageComplete: Boolean = false,
    val collectorStatus: String = "", val collectorReason: String = "",
    val publicationStage: String = ""
) {
    val readable get() = status in setOf("ready", "review") && snapshotId.isNotBlank()
    val internalTrial get() = publicationStage == "internal_trial"
    val canRecordReply get() = status == "ready" && !internalTrial && accountScopeVerified && snapshotId.isNotBlank()
}

data class PurchaseSupplier(val code: String, val name: String, val rowCount: Int, val orderCount: Int,
                            val ambiguousRows: Int = 0)
data class PurchaseSuppliers(val meta: PurchaseSnapshotMeta, val items: List<PurchaseSupplier>)
data class PurchaseUnfinishedLine(
    val lineKey: String, val lineNumber: Int, val supplierCode: String, val supplierName: String,
    val orderNo: String, val productCode: String, val productName: String, val warehouse: String,
    val orderDate: String, val arrivalDate: String, val ordered: String, val completed: String,
    val unfinished: String, val unit: String, val ambiguous: Boolean = true,
    val promisedDate: String = "", val promiseNote: String = "", val promiseVersion: Int = 0,
    val priorPromisedDate: String = "", val priorPromiseNote: String = "",
    val promiseRequiresConfirmation: Boolean = false, val promiseSourceSnapshotId: String = ""
)
data class PurchaseUnfinishedPage(val meta: PurchaseSnapshotMeta = PurchaseSnapshotMeta(),
    val items: List<PurchaseUnfinishedLine> = emptyList(), val page: Int = 1, val total: Int = 0,
    val hasMore: Boolean = false)
data class PurchaseReminderGate(val snapshotId: String = "", val reason: String = "采购计量单位、明细身份和历史覆盖尚未核实",
    val sendAllowed: Boolean = false, val draftOnly: Boolean = true,
    val unitsVerified: Boolean = false, val coverageComplete: Boolean = false)
data class PurchasePromiseReview(val snapshotId: String, val accountScope: String, val line: PurchaseUnfinishedLine)
data class PurchasePromiseDraft(
    val snapshotId: String, val accountScope: String, val line: PurchaseUnfinishedLine,
    val promisedDate: String = "", val note: String = "", val frozenRequest: String = "",
    val outcomeUncertain: Boolean = false, val requiresReview: Boolean = false,
    val current: PurchasePromiseReview? = null, val error: String? = null
)
data class PurchaseUnfinishedState(
    val sessionContext: String = "",
    val open: Boolean = false, val loading: Boolean = false, val loadingMore: Boolean = false,
    val saving: Boolean = false, val reviewing: Boolean = false,
    val meta: PurchaseSnapshotMeta = PurchaseSnapshotMeta(), val suppliers: List<PurchaseSupplier> = emptyList(),
    val selectedSupplier: PurchaseSupplier? = null, val page: PurchaseUnfinishedPage = PurchaseUnfinishedPage(),
    val reminder: PurchaseReminderGate = PurchaseReminderGate(), val editor: PurchasePromiseDraft? = null,
    val editorOpen: Boolean = false, val error: String? = null, val message: String? = null
)

object PurchaseUnfinishedJson {
    private fun text(value: JSONObject, key: String) = if (value.isNull(key)) "" else value.optString(key)
    private fun rows(value: JSONObject) = value.optJSONArray("items")?.let { array ->
        (0 until array.length()).mapNotNull(array::optJSONObject)
    }.orEmpty()
    fun meta(value: JSONObject): PurchaseSnapshotMeta {
        val collector = value.optJSONObject("collector") ?: JSONObject()
        return PurchaseSnapshotMeta(status = text(value, "status").ifBlank { "unavailable" },
            reason = text(value, "reason").ifBlank { text(value, "review_reason") },
            snapshotId = text(value, "snapshot_id"), capturedAt = text(value, "captured_at_utc"),
            queryStart = text(value, "query_start"), queryEnd = text(value, "query_end"),
            rowCount = value.optInt("row_count"), supplierCount = value.optInt("supplier_count"), orderCount = value.optInt("order_count"),
            stale = value.optBoolean("stale", true), accountScope = text(value, "account_scope"),
            accountScopeVerified = value.optBoolean("account_scope_verified", false),
            unitsVerified = value.optBoolean("units_verified", false), coverageComplete = value.optBoolean("coverage_complete", false),
            collectorStatus = text(collector, "status"), collectorReason = text(collector, "reason"),
            publicationStage = text(value, "publication_stage"))
    }
    fun suppliers(value: JSONObject) = PurchaseSuppliers(meta(value), rows(value).map { row ->
        PurchaseSupplier(text(row,"supplier_code"), text(row,"supplier_name"), row.optInt("row_count"),
            row.optInt("order_count"), row.optInt("ambiguous_rows"))
    })
    fun line(row: JSONObject) = PurchaseUnfinishedLine(
        text(row,"line_key"), row.optInt("line_number"), text(row,"supplier_code"), text(row,"supplier_name"),
        text(row,"order_no"), text(row,"product_code"), text(row,"product_name"), text(row,"warehouse"),
        text(row,"order_date"), text(row,"arrival_date"), text(row,"ordered_quantity"), text(row,"completed_quantity"),
        text(row,"unfinished_quantity"), text(row,"unit"), row.optBoolean("identity_ambiguous",true),
        text(row,"promised_date"), text(row,"promise_note"), row.optInt("promise_version"),
        text(row,"prior_promised_date"), text(row,"prior_promise_note"), row.optBoolean("promise_requires_confirmation",false),
        text(row,"source_snapshot_id"))
    fun page(value: JSONObject) = PurchaseUnfinishedPage(meta(value), rows(value).map(::line),
        value.optInt("page",1), value.optInt("total"), value.optBoolean("has_more",false))
    fun reminder(value: JSONObject) = PurchaseReminderGate(text(value,"snapshot_id"), text(value,"reason"),
        value.optBoolean("send_allowed",false), value.optBoolean("draft_only",true),
        value.optBoolean("units_verified",false), value.optBoolean("coverage_complete",false))

    private fun lineJson(line: PurchaseUnfinishedLine) = JSONObject().apply {
        put("line_key",line.lineKey); put("line_number",line.lineNumber)
        put("supplier_code",line.supplierCode); put("supplier_name",line.supplierName)
        put("order_no",line.orderNo); put("product_code",line.productCode); put("product_name",line.productName)
        put("warehouse",line.warehouse); put("order_date",line.orderDate); put("arrival_date",line.arrivalDate)
        put("ordered_quantity",line.ordered); put("completed_quantity",line.completed); put("unfinished_quantity",line.unfinished)
        put("unit",line.unit); put("identity_ambiguous",line.ambiguous)
        put("promised_date",line.promisedDate); put("promise_note",line.promiseNote); put("promise_version",line.promiseVersion)
        put("prior_promised_date",line.priorPromisedDate); put("prior_promise_note",line.priorPromiseNote)
        put("promise_requires_confirmation",line.promiseRequiresConfirmation); put("source_snapshot_id",line.promiseSourceSnapshotId)
    }
    fun draftJson(draft: PurchasePromiseDraft) = JSONObject().apply {
        put("snapshot_id",draft.snapshotId); put("account_scope",draft.accountScope); put("line",lineJson(draft.line))
        put("promised_date",draft.promisedDate); put("note",draft.note); put("frozen_request",draft.frozenRequest)
        put("outcome_uncertain",draft.outcomeUncertain); put("requires_review",draft.requiresReview)
    }.toString()
    fun draft(raw: String): PurchasePromiseDraft? = runCatching {
        val value = JSONObject(raw)
        PurchasePromiseDraft(text(value,"snapshot_id"), text(value,"account_scope"), line(value.getJSONObject("line")),
            text(value,"promised_date"),text(value,"note"),text(value,"frozen_request"),
            value.optBoolean("outcome_uncertain") || text(value,"frozen_request").isNotBlank(),
            value.optBoolean("requires_review"))
    }.getOrNull()?.takeIf { it.snapshotId.isNotBlank() && it.accountScope.isNotBlank() && it.line.lineKey.isNotBlank() }
}
