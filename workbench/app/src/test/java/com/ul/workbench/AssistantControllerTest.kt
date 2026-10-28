package com.ul.workbench

import android.app.Application
import com.ul.workbench.data.*
import com.ul.workbench.ui.*
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.*
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.net.SocketTimeoutException

@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
class AssistantControllerTest {
    @Test fun historicalCandidatesStayReadOnlyAcrossLocalRestoreAndAreNeverSentAsDraftFacts() = runTest {
        val env = Environment(this)
        val first = env.fake.session().getJSONObject("session").getJSONObject("draft").getJSONArray("lines").getJSONObject(0).put("id", "first")
        env.fake.serverLines = JSONArray().put(first).put(JSONObject(first.toString()).put("id", "second"))
        env.fake.lineMetadata = JSONObject().put("source_label", "客户订单.xlsx · 第 4 行").put("source_preview", "客户 M6×40，100 支")
            .put("match_candidates", JSONArray().put(JSONObject().put("product_code", "HISTORY-001").put("product_name", "历史目录螺钉")
                .put("spec", "M6×40").put("model", "8.8级").put("method", "customer_part_exact").put("evidence_count", 3)
                .put("last_order_date", "2026-09-28").put("available", true).put("message", "历史候选，需要核对")))
            .put("match_status", JSONObject().put("status", "ready").put("message", "历史候选，需要核对")
                .put("covered_from", "2026-01-01").put("covered_to", "2026-10-07"))
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.updateSalesInput("对话输入保留")
        controller.actions.beginSalesDraftEdit()
        val initial = controller.state.value.sales.editor!!
        val candidate = initial.lines.first().matchCandidates.single()
        assertTrue(candidate.available); assertEquals(3, candidate.evidenceCount)
        assertEquals("2026-09-28", candidate.lastOrderDate)
        assertEquals("2026-01-01", initial.lines.first().matchStatus!!.coveredFrom)
        controller.actions.updateSalesDraftEdit(initial.copy(note = "原说明及新备注", lines = initial.lines.map {
            if (it.id == "first") it.copy(productCode = candidate.productCode, quantity = "100") else it
        })); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.method == "PUT" || it.path.endsWith("/confirm") })
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        val restored = restarted.state.value.sales.editor!!
        assertEquals(candidate, restored.lines.first().matchCandidates.single())
        assertEquals(initial.lines.first().matchStatus, restored.lines.first().matchStatus)
        assertEquals(initial.lines[1], restored.lines[1]); assertEquals(initial.version, restored.version)
        assertEquals("HISTORY-001", restored.lines.first().productCode); assertEquals("100", restored.lines.first().quantity)
        assertEquals("客户订单.xlsx · 第 4 行", restored.lines.first().sourceLabel)
        assertEquals("客户 M6×40，100 支", restored.lines.first().sourcePreview)
        assertEquals("对话输入保留", restarted.state.value.sales.input)
        restarted.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val saved = JSONObject(env.fake.requests.single { it.method == "PUT" }.body!!)
        val lines = saved.getJSONArray("lines")
        assertEquals("first", lines.getJSONObject(0).getString("id"))
        assertEquals("HISTORY-001", lines.getJSONObject(0).getString("product_code"))
        assertTrue(lines.getJSONObject(0).getBoolean("replace_product_code"))
        assertEquals("UL-001", lines.getJSONObject(1).getString("product_code"))
        assertFalse(lines.getJSONObject(1).has("replace_product_code"))
        for (index in 0 until lines.length()) {
            assertFalse(lines.getJSONObject(index).has("match_candidates"))
            assertFalse(lines.getJSONObject(index).has("match_status"))
            assertFalse(lines.getJSONObject(index).has("method"))
            assertFalse(lines.getJSONObject(index).has("evidence_count"))
        }
        assertFalse(env.fake.requests.any { it.path.endsWith("/confirm") })
    }

    @Test fun historicalCandidateAvailabilityRequiresActualBooleanTrue() {
        val raw = Fake().session().getJSONObject("session")
        raw.getJSONObject("draft").getJSONArray("lines").getJSONObject(0).put("match_candidates", JSONArray()
            .put(JSONObject().put("product_code", "READY").put("available", true))
            .put(JSONObject().put("product_code", "FALSE").put("available", false))
            .put(JSONObject().put("product_code", "UNKNOWN"))
            .put(JSONObject().put("product_code", "MALFORMED").put("available", "true")))
        val draft = AssistantJson.draft(raw)!!
        assertEquals(listOf(true, false, false, false), draft.lines.single().matchCandidates.map { it.available })
        assertEquals(draft.lines.single().matchCandidates, AssistantJson.draft(AssistantJson.draftRecord(draft))!!.lines.single().matchCandidates)
    }

    private fun document(status: String = "ready", unreadyPages: JSONArray = JSONArray()) = JSONArray().put(
        JSONObject().put("id", "document-1").put("name", "客户订单.pdf").put("status", "needs_processing")
            .put("document_processing", JSONObject().put("status", status).put("page_count", 2).put("line_count", 6)
                .put("candidate_count", 2).put("result_hash", "result-hash-1").put("unready_pages", unreadyPages)))

    @Test fun documentResultDoesNotAdoptOnRefreshOrOverwriteDirtyDraftAndUsesFrozenIdentity() = runTest {
        val env = Environment(this); env.fake.documentAttachments = document()
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        val result = controller.state.value.sales.session!!.documentAttachments.single().documentProcessing!!
        assertEquals(2, result.pageCount); assertEquals(6, result.lineCount); assertEquals(2, result.candidateCount)
        assertFalse(env.fake.requests.any { it.path.endsWith("/adopt") })
        controller.actions.beginSalesDraftEdit()
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(customerOrderNo = "本地输入"))
        controller.actions.adoptSalesDocument("document-1", "result-hash-1"); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/adopt") })
        assertEquals("本地输入", controller.state.value.sales.editor!!.customerOrderNo)
        controller.actions.discardSalesDraftEdit(); advanceUntilIdle()
        controller.actions.adoptSalesDocument("document-1", "wrong-result"); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/adopt") })
        controller.actions.adoptSalesDocument("document-1", "result-hash-1"); advanceUntilIdle()
        val request = env.fake.requests.single { it.path.endsWith("/adopt") }
        val body = JSONObject(request.body!!)
        assertEquals("/sales-assistant/sessions/session-1/attachments/document-1/adopt", request.path)
        assertEquals(1, body.getInt("version")); assertEquals("data-1", body.getString("data_version"))
        assertEquals("result-hash-1", body.getString("result_hash")); assertTrue(body.getString("idempotency_key").isNotBlank())
        assertEquals("adopted", controller.state.value.sales.session!!.documentAttachments.single().documentProcessing!!.status)
        assertTrue(controller.state.value.sales.pendingDocumentAdoptionId.isBlank())
    }

    @Test fun newSessionForSameCustomerDoesNotReusePreviousSuccessfulCreationOrConfirmedDraft() = runTest {
        val env = Environment(this); env.fake.distinctCreatedSessions = true
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle()
        controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        val firstId = controller.state.value.sales.session!!.id
        controller.actions.confirmSalesDraft(firstId, 1); advanceUntilIdle()
        assertEquals("confirmed", controller.state.value.sales.draft!!.status)
        controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        val secondId = controller.state.value.sales.session!!.id
        assertNotEquals(firstId, secondId)
        assertEquals("draft", controller.state.value.sales.draft!!.status)
        assertEquals(1, controller.state.value.sales.draft!!.version)
        val creates = env.fake.requests.filter { it.path == "/sales-assistant/sessions" && it.method == "POST" }
        assertEquals(2, creates.size)
        assertNotEquals(JSONObject(creates.first().body!!).getString("idempotency_key"), JSONObject(creates.last().body!!).getString("idempotency_key"))
        assertFalse(env.disk.keys.any { it.endsWith(":new:create") })
    }

    @Test fun unknownDocumentAdoptionPersistsAndOnlyExplicitRetryReplaysSameRequestAfterRestart() = runTest {
        val env = Environment(this); env.fake.documentAttachments = document(); env.fake.dropDocumentReplyOnce = true
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.adoptSalesDocument("document-1", "result-hash-1"); advanceUntilIdle()
        val original = env.fake.requests.single { it.path.endsWith("/adopt") }.body
        assertEquals("document-1", controller.state.value.sales.pendingDocumentAdoptionId)
        controller.actions.refreshSales(); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/adopt") })
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals("document-1", restarted.state.value.sales.pendingDocumentAdoptionId)
        restarted.actions.confirmSalesDraft("session-1", 2); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/confirm") })
        restarted.actions.retryUnknownSalesDocumentAdoption(); advanceUntilIdle()
        assertEquals(listOf(original, original), env.fake.requests.filter { it.path.endsWith("/adopt") }.map { it.body })
        assertEquals(2, env.fake.version)
        assertTrue(restarted.state.value.sales.pendingDocumentAdoptionId.isBlank())
        assertFalse(env.disk.keys.any { it.endsWith(":document") })
    }

    @Test fun failedOrIncompleteDocumentCannotAdoptAndSourcePageLabelsArePreserved() = runTest {
        val env = Environment(this); env.fake.documentAttachments = document("failed", JSONArray().put(2))
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.adoptSalesDocument("document-1", "result-hash-1"); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/adopt") })
        env.fake.documentAttachments = document("ready", JSONArray().put(2)); controller.actions.refreshSales(); advanceUntilIdle()
        controller.actions.adoptSalesDocument("document-1", "result-hash-1"); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/adopt") })
        val raw = env.fake.session().getJSONObject("session")
        raw.getJSONObject("draft").getJSONArray("lines").getJSONObject(0).put("source_ref", JSONObject()
            .put("kind", "document").put("source_id", "document-1").put("page", 2).put("line", 3).put("preview", "原文报价"))
        raw.getJSONObject("draft").put("source_rows", JSONArray().put(JSONObject().put("id", "coverage-2")
            .put("kind", "document_page").put("source_id", "document-1").put("page", 2).put("status", "needs_review")))
        val draft = AssistantJson.draft(raw)!!
        assertEquals("客户订单.pdf · 第 2 页 · 第 3 行", draft.lines.single().sourceLabel)
        assertEquals("document-1", draft.lines.single().sourceAttachmentId)
        assertEquals(draft.lines.single().sourceAttachmentId, AssistantJson.draft(AssistantJson.draftRecord(draft))!!.lines.single().sourceAttachmentId)
        assertFalse(draft.sourceRows.single().canExclude)
        assertEquals(draft.sourceRows, AssistantJson.draft(AssistantJson.draftRecord(draft))!!.sourceRows)
    }

    @Test fun explicitSourceProductReviewPersistsEvidenceAndSendsOnlyConfirmedHash() = runTest {
        val env = Environment(this)
        env.fake.lineMetadata = JSONObject().put("source_review", JSONObject().put("required", true)
            .put("reasons", JSONArray().put("spec_mismatch")).put("confirmed", false).put("evidence_hash", "evidence-1")
            .put("original", JSONObject().put("product_name", "客户螺钉").put("spec", "M6*40").put("grade", "8.8").put("surface", "镀锌").put("standard", "GB/T 70.1"))
            .put("catalog", JSONObject().put("product_name", "目录螺钉").put("spec", "M6*30").put("model", "8.8级热浸锌").put("standard", "GB 70.1")))
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        val editor = controller.state.value.sales.editor!!
        assertFalse(editor.lines.single().sourceMatchConfirmed)
        assertEquals("M6*40", editor.lines.single().sourceReview!!.original.spec)
        assertEquals("GB/T 70.1", editor.lines.single().sourceReview!!.original.standard)
        assertEquals("GB 70.1", editor.lines.single().sourceReview!!.catalog.standard)
        controller.actions.updateSalesDraftEdit(editor.copy(lines = editor.lines.map { it.copy(sourceMatchConfirmed = true) })); advanceUntilIdle()
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertTrue(restarted.state.value.sales.editor!!.lines.single().sourceMatchConfirmed)
        assertEquals(editor.lines.single().sourceReview, restarted.state.value.sales.editor!!.lines.single().sourceReview)
        restarted.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val line = JSONObject(env.fake.requests.last { it.path.endsWith("/draft") }.body!!).getJSONArray("lines").getJSONObject(0)
        assertTrue(line.getBoolean("source_match_confirmed")); assertEquals("evidence-1", line.getString("source_match_evidence_hash"))
        assertFalse(line.has("source_review"))
        assertFalse(line.has("standard"))
    }

    @Test fun changedSourceStandardEvidenceRejectsOldHashAndRetainsInputWithoutRetryOrConfirmation() = runTest {
        val env = Environment(this)
        env.fake.lineMetadata = JSONObject().put("source_review", JSONObject().put("required", true).put("confirmed", false)
            .put("evidence_hash", "original-standard-hash").put("original", JSONObject().put("standard", "GB/T 5782"))
            .put("catalog", JSONObject().put("standard", "GB 5782")))
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        val editor = controller.state.value.sales.editor!!
        controller.actions.updateSalesDraftEdit(editor.copy(note = "用户输入保留", lines = editor.lines.map { it.copy(sourceMatchConfirmed = true) }))
        // The server has newer source/catalog evidence and rejects the previously reviewed hash.
        env.fake.lineMetadata!!.getJSONObject("source_review").put("evidence_hash", "changed-standard-hash")
        env.fake.failureSuffix = "/draft"
        env.fake.failure = ServiceException("source_match_evidence_changed", "原文或目录已变化，请重新核对商品对应", 422)
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val request = JSONObject(env.fake.requests.single { it.method == "PUT" }.body!!).getJSONArray("lines").getJSONObject(0)
        assertEquals("original-standard-hash", request.getString("source_match_evidence_hash"))
        assertTrue(controller.state.value.sales.editorOpen)
        assertEquals("用户输入保留", controller.state.value.sales.editor!!.note)
        assertEquals("GB/T 5782", controller.state.value.sales.editor!!.lines.single().sourceReview!!.original.standard)
        assertEquals("GB 5782", controller.state.value.sales.editor!!.lines.single().sourceReview!!.catalog.standard)
        assertTrue(controller.state.value.sales.error!!.contains("原文或目录已变化"))
        assertFalse(controller.state.value.sales.pendingDraftSave)
        controller.actions.refreshSales(); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/confirm") })
        assertEquals(1, env.fake.requests.count { it.method == "PUT" })
    }

    @Test fun sourceReviewIsClearedWhenCodeChangesWithoutLosingOriginalPreview() = runTest {
        val env = Environment(this)
        env.fake.lineMetadata = JSONObject().put("source_preview", "客户 M6*40 原文")
            .put("source_label", "第 2 页第 3 行").put("source_review", JSONObject().put("required", true)
                .put("confirmed", false).put("evidence_hash", "evidence-1").put("original", JSONObject().put("spec", "M6*40")))
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        val editor = controller.state.value.sales.editor!!
        controller.actions.updateSalesDraftEdit(editor.copy(lines = editor.lines.map { it.copy(productCode = "NEW-CODE", sourceMatchConfirmed = true) }))
        val changed = controller.state.value.sales.editor!!.lines.single()
        assertNull(changed.sourceReview); assertFalse(changed.sourceMatchConfirmed)
        assertEquals("客户 M6*40 原文", changed.sourcePreview); assertEquals("第 2 页第 3 行", changed.sourceLabel)
    }

    @Test fun viewingOriginalValidatesExactBytesAndNeverOpensAfterContextChange() = runTest {
        val env = Environment(this)
        env.fake.documentAttachments = document()
        val bytes = env.fake.originalAttachmentFile.bytes
        val hash = java.security.MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
        env.fake.documentAttachments.getJSONObject(0).put("mime_type", "application/pdf").put("sha256", hash).put("size", bytes.size)
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.viewSalesAttachment("document-1"); advanceUntilIdle()
        assertEquals("session-1" to "document-1", env.fake.downloaded)
        assertEquals(1, env.viewedFiles.size); assertEquals("客户订单.pdf", env.viewedFiles.single().first)
        env.fake.originalAttachmentFile = SalesAttachmentFile(byteArrayOf(9, 8, 7), "application/pdf")
        controller.actions.viewSalesAttachment("document-1"); advanceUntilIdle()
        assertEquals(1, env.viewedFiles.size)
        assertTrue(controller.state.value.sales.error.orEmpty().contains("完整性校验"))
        env.fake.originalAttachmentFile = SalesAttachmentFile(bytes, "application/pdf")
        env.fake.downloadGate = CompletableDeferred()
        controller.actions.viewSalesAttachment("document-1"); runCurrent()
        env.identity = "server-2\nsales-2"; controller.contextChanged()
        env.fake.downloadGate!!.complete(Unit); advanceUntilIdle()
        assertEquals(1, env.viewedFiles.size)
    }

    private fun readyCodex(version: Int = 1) = JSONObject().put("id", "codex-1").put("status", "ready_for_review")
        .put("base_version", version).put("result_hash", "a".repeat(64)).put("can_adopt", true)
        .put("assistant_text", "已按原流程匹配，请核对编码")
        .put("preview", JSONObject().put("customer_order_no", "CODEX-PO").put("delivery_date", "2026-10-20")
            .put("lines", JSONArray().put(JSONObject().put("id", "proposal-1").put("product_code", "UL-002")
                .put("product_name", "建议商品").put("quantity", "8").put("unit_price", "1.13").put("unit", "支"))))

    companion object {
        private fun newProductPlan(status: String = "ready_for_confirmation") = JSONObject().put("id", "plan-1").put("status", status)
            .put("suggestion_id", "suggestion-1").put("job_id", "codex-1").put("source_result_hash", "a".repeat(64))
            .put("plan_hash", "b".repeat(64)).put("product_name", "非标螺栓").put("spec", "M6×35").put("model", "8.8级")
            .put("code", "NEW-001").put("base_unit", "支").put("classification_path", JSONArray().put("非标件").put("螺栓"))
            .put("can_confirm", status == "ready_for_confirmation").put("can_prepare", status in setOf("blocked", "failed"))
            .put("created_code", if (status == "created") "NEW-001" else "")
    }

    private fun codexWithNewProduct() = readyCodex().put("new_product_suggestions", JSONArray().put(
        JSONObject().put("id", "suggestion-1").put("product_name", "非标螺栓").put("spec", "M6×35").put("can_prepare", true)))

    @Test fun codexCapabilityMustBeTrueBooleanAndUnavailableNeverFallsBackToRuleMessages() = runTest {
        val env = Environment(this); env.fake.codexEnabled = "true"
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.updateSalesInput("识别这份订单"); controller.actions.sendSalesMessage(); advanceUntilIdle()
        assertFalse(controller.state.value.sales.codexEnabled)
        assertTrue(controller.state.value.sales.error!!.contains("尚未就绪"))
        assertEquals("识别这份订单", controller.state.value.sales.input)
        assertFalse(env.fake.requests.any { it.method == "POST" })
    }

    @Test fun codexQueuesFrozenVersionAndRawAttachmentsWithoutChangingDraftOrAdoptingResult() = runTest {
        val env = Environment(this); env.fake.documentAttachments = document()
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        val before = controller.state.value.sales.draft!!
        controller.actions.updateSalesInput("按原录单流程整理，交期10月20日"); controller.actions.sendSalesMessage(); advanceUntilIdle()
        val request = JSONObject(env.fake.requests.single { it.path.endsWith("/codex-jobs") }.body!!)
        assertEquals(1, request.getInt("version")); assertEquals("data-1", request.getString("data_version"))
        assertEquals("按原录单流程整理，交期10月20日", request.getString("text"))
        assertFalse(request.has("attachment_ids")) // server freezes all current original attachments
        assertTrue(request.getString("idempotency_key").isNotBlank())
        assertEquals(before.lines, controller.state.value.sales.draft!!.lines)
        assertEquals("queued", controller.state.value.sales.codexJob!!.status)
        assertEquals("", controller.state.value.sales.input)
        controller.actions.sendSalesMessage(); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/codex-jobs") })
        env.fake.codexJob = readyCodex(2); controller.actions.refreshSales(); advanceUntilIdle()
        assertEquals("CODEX-PO", controller.state.value.sales.codexJob!!.preview!!.customerOrderNo)
        assertEquals(before.customerOrderNo, controller.state.value.sales.draft!!.customerOrderNo)
        assertFalse(controller.state.value.sales.codexJob!!.preview!!.canConfirm)
        assertFalse(env.fake.requests.any { it.path.endsWith("/adopt") || it.path.endsWith("/confirm") || it.path.endsWith("/submit") })
    }

    @Test fun codexDraftAdoptionRequiresExplicitMatchingHashVersionAndNoLocalEdit() = runTest {
        val env = Environment(this); env.fake.codexJob = readyCodex()
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.adoptSalesCodexDraft("codex-1", "wrong"); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/adopt") })
        controller.actions.beginSalesDraftEdit()
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(note = "本地编辑不丢"))
        controller.actions.adoptSalesCodexDraft("codex-1", "a".repeat(64)); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/adopt") })
        assertEquals("本地编辑不丢", controller.state.value.sales.editor!!.note)
        controller.actions.discardSalesDraftEdit(); advanceUntilIdle()
        controller.actions.adoptSalesCodexDraft("codex-1", "a".repeat(64)); advanceUntilIdle()
        val request = JSONObject(env.fake.requests.single { it.path.endsWith("/adopt") }.body!!)
        assertEquals(setOf("version", "data_version", "result_hash", "idempotency_key"), request.keys().asSequence().toSet())
        assertEquals(1, request.getInt("version")); assertEquals("a".repeat(64), request.getString("result_hash"))
        assertEquals("CODEX-PO", controller.state.value.sales.draft!!.customerOrderNo)
        assertEquals("draft", controller.state.value.sales.draft!!.status)
        assertEquals("adopted", controller.state.value.sales.codexJob!!.status)
        assertFalse(env.fake.requests.any { it.path.endsWith("/confirm") || it.path.endsWith("/submit") })
    }

    @Test fun codexUnknownAdoptionSurvivesRestartAndOnlyExplicitRecoveryReusesOriginalRequest() = runTest {
        val env = Environment(this); env.fake.codexJob = readyCodex(); env.fake.dropCodexAdoptionReplyOnce = true
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.adoptSalesCodexDraft("codex-1", "a".repeat(64)); advanceUntilIdle()
        val original = env.fake.requests.single { it.path.endsWith("/adopt") }.body
        assertEquals("codex-adopt", controller.state.value.sales.pendingCodexOperation)
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals("codex-adopt", restarted.state.value.sales.pendingCodexOperation)
        restarted.actions.confirmSalesDraft("session-1", 2); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/confirm") })
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/adopt") })
        restarted.actions.retryUnknownSalesCodexOperation(); advanceUntilIdle()
        assertEquals(listOf(original, original), env.fake.requests.filter { it.path.endsWith("/adopt") }.map { it.body })
        assertEquals(2, env.fake.version); assertEquals("", restarted.state.value.sales.pendingCodexOperation)
        assertFalse(env.disk.keys.any { it.endsWith(":codex-adopt") })
    }

    @Test fun codexUnknownQueueRecoveryKeepsNewInputAndNeverAutomaticallyRerunsAfterRestart() = runTest {
        val env = Environment(this); env.fake.dropCodexReplyOnce = true
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.updateSalesInput("原订单文字"); controller.actions.sendSalesMessage(); advanceUntilIdle()
        val original = env.fake.requests.single { it.path.endsWith("/codex-jobs") }.body
        controller.actions.updateSalesInput("后续新增说明"); advanceUntilIdle()
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals("codex", restarted.state.value.sales.pendingCodexOperation)
        assertEquals("后续新增说明", restarted.state.value.sales.input)
        restarted.actions.sendSalesMessage(); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/codex-jobs") })
        restarted.actions.retryUnknownSalesCodexOperation(); advanceUntilIdle()
        assertEquals(listOf(original, original), env.fake.requests.filter { it.path.endsWith("/codex-jobs") }.map { it.body })
        assertEquals(2, env.fake.version); assertEquals("", restarted.state.value.sales.pendingCodexOperation)
        assertEquals("后续新增说明", restarted.state.value.sales.input)
    }

    @Test fun uncertainOrExistingOrderNumberLocksCodexRunAndAdoption() = runTest {
        val env = Environment(this); env.fake.codexJob = readyCodex(); env.fake.status = "submission_unknown"
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.updateSalesInput("换个编码"); controller.actions.sendSalesMessage()
        controller.actions.adoptSalesCodexDraft("codex-1", "a".repeat(64)); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.method == "POST" })
        assertEquals("换个编码", controller.state.value.sales.input)
        assertTrue(controller.state.value.sales.error!!.contains("不能重新分析"))
    }

    @Test fun codexProcessDeathBeforeUnknownMarkerStillRestoresOriginalDurableRequest() = runTest {
        val env = Environment(this)
        val original = JSONObject().put("version", 1).put("data_version", "data-1").put("text", "进程退出前资料")
            .put("idempotency_key", "durable-before-process-death")
        env.disk["sales:${env.identity}:session-1:codex"] = original.toString()
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals("codex", restarted.state.value.sales.pendingCodexOperation)
        assertFalse(env.fake.requests.any { it.method == "POST" })
        restarted.actions.retryUnknownSalesCodexOperation(); advanceUntilIdle()
        assertTrue(AssistantJson.sameObject(original, JSONObject(env.fake.requests.single { it.path.endsWith("/codex-jobs") }.body!!)))
        assertEquals("", restarted.state.value.sales.pendingCodexOperation)
    }

    @Test fun codexCapabilityAndPreviewNeverGrantConfirmationOrAdoptionFromMalformedOrStaleFlags() {
        val raw = Fake().session().getJSONObject("session")
        raw.put("codex_job", readyCodex().put("can_adopt", "true"))
        assertFalse(AssistantJson.codexJob(raw)!!.canAdopt)
        raw.getJSONObject("codex_job").put("can_adopt", true).put("base_version", 2)
        assertFalse(AssistantJson.codexJob(raw)!!.canAdopt)
        val preview = AssistantJson.codexJob(raw)!!.preview!!
        assertFalse(preview.canConfirm); assertFalse(preview.canSubmit)
        assertEquals("codex_preview", preview.status)
    }

    @Test fun codexPreviewPreservesOriginalSourceCatalogAndCustomerLineWithoutGrantingDraftFacts() {
        val job = readyCodex()
        val line = job.getJSONObject("preview").getJSONArray("lines").getJSONObject(0)
        line.put("original_name", "客户原品名").put("original_spec", "M6×35").put("standard", "GB70.1")
            .put("grade", "10.9").put("surface", "达克罗").put("source_quote", "客户原文")
            .put("line_no", "客户第4行").put("project_no", "P1").put("drawing_no", "D1").put("original_material", "40Cr")
            .put("catalog_product", JSONObject().put("product_name", "目录商品").put("spec", "M6×35")
                .put("model", "标准件").put("standard", "GB/T70.1").put("grade", "10.9").put("surface", "达克罗").put("area", "中国"))
        val raw = Fake().session().getJSONObject("session").put("codex_job", job)
        val preview = AssistantJson.codexJob(raw)!!.preview!!
        val parsed = preview.lines.single()
        assertEquals("客户原品名", parsed.previewOriginal!!.productName)
        assertEquals("GB70.1", parsed.previewOriginal.standard)
        assertEquals("目录商品", parsed.previewCatalog!!.productName)
        assertEquals("标准件", parsed.previewCatalog.model)
        assertEquals("客户原文", parsed.sourcePreview); assertEquals("客户第4行", parsed.customerLineNo)
        assertEquals("P1", parsed.projectNo); assertEquals("D1", parsed.drawingNo)
        assertEquals("40Cr", parsed.originalMaterial); assertEquals("中国", parsed.catalogArea)
        assertFalse(parsed.sourceMatchConfirmed); assertFalse(parsed.summaryMetadataConfirmed)
        val recovered = AssistantJson.draft(AssistantJson.draftRecord(preview))!!
        assertEquals(preview.lines, recovered.lines)
        assertFalse(recovered.canConfirm); assertFalse(recovered.canSubmit)
    }

    @Test fun currentCodexSuggestionMustBeAdoptedBeforeOldCompleteDraftCanConfirmButExplicitNewVersionCanProceed() = runTest {
        val env = Environment(this); env.fake.codexJob = readyCodex()
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertTrue(controller.state.value.sales.draft!!.canConfirm)
        controller.actions.confirmSalesDraft("session-1", 1); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/confirm") })
        env.fake.version = 2; env.fake.codexJob!!.put("status", "stale")
        controller.actions.refreshSales(); advanceUntilIdle()
        controller.actions.confirmSalesDraft("session-1", 2); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/confirm") })
    }

    @Test fun codexProgressPollIsReadOnlyPreservesLocalEditorAndStopsWhenPanelCloses() = runTest {
        val env = Environment(this); env.fake.codexJob = readyCodex().put("status", "running").put("can_adopt", false)
        val controller = env.controller(pollIntervalMs = 5_000)
        controller.actions.openSales(); runCurrent(); controller.actions.selectSalesSession("session-1"); runCurrent()
        controller.actions.beginSalesDraftEdit()
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(note = "处理中编辑也保留")); runCurrent()
        env.fake.codexJob = readyCodex()
        advanceTimeBy(5_000); runCurrent()
        assertEquals("ready_for_review", controller.state.value.sales.codexJob!!.status)
        assertEquals("处理中编辑也保留", controller.state.value.sales.editor!!.note)
        assertTrue(controller.state.value.sales.hasLocalDraftEdit)
        assertFalse(env.fake.requests.any { it.method == "POST" })
        controller.actions.close(); advanceUntilIdle()
        val reads = env.fake.requests.size; advanceTimeBy(30_000); runCurrent()
        assertEquals(reads, env.fake.requests.size)
    }

    @Test fun salesPreparesOneNewProductWithDurableReferenceOnlyAndCannotBypassAdminConfirmation() = runTest {
        val env = Environment(this); env.fake.codexJob = codexWithNewProduct()
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertFalse(controller.state.value.sales.codexJob!!.canAdopt)
        controller.actions.updateSalesNewProductReference("codex-1:suggestion-1", "UL-REFERENCE"); advanceUntilIdle()
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals("UL-REFERENCE", restarted.state.value.sales.newProductReferences["codex-1:suggestion-1"])
        restarted.actions.prepareSalesNewProductSuggestion("codex-1", "a".repeat(64), "suggestion-1"); advanceUntilIdle()
        val request = JSONObject(env.fake.requests.single { it.path.endsWith("/new-products/prepare") }.body!!)
        assertEquals("UL-REFERENCE", request.getString("reference_code")); assertEquals(1, request.getInt("version"))
        assertEquals("data-1", request.getString("data_version")); assertEquals("suggestion-1", request.getString("suggestion_id"))
        assertEquals("a".repeat(64), request.getString("result_hash"))
        assertEquals("preparing", restarted.state.value.sales.newProductPlans.single().status)
        restarted.actions.prepareSalesNewProductSuggestion("codex-1", "a".repeat(64), "suggestion-1"); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/new-products/prepare") })
        env.fake.newProductPlans = JSONArray().put(newProductPlan()); restarted.actions.refreshSales(); advanceUntilIdle()
        restarted.actions.confirmSalesNewProduct("plan-1", "b".repeat(64)); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/new-products/plan-1/confirm") })
        assertTrue(restarted.state.value.sales.error!!.contains("只有管理员"))
    }

    @Test fun newProductPrepareTimeoutRestoresOriginalHashReferenceAndKeyWithoutAutomaticReplay() = runTest {
        val env = Environment(this); env.fake.codexJob = codexWithNewProduct(); env.fake.dropNewProductPrepareReplyOnce = true
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.updateSalesNewProductReference("codex-1:suggestion-1", "FIRST-REF")
        controller.actions.prepareSalesNewProductSuggestion("codex-1", "a".repeat(64), "suggestion-1"); advanceUntilIdle()
        val original = env.fake.requests.single { it.path.endsWith("/new-products/prepare") }.body
        controller.actions.updateSalesNewProductReference("codex-1:suggestion-1", "NEW-INPUT"); advanceUntilIdle()
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals("new-product-prepare", restarted.state.value.sales.pendingNewProductOperation)
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/new-products/prepare") })
        restarted.actions.retryUnknownSalesNewProductOperation(); advanceUntilIdle()
        assertEquals(listOf(original, original), env.fake.requests.filter { it.path.endsWith("/new-products/prepare") }.map { it.body })
        assertEquals("", restarted.state.value.sales.pendingNewProductOperation)
        assertEquals("NEW-INPUT", restarted.state.value.sales.newProductReferences["codex-1:suggestion-1"])
    }

    @Test fun onlyAdminWithCurrentPlanHashCanConfirmExactlyOneProductAndQueuedIsNotCreated() = runTest {
        val env = Environment(this); env.currentUser = env.currentUser.copy(isAdmin = true)
        env.fake.newProductPlans = JSONArray().put(newProductPlan())
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.confirmSalesNewProduct("plan-1", "wrong"); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/new-products/plan-1/confirm") })
        controller.actions.confirmSalesNewProduct("plan-1", "b".repeat(64)); advanceUntilIdle()
        val request = JSONObject(env.fake.requests.single { it.path.endsWith("/new-products/plan-1/confirm") }.body!!)
        assertEquals(setOf("version", "data_version", "plan_hash", "idempotency_key"), request.keys().asSequence().toSet())
        assertEquals("b".repeat(64), request.getString("plan_hash"))
        assertEquals("queued", controller.state.value.sales.newProductPlans.single().status)
        assertFalse(controller.state.value.sales.newProductPlans.single().created)
        controller.actions.confirmSalesNewProduct("plan-1", "b".repeat(64)); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/new-products/plan-1/confirm") })
        assertFalse(env.fake.requests.any { it.path.endsWith("/submit") })
    }

    @Test fun unknownAdminConfirmationUsesOriginalRequestAfterRestartAndLiveRoleRevocationKeepsItLocked() = runTest {
        val env = Environment(this); env.currentUser = env.currentUser.copy(isAdmin = true)
        env.fake.newProductPlans = JSONArray().put(newProductPlan()); env.fake.dropNewProductConfirmReplyOnce = true
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.confirmSalesNewProduct("plan-1", "b".repeat(64)); advanceUntilIdle()
        val original = env.fake.requests.single { it.path.endsWith("/new-products/plan-1/confirm") }.body
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals("new-product-confirm", restarted.state.value.sales.pendingNewProductOperation)
        env.currentUser = env.currentUser.copy(isAdmin = false)
        restarted.actions.retryUnknownSalesNewProductOperation(); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/new-products/plan-1/confirm") })
        assertEquals("new-product-confirm", restarted.state.value.sales.pendingNewProductOperation)
        env.currentUser = env.currentUser.copy(isAdmin = true)
        restarted.actions.retryUnknownSalesNewProductOperation(); advanceUntilIdle()
        assertEquals(listOf(original, original), env.fake.requests.filter { it.path.endsWith("/new-products/plan-1/confirm") }.map { it.body })
        assertEquals("", restarted.state.value.sales.pendingNewProductOperation)
        assertEquals("queued", restarted.state.value.sales.newProductPlans.single().status)
    }

    @Test fun newProductCannotConfirmWhenUnknownOrCapabilityMalformedAndCreatedNeedsMatchingActualCode() {
        val session = Fake().session().getJSONObject("session")
        val raw = newProductPlan().put("can_confirm", "true")
        session.put("new_product_plans", JSONArray().put(raw))
        assertFalse(AssistantJson.newProductPlans(session).single().canConfirm)
        raw.put("status", "unknown").put("can_confirm", true)
        assertFalse(AssistantJson.newProductPlans(session).single().canConfirm)
        raw.put("status", "created").put("created_code", "OTHER-CODE")
        assertFalse(AssistantJson.newProductPlans(session).single().created)
        raw.put("created_code", "NEW-001")
        assertTrue(AssistantJson.newProductPlans(session).single().created)
        assertEquals("非标件 / 螺栓", AssistantJson.newProductPlans(session).single().classificationPath)
    }

    @Test fun blockedNewProductCanBePreparedWithNewReferenceButUnknownCanNeverStartAnotherPlan() = runTest {
        val env = Environment(this); env.fake.codexJob = codexWithNewProduct(); env.fake.newProductPlans = JSONArray().put(newProductPlan("blocked"))
        val controller = env.controller(); controller.actions.openSales(); advanceUntilIdle()
        controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.updateSalesNewProductReference("codex-1:suggestion-1", "MORE-SPECIFIC-REF")
        controller.actions.prepareSalesNewProductSuggestion("codex-1", "a".repeat(64), "suggestion-1"); advanceUntilIdle()
        assertEquals("MORE-SPECIFIC-REF", JSONObject(env.fake.requests.single { it.path.endsWith("/new-products/prepare") }.body!!).getString("reference_code"))
        env.fake.newProductPlans = JSONArray().put(newProductPlan("unknown")); controller.actions.refreshSales(); advanceUntilIdle()
        controller.actions.prepareSalesNewProductSuggestion("codex-1", "a".repeat(64), "suggestion-1"); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/new-products/prepare") })
    }

    @Test fun newProductOnlyPollsWhileActiveAndActualCreatedCodeDoesNotAutomaticallyAnalyseAdoptOrSubmit() = runTest {
        val env = Environment(this); env.fake.newProductPlans = JSONArray().put(newProductPlan("creating"))
        val controller = env.controller(pollIntervalMs = 5_000)
        controller.actions.openSales(); runCurrent(); controller.actions.selectSalesSession("session-1"); runCurrent()
        env.fake.newProductPlans = JSONArray().put(newProductPlan("created"))
        advanceTimeBy(5_000); runCurrent()
        assertTrue(controller.state.value.sales.newProductPlans.single().created)
        assertEquals("NEW-001", controller.state.value.sales.newProductPlans.single().createdCode)
        assertFalse(env.fake.requests.any { it.method == "POST" })
        controller.actions.close(); advanceUntilIdle()
    }

    private class Fake : WorkbenchRepository by HttpWorkbenchRepository(baseUrl = { "http://127.0.0.1" }) {
        data class Request(val path: String, val body: String?, val method: String)
        val requests = mutableListOf<Request>()
        var failure: Exception? = null
        var failureSuffix = "/enroll"
        var gate: CompletableDeferred<Unit>? = null
        var version = 1
        var status = "draft"
        var submitAvailable = false
        var sourceVersion = "source-1"
        var enrolled = false
        var purchaseState = "todo"
        var uploaded = 0
        val uploadKeys = mutableListOf<String>()
        val uploadVersions = mutableListOf<Int>()
        var lineMetadata: JSONObject? = null
        var extraServerLine = false
        var dropDraftResponseOnce = false
        val acceptedDraftKeys = mutableSetOf<String>()
        val acceptedDraftResults = mutableMapOf<String, JSONObject>()
        var materializeDraft = false
        var serverLines: JSONArray? = null
        var serverSourceRows: JSONArray? = null
        var serverCustomerOrderNo = "PO-1"
        var serverNote = "原说明"
        var generatedLineNumber = 0
        var distinctCreatedSessions = false
        var createdSessionCounter = 0
        var sessionId = "session-1"
        var originalAttachmentFile = SalesAttachmentFile(byteArrayOf(1, 2, 3), "application/pdf")
        var downloadGate: CompletableDeferred<Unit>? = null
        var downloaded: Pair<String, String>? = null
        var documentAttachments = JSONArray()
        var codexEnabled: Any = true
        var codexJob: JSONObject? = null
        var newProductPlans = JSONArray()
        var dropNewProductPrepareReplyOnce = false
        var dropNewProductConfirmReplyOnce = false
        val newProductPrepareResults = mutableMapOf<String, JSONObject>()
        val newProductConfirmResults = mutableMapOf<String, JSONObject>()
        var dropCodexReplyOnce = false
        var dropCodexAdoptionReplyOnce = false
        val codexResults = mutableMapOf<String, JSONObject>()
        val codexAdoptionResults = mutableMapOf<String, JSONObject>()
        var dropDocumentReplyOnce = false
        val documentResults = mutableMapOf<String, JSONObject>()
        fun session() = JSONObject().put("ok", true).put("session", JSONObject().put("id", sessionId)
            .put("profile_id", "profile-1").put("data_version", "data-1").put("version", version).put("status", status).put("customer_name", "青岛烁然")
            .put("confirmed_version", if (status == "confirmed") version else 0)
            .put("submit_available", submitAvailable).put("messages", JSONArray()).put("attachments", JSONArray(documentAttachments.toString()))
            .put("draft", JSONObject().put("customer_name", "青岛烁然").put("customer_order_no", serverCustomerOrderNo)
                .put("delivery_date", "2026-10-20").put("note", serverNote).put("issues", JSONArray()).put("lines", JSONArray().put(
                    JSONObject().put("product_code", "UL-001").put("product_name", "内六角螺钉")
                        .put("resolved_identity", JSONObject().put("profile_id", "profile-1").put("sku_id", "sku-7"))
                        .put("quantity", "2").put("unit_price", "0.8").put("unit", "个").put("warehouse", "08390")
                        .put("delivery_date", "2026-10-20").put("issues", JSONArray()))))).also { root ->
            val draft = root.getJSONObject("session").getJSONObject("draft")
            root.getJSONObject("session").put("codex_enabled", codexEnabled)
            root.getJSONObject("session").put("new_product_plans", JSONArray(newProductPlans.toString()))
            codexJob?.let { root.getJSONObject("session").put("codex_job", JSONObject(it.toString())) }
            serverLines?.let { draft.put("lines", JSONArray(it.toString())) }
            serverSourceRows?.let { draft.put("source_rows", JSONArray(it.toString())) }
            val lines = draft.getJSONArray("lines")
            lineMetadata?.let { metadata -> metadata.keys().forEach { key -> lines.getJSONObject(0).put(key, metadata.get(key)) } }
            if (extraServerLine) lines.put(JSONObject(lines.getJSONObject(0).toString()).put("id", "new-server-line"))
        }
        override suspend fun assistantRequest(path: String, body: JSONObject?, method: String): JSONObject {
            requests += Request(path, body?.toString(), method)
            gate?.await()
            if (path.endsWith(failureSuffix)) failure?.let { throw it }
            return when {
                path.startsWith("/assistant/web-operations") -> JSONObject().put("items", JSONArray())
                path.startsWith("/sales-assistant/customers?") -> JSONObject().put("total", 2)
                    .put("page", 1).put("has_more", false).put("items", JSONArray()
                        .put(JSONObject().put("customer_name", "青岛烁然科技有限公司")
                            .put("owner_id", "sales-1").put("owner_name", "张销售"))
                        .put(JSONObject().put("customer_name", "青岛烁然贸易有限公司")
                            .put("owner_id", "sales-1").put("owner_name", "张销售")))
                path == "/assistant/purchase/plan" -> JSONObject().put("source_version", sourceVersion)
                    .put("whitelist_version", "whitelist-1").put("generated_at", "2026-10-06 16:00")
                    .put("items", JSONArray().put(JSONObject().put("id", "purchase:1").put("version", version)
                        .put("product_code", "UL-001").put("product_name", "内六角螺钉").put("quantity", "20")
                        .put("unit", "个").put("warehouse", "08390").put("platform", "test")
                        .put("price_limit", "1.00").put("eligible", true).put("can_enroll", !enrolled)
                        .put("state", if (enrolled) "in_progress" else purchaseState).put("plan_id", if (enrolled) "plan-1" else "")
                        .put("job", if (enrolled) JSONObject().put("id", "job-1").put("status", "waiting_configuration") else JSONObject.NULL)))
                path.endsWith("/enroll") -> { enrolled = true; JSONObject().put("plan_id", "plan-1")
                    .put("job", JSONObject().put("id", "job-1").put("status", "waiting_configuration").put("detail", "待平台实时核验")) }
                path == "/sales-assistant/sessions" && method == "GET" -> JSONObject().put("items", JSONArray().put(session().getJSONObject("session")))
                path == "/sales-assistant/sessions" && method == "POST" && distinctCreatedSessions -> {
                    sessionId = "created-session-${++createdSessionCounter}"; version = 1; status = "draft"; session()
                }
                path.endsWith("/new-products/prepare") -> {
                    val key = body!!.getString("idempotency_key")
                    newProductPrepareResults[key]?.let { return JSONObject(it.toString()).put("replayed", true) }
                    if (newProductPlans.length() == 0) newProductPlans.put(newProductPlan("preparing"))
                    newProductPlans.getJSONObject(0).put("status", "preparing")
                        .put("suggestion_id", body.optString("suggestion_id")).put("job_id", body.optString("job_id"))
                        .put("source_result_hash", body.optString("result_hash"))
                    val result = session().put("plan_id", "plan-1")
                    newProductPrepareResults[key] = JSONObject(result.toString())
                    if (dropNewProductPrepareReplyOnce) { dropNewProductPrepareReplyOnce = false; throw SocketTimeoutException("整理已排队，响应丢失") }
                    result
                }
                path.contains("/new-products/") && path.endsWith("/confirm") -> {
                    val key = body!!.getString("idempotency_key")
                    newProductConfirmResults[key]?.let { return JSONObject(it.toString()).put("replayed", true) }
                    newProductPlans.getJSONObject(0).put("status", "queued").put("can_confirm", false)
                    val result = session().put("plan_id", "plan-1")
                    newProductConfirmResults[key] = JSONObject(result.toString())
                    if (dropNewProductConfirmReplyOnce) { dropNewProductConfirmReplyOnce = false; throw SocketTimeoutException("确认已排队，响应丢失") }
                    result
                }
                path.endsWith("/confirm") -> { version++; status = "confirmed"; session() }
                path.endsWith("/codex-jobs") && method == "POST" -> {
                    val key = body!!.getString("idempotency_key")
                    codexResults[key]?.let { return JSONObject(it.toString()).put("replayed", true) }
                    version++; status = "draft"
                    codexJob = JSONObject().put("id", "codex-1").put("status", "queued").put("base_version", version)
                        .put("can_adopt", false)
                    val result = session(); codexResults[key] = JSONObject(result.toString())
                    if (dropCodexReplyOnce) { dropCodexReplyOnce = false; throw SocketTimeoutException("Codex 已排队，响应丢失") }
                    result
                }
                path.contains("/codex-jobs/") && path.endsWith("/adopt") -> {
                    val key = body!!.getString("idempotency_key")
                    codexAdoptionResults[key]?.let { return JSONObject(it.toString()).put("replayed", true) }
                    version++; status = "draft"
                    codexJob!!.put("status", "adopted").put("can_adopt", false).put("adopted_version", version)
                    codexJob!!.optJSONObject("preview")?.let { serverCustomerOrderNo = it.optString("customer_order_no") }
                    val result = session(); codexAdoptionResults[key] = JSONObject(result.toString())
                    if (dropCodexAdoptionReplyOnce) { dropCodexAdoptionReplyOnce = false; throw SocketTimeoutException("Codex 草稿已采用，响应丢失") }
                    result
                }
                path.endsWith("/adopt") -> {
                    val key = body!!.getString("idempotency_key")
                    documentResults[key]?.let { return JSONObject(it.toString()).put("replayed", true) }
                    version++
                    documentAttachments.getJSONObject(0).getJSONObject("document_processing")
                        .put("status", "adopted").put("adopted_version", version)
                    val result = session(); documentResults[key] = JSONObject(result.toString())
                    if (dropDocumentReplyOnce) { dropDocumentReplyOnce = false; throw SocketTimeoutException("加入已完成，但响应丢失") }
                    result
                }
                path.endsWith("/draft") -> {
                    val idempotencyKey = body?.optString("idempotency_key").orEmpty()
                    if (idempotencyKey !in acceptedDraftKeys) {
                        acceptedDraftKeys += idempotencyKey
                        version++; status = "draft"
                        if (materializeDraft && body != null) {
                            serverCustomerOrderNo = body.optString("customer_order_no")
                            serverNote = body.optString("note")
                            val requested = body.getJSONArray("lines")
                            serverLines = JSONArray().also { result ->
                                for (index in 0 until requested.length()) {
                                    val line = JSONObject(requested.getJSONObject(index).toString())
                                    if (line.optBoolean("new_line")) line.put("id", "server-line-${++generatedLineNumber}")
                                    line.remove("new_line"); line.remove("replace_product_code")
                                    line.put("product_name", "内六角螺钉")
                                    line.put("quoted_unit_price", line.optString("unit_price"))
                                    line.put("issues", JSONArray())
                                    line.optJSONObject("identity")?.let { line.put("resolved_identity", it) }
                                    result.put(line)
                                }
                            }
                            body.optJSONArray("source_row_actions")?.let { actions ->
                                serverSourceRows?.let { reports ->
                                    for (index in 0 until actions.length()) {
                                        val action = actions.getJSONObject(index)
                                        for (rowIndex in 0 until reports.length()) {
                                            val report = reports.getJSONObject(rowIndex)
                                            if (report.optString("id") == action.optString("source_row_id"))
                                                report.put("status", "excluded_by_user")
                                                    .put("exclusion_reason", action.optString("reason"))
                                                    .put("excluded_by", "sales-1")
                                        }
                                    }
                                }
                            }
                        }
                        val committed = session()
                        acceptedDraftResults[idempotencyKey] = JSONObject(committed.toString())
                        if (dropDraftResponseOnce) {
                            dropDraftResponseOnce = false
                            throw SocketTimeoutException("保存已处理，但响应丢失")
                        }
                        committed
                    } else JSONObject(acceptedDraftResults.getValue(idempotencyKey).toString()).put("replayed", true)
                }
                path.endsWith("/codex-jobs") -> { version++; session() }
                else -> session()
            }
        }
        override suspend fun uploadSalesAttachment(sessionId: String, version: Int, key: String, name: String, mimeType: String, bytes: ByteArray): JSONObject {
            uploaded++; uploadKeys += key; uploadVersions += version
            if (failureSuffix == "/attachments") failure?.let { throw it }
            this.version++; return session()
        }
        override suspend fun downloadSalesAttachment(sessionId: String, attachmentId: String): SalesAttachmentFile {
            downloaded = sessionId to attachmentId; downloadGate?.await(); return originalAttachmentFile
        }
    }
    private class Environment(val scope: TestScope, val fake: Fake = Fake(), val disk: MutableMap<String, String> = mutableMapOf()) {
        var identity = "server-1\nsales-1"
        var currentUser = CurrentUser("sales-1", roles = listOf(ROLE_SALES, ROLE_PURCHASE))
        var fileReader: suspend (AssistantLocalAttachment) -> ByteArray = { byteArrayOf(1, 2, 3) }
        val viewedFiles = mutableListOf<Pair<String, SalesAttachmentFile>>()
        fun controller(pollIntervalMs: Long = 0) = AssistantController(fake, scope, { identity }, { currentUser }, { "profile-1" },
            { disk[it] }, { key, value -> if (value == null) disk.remove(key) else disk[key] = value; true },
            { fileReader(it) }, StandardTestDispatcher(scope.testScheduler),
            { name, file, stillCurrent -> if (stillCurrent()) viewedFiles += name to file }, codexPollIntervalMs = pollIntervalMs)
    }

    @Test fun purchaseTimeoutRetryUsesOriginalVersionAndKeyWithoutBlindBackgroundReplay() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openPurchase(); advanceUntilIdle()
        assertEquals("test", controller.state.value.purchase.items.single().platformName)
        env.fake.failure = SocketTimeoutException("超时")
        controller.actions.enrollPurchaseItem("purchase:1", 1)
        controller.actions.enrollPurchaseItem("purchase:1", 1)
        advanceUntilIdle()
        val first = env.fake.requests.single { it.path.endsWith("/enroll") }.body
        assertTrue(env.disk.keys.any { it.startsWith("purchase:") })
        env.fake.version = 4; env.fake.sourceVersion = "source-2"
        controller.actions.refreshPurchase(); advanceUntilIdle()
        env.fake.failure = null
        controller.actions.enrollPurchaseItem("purchase:1", 4); advanceUntilIdle()
        assertEquals(first, env.fake.requests.filter { it.path.endsWith("/enroll") }.last().body)
        assertEquals("in_progress", controller.state.value.purchase.items.single().state)
        assertFalse(controller.state.value.purchase.items.single().canEnroll)
        assertTrue(controller.state.value.purchase.notice.orEmpty().contains("付款尚未开启"))
        assertFalse(controller.state.value.purchase.conversationEnabled)
    }

    @Test fun purchaseConflictDoesNotRewriteVersionsOrAutomaticallyRetry() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openPurchase(); advanceUntilIdle()
        env.fake.failure = ServiceException("data_version_changed", "数据已更新", 409)
        controller.actions.enrollPurchaseItem("purchase:1", 1); advanceUntilIdle()
        env.fake.failure = null
        controller.actions.enrollPurchaseItem("purchase:1", 1); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/enroll") })
        assertTrue(controller.state.value.purchase.error.orEmpty().contains("人工核对"))
        controller.actions.refreshPurchase(); advanceUntilIdle()
        controller.actions.enrollPurchaseItem("purchase:1", 1); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/enroll") })
        assertTrue(controller.state.value.purchase.items.single().lastFailure.contains("数据已更新"))
    }

    @Test fun purchaseConflictAllowsOnlyExplicitFreshScopeClickWithNewKeyAndRetainsFailure() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openPurchase(); advanceUntilIdle()
        env.fake.failure = ServiceException("task_version_changed", "任务已更新", 409)
        controller.actions.enrollPurchaseItem("purchase:1", 1); advanceUntilIdle()
        val rejected = JSONObject(env.fake.requests.last().body!!)
        env.fake.failure = null; env.fake.version = 4; env.fake.sourceVersion = "source-2"
        controller.actions.refreshPurchase(); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/enroll") })
        assertTrue(controller.state.value.purchase.items.single().lastFailure.contains("任务已更新"))
        controller.actions.enrollPurchaseItem("purchase:1", 4); advanceUntilIdle()
        val submitted = JSONObject(env.fake.requests.last().body!!)
        assertEquals(4, submitted.getInt("version"))
        assertEquals("source-2", submitted.getString("source_version"))
        assertNotEquals(rejected.getString("idempotency_key"), submitted.getString("idempotency_key"))
        assertFalse(submitted.has("requires_review"))
        assertTrue(controller.state.value.purchase.items.single().hasPlan)
        assertTrue(controller.state.value.purchase.items.single().lastFailure.contains("任务已更新"))
        val restarted = env.controller(); restarted.actions.openPurchase(); advanceUntilIdle()
        assertTrue(restarted.state.value.purchase.items.single().lastFailure.contains("任务已更新"))
        assertTrue(restarted.state.value.purchase.items.single().hasPlan)
    }

    @Test fun conflictOnAnOldTimeoutRetryRequiresAnotherRefreshBeforeReplacingTheRequest() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openPurchase(); advanceUntilIdle()
        env.fake.failure = SocketTimeoutException("超时")
        controller.actions.enrollPurchaseItem("purchase:1", 1); advanceUntilIdle()
        val original = env.fake.requests.last().body
        env.fake.version = 4; env.fake.sourceVersion = "source-2"
        controller.actions.refreshPurchase(); advanceUntilIdle()
        env.fake.failure = ServiceException("task_version_changed", "原请求版本已失效", 409)
        controller.actions.enrollPurchaseItem("purchase:1", 4); advanceUntilIdle()
        assertEquals(original, env.fake.requests.last().body)
        env.fake.failure = null
        controller.actions.enrollPurchaseItem("purchase:1", 4); advanceUntilIdle()
        assertEquals(2, env.fake.requests.count { it.path.endsWith("/enroll") })
        controller.actions.refreshPurchase(); advanceUntilIdle()
        assertEquals(2, env.fake.requests.count { it.path.endsWith("/enroll") })
        controller.actions.enrollPurchaseItem("purchase:1", 4); advanceUntilIdle()
        assertEquals(3, env.fake.requests.count { it.path.endsWith("/enroll") })
        assertEquals(4, JSONObject(env.fake.requests.last().body!!).getInt("version"))
    }

    @Test fun purchaseLegacyWaitingWithoutPlanKeepsItsStateAndDoesNotGainExecutionIdentity() = runTest {
        val env = Environment(this); env.fake.purchaseState = "waiting"
        val controller = env.controller(); controller.actions.openPurchase(); advanceUntilIdle()
        val item = controller.state.value.purchase.items.single()
        assertEquals("waiting", item.state)
        assertFalse(item.hasPlan)
        assertEquals("", item.planId)
        assertNull(item.job)
    }

    @Test fun purchaseWhitelistCountsComeFromBackendAndMissingCountsRemainUnknown() {
        val counts = JSONObject().put("candidate_total", 385).put("verified_total", 0)
            .put("review_total", 385).put("matched_demand_total", 0)
        val payload = JSONObject().put("items", JSONArray()).put("whitelist_counts", counts)
        assertEquals(PurchaseWhitelistCounts(385, 0, 385, 0), AssistantJson.purchase(payload).whitelistCounts)
        counts.remove("review_total")
        assertNull(AssistantJson.purchase(payload).whitelistCounts)
    }

    @Test fun latePlanResponseCannotLeakAcrossServerOrUserChange() = runTest {
        val env = Environment(this); val controller = env.controller()
        env.fake.gate = CompletableDeferred()
        controller.actions.openPurchase(); runCurrent()
        env.identity = "server-2\nsales-2"; env.currentUser = CurrentUser("sales-2", roles = listOf(ROLE_SALES))
        controller.contextChanged()
        env.fake.gate!!.complete(Unit); advanceUntilIdle()
        assertNull(controller.state.value.panel)
        assertTrue(controller.state.value.purchase.items.isEmpty())
        controller.actions.openPurchase(); advanceUntilIdle()
        assertEquals(2, env.fake.requests.size)
    }

    @Test fun confirmedSalesDraftIsDurableButServerCapabilityKeepsWebUploadDisabled() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle()
        controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        assertEquals("profile-1", controller.state.value.sales.session?.profileId)
        val created = JSONObject(env.fake.requests.first { it.method == "POST" }.body!!)
        assertEquals("青岛烁然", created.getString("customer_name"))
        assertEquals("profile-1", created.getString("profile_id"))
        assertTrue(controller.state.value.sales.draft!!.canConfirm)
        controller.actions.confirmSalesDraft("session-1", 1); advanceUntilIdle()
        val draft = controller.state.value.sales.draft!!
        assertEquals(2, draft.confirmedVersion)
        assertFalse(draft.canSubmit)
        controller.actions.submitSalesDraft("session-1", 2); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/submit") })
        assertTrue(controller.state.value.sales.error.orEmpty().contains("网页上传暂不可用"))
    }

    @Test fun customerSearchUsesScopedReadOnlyEndpointAndKeepsFullNames() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle()
        controller.actions.searchSalesCustomers(""); advanceUntilIdle()
        assertEquals(2, controller.state.value.sales.customerChoicesTotal)
        assertEquals("青岛烁然科技有限公司", controller.state.value.sales.customerChoices.first().customerName)
        assertEquals("张销售", controller.state.value.sales.customerChoices.first().ownerName)
        assertTrue(env.fake.requests.any { it.path == "/sales-assistant/customers?profile_id=profile-1&q=&page=1" && it.method == "GET" })
        controller.actions.searchSalesCustomers("青岛")
        controller.actions.searchSalesCustomers("烁然")
        advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.contains("q=%E9%9D%92%E5%B2%9B") })
        assertTrue(env.fake.requests.any { it.path.contains("q=%E7%83%81%E7%84%B6") })
        assertFalse(env.fake.requests.any { it.path.startsWith("/sales-assistant/customers?") && it.method != "GET" })
    }

    @Test fun lateCustomerChoicesCannotCrossAccountContext() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle()
        env.fake.gate = CompletableDeferred()
        controller.actions.searchSalesCustomers("烁然")
        advanceTimeBy(250); runCurrent()
        env.identity = "server-2\nsales-2"
        env.currentUser = CurrentUser("sales-2", roles = listOf(ROLE_SALES))
        controller.contextChanged()
        env.fake.gate!!.complete(Unit); advanceUntilIdle()
        assertNull(controller.state.value.panel)
        assertTrue(controller.state.value.sales.customerChoices.isEmpty())
    }

    @Test fun createOwnerReviewPreservesCustomerAndNeedsFreshListBeforeNewIntent() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle()
        env.fake.failureSuffix = "/sessions"
        env.fake.failure = ServiceException("customer_owner_review", "客户固定销售待指定或核对", 409)
        controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        val rejected = JSONObject(env.fake.requests.last().body!!)
        assertEquals("青岛烁然", rejected.getString("customer_name"))
        assertTrue(controller.state.value.sales.error.orEmpty().contains("客户归属需核对"))
        assertFalse(controller.state.value.sales.error.orEmpty().contains("版本冲突"))
        env.fake.failure = null
        controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path == "/sales-assistant/sessions" && it.method == "POST" })
        controller.actions.refreshSales(); advanceUntilIdle()
        controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        val creates = env.fake.requests.filter { it.path == "/sales-assistant/sessions" && it.method == "POST" }
        assertEquals(2, creates.size)
        assertNotEquals(rejected.getString("idempotency_key"), JSONObject(creates.last().body!!).getString("idempotency_key"))
        assertEquals("session-1", controller.state.value.sales.session?.id)
    }

    @Test fun legacyBlockedCreateRequiresNewReadAndCanThenRecover() = runTest {
        val env = Environment(this); val controller = env.controller()
        val slot = "sales:${env.identity}:new:create"
        env.disk[slot] = JSONObject().put("profile_id", "profile-1").put("customer_name", "青岛烁然")
            .put("idempotency_key", "legacy-create-key").put("requires_review", true).toString()
        controller.actions.openSales(); advanceUntilIdle()
        controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path == "/sales-assistant/sessions" && it.method == "POST" })
        assertTrue(controller.state.value.sales.error.orEmpty().contains("请刷新"))
        controller.actions.refreshSales(); advanceUntilIdle()
        controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        val create = JSONObject(env.fake.requests.last().body!!)
        assertNotEquals("legacy-create-key", create.getString("idempotency_key"))
        assertEquals("青岛烁然", create.getString("customer_name"))
    }

    @Test fun draftEditConflictPreservesFieldsAndOriginalVersionAcrossControllerRestart() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle()
        controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        val editor = controller.state.value.sales.editor!!.let { it.copy(customerOrderNo = "PO-new", dueDate = "2026-11-01",
            lines = it.lines.map { line -> line.copy(quantity = "7", unitPrice = "1.2", taxBasis = "tax_exclusive", dueDate = "2026-11-02") }) }
        controller.actions.updateSalesDraftEdit(editor); advanceUntilIdle()
        env.fake.failureSuffix = "/draft"; env.fake.failure = ServiceException("version_changed", "版本冲突", 409)
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        assertEquals(editor, controller.state.value.sales.editor)
        val body = JSONObject(env.fake.requests.last().body!!)
        assertEquals("PUT", env.fake.requests.last().method)
        assertEquals(1, body.getInt("version"))
        assertEquals("tax_inclusive", body.getString("tax_basis"))
        assertEquals("13", body.getString("tax_rate"))
        assertEquals("2026-11-02", body.getJSONArray("lines").getJSONObject(0).getString("delivery_date"))
        assertEquals("tax_exclusive", body.getJSONArray("lines").getJSONObject(0).getString("tax_basis"))
        assertEquals("13", body.getJSONArray("lines").getJSONObject(0).getString("tax_rate"))
        assertTrue(controller.state.value.sales.editorError.orEmpty().contains("编辑内容已保留"))
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals("PO-new", restarted.state.value.sales.editor?.customerOrderNo)
        assertEquals("7", restarted.state.value.sales.editor?.lines?.single()?.quantity)
        assertEquals("tax_exclusive", restarted.state.value.sales.editor?.lines?.single()?.taxBasis)
        assertEquals(1, restarted.state.value.sales.editor?.version)
    }

    @Test fun salesConflictWithUnchangedVersionNeedsFreshReadBeforeNewKey() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.updateSalesInput("LS3073 1 支")
        env.fake.failureSuffix = "/codex-jobs"; env.fake.failure = ServiceException("sales_draft_version_changed", "草稿已变化", 409)
        controller.actions.sendSalesMessage(); advanceUntilIdle()
        val rejected = JSONObject(env.fake.requests.last().body!!)
        env.fake.failure = null
        controller.actions.sendSalesMessage(); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/codex-jobs") })
        assertEquals("LS3073 1 支", controller.state.value.sales.input)
        assertTrue(controller.state.value.sales.error.orEmpty().contains("请刷新"))
        controller.actions.refreshSales(); advanceUntilIdle()
        assertEquals("LS3073 1 支", controller.state.value.sales.input)
        controller.actions.sendSalesMessage(); advanceUntilIdle()
        val messages = env.fake.requests.filter { it.path.endsWith("/codex-jobs") }
        assertEquals(2, messages.size)
        val reviewed = JSONObject(messages.last().body!!)
        assertEquals(1, reviewed.getInt("version"))
        assertNotEquals(rejected.getString("idempotency_key"), reviewed.getString("idempotency_key"))
        assertEquals("", controller.state.value.sales.input)
    }

    @Test fun staleDraftEditKeepsInputUntilFreshReadAndExplicitVersionAdoption() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(customerOrderNo = "PO-local",
            lines = controller.state.value.sales.editor!!.lines.map { it.copy(quantity = "7") }))
        env.fake.failureSuffix = "/draft"; env.fake.failure = ServiceException("sales_draft_version_changed", "草稿已变化", 409)
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val rejected = JSONObject(env.fake.requests.last().body!!)
        env.fake.failure = null; env.fake.version = 2
        controller.actions.refreshSales(); advanceUntilIdle()
        assertEquals(1, controller.state.value.sales.editor?.version)
        assertEquals("PO-local", controller.state.value.sales.editor?.customerOrderNo)
        assertEquals(2, controller.state.value.sales.draft?.version)
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/draft") })
        controller.actions.adoptLatestSalesDraftVersion()
        assertEquals(2, controller.state.value.sales.editor?.version)
        assertEquals("7", controller.state.value.sales.editor?.lines?.single()?.quantity)
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val saved = JSONObject(env.fake.requests.last().body!!)
        assertEquals(2, saved.getInt("version"))
        assertNotEquals(rejected.getString("idempotency_key"), saved.getString("idempotency_key"))
        assertEquals("PO-local", saved.getString("customer_order_no"))
        assertNull(controller.state.value.sales.editor)
    }

    @Test fun changedServerLineScopeCannotBeOverwrittenByAdoptingItsVersion() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(customerOrderNo = "PO-local"))
        env.fake.version = 2; env.fake.extraServerLine = true
        controller.actions.refreshSales(); advanceUntilIdle()
        controller.actions.adoptLatestSalesDraftVersion()
        assertEquals(1, controller.state.value.sales.editor?.version)
        assertEquals("PO-local", controller.state.value.sales.editor?.customerOrderNo)
        assertEquals(2, controller.state.value.sales.draft?.lines?.size)
        assertTrue(controller.state.value.sales.editorError.orEmpty().contains("明细行或商品已变化"))
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/draft") })
    }

    @Test fun unrelatedEditRetainsDimensionProvenanceWhileExplicitDimensionEditRequiresNewReview() = runTest {
        val env = Environment(this)
        env.fake.lineMetadata = JSONObject().put("summary_dimension", "M6X30")
            .put("summary_dimension_source", "human_confirmed").put("summary_metadata_confirmed", true)
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(customerOrderNo = "PO-new"))
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val unchanged = JSONObject(env.fake.requests.last().body!!).getJSONArray("lines").getJSONObject(0)
        assertEquals("human_confirmed", unchanged.getString("summary_dimension_source"))
        controller.actions.beginSalesDraftEdit()
        val editor = controller.state.value.sales.editor!!
        controller.actions.updateSalesDraftEdit(editor.copy(lines = editor.lines.map {
            it.copy(summaryDimension = "M6X40", summaryDimensionSource = "", summaryMetadataConfirmed = true)
        }))
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val changed = JSONObject(env.fake.requests.last().body!!).getJSONArray("lines").getJSONObject(0)
        assertEquals("M6X40", changed.getString("summary_dimension"))
        assertFalse(changed.has("summary_dimension_source"))
    }

    @Test fun validationRejectionIsNotMisreportedAsVersionConflict() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        env.fake.failureSuffix = "/draft"; env.fake.failure = ServiceException("sales_draft_needs_review", "明细行身份与原草稿不一致", 422)
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        assertTrue(controller.state.value.sales.editorError.orEmpty().contains("明细行身份与原草稿不一致"))
        env.fake.failure = null
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/draft") })
        assertTrue(controller.state.value.sales.editorError.orEmpty().contains("被服务器拒绝"))
        assertTrue(controller.state.value.sales.editorError.orEmpty().contains("明细行身份与原草稿不一致"))
        assertFalse(controller.state.value.sales.editorError.orEmpty().contains("版本冲突"))
        assertNotNull(controller.state.value.sales.editor)
        val rejection = JSONObject(env.disk.entries.single { it.key.endsWith(":session-1:draft") }.value)
        assertEquals("明细行身份与原草稿不一致", rejection.getString("review_message"))
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/draft") })
        env.fake.failure = ServiceException("sales_draft_needs_review", "明细行身份与原草稿不一致", 422)
        restarted.actions.saveSalesDraftEdit(); advanceUntilIdle()
        assertEquals(2, env.fake.requests.count { it.path.endsWith("/draft") })
        assertTrue(restarted.state.value.sales.editorError.orEmpty().contains("明细行身份与原草稿不一致"))
        assertFalse(JSONObject(env.fake.requests.last { it.path.endsWith("/draft") }.body!!).has("review_message"))
    }

    @Test fun editingConfirmedDraftSuccessfullyRequiresFreshConfirmation() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.confirmSalesDraft("session-1", 1); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(customerOrderNo = "changed"))
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        assertNull(controller.state.value.sales.editor)
        assertNull(controller.state.value.sales.draft?.confirmedVersion)
        assertEquals(3, controller.state.value.sales.draft?.version)
        assertTrue(controller.state.value.sales.draft!!.canConfirm)
    }

    @Test fun unrelatedDraftEditsKeepReviewedSkuIdentityAndCodeChangeRequestsNewIdentity() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(customerOrderNo = "PO-new"))
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val original = JSONObject(env.fake.requests.last().body!!).getJSONArray("lines").getJSONObject(0)
        assertEquals("sku-7", original.getJSONObject("identity").getString("sku_id"))
        controller.actions.beginSalesDraftEdit()
        val editor = controller.state.value.sales.editor!!
        controller.actions.updateSalesDraftEdit(editor.copy(lines = editor.lines.map { it.copy(productCode = "UL-002") }))
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val changed = JSONObject(env.fake.requests.last().body!!).getJSONArray("lines").getJSONObject(0)
        assertEquals("UL-002", changed.getString("product_code"))
        assertFalse(changed.has("identity"))
        assertTrue(changed.getBoolean("replace_product_code"))
    }

    @Test fun repeatedSkuDraftEditsKeepDistinctLineIdsAndManualSummaryAcrossRestart() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        val editor = controller.state.value.sales.editor!!
        val first = editor.lines.single().copy(id = "line-a", summaryDimension = "非标 M6×30",
            projectNo = "工程甲", drawingNo = "图-A", grade = "10.9", surface = "镀锌", summaryMetadataConfirmed = true)
        val second = first.copy(id = "line-b", quantity = "3", summaryDimension = "非标 M6×40", drawingNo = "图-B")
        controller.actions.updateSalesDraftEdit(editor.copy(lines = listOf(first, second)))
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        val restored = restarted.state.value.sales.editor!!
        assertEquals(listOf("line-a", "line-b"), restored.lines.map { it.id })
        assertEquals(listOf("非标 M6×30", "非标 M6×40"), restored.lines.map { it.summaryDimension })
        assertTrue(restored.lines.all { it.summaryMetadataConfirmed })
        restarted.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val lines = JSONObject(env.fake.requests.last().body!!).getJSONArray("lines")
        assertEquals("line-a", lines.getJSONObject(0).getString("id"))
        assertEquals("line-b", lines.getJSONObject(1).getString("id"))
        assertTrue(lines.getJSONObject(0).getBoolean("new_line"))
        assertTrue(lines.getJSONObject(1).getBoolean("new_line"))
        assertEquals("图-A", lines.getJSONObject(0).getString("drawing_no"))
        assertEquals("图-B", lines.getJSONObject(1).getString("drawing_no"))
        assertEquals("工程甲", lines.getJSONObject(0).getString("project_no"))
        assertTrue(lines.getJSONObject(1).getBoolean("summary_metadata_confirmed"))
        assertFalse(lines.getJSONObject(0).has("summary_dimension_confirmed_by"))
    }

    @Test fun unknownSalesMessageRetryIsFrozenAndChangedTextCannotReuseThePendingRequest() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.updateSalesInput("UL-001 20")
        env.fake.failureSuffix = "/codex-jobs"; env.fake.failure = SocketTimeoutException("超时")
        controller.actions.sendSalesMessage(); advanceUntilIdle()
        val first = env.fake.requests.last().body
        controller.actions.updateSalesInput("UL-001 30"); controller.actions.sendSalesMessage(); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/codex-jobs") })
        assertEquals("UL-001 30", controller.state.value.sales.input)
        controller.actions.updateSalesInput("UL-001 20"); env.fake.failure = null
        controller.actions.retryUnknownSalesCodexOperation(); advanceUntilIdle()
        assertEquals(first, env.fake.requests.last().body)
        assertEquals("", controller.state.value.sales.input)
    }

    @Test fun oversizeAttachmentNeverUploadsAndDoesNotPretendItIsReady() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        env.fileReader = { ByteArray(10 * 1024 * 1024 + 1) }
        controller.actions.attachSalesFile(AssistantLocalAttachment("content://orders/1", "订单.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        advanceUntilIdle()
        assertEquals(0, env.fake.uploaded)
        assertEquals("failed", controller.state.value.sales.attachments.single().status)
        controller.actions.updateSalesInput("UL-001 20"); controller.actions.sendSalesMessage(); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/codex-jobs") })
        assertEquals("UL-001 20", controller.state.value.sales.input)
    }

    @Test fun accountSwitchWhileReadingFileCannotUploadToNewAccount() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        val fileRead = CompletableDeferred<ByteArray>()
        env.fileReader = { fileRead.await() }
        controller.actions.attachSalesFile(AssistantLocalAttachment("content://orders/1", "订单.pdf", "application/pdf")); runCurrent()
        env.identity = "server-2\nsales-2"; controller.contextChanged()
        fileRead.complete(byteArrayOf(1, 2, 3)); advanceUntilIdle()
        assertEquals(0, env.fake.uploaded)
        assertNull(controller.state.value.sales.session)
        assertTrue(controller.state.value.sales.attachments.isEmpty())
    }

    @Test fun attachmentTimeoutReattachmentReusesOriginalKeyAndVersion() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        env.fake.failureSuffix = "/attachments"; env.fake.failure = SocketTimeoutException("超时")
        val file = AssistantLocalAttachment("content://orders/1", "订单.pdf", "application/pdf")
        controller.actions.attachSalesFile(file); advanceUntilIdle()
        val firstId = controller.state.value.sales.attachments.single().id
        controller.actions.removeSalesAttachment(firstId)
        env.fake.failure = null
        controller.actions.attachSalesFile(file); advanceUntilIdle()
        assertEquals(2, env.fake.uploaded)
        assertEquals(env.fake.uploadKeys.first(), env.fake.uploadKeys.last())
        assertEquals("needs_review", controller.state.value.sales.attachments.single().status)
        assertTrue(controller.state.value.sales.attachments.single().detail.contains("已上传"))
    }

    @Test fun pickerMultipleFilesAreQueuedAndUploadWithEachConfirmedSessionVersion() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        for (index in 1..5) controller.actions.attachSalesFile(AssistantLocalAttachment("content://orders/$index", "订单$index.pdf", "application/pdf"))
        assertEquals(5, controller.state.value.sales.attachments.size)
        advanceUntilIdle()
        assertEquals(5, env.fake.uploaded)
        assertEquals(listOf(1, 2, 3, 4, 5), env.fake.uploadVersions)
        assertTrue(controller.state.value.sales.attachments.all { it.status == "needs_review" })
        assertFalse(controller.state.value.sales.busy)
        assertEquals(6, controller.state.value.sales.draft?.version)
    }

    @Test fun failedFirstUploadPausesQueueAndExplicitRetryKeepsTheOriginalUploadKey() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        env.fake.failureSuffix = "/attachments"; env.fake.failure = SocketTimeoutException("超时")
        for (index in 1..3) controller.actions.attachSalesFile(AssistantLocalAttachment("content://orders/$index", "订单$index.pdf", "application/pdf"))
        advanceUntilIdle()
        assertEquals(1, env.fake.uploaded)
        assertEquals(listOf("failed", "pending", "pending"), controller.state.value.sales.attachments.map { it.status })
        env.fake.failure = null
        controller.actions.retrySalesAttachment(controller.state.value.sales.attachments.first().id); advanceUntilIdle()
        assertEquals(4, env.fake.uploaded)
        assertEquals(env.fake.uploadKeys[0], env.fake.uploadKeys[1])
        assertEquals(listOf(1, 1, 2, 3), env.fake.uploadVersions)
        assertTrue(controller.state.value.sales.attachments.all { it.status == "needs_review" })
    }

    @Test fun unsentTextRestoresOnlyInsideTheOriginalServerUserAndSession() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.updateSalesInput("这份输入没有发送"); advanceUntilIdle()
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals("这份输入没有发送", restarted.state.value.sales.input)
        env.identity = "server-2\nsales-2"; restarted.contextChanged()
        restarted.actions.openSales(); advanceUntilIdle(); restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals("", restarted.state.value.sales.input)
        assertFalse(env.fake.requests.any { it.path.endsWith("/codex-jobs") })
    }

    @Test fun purchaseStaleClickCannotSubmitAndConversationNeverPretendsBackendReply() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openPurchase(); advanceUntilIdle()
        controller.actions.enrollPurchaseItem("purchase:1", 0); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/enroll") })
        controller.actions.updatePurchaseInput("帮我看看这些采购"); controller.actions.sendPurchaseMessage(); advanceUntilIdle()
        assertEquals("帮我看看这些采购", controller.state.value.purchase.input)
        assertFalse(controller.state.value.purchase.conversationEnabled)
        assertTrue(controller.state.value.purchase.messages.isEmpty())
        assertTrue(controller.state.value.purchase.error.orEmpty().contains("待接入"))
    }

    @Test fun submissionCapabilityAloneCannotGrantAnUnconfirmedVersion() {
        val fake = Fake().apply { status = "confirmed"; version = 7 }
        val raw = fake.session().getJSONObject("session").put("submit_available", true).put("confirmed_version", 6)
        val draft = AssistantJson.draft(raw)!!
        assertEquals(6, draft.confirmedVersion)
        assertFalse(draft.canSubmit)
        raw.remove("confirmed_version")
        assertNull(AssistantJson.draft(raw)?.confirmedVersion)
        assertFalse(AssistantJson.draft(raw)!!.canSubmit)
    }

    @Test fun webImportCompletionBasisAndReadbackEvidenceHaveSafeDefaultsAndSurviveLocalRestore() {
        val raw = Fake().session().getJSONObject("session").put("status", "submitted").put("erp_order_no", "SO-001")
            .put("submission_completion_basis", "web_import_confirmation").put("erp_readback_performed", false)
            .put("submit_available", true)
        val draft = AssistantJson.draft(raw)!!
        assertEquals("web_import_confirmation", draft.submissionCompletionBasis)
        assertFalse(draft.erpReadbackPerformed); assertFalse(draft.canSubmit)
        val restored = AssistantJson.draft(AssistantJson.draftRecord(draft))!!
        assertEquals(draft.submissionCompletionBasis, restored.submissionCompletionBasis)
        assertFalse(restored.erpReadbackPerformed); assertEquals("submitted", restored.status)
        raw.remove("submission_completion_basis"); raw.remove("erp_readback_performed")
        assertEquals("", AssistantJson.draft(raw)!!.submissionCompletionBasis)
        assertFalse(AssistantJson.draft(raw)!!.erpReadbackPerformed)
        raw.put("erp_readback_performed", "true")
        assertFalse(AssistantJson.draft(raw)!!.erpReadbackPerformed)
        raw.put("erp_readback_performed", true)
        assertTrue(AssistantJson.draft(raw)!!.erpReadbackPerformed)
    }

    @Test fun lineDocumentReceiptRoundTripsAllNumbersAndRejectsIncompleteOrDuplicateMetadata() {
        val raw = Fake().session().getJSONObject("session").put("status", "submitted").put("erp_order_no", "SO-001")
            .put("submission_completion_basis", "web_import_confirmation").put("erp_reference_scope", "first_line_document")
            .put("erp_document_count", 2).put("erp_order_numbers", JSONArray().put("SO-001").put("SO-002"))
        val original = AssistantJson.draft(raw)!!
        assertTrue(original.hasCompleteLineDocumentReceipt)
        assertEquals(listOf("SO-001", "SO-002"), original.erpOrderNumbers)
        val restored = AssistantJson.draft(AssistantJson.draftRecord(original))!!
        assertEquals(original.erpOrderNumbers, restored.erpOrderNumbers); assertEquals("SO-001", restored.erpOrderNo)
        assertEquals(2, restored.erpDocumentCount); assertEquals("first_line_document", restored.erpReferenceScope)
        assertTrue(restored.hasCompleteLineDocumentReceipt); assertFalse(restored.canSubmit)
        raw.put("erp_document_count", 2.5)
        assertFalse(AssistantJson.draft(raw)!!.hasCompleteLineDocumentReceipt)
        raw.put("erp_document_count", 2).put("erp_order_numbers", JSONArray().put("SO-001").put("SO-001"))
        assertFalse(AssistantJson.draft(raw)!!.hasCompleteLineDocumentReceipt)
        raw.put("erp_order_numbers", JSONArray().put("SO-001").put("SO-002")).put("status", "submission_unknown")
        assertFalse(AssistantJson.draft(raw)!!.hasCompleteLineDocumentReceipt)
    }

    @Test fun legacySingleDocumentReceiptKeepsItsNumberAndDoesNotInventMultiDocumentMetadata() {
        val raw = Fake().session().getJSONObject("session").put("status", "submitted").put("erp_order_no", "OLD-SO-001")
            .put("submission_completion_basis", "web_import_confirmation")
        val original = AssistantJson.draft(raw)!!
        assertEquals(emptyList<String>(), original.erpOrderNumbers); assertEquals(0, original.erpDocumentCount)
        assertEquals("", original.erpReferenceScope); assertFalse(original.hasCompleteLineDocumentReceipt)
        val restored = AssistantJson.draft(AssistantJson.draftRecord(original))!!
        assertEquals("OLD-SO-001", restored.erpOrderNo); assertEquals("", restored.erpReferenceScope)
        assertFalse(restored.hasCompleteLineDocumentReceipt)
    }

    @Test fun salesSubmissionTimeoutWarnsAgainstRepeatUploadAndKeepsFrozenRequestAndInput() = runTest {
        val env = Environment(this)
        env.fake.status = "confirmed"; env.fake.submitAvailable = true
        env.fake.failureSuffix = "/submit"; env.fake.failure = SocketTimeoutException("网页请求超时")
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.selectSalesSession("session-1"); advanceUntilIdle()
        controller.actions.updateSalesInput("原始客户说明")
        controller.actions.submitSalesDraft("session-1", 1); advanceUntilIdle()
        assertTrue(controller.state.value.sales.error!!.contains("导入结果待核对，勿重复上传"))
        assertEquals("原始客户说明", controller.state.value.sales.input)
        val request = env.fake.requests.single { it.path.endsWith("/submit") }
        val body = JSONObject(request.body!!)
        assertEquals(1, body.getInt("version")); assertTrue(body.getString("idempotency_key").isNotBlank())
        assertFalse(body.has("submission_completion_basis")); assertFalse(body.has("erp_readback_performed"))
        assertTrue(env.disk.keys.any { it.endsWith(":submit") })
        controller.actions.refreshSales(); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/submit") })
        assertEquals("原始客户说明", controller.state.value.sales.input)
    }

    @Test fun salesDraftReadsOriginalQuoteAndServerUntaxedAmountsWithoutConvertingAgain() {
        val raw = Fake().session().getJSONObject("session")
        val stored = raw.getJSONObject("draft")
        stored.put("tax_basis", "tax_inclusive").put("tax_rate", "13")
        stored.getJSONArray("lines").getJSONObject(0).put("unit_price", "1.2345")
            .put("quoted_unit_price", "1.2345").put("tax_basis", "tax_exclusive")
            .put("tax_rate", "13").put("untaxed_unit_price", "1.235").put("untaxed_line_amount", "2.47")
        val draft = AssistantJson.draft(raw)!!
        assertEquals("tax_inclusive", draft.taxBasis)
        assertEquals("13", draft.taxRate)
        assertEquals("1.2345", draft.lines.single().unitPrice)
        assertEquals("tax_exclusive", draft.lines.single().taxBasis)
        assertEquals("1.235", draft.lines.single().untaxedUnitPrice)
        assertEquals("2.47", draft.lines.single().untaxedLineAmount)
        val restored = AssistantJson.draft(AssistantJson.draftRecord(draft))!!
        assertEquals(draft.lines.single().taxBasis, restored.lines.single().taxBasis)
        assertEquals(draft.lines.single().untaxedUnitPrice, restored.lines.single().untaxedUnitPrice)
        assertEquals(draft.lines.single().untaxedLineAmount, restored.lines.single().untaxedLineAmount)
    }

    @Test fun lineIssuesUseTheirOwnBackendMessageAndNeverExposeInternalCodes() {
        val raw = Fake().session().getJSONObject("session")
        val draft = raw.getJSONObject("draft")
        val first = draft.getJSONArray("lines").getJSONObject(0).put("id", "line-1")
            .put("issues", JSONArray().put("delivery_missing").put("unknown_internal_check"))
        val second = JSONObject(first.toString()).put("id", "line-2")
            .put("issues", JSONArray().put("delivery_missing"))
        draft.put("lines", JSONArray().put(first).put(second)).put("issues", JSONArray()
            .put(JSONObject().put("code", "delivery_missing").put("line", 1).put("message", "请填写整单交期或本行交期"))
            .put(JSONObject().put("code", "delivery_missing").put("line", 2).put("message", "第二行交期需重新核对")))
        val parsed = AssistantJson.draft(raw)!!
        assertEquals("请填写整单交期或本行交期；本行信息需核对", parsed.lines.first().issue)
        assertEquals("第二行交期需重新核对", parsed.lines.last().issue)
        assertFalse(parsed.lines.any { it.issue.contains("delivery_missing") || it.issue.contains("unknown_internal_check") })
        assertFalse(parsed.canConfirm)
    }

    @Test fun salesDraftRoundTripRetainsSummaryMetadataAndOriginalDimension() {
        val raw = Fake().session().getJSONObject("session")
        raw.getJSONObject("draft").getJSONArray("lines").getJSONObject(0)
            .put("summary_dimension", "M6X30").put("project_no", "项目-1").put("drawing_no", "图-1")
            .put("grade", "8.8").put("surface", "热浸锌").put("summary_metadata_confirmed", true)
            .put("summary_dimension_source", "human_confirmed").put("source_ref", JSONObject()
                .put("sheet", "订单明细").put("row", 7).put("preview", "原始资料第七行"))
        val draft = AssistantJson.draft(raw)!!
        assertEquals("M6X30", draft.lines.single().specification)
        assertEquals("M6X30", draft.lines.single().summaryDimension)
        assertEquals("订单明细 · 第 7 行", draft.lines.single().sourceLabel)
        val restored = AssistantJson.draft(AssistantJson.draftRecord(draft))!!
        assertEquals(draft.lines.single(), restored.lines.single())
    }

    @Test fun sourceExclusionSurvivesRestartAndSendsOnlyExplicitAction() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        val editor = controller.state.value.sales.editor!!.copy(
            sourceRows = listOf(SalesSourceRowUi("source-7", "客户文件 · 第 7 行", "备注明细", "exclude_pending", "只是一行说明")),
            sourceActions = listOf(SalesSourceActionUi("source-7", "只是一行说明")))
        controller.actions.updateSalesDraftEdit(editor)
        val restarted = env.controller(); restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals(editor.sourceRows, restarted.state.value.sales.editor?.sourceRows)
        assertEquals(editor.sourceActions, restarted.state.value.sales.editor?.sourceActions)
        restarted.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val body = JSONObject(env.fake.requests.last().body!!)
        assertFalse(body.has("source_rows"))
        val action = body.getJSONArray("source_row_actions").getJSONObject(0)
        assertEquals("source-7", action.getString("source_row_id"))
        assertEquals("exclude", action.getString("action"))
        assertEquals("只是一行说明", action.getString("reason"))
        assertFalse(action.has("excluded_by"))
    }

    @Test fun leavingUnsavedDraftEditorKeepsLocalLinesAndBlocksOldServerConfirmation() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        val initial = controller.state.value.sales.editor!!
        val changed = initial.copy(customerOrderNo = "PO-local", lines = initial.lines +
            initial.lines.single().copy(id = "phone-line-2", quantity = "3"))
        controller.actions.updateSalesDraftEdit(changed); advanceUntilIdle()
        controller.actions.closeSalesDraftEditor(); advanceUntilIdle()
        assertFalse(controller.state.value.sales.editorOpen)
        assertTrue(controller.state.value.sales.hasLocalDraftEdit)
        assertEquals(changed, controller.state.value.sales.editor)
        controller.actions.confirmSalesDraft("session-1", 1); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/confirm") })

        val restarted = env.controller()
        restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertFalse(restarted.state.value.sales.editorOpen)
        assertTrue(restarted.state.value.sales.hasLocalDraftEdit)
        assertEquals("PO-local", restarted.state.value.sales.editor?.customerOrderNo)
        assertEquals(2, restarted.state.value.sales.editor?.lines?.size)
        restarted.actions.beginSalesDraftEdit()
        assertTrue(restarted.state.value.sales.editorOpen)
        assertEquals("phone-line-2", restarted.state.value.sales.editor?.lines?.last()?.id)

        restarted.actions.discardSalesDraftEdit(); advanceUntilIdle()
        assertNull(restarted.state.value.sales.editor)
        assertFalse(restarted.state.value.sales.hasLocalDraftEdit)
        restarted.actions.confirmSalesDraft("session-1", 1); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/confirm") })

        env.identity = "server-2\nsales-2"; restarted.contextChanged()
        restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertFalse(restarted.state.value.sales.hasLocalDraftEdit)
        assertNull(restarted.state.value.sales.editor)
    }

    @Test fun unknownDraftPutCanReplayFrozenRequestAfterRefreshAndRestartWithoutLosingNewerLocalEdit() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(customerOrderNo = "PO-original"))
        env.fake.dropDraftResponseOnce = true
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        assertEquals(2, env.fake.version)
        assertTrue(controller.state.value.sales.pendingDraftSave)
        val firstPut = env.fake.requests.last { it.path.endsWith("/draft") }
        val firstBody = JSONObject(firstPut.body!!)
        assertEquals(1, firstBody.getInt("version"))
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(customerOrderNo = "PO-newer"))
        controller.actions.refreshSales(); advanceUntilIdle()
        assertEquals(2, controller.state.value.sales.draft?.version)
        assertEquals(1, controller.state.value.sales.editor?.version)

        val restarted = env.controller()
        restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertTrue(restarted.state.value.sales.pendingDraftSave)
        assertEquals("PO-newer", restarted.state.value.sales.editor?.customerOrderNo)
        restarted.actions.discardSalesDraftEdit(); advanceUntilIdle()
        assertTrue(restarted.state.value.sales.pendingDraftSave)
        assertEquals("PO-newer", restarted.state.value.sales.editor?.customerOrderNo)
        restarted.actions.saveSalesDraftEdit(); advanceUntilIdle()
        assertEquals(1, env.fake.requests.count { it.path.endsWith("/draft") })
        restarted.actions.retryUnknownSalesDraftSave(); advanceUntilIdle()
        val puts = env.fake.requests.filter { it.path.endsWith("/draft") }
        assertEquals(2, puts.size)
        assertEquals(firstPut.body, puts.last().body)
        assertEquals(firstBody.getString("idempotency_key"), JSONObject(puts.last().body!!).getString("idempotency_key"))
        assertEquals(2, env.fake.version)
        assertFalse(restarted.state.value.sales.pendingDraftSave)
        assertTrue(restarted.state.value.sales.hasLocalDraftEdit)
        assertEquals("PO-newer", restarted.state.value.sales.editor?.customerOrderNo)
        restarted.actions.confirmSalesDraft("session-1", 2); advanceUntilIdle()
        assertFalse(env.fake.requests.any { it.path.endsWith("/confirm") })
        restarted.actions.adoptLatestSalesDraftVersion()
        assertEquals(2, restarted.state.value.sales.editor?.version)
        restarted.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val finalBody = JSONObject(env.fake.requests.last { it.path.endsWith("/draft") }.body!!)
        assertEquals("PO-newer", finalBody.getString("customer_order_no"))
        assertNotEquals(firstBody.getString("idempotency_key"), finalBody.getString("idempotency_key"))
    }

    @Test fun unknownSourceExclusionReplaysExactActionAndKeepsLocalReviewAfterRestart() = runTest {
        val env = Environment(this); val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        val editor = controller.state.value.sales.editor!!.copy(
            sourceRows = listOf(SalesSourceRowUi("source-7", "客户文件 · 第 7 行", "备注明细", "exclude_pending", "说明行")),
            sourceActions = listOf(SalesSourceActionUi("source-7", "说明行")))
        controller.actions.updateSalesDraftEdit(editor); advanceUntilIdle()
        env.fake.dropDraftResponseOnce = true
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val first = env.fake.requests.last { it.path.endsWith("/draft") }.body!!
        controller.actions.refreshSales(); advanceUntilIdle()
        val restarted = env.controller()
        restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertTrue(restarted.state.value.sales.pendingDraftSave)
        assertEquals(editor.sourceActions, restarted.state.value.sales.editor?.sourceActions)
        restarted.actions.retryUnknownSalesDraftSave(); advanceUntilIdle()
        val puts = env.fake.requests.filter { it.path.endsWith("/draft") }
        assertEquals(listOf(first, first), puts.map { it.body })
        assertEquals(2, env.fake.version)
        assertTrue(restarted.state.value.sales.hasLocalDraftEdit)
        assertEquals(editor.sourceActions, restarted.state.value.sales.editor?.sourceActions)
    }

    @Test fun replayedNewLineMapsServerIdentityBeforeTheNextSave() = runTest {
        val env = Environment(this)
        env.fake.materializeDraft = true
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        val editor = controller.state.value.sales.editor!!
        val added = editor.lines.single().copy(id = "phone-new-line", quantity = "3")
        controller.actions.updateSalesDraftEdit(editor.copy(lines = editor.lines + added)); advanceUntilIdle()
        env.fake.dropDraftResponseOnce = true
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        controller.actions.refreshSales(); advanceUntilIdle()
        val restarted = env.controller()
        restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        assertEquals("phone-new-line", restarted.state.value.sales.editor?.lines?.last()?.id)
        restarted.actions.retryUnknownSalesDraftSave(); advanceUntilIdle()
        assertEquals(listOf("0", "server-line-1"), restarted.state.value.sales.editor?.lines?.map { it.id })
        assertEquals(listOf("0", "server-line-1"), restarted.state.value.sales.draft?.lines?.map { it.id })
        restarted.actions.adoptLatestSalesDraftVersion()
        assertEquals(2, restarted.state.value.sales.editor?.version)
        restarted.actions.updateSalesDraftEdit(restarted.state.value.sales.editor!!.let { current ->
            current.copy(lines = current.lines.map { if (it.id == "server-line-1") it.copy(quantity = "4") else it })
        })
        restarted.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val next = JSONObject(env.fake.requests.last { it.path.endsWith("/draft") }.body!!).getJSONArray("lines")
        assertEquals("server-line-1", next.getJSONObject(1).getString("id"))
        assertFalse(next.getJSONObject(1).has("new_line"))
    }

    @Test fun replayedExclusionRemovesOnlyProvenCompletedActionBeforeNextSave() = runTest {
        val env = Environment(this)
        env.fake.materializeDraft = true
        env.fake.serverSourceRows = JSONArray().put(JSONObject().put("id", "source-7")
            .put("kind", "text").put("status", "needs_review").put("source_label", "文件第 7 行")
            .put("preview", "单外说明"))
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        controller.actions.beginSalesDraftEdit()
        val editor = controller.state.value.sales.editor!!
        val source = editor.sourceRows.single()
        controller.actions.updateSalesDraftEdit(editor.copy(
            sourceRows = listOf(source.copy(status = "exclude_pending", reason = "非订单行")),
            sourceActions = listOf(SalesSourceActionUi("source-7", "非订单行"))))
        env.fake.dropDraftResponseOnce = true
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        controller.actions.refreshSales(); advanceUntilIdle()
        val restarted = env.controller()
        restarted.actions.openSales(); advanceUntilIdle()
        restarted.actions.selectSalesSession("session-1"); advanceUntilIdle()
        restarted.actions.retryUnknownSalesDraftSave(); advanceUntilIdle()
        assertTrue(restarted.state.value.sales.editor!!.sourceActions.isEmpty())
        assertEquals("excluded_by_user", restarted.state.value.sales.editor!!.sourceRows.single().status)
        restarted.actions.adoptLatestSalesDraftVersion()
        restarted.actions.updateSalesDraftEdit(restarted.state.value.sales.editor!!.copy(customerOrderNo = "PO-followup"))
        restarted.actions.saveSalesDraftEdit(); advanceUntilIdle()
        val body = JSONObject(env.fake.requests.last { it.path.endsWith("/draft") }.body!!)
        assertEquals(0, body.getJSONArray("source_row_actions").length())
    }

    @Test fun savedCustomerNoteRoundTripsAndAnExplicitEditChangesTheNextFrozenBody() = runTest {
        val env = Environment(this)
        env.fake.materializeDraft = true
        val controller = env.controller()
        controller.actions.openSales(); advanceUntilIdle(); controller.actions.newSalesSession("青岛烁然"); advanceUntilIdle()
        assertEquals("原说明", controller.state.value.sales.draft?.note)
        controller.actions.beginSalesDraftEdit()
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(customerOrderNo = "PO-updated"))
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        assertEquals("原说明", JSONObject(env.fake.requests.last { it.path.endsWith("/draft") }.body!!).getString("note"))
        controller.actions.beginSalesDraftEdit()
        controller.actions.updateSalesDraftEdit(controller.state.value.sales.editor!!.copy(note = "人工交付说明"))
        controller.actions.saveSalesDraftEdit(); advanceUntilIdle()
        assertEquals("人工交付说明", JSONObject(env.fake.requests.last { it.path.endsWith("/draft") }.body!!).getString("note"))
        assertEquals("人工交付说明", controller.state.value.sales.draft?.note)
    }
}
