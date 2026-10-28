package com.ul.workbench.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import android.view.inspector.WindowInspector
import androidx.activity.ComponentActivity
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.unit.Density
import com.ul.workbench.data.*
import com.ul.workbench.AssistantJson
import org.json.JSONArray
import org.json.JSONObject
import com.ul.workbench.ui.screens.DailyWorkScreen
import com.ul.workbench.ui.screens.PurchaseAssistantScreen
import com.ul.workbench.ui.screens.SalesAssistantScreen
import com.ul.workbench.ui.theme.WorkbenchTheme
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import org.robolectric.annotation.LooperMode
import java.io.File

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@LooperMode(LooperMode.Mode.PAUSED)
@Config(sdk = [35], qualifiers = "w390dp-h844dp-mdpi")
class AssistantUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Test fun choosingHistoricalCandidateChangesOnlyTargetLineAndKeepsInputsSourcesAndVersion() {
        val candidate = SalesMatchCandidateUi("HISTORY-001", "历史目录螺钉", "M6×40", "8.8级", "customer_part_exact",
            3, "2026-09-28", true, "历史候选，需要核对")
        val first = draft().lines.single().copy(sourceLabel = "客户订单.xlsx · 第 4 行", sourcePreview = "客户 M6×40，100 支",
            sourceReview = SalesSourceReviewUi(true, evidenceHash = "old-hash"), sourceMatchConfirmed = true,
            summaryDimension = "旧规格", projectNo = "旧项目", summaryMetadataConfirmed = true, dueDate = "2026-10-20",
            matchCandidates = listOf(candidate), matchStatus = SalesMatchStatusUi("ready", coveredFrom = "2026-01-01", coveredTo = "2026-10-07"))
        val second = first.copy(id = "l2", matchCandidates = emptyList(), matchStatus = null)
        val initial = draft().copy(note = "客户备注保留", lines = listOf(first, second))
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = initial, editor = initial,
            editorOpen = true, input = "对话输入保留"))
        var saves = 0
        var confirms = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(
            updateSalesDraftEdit = { state = state.copy(editor = it, hasLocalDraftEdit = true) }, saveSalesDraftEdit = { saves++ },
            confirmSalesDraft = { _, _ -> confirms++ })) } }
        compose.onNodeWithTag("sales-draft-editor-list").performScrollToNode(hasTestTag("edit-line:l1:candidate:0"))
        compose.onNodeWithTag("edit-line:l1:candidate:0").performScrollTo()
        compose.onNodeWithText("HISTORY-001 · 历史目录螺钉").assertExists()
        compose.onNodeWithText("M6×40 · 8.8级").assertExists()
        compose.onNodeWithText("同客户料号历史 · 3 次 · 最近 2026-09-28").assertExists()
        compose.onNodeWithText("历史覆盖：2026-01-01 至 2026-10-07").assertExists()
        compose.onNodeWithTag("edit-line:l1:candidate:0").performClick()
        compose.runOnIdle {
            val changed = state.editor!!.lines.first()
            assertEquals("HISTORY-001", changed.productCode)
            assertEquals(second, state.editor!!.lines[1]); assertEquals(initial.version, state.editor!!.version)
            assertEquals(first.quantity, changed.quantity); assertEquals(first.unitPrice, changed.unitPrice)
            assertEquals(first.unit, changed.unit); assertEquals(first.dueDate, changed.dueDate); assertEquals(first.warehouse, changed.warehouse)
            assertEquals(first.sourceLabel, changed.sourceLabel); assertEquals(first.sourcePreview, changed.sourcePreview)
            assertEquals(first.matchCandidates, changed.matchCandidates); assertEquals(first.matchStatus, changed.matchStatus)
            assertEquals("", changed.productName); assertEquals("", changed.specification); assertEquals("", changed.summaryDimension)
            assertEquals("", changed.projectNo); assertFalse(changed.summaryMetadataConfirmed)
            assertNull(changed.sourceReview); assertFalse(changed.sourceMatchConfirmed)
            assertEquals("客户备注保留", state.editor!!.note); assertEquals("对话输入保留", state.input)
            assertEquals(0, saves); assertEquals(0, confirms)
        }
        compose.onNodeWithTag("edit-line:l1:candidate:0").assertIsNotEnabled()
    }

    @Test fun candidateListCapsAtFiveAndUnavailableOrUnknownCandidatesCannotBeSelected() {
        val candidates = listOf(
            SalesMatchCandidateUi("UNAVAILABLE", available = false),
            SalesMatchCandidateUi("UNKNOWN"),
            SalesMatchCandidateUi("", available = true),
            SalesMatchCandidateUi("READY", method = "same_customer_history_attributes", available = true),
            SalesMatchCandidateUi("FIFTH", available = true),
            SalesMatchCandidateUi("HIDDEN-SIXTH", available = true))
        val initial = draft().copy(lines = draft().lines.map { it.copy(matchCandidates = candidates) })
        var changes = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"),
            draft = initial, editor = initial, editorOpen = true), AssistantActions(updateSalesDraftEdit = { changes++ })) } }
        compose.onNodeWithTag("sales-draft-editor-list").performScrollToNode(hasTestTag("edit-line:l1:candidate:0"))
        (0..2).forEach { compose.onNodeWithTag("edit-line:l1:candidate:$it").performScrollTo().assertIsNotEnabled() }
        compose.onNodeWithTag("edit-line:l1:candidate:3").performScrollTo().assertIsEnabled()
        compose.onNodeWithTag("edit-line:l1:candidate:4").performScrollTo().assertIsEnabled()
        compose.onNodeWithTag("edit-line:l1:candidate:5").assertDoesNotExist()
        compose.onNodeWithText("HIDDEN-SIXTH", substring = true).assertDoesNotExist()
        assertEquals(0, changes)
    }

    @Test fun originalAttachmentViewerFailureIsVisibleEvenAfterScrollingConversationToAttachment() {
        val attachment = AssistantAttachmentUi("pdf-1", "客户订单.pdf", documentProcessing = SalesDocumentProcessingUi("adopted"),
            mimeType = "application/pdf", sha256 = "a".repeat(64), sizeBytes = 100)
        val session = SalesSessionUi("s1", "订单", messages = (1..20).map { AssistantMessageUi("m$it", "user", "客户资料第 $it 条") },
            documentAttachments = listOf(attachment))
        var state by mutableStateOf(SalesAssistantUiState(session = session, input = "待发送的说明", draft = draft(), pendingDraftSave = true))
        var views = 0
        val failure = "手机未安装可查看 PDF 的应用；原附件和草稿已保留"
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(viewSalesAttachment = {
            views++; state = state.copy(error = failure)
        })) } }
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasTestTag("sales-document:pdf-1:view-original"))
        compose.onNodeWithTag("sales-document:pdf-1:view-original").performScrollTo().performClick()
        compose.onNodeWithTag("sales-conversation-error").assertIsDisplayed()
        compose.onNodeWithText(failure).assertIsDisplayed()
        compose.onNodeWithTag("sales-input").assertTextContains("待发送的说明")
        compose.runOnIdle {
            assertEquals(1, views); assertEquals(session, state.session); assertEquals(draft(), state.draft)
            assertTrue(state.pendingDraftSave); assertEquals("待发送的说明", state.input)
        }
    }

    @Test fun documentResultRequiresExplicitAdoptionAndCannotReplaceLocalDraftOrMissingPages() {
        val ready = AssistantAttachmentUi("pdf-1", "客户订单.pdf", "needs_processing", documentProcessing = SalesDocumentProcessingUi(
            "ready", pageCount = 2, lineCount = 6, candidateCount = 2, resultHash = "result-1"), mimeType = "application/pdf",
            sha256 = "a".repeat(64), sizeBytes = 100)
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单", documentAttachments = listOf(ready))))
        var adopted = ""
        var hash = ""
        var viewed = ""
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(adoptSalesDocument = { id, value -> adopted = id; hash = value },
            viewSalesAttachment = { viewed = it })) } }
        compose.onNodeWithTag("sales-document:pdf-1:view-original").performScrollTo().performClick()
        assertEquals("pdf-1", viewed)
        compose.onNodeWithTag("sales-document:pdf-1:adopt").performScrollTo().assertIsEnabled()
        assertEquals("", adopted)
        compose.onNodeWithTag("sales-document:pdf-1:adopt").performClick()
        assertEquals("pdf-1", adopted); assertEquals("result-1", hash)
        compose.runOnIdle { state = state.copy(hasLocalDraftEdit = true) }
        compose.onNodeWithTag("sales-document:pdf-1:adopt").assertIsNotEnabled()
        compose.runOnIdle { state = state.copy(hasLocalDraftEdit = false, session = state.session!!.copy(documentAttachments = listOf(
            ready.copy(documentProcessing = ready.documentProcessing!!.copy(unreadyPages = listOf(2)))))) }
        compose.onNodeWithTag("sales-document:pdf-1:adopt").assertIsNotEnabled()
        compose.onNodeWithText("第 2 页未完整识别，不能加入草稿。").assertExists()
        compose.runOnIdle { state = state.copy(pendingDocumentAdoptionId = "pdf-1") }
        compose.onNodeWithTag("sales-reconcile-document-adoption").assertExists()
        compose.onNodeWithTag("sales-send").assertIsNotEnabled()
        compose.onNodeWithTag("sales-attach").assertIsNotEnabled()
    }

    @Test fun sourceProductReviewShowsDifferenceAndClearsConfirmationWhenProductCodeChanges() {
        val review = SalesSourceReviewUi(true, listOf("规格不一致"), evidenceHash = "evidence-1",
            original = SalesSourceProductUi("客户螺钉", "M6*40", grade = "8.8", surface = "镀锌"),
            catalog = SalesSourceProductUi("目录螺钉", "M6*30", "8.8级热浸锌"))
        val initial = draft().copy(lines = draft().lines.map { it.copy(sourceReview = review) })
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = initial, editor = initial, editorOpen = true))
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(updateSalesDraftEdit = { state = state.copy(editor = it) })) } }
        compose.onNodeWithTag("source-product-review:l1:confirm").performScrollTo()
        compose.onNodeWithText("客户原文：客户螺钉").assertExists()
        compose.onNodeWithText("原文规格：M6*40 · 等级：8.8 · 表面：镀锌").assertExists()
        compose.onNodeWithText("目录规格：M6*30 · 型号：8.8级热浸锌").assertExists()
        compose.onNodeWithTag("source-product-review:l1:confirm").performClick()
        compose.runOnIdle { assertTrue(state.editor!!.lines.single().sourceMatchConfirmed) }
        compose.onNodeWithTag("edit-line:l1:sku").performScrollTo().performTextReplacement("另一个编码")
        compose.runOnIdle { assertFalse(state.editor!!.lines.single().sourceMatchConfirmed); assertNull(state.editor!!.lines.single().sourceReview) }
    }

    @Test fun sourceStandardReviewShowsExactOriginalAndCatalogWithoutTreatingAliasesAsEqual() {
        val original = SalesSourceProductUi("六角头螺栓", "M6×30", standard = "GB/T 5782")
        val review = SalesSourceReviewUi(true, listOf("来源标准“GB/T 5782”与商品资料尚未证明一致，请核对原件与所选商品"),
            evidenceHash = "standard-hash-1", original = original,
            catalog = SalesSourceProductUi("六角头螺栓", "M6×30", standard = "GB 5782"))
        val initial = draft().copy(lines = draft().lines.map { it.copy(sourceReview = review) })
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = initial, editor = initial, editorOpen = true))
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(updateSalesDraftEdit = { state = state.copy(editor = it) })) } }
        compose.onNodeWithTag("sales-draft-editor-list").performScrollToNode(hasTestTag("source-product-review:l1:confirm"))
        compose.onNodeWithText("原文标准：GB/T 5782").assertExists()
        compose.onNodeWithText("目录标准：GB 5782").assertExists()
        compose.onNodeWithTag("source-product-review:l1:confirm").performScrollTo().assertIsEnabled()
        compose.runOnIdle { assertFalse(state.editor!!.lines.single().sourceMatchConfirmed) }
        compose.onNodeWithTag("source-product-review:l1:confirm").performClick()
        compose.runOnIdle {
            assertTrue(state.editor!!.lines.single().sourceMatchConfirmed)
            val changed = review.copy(evidenceHash = "standard-hash-2", catalog = review.catalog.copy(standard = ""))
            state = state.copy(editor = state.editor!!.copy(lines = state.editor!!.lines.map { it.copy(sourceReview = changed, sourceMatchConfirmed = false) }))
        }
        compose.onNodeWithText("原文标准：GB/T 5782").assertExists()
        compose.onNodeWithText("目录标准：未提供").assertExists()
        compose.onNodeWithTag("source-product-review:l1:confirm").assertExists()
    }

    @Test fun unreadyDocumentCoverageCannotBeExcludedFromSourceReview() {
        val coverage = SalesSourceRowUi("coverage-2", "客户订单.pdf · 第 2 页", "未完整识别", "needs_review", "page_not_ready", canExclude = false)
        val initial = draft().copy(sourceRows = listOf(coverage))
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"),
            draft = initial, editor = initial, editorOpen = true), AssistantActions()) } }
        compose.onNodeWithTag("sales-source-review").performScrollTo().performClick()
        compose.onNodeWithText("这项用于核对资料覆盖，不能作为无关行排除。").assertExists()
        compose.onNodeWithTag("source-row:coverage-2:exclude").assertDoesNotExist()
    }

    @Test fun salesAcceptsTextAndAttachmentAndOnlySendUsesComposer() {
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "青岛烁然"), codexEnabled = true))
        var sent = 0
        var picks = 0
        val actions = AssistantActions(updateSalesInput = { state = state.copy(input = it) }, sendSalesMessage = { sent++ })
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, actions, onPickAttachment = { picks++ }) } }
        compose.onNodeWithTag("sales-send").assertIsNotEnabled()
        compose.onNodeWithTag("sales-input").performTextInput("青岛烁然，M6 内六角 100 件")
        compose.onNodeWithTag("sales-send").performClick()
        assertEquals(1, sent)
        compose.onNodeWithTag("sales-attach").performClick()
        assertEquals(1, picks)
        compose.runOnIdle { state = state.copy(input = "", attachments = listOf(AssistantAttachmentUi("f1", "客户订单.xlsx"))) }
        compose.onNodeWithTag("sales-send").assertIsEnabled()
        compose.onNodeWithText("客户订单.xlsx").assertIsDisplayed()
        screenshot("assistant-sales-390.png")
    }

    @Test fun codexClarificationAcceptsReplyButNeverAutomaticallyAdoptsOrConfirms() {
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "客户"), codexEnabled = true,
            codexJob = SalesCodexJobUi("codex-1", "needs_clarification", 1, assistantText = "这批螺钉需要什么强度等级？")))
        var sent = 0; var adopted = 0; var confirmed = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(
            updateSalesInput = { state = state.copy(input = it) }, sendSalesMessage = { sent++ },
            adoptSalesCodexDraft = { _, _ -> adopted++ }, confirmSalesDraft = { _, _ -> confirmed++ })) } }
        compose.onNodeWithText("这批螺钉需要什么强度等级？").assertExists()
        compose.onNodeWithText("请补充信息 · 在下方回复").assertExists()
        compose.onNodeWithTag("sales-input").performTextInput("8.8级，含税报价")
        screenshot("assistant-codex-clarification-390.png")
        compose.onNodeWithTag("sales-send").performClick()
        assertEquals(1, sent); assertEquals(0, adopted); assertEquals(0, confirmed)
        compose.onNodeWithTag("sales-codex:codex-1:adopt").assertDoesNotExist()
    }

    @Test fun codexProposalNeedsExplicitAdoptionAndNewProductSuggestionDoesNotClaimCreation() {
        val job = SalesCodexJobUi("codex-1", "ready_for_review", 1, "result-1", "请核对匹配商品", preview = draft(), canAdopt = true,
            newProductSuggestions = listOf(SalesNewProductSuggestionUi("new-1", "非标螺栓", "M6×35", "SUGGEST-001")))
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "客户"), codexEnabled = true, codexJob = job))
        var adopted: Pair<String, String>? = null; var confirmed = 0; var submitted = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(
            adoptSalesCodexDraft = { id, hash -> adopted = id to hash }, confirmSalesDraft = { _, _ -> confirmed++ },
            submitSalesDraft = { _, _ -> submitted++ })) } }
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasTestTag("sales-codex:codex-1:adopt"))
        compose.onNodeWithTag("sales-codex:codex-1:adopt").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("新商品建议 · 待管理员确认").assertExists()
        compose.onNodeWithText("建议尚未创建到 ERP。").assertExists()
        assertNull(adopted); assertEquals(0, confirmed); assertEquals(0, submitted)
        compose.runOnIdle { state = state.copy(codexJob = job.copy(newProductSuggestions = emptyList())) }
        compose.onNodeWithTag("sales-codex:codex-1:adopt").performClick()
        assertNull(adopted)
        compose.onNodeWithTag("sales-codex-adopt-frozen").assertIsEnabled()
        screenshot("assistant-codex-review-390.png", dialog = true)
        compose.onNodeWithTag("sales-codex-adopt-frozen").performClick()
        assertEquals("codex-1" to "result-1", adopted)
        assertEquals(0, confirmed); assertEquals(0, submitted)
        compose.runOnIdle { state = state.copy(hasLocalDraftEdit = true) }
        compose.onNodeWithTag("sales-codex:codex-1:adopt").assertIsNotEnabled()
    }

    @Test fun codexReviewShowsAllRowsAndOriginalAttributesAndKeepsInputWhenReturned() {
        val original = SalesSourceProductUi("客户原品名", "M6×35", grade = "10.9", surface = "达克罗", standard = "GB70.1")
        val catalog = SalesSourceProductUi("ERP目录品名", "M6×35", "标准件", "10.9", "达克罗", "GB/T70.1")
        val lines = (1..8).map { index -> draft().lines.single().copy(id = "line-$index", productCode = "ERP-$index",
            quantity = "17", unit = "支", unitPrice = "1.13", dueDate = "2026-10-20", projectNo = "PROJ-$index",
            drawingNo = "DRAW-$index", customerLineNo = "客户-$index", sourcePreview = "原文第 $index 行",
            previewOriginal = original, previewCatalog = catalog, originalMaterial = "40Cr", catalogArea = "中国产地", catalogMaterial = "ERP原字段") }
        val job = SalesCodexJobUi("codex-1", "ready_for_review", 3, "result-1", preview = draft().copy(lines = lines), canAdopt = true)
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "客户", dataVersion = "v1"),
            codexEnabled = true, codexJob = job, input = "保留我的追问"))
        var adopted = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(adoptSalesCodexDraft = { _, _ -> adopted++ })) } }
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasTestTag("sales-codex:codex-1:adopt"))
        compose.onNodeWithTag("sales-codex:codex-1:adopt").performScrollTo().performClick()
        compose.onAllNodesWithText("客户原品名 · M6×35")[0].assertExists()
        compose.onAllNodesWithText("标准：GB70.1 · 等级：10.9 · 表面处理：达克罗")[0].assertExists()
        compose.onAllNodesWithText("原资料材质：40Cr")[0].assertExists()
        compose.onNodeWithTag("sales-codex-review-lines").performScrollToNode(hasTestTag("sales-codex-review-line:8"))
        compose.onNodeWithText("选定 ERP 编码：ERP-8").assertExists()
        compose.onNodeWithText("客户行号：客户-8").assertExists()
        compose.onNodeWithText("项目号：PROJ-8").assertExists()
        compose.onNodeWithText("图号：DRAW-8").assertExists()
        compose.onNodeWithTag("sales-codex-review-line:8:expand").performScrollTo().performClick()
        compose.onNodeWithText("原文：原文第 8 行").assertExists()
        compose.onNodeWithText("ERP 资料 Area：中国产地").assertExists()
        compose.onNodeWithText("ERP 资料 Area／材质字段原值：ERP原字段").assertExists()
        compose.onNodeWithText("编码型号：标准件 · 编码标准：GB/T70.1 · 编码等级：10.9 · 编码表面处理：达克罗").assertExists()
        compose.onNodeWithText("原文：原文第 8 行").performScrollTo()
        screenshot("assistant-codex-full-row-390.png", dialog = true)
        compose.runOnIdle { state = state.copy(session = state.session!!.copy(dataVersion = "v2")) }
        compose.onNodeWithTag("sales-codex-adopt-frozen").assertIsNotEnabled()
        compose.onNodeWithTag("sales-codex-review-dismiss").performClick()
        compose.onNodeWithText("保留我的追问").assertExists()
        assertEquals(0, adopted)
    }

    @Test fun salesMayPrepareReferenceButCannotCreateAndUnknownNeverClaimsCreation() {
        val suggestion = SalesNewProductSuggestionUi("new-1", "非标螺栓", "M6×35", canPrepare = true)
        val job = SalesCodexJobUi("codex-1", "ready_for_review", 3, "result-1", preview = draft(), canAdopt = false,
            newProductSuggestions = listOf(suggestion))
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "客户"), codexEnabled = true, codexJob = job))
        var prepared: Triple<String, String, String>? = null; var confirmed = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(
            updateSalesNewProductReference = { key, value -> state = state.copy(newProductReferences = state.newProductReferences + (key to value)) },
            prepareSalesNewProductSuggestion = { id, hash, suggestionId -> prepared = Triple(id, hash, suggestionId) },
            confirmSalesNewProduct = { _, _ -> confirmed++ })) } }
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasTestTag("sales-new-product-reference:new-1"))
        compose.onNodeWithTag("sales-new-product-reference:new-1").performScrollTo().performTextInput("UL-REFERENCE")
        compose.onNodeWithTag("sales-new-product-prepare:new-1").performScrollTo().performClick()
        assertEquals(Triple("codex-1", "result-1", "new-1"), prepared)
        assertEquals("UL-REFERENCE", state.newProductReferences["codex-1:new-1"])
        val plan = productPlan()
        compose.runOnIdle { state = state.copy(newProductPlans = listOf(plan)) }
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasTestTag("sales-new-product:plan-1"))
        compose.onNodeWithTag("sales-new-product:plan-1:confirm").assertDoesNotExist()
        compose.onNodeWithText("请管理员进入这份会话，核对并确认新建编码。").assertExists()
        compose.runOnIdle { state = state.copy(newProductPlans = listOf(plan.copy(status = "unknown"))) }
        compose.onNodeWithText("创建结果待核对 · 请勿重复创建").assertExists()
        compose.onNodeWithText("已创建编码：NEW-001").assertDoesNotExist()
        assertEquals(0, confirmed)
    }

    @Test fun adminNewProductConfirmationFreezesPlanHashAndCreatedRequiresExplicitRematch() {
        val plan = productPlan()
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "客户", dataVersion = "v1"),
            draft = draft(), codexEnabled = true, newProductAdmin = true, newProductPlans = listOf(plan)))
        var confirmed: Pair<String, String>? = null; var rematched = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(
            confirmSalesNewProduct = { id, hash -> confirmed = id to hash }, sendSalesMessage = { rematched++ })) } }
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasTestTag("sales-new-product:plan-1:confirm"))
        compose.onNodeWithTag("sales-new-product:plan-1:confirm").performScrollTo().performClick()
        assertNull(confirmed)
        compose.onNodeWithTag("sales-new-product-confirm-frozen").assertIsEnabled()
        screenshot("assistant-new-product-admin-390.png", dialog = true)
        compose.runOnIdle { state = state.copy(newProductPlans = listOf(plan.copy(planHash = "changed"))) }
        compose.onNodeWithTag("sales-new-product-confirm-frozen").assertIsNotEnabled()
        compose.onNodeWithText("继续核对").performClick()
        compose.runOnIdle { state = state.copy(newProductPlans = listOf(plan)) }
        compose.onNodeWithTag("sales-new-product:plan-1:confirm").performScrollTo().performClick()
        compose.onNodeWithTag("sales-new-product-confirm-frozen").performClick()
        assertEquals("plan-1" to "plan-hash", confirmed)
        compose.runOnIdle { state = state.copy(newProductPlans = listOf(plan.copy(status = "created", createdCode = "OTHER"))) }
        compose.onNodeWithTag("sales-new-product:plan-1:rematch").assertDoesNotExist()
        compose.runOnIdle { state = state.copy(newProductPlans = listOf(plan.copy(status = "created", createdCode = "NEW-001"))) }
        assertEquals(0, rematched)
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasTestTag("sales-new-product:plan-1:rematch"))
        compose.onNodeWithTag("sales-new-product:plan-1:rematch").performScrollTo().performClick()
        assertEquals(1, rematched)
    }

    @Test fun codexUnavailablePendingAndUnknownUploadKeepInputAndCannotSendOrAdopt() {
        val job = SalesCodexJobUi("codex-1", "ready_for_review", 1, "result-1", canAdopt = true)
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "客户"), input = "原输入保留",
            codexJob = job))
        var sent = 0; var adopted = 0; var recovered = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(sendSalesMessage = { sent++ },
            adoptSalesCodexDraft = { _, _ -> adopted++ }, retryUnknownSalesCodexOperation = { recovered++ })) } }
        compose.onNodeWithTag("sales-codex-unavailable").assertExists()
        compose.onNodeWithTag("sales-send").assertIsNotEnabled()
        compose.runOnIdle { state = state.copy(codexEnabled = true, pendingCodexOperation = "codex-adopt") }
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasTestTag("sales-codex-reconcile"))
        compose.onNodeWithTag("sales-codex-reconcile").performScrollTo().performClick()
        assertEquals(1, recovered)
        compose.onNodeWithTag("sales-send").assertIsNotEnabled()
        compose.runOnIdle { state = state.copy(pendingCodexOperation = "", draft = draft().copy(status = "submission_unknown")) }
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasTestTag("sales-codex:codex-1:adopt"))
        compose.onNodeWithTag("sales-codex:codex-1:adopt").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithTag("sales-input").assertIsNotEnabled()
        compose.onNodeWithText("原输入保留").assertExists()
        assertEquals(0, sent); assertEquals(0, adopted)
    }

    @Test fun uploadingOrFailedFileKeepsTextAndStopsSendUntilRemoved() {
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), input = "备注别丢",
            attachments = listOf(AssistantAttachmentUi("f1", "订单.pdf", "uploading"))))
        var removed = ""
        var retried = ""
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(removeSalesAttachment = { removed = it }, retrySalesAttachment = { retried = it })) } }
        compose.onNodeWithTag("sales-send").assertIsNotEnabled()
        compose.onNodeWithTag("retry-attachment:f1").assertDoesNotExist()
        compose.runOnIdle { state = state.copy(error = "上传失败，请重新选择", attachments = listOf(AssistantAttachmentUi("f1", "订单.pdf", "failed"))) }
        compose.onNodeWithText("备注别丢").assertExists()
        compose.onNodeWithText("上传失败，请重新选择").assertIsDisplayed()
        compose.onNodeWithTag("sales-send").assertIsNotEnabled()
        compose.onNodeWithTag("retry-attachment:f1").performClick()
        assertEquals("f1", retried)
        compose.onNodeWithTag("remove-attachment:f1").performClick()
        assertEquals("f1", removed)
    }

    @Test fun newSessionRequiresCustomerAndKeepsNameWhenIdentityCannotBeVerified() {
        var state by mutableStateOf(SalesAssistantUiState(accountLabel = "江苏优螺"))
        var requested = ""
        var refreshed = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(refreshSales = { refreshed++ }, newSalesSession = { name ->
            requested = name; state = state.copy(error = "客户身份无法唯一匹配，请核对完整名称", createError = "客户身份无法唯一匹配，请核对完整名称")
        })) } }
        compose.onNodeWithTag("sales-new-session").performClick()
        compose.onNodeWithTag("sales-create-session").assertIsNotEnabled()
        compose.onNodeWithText("账套：江苏优螺").assertIsDisplayed()
        compose.onNodeWithTag("sales-new-customer").performTextInput("青岛烁然")
        compose.onNodeWithTag("sales-create-session").performClick()
        assertEquals("青岛烁然", requested)
        compose.onNodeWithTag("sales-new-customer").assertTextContains("青岛烁然")
        compose.onNodeWithText("客户身份无法唯一匹配，请核对完整名称").assertIsDisplayed()
        compose.onNodeWithTag("sales-refresh-create").performClick()
        assertEquals(1, refreshed)
        compose.onNodeWithTag("sales-new-customer").assertTextContains("青岛烁然")
        screenshot("assistant-sales-new-customer-390.png", dialog = true)
        compose.runOnIdle { state = state.copy(session = SalesSessionUi("created-session", "青岛烁然"), error = null) }
        compose.onNodeWithTag("sales-new-customer").assertTextContains("青岛烁然")
        compose.onNodeWithTag("sales-create-session").performClick()
        compose.runOnIdle { state = state.copy(session = SalesSessionUi("new-session", "青岛烁然"), error = null) }
        compose.onNodeWithTag("sales-new-customer").assertDoesNotExist()
    }

    @Test fun newSessionFooterStaysVisibleWithEightCustomersAndThreeExistingSessionsAndBlocksBusyClicks() {
        val target = "青岛烁然科技有限公司"
        val choices = listOf(SalesCustomerChoiceUi(target, "李瑄")) + (2..8).map { SalesCustomerChoiceUi("其他客户 $it", "刘锋") }
        val sessions = (1..3).map { SalesSessionUi("old-$it", target, "2026-10-07T18:33:3$it") }
        var state by mutableStateOf(SalesAssistantUiState(accountLabel = "P", sessions = sessions,
            customerChoices = choices, customerChoicesTotal = 8, input = "对话输入保留"))
        var creates = 0
        var requested = ""
        var opens = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(newSalesSession = {
            creates++; requested = it; state = state.copy(busy = true)
        }, selectSalesSession = { opens++ })) } }
        compose.onNodeWithTag("sales-new-session").performClick()
        compose.onNodeWithTag("sales-new-customer").performTextInput(target)
        compose.onAllNodesWithText("打开近期同名会话：", substring = true).assertCountEquals(3)
        // No scroll action: starting a new order remains visible despite the long list.
        compose.onNodeWithTag("sales-create-session").assertIsDisplayed().assertIsEnabled().performClick()
        compose.onNodeWithTag("sales-create-session").assertIsDisplayed().assertIsNotEnabled()
        compose.onNodeWithTag("sales-cancel-create").assertIsDisplayed().assertIsNotEnabled()
        compose.onNodeWithTag("sales-new-customer").assertTextContains(target)
        compose.onNodeWithTag("sales-create-session").performClick()
        compose.runOnIdle {
            assertEquals(1, creates); assertEquals(target, requested); assertEquals(0, opens)
            assertEquals("对话输入保留", state.input)
            state = state.copy(busy = false, createError = "客户身份待核对，输入已保留")
        }
        compose.onNodeWithTag("sales-create-session").assertIsDisplayed().assertIsEnabled()
        compose.onNodeWithTag("sales-new-customer").assertTextContains(target)
        screenshot("assistant-sales-fixed-create-footer-390.png", dialog = true)
    }

    @Test @Config(qualifiers = "w320dp-h740dp-mdpi") fun newSessionFooterRemainsVisibleAfterSelectingLastCustomerWithLargeText() {
        val target = "第八家完整客户名称有限公司"
        val choices = (1..7).map { SalesCustomerChoiceUi("其他客户 $it", "销售") } + SalesCustomerChoiceUi(target, "李瑄")
        var state by mutableStateOf(SalesAssistantUiState(accountLabel = "江苏优螺", customerChoices = choices, customerChoicesTotal = 8,
            sessions = (1..3).map { SalesSessionUi("old-$it", target, "2026-10-07T18:33:3$it") }))
        var requested = ""
        compose.setContent { CompositionLocalProvider(LocalDensity provides Density(1f, 1.3f)) {
            WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(newSalesSession = { requested = it })) }
        } }
        compose.onNodeWithTag("sales-new-session").performClick()
        compose.onNodeWithTag("sales-customer-choice:$target").performScrollTo().performClick()
        // The form remains scrolled; the primary action is outside that scroll region.
        compose.onNodeWithTag("sales-create-session").assertIsDisplayed().assertIsEnabled().performClick()
        compose.onNodeWithTag("sales-cancel-create").assertIsDisplayed()
        assertEquals(target, requested)
        compose.onNodeWithTag("sales-new-customer").assertTextContains(target)
    }

    @Test fun customerChoiceFillsExactNameAndKeepsTypedNameWhenLookupFails() {
        var state by mutableStateOf(SalesAssistantUiState(accountLabel = "P",
            customerChoices = listOf(SalesCustomerChoiceUi("青岛烁然科技有限公司", "张销售")),
            customerChoicesTotal = 1))
        var searched = "not requested"
        var created = ""
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(
            searchSalesCustomers = { searched = it }, newSalesSession = { created = it })) } }
        compose.onNodeWithTag("sales-new-session").performClick()
        compose.waitForIdle()
        assertEquals("", searched)
        compose.onNodeWithTag("sales-customer-choice:青岛烁然科技有限公司").performClick()
        compose.onNodeWithTag("sales-new-customer").assertTextContains("青岛烁然科技有限公司")
        compose.onNodeWithTag("sales-create-session").performClick()
        assertEquals("青岛烁然科技有限公司", created)
        compose.runOnIdle { state = state.copy(customerChoicesError = "客户名单读取失败；已输入名称保留",
            customerChoices = emptyList()) }
        compose.onNodeWithTag("sales-new-customer").assertTextContains("青岛烁然科技有限公司")
        compose.onNodeWithText("客户名单读取失败；已输入名称保留").assertIsDisplayed()
    }

    @Test fun refreshInsideCreateDialogKeepsCustomerAndDialogOpen() {
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("old", "旧录单")))
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(
            newSalesSession = { state = state.copy(error = "客户固定销售待核对", createError = "客户固定销售待核对") },
            refreshSales = { state = state.copy(session = SalesSessionUi("read-back", "已读回录单"), error = null) })) } }
        compose.onNodeWithTag("sales-new-session").performClick()
        compose.onNodeWithTag("sales-new-customer").performTextInput("青岛烁然")
        compose.onNodeWithTag("sales-create-session").performClick()
        compose.onNodeWithText("客户固定销售待核对").assertIsDisplayed()
        compose.onNodeWithTag("sales-refresh-create").performClick()
        compose.onNodeWithTag("sales-new-customer").assertTextContains("青岛烁然")
        compose.onNodeWithTag("sales-create-session").assertIsEnabled()
    }

    @Test fun draftConfirmationFreezesExactVersionAndRejectsUpdatedDraft() {
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = draft()))
        var confirmed: Pair<String, Int>? = null
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(confirmSalesDraft = { id, version -> confirmed = id to version })) } }
        compose.onNodeWithTag("sales-confirm-draft").performScrollTo().performClick()
        compose.onNodeWithText("第 3 版").assertIsDisplayed()
        compose.onNodeWithText("1 行 · 默认报价口径 含税").assertIsDisplayed()
        compose.runOnIdle { state = state.copy(draft = draft().copy(version = 4)) }
        compose.onNodeWithTag("sales-confirm-frozen").assertIsNotEnabled()
        compose.onNodeWithText("草稿已经更新，请关闭后核对最新版本。").assertIsDisplayed()
        assertNull(confirmed)
    }

    @Test fun confirmAndSubmitAreDistinctAndRespectServerGate() {
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = draft()))
        var confirmed: Pair<String, Int>? = null
        var submitted: Pair<String, Int>? = null
        val actions = AssistantActions(confirmSalesDraft = { id, version -> confirmed = id to version },
            submitSalesDraft = { id, version -> submitted = id to version })
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, actions) } }
        compose.onNodeWithTag("sales-submit-draft").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithTag("sales-confirm-draft").performScrollTo().performClick()
        compose.onNodeWithTag("sales-confirm-frozen").performClick()
        assertEquals("d1" to 3, confirmed)
        assertNull(submitted)
        compose.runOnIdle { state = state.copy(draft = draft().copy(confirmedVersion = 3, canConfirm = false)) }
        compose.onNodeWithTag("sales-submit-draft").assertIsNotEnabled()
        compose.runOnIdle { state = state.copy(draft = draft().copy(confirmedVersion = 3, canConfirm = false, canSubmit = true)) }
        compose.onNodeWithTag("sales-submit-draft").performScrollTo().performClick()
        assertNull(submitted)
        compose.onNodeWithText("自动上传这份已确认订单？").assertIsDisplayed()
        compose.onNodeWithText("使用已确认的这一版生成表格，校验通过后自动网页上传；不执行 ERP 回查。").assertExists()
        compose.onNodeWithText("将提交已确认的这一版，随后查询 ERP 结果。").assertDoesNotExist()
        compose.onNodeWithText("确认上传").performClick()
        assertEquals("d1" to 3, submitted)
    }

    @Test fun webImportConfirmationIsAccurateAndDoesNotExposeRepeatUploadEvenWithoutOrderNumber() {
        val completed = draft().copy(status = "submitted", canConfirm = false, canSubmit = false,
            submissionCompletionBasis = "web_import_confirmation", erpReadbackPerformed = false)
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = completed))
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions()) } }
        compose.onNodeWithTag("sales-draft").performScrollTo()
        compose.onNodeWithText("网页已确认导入").assertExists()
        compose.onNodeWithText("ERP 已回查").assertDoesNotExist()
        compose.onNodeWithText("已审核", substring = true).assertDoesNotExist()
        compose.onNodeWithTag("sales-edit-draft").assertDoesNotExist()
        compose.onNodeWithTag("sales-confirm-draft").assertDoesNotExist()
        compose.onNodeWithTag("sales-submit-draft").assertDoesNotExist()
        compose.runOnIdle { state = state.copy(draft = completed.copy(erpOrderNo = "SO-20261007-001")) }
        compose.onNodeWithText("网页已确认导入").assertExists()
        compose.onNodeWithText("单号：SO-20261007-001").assertExists()
        compose.onNodeWithText("ERP 已回查").assertDoesNotExist()
        compose.onNodeWithTag("sales-submit-draft").assertDoesNotExist()
    }

    @Test fun unknownSalesImportShowsDoNotRepeatWarningAndKeepsOriginalInput() {
        val pending = draft().copy(status = "submission_unknown", canConfirm = false, canSubmit = false)
        val state = SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = pending, input = "原始客户说明",
            job = AssistantJobUi("upload-1", "uncertain", "导入结果待核对", requiresReview = true))
        var submits = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(submitSalesDraft = { _, _ -> submits++ })) } }
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasText("导入结果待核对，勿重复上传"))
        compose.onNodeWithText("导入结果待核对，勿重复上传").performScrollTo().assertIsDisplayed()
        compose.onNodeWithTag("sales-submit-draft").assertDoesNotExist()
        compose.onNodeWithTag("sales-edit-draft").assertDoesNotExist()
        compose.onNodeWithTag("sales-input").assertTextContains("原始客户说明")
        assertEquals(0, submits)
    }

    @Test fun submittedLineDocumentsClearlyLabelFirstNumberAndShowAllSeparateErpDocuments() {
        val completed = draft().copy(status = "submitted", canConfirm = false, canSubmit = false,
            erpOrderNo = "SO-001", erpOrderNumbers = listOf("SO-001", "SO-002"), erpDocumentCount = 2,
            erpReferenceScope = "first_line_document", submissionCompletionBasis = "web_import_confirmation")
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"),
            draft = completed), AssistantActions()) } }
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasTestTag("sales-erp-document-numbers"))
        compose.onNodeWithText("共 2 张 ERP 单据").assertExists()
        compose.onNodeWithText("首条单号：SO-001").assertExists()
        compose.onNodeWithText("单号：SO-001").assertDoesNotExist()
        compose.onNodeWithTag("sales-erp-document-numbers").performScrollTo().performClick()
        compose.onNodeWithText("本次导入的 2 张 ERP 单据").assertExists()
        compose.onNodeWithText("1. SO-001").assertExists(); compose.onNodeWithText("2. SO-002").assertExists()
        screenshot("assistant-sales-multiple-documents-390.png", dialog = true)
        compose.onNodeWithText("关闭").performClick()
        compose.onNodeWithTag("sales-submit-draft").assertDoesNotExist()
        compose.onNodeWithText("ERP 已回查").assertDoesNotExist()
    }

    @Test fun unknownOrIncompleteLineDocumentMetadataCannotClaimCompletedErpDocuments() {
        val base = draft().copy(status = "submission_unknown", canConfirm = false, canSubmit = false,
            erpOrderNo = "SO-001", erpOrderNumbers = listOf("SO-001", "SO-002"), erpDocumentCount = 2,
            erpReferenceScope = "first_line_document", submissionCompletionBasis = "web_import_confirmation")
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = base, input = "原输入保留"))
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions()) } }
        compose.onNodeWithTag("sales-conversation").performScrollToNode(hasText("单据清单待核对"))
        compose.onNodeWithText("首条参考单号：SO-001").assertExists()
        compose.onNodeWithText("共 2 张 ERP 单据").assertDoesNotExist()
        compose.onNodeWithText("导入结果待核对，勿重复上传").assertExists()
        compose.onNodeWithTag("sales-erp-document-numbers").assertDoesNotExist()
        compose.onNodeWithText("原输入保留").assertExists()
        compose.runOnIdle { state = state.copy(draft = base.copy(status = "submitted", erpDocumentCount = 3)) }
        compose.onNodeWithText("单据清单待核对").assertExists()
        compose.onNodeWithText("共 3 张 ERP 单据").assertDoesNotExist()
        compose.onNodeWithTag("sales-erp-document-numbers").assertDoesNotExist()
        compose.onNodeWithTag("sales-submit-draft").assertDoesNotExist()
    }

    @Test fun aLegacyOrderNumberAloneDoesNotClaimErpReadback() {
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"),
            draft = draft().copy(erpOrderNo = "OLD-SO-001")), AssistantActions()) } }
        compose.onNodeWithTag("sales-draft").performScrollTo()
        compose.onNodeWithText("已有单号").assertExists()
        compose.onNodeWithText("ERP 已回查").assertDoesNotExist()
        compose.onNodeWithText("单号：OLD-SO-001").assertExists()
        compose.onNodeWithTag("sales-erp-document-count").assertDoesNotExist()
        compose.onNodeWithTag("sales-erp-document-numbers").assertDoesNotExist()
    }

    @Test fun incompleteDraftCannotConfirmOrSubmit() {
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"),
            draft = draft().copy(issues = listOf("商品有多个候选，请先选择"), canConfirm = false)), AssistantActions()) } }
        compose.onNodeWithTag("sales-confirm-draft").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithTag("sales-submit-draft").assertIsNotEnabled()
        compose.onNodeWithText("商品有多个候选，请先选择").assertIsDisplayed()
    }

    @Test fun retainedLocalEditCannotConfirmServerDraftUntilExplicitlyResolved() {
        var resumed = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(SalesAssistantUiState(
            session = SalesSessionUi("s1", "订单"), draft = draft().copy(canConfirm = true), editor = draft(),
            editorOpen = false, hasLocalDraftEdit = true, input = "UL-001,3,1.13"),
            AssistantActions(beginSalesDraftEdit = { resumed++ })) } }
        compose.onNodeWithTag("sales-draft-editor").assertDoesNotExist()
        compose.onNodeWithTag("sales-resume-local-edit").performScrollTo().performClick()
        assertEquals(1, resumed)
        compose.onNodeWithTag("sales-confirm-draft").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithTag("sales-send").assertIsNotEnabled()
        compose.onNodeWithTag("sales-attach").assertIsNotEnabled()
    }

    @Test fun pendingSaveOffersOriginalRequestReadbackAndBlocksNewSave() {
        var reconciled = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(SalesAssistantUiState(
            session = SalesSessionUi("s1", "订单"), draft = draft(), editor = draft(), editorOpen = true,
            hasLocalDraftEdit = true, pendingDraftSave = true),
            AssistantActions(retryUnknownSalesDraftSave = { reconciled++ })) } }
        compose.onNodeWithTag("sales-reconcile-draft-save").performScrollTo().performClick()
        assertEquals(1, reconciled)
        compose.onNodeWithTag("sales-save-draft-edit").assertIsNotEnabled()
    }

    @Test fun structuredEditorKeepsChangesOnSaveConflictAndCannotConfirmOldVersion() {
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = draft()))
        var saved: SalesDraftUi? = null
        val actions = AssistantActions(beginSalesDraftEdit = { state = state.copy(editor = state.draft, editorOpen = true) },
            updateSalesDraftEdit = { state = state.copy(editor = it) }, saveSalesDraftEdit = {
                saved = state.editor
                state = state.copy(editorError = "草稿版本已变化，输入已保留，请核对最新版本")
            })
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, actions) } }
        compose.onNodeWithTag("sales-edit-draft").performScrollTo().performClick()
        compose.onNodeWithTag("edit-draft-customer").performTextReplacement("上海振华")
        compose.onNodeWithTag("edit-line:l1:quantity").performScrollTo().performTextReplacement("250")
        compose.onNodeWithTag("edit-line:l1:tax-exclusive").performScrollTo().performClick()
        compose.onNodeWithTag("edit-line:l1:due").performScrollTo().performTextInput("2026-10-20")
        compose.onNodeWithTag("sales-save-draft-edit").performClick()
        compose.onNodeWithText("草稿版本已变化，输入已保留，请核对最新版本").assertIsDisplayed()
        compose.runOnIdle {
            assertEquals("上海振华", saved?.customerName)
            assertEquals("250", saved?.lines?.first()?.quantity)
            assertEquals("tax_exclusive", saved?.lines?.first()?.taxBasis)
            assertEquals("2026-10-20", saved?.lines?.first()?.dueDate)
            assertEquals(3, saved?.version)
        }
        compose.onNodeWithTag("edit-line:l1:due").assertTextContains("2026-10-20")
        compose.onNodeWithTag("sales-confirm-draft").assertDoesNotExist()
    }

    @Test fun saveValidationErrorIsImmediatelyVisibleAboveScrolledEditorAndKeepsInputs() {
        val initial = draft().copy(lines = (1..6).map { draft().lines.single().copy(id = "line-$it") }, note = "客户附加说明")
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = initial, editor = initial, editorOpen = true))
        var saves = 0
        val failure = "明细行身份与原草稿不一致；编辑内容已保留"
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(
            updateSalesDraftEdit = { state = state.copy(editor = it, hasLocalDraftEdit = true) },
            saveSalesDraftEdit = { saves++; state = state.copy(error = failure, editorError = null) })) } }
        compose.onNodeWithTag("sales-draft-editor-list").performScrollToNode(hasTestTag("edit-line:line-6:quantity"))
        compose.onNodeWithTag("edit-line:line-6:quantity").performScrollTo().performTextReplacement("250")
        compose.onNodeWithTag("sales-save-draft-edit").performClick()
        compose.onNodeWithTag("sales-editor-error").assertIsDisplayed()
        compose.onNodeWithText(failure).assertIsDisplayed()
        compose.onNodeWithTag("sales-draft-editor").assertIsDisplayed()
        compose.runOnIdle {
            assertEquals(1, saves); assertTrue(state.editorOpen); assertFalse(state.pendingDraftSave)
            assertEquals("250", state.editor!!.lines.last().quantity)
            assertEquals("客户附加说明", state.editor!!.note)
            assertEquals(6, state.editor!!.lines.size)
        }
    }

    @Test fun newSessionDialogShowsOnlyCreationErrorAndDoesNotClearOldDraftFailureOrPendingFacts() {
        val oldFailure = "旧草稿：明细行身份与原草稿不一致"
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "旧订单"), draft = draft(),
            error = oldFailure, editorError = oldFailure, pendingDraftSave = true))
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions(newSalesSession = {
            state = state.copy(createError = "新客户固定归属待核对")
        })) } }
        compose.onNodeWithTag("sales-new-session").performClick()
        compose.onNodeWithText(oldFailure).assertDoesNotExist()
        compose.onNodeWithTag("sales-new-customer").performTextInput("新客户")
        compose.onNodeWithTag("sales-create-session").performClick()
        compose.onNodeWithText("新客户固定归属待核对").assertIsDisplayed()
        compose.runOnIdle {
            assertTrue(state.pendingDraftSave)
            assertEquals(oldFailure, state.editorError); assertEquals(oldFailure, state.error)
            assertEquals(draft().id, state.draft!!.id)
        }
    }

    @Test fun salesDraftShowsOriginalQuoteAndServerCalculatedUntaxedAmounts() {
        val priced = draft().copy(lines = listOf(draft().lines.single().copy(unitPrice = "1.13",
            taxBasis = "tax_inclusive", taxRate = "13", untaxedUnitPrice = "1.000", untaxedLineAmount = "100.00")))
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(SalesAssistantUiState(
            session = SalesSessionUi("s1", "订单"), draft = priced), AssistantActions()) } }
        compose.onNodeWithText("客户原报价 1.13（含税）").assertExists()
        compose.onNodeWithText("未税单价 1.000 · 未税行金额 100.00").assertExists()
    }

    @Test fun draftAndEditorShowRowSpecificChineseHintInsteadOfDeliveryCode() {
        val raw = AssistantJson.draftRecord(draft())
        raw.getJSONObject("draft").put("issues", JSONArray().put(JSONObject().put("code", "delivery_missing")
            .put("line", 1).put("message", "请填写整单交期或本行交期")))
        raw.getJSONObject("draft").getJSONArray("lines").getJSONObject(0).put("issues", JSONArray().put("delivery_missing"))
        val parsed = AssistantJson.draft(raw)!!
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = parsed))
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, AssistantActions()) } }
        compose.onNodeWithTag("sales-draft").performScrollTo()
        compose.onNodeWithText("delivery_missing").assertDoesNotExist()
        compose.onAllNodesWithText("请填写整单交期或本行交期").assertCountEquals(2)
        compose.runOnIdle { state = state.copy(editor = parsed, editorOpen = true) }
        compose.onNodeWithText("请填写整单交期或本行交期").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("delivery_missing").assertDoesNotExist()
        compose.onNodeWithText("第 1 行 · 内六角圆柱头螺钉").assertExists()
        compose.onNodeWithTag("sales-confirm-draft").assertDoesNotExist()
    }

    @Test fun manualSummaryEditsApplyOnlyToSelectedRepeatedSkuLine() {
        val original = draft().lines.single()
        val repeated = draft().copy(lines = listOf(original, original.copy(id = "l2", quantity = "200")))
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = repeated, editor = repeated, editorOpen = true))
        val actions = AssistantActions(updateSalesDraftEdit = { state = state.copy(editor = it) })
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state, actions) } }
        compose.onNodeWithTag("edit-line:l1:dimension").assertDoesNotExist()
        compose.onNodeWithTag("edit-line:l1:summary-toggle").performScrollTo().performClick()
        compose.onNodeWithTag("edit-line:l1:dimension").performScrollTo().performTextReplacement("M6×30 非标")
        compose.onNodeWithTag("edit-line:l1:drawing").performScrollTo().performTextReplacement("图-甲")
        compose.runOnIdle {
            assertEquals("M6×30 非标", state.editor?.lines?.first()?.summaryDimension)
            assertEquals("图-甲", state.editor?.lines?.first()?.drawingNo)
            assertTrue(state.editor?.lines?.first()?.summaryMetadataConfirmed == true)
            assertEquals("", state.editor?.lines?.last()?.summaryDimension)
            assertFalse(state.editor?.lines?.last()?.summaryMetadataConfirmed == true)
            assertEquals("200", state.editor?.lines?.last()?.quantity)
        }
    }

    @Test fun excludingSourceRequiresReasonAndRemovesOnlyItsLinkedLineInLocalEdit() {
        val original = draft().lines.single()
        val source = SalesSourceRowUi("source-1", "客户表 · 第 7 行", "原始数量备注", "needs_review", "source_line_needs_match", original.id)
        val withSource = draft().copy(lines = listOf(original, original.copy(id = "l2", quantity = "200")), sourceRows = listOf(source))
        var state by mutableStateOf(SalesAssistantUiState(session = SalesSessionUi("s1", "订单"), draft = withSource, editor = withSource, editorOpen = true))
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(state,
            AssistantActions(updateSalesDraftEdit = { state = state.copy(editor = it) })) } }
        compose.onNodeWithTag("sales-source-review").performScrollTo().performClick()
        compose.onNodeWithTag("source-row:source-1:exclude").performScrollTo().performClick()
        compose.onNodeWithTag("sales-source-exclude-confirm").assertIsNotEnabled()
        compose.onNodeWithTag("sales-source-exclude-reason").performTextInput("本次不采购该行")
        compose.onNodeWithTag("sales-source-exclude-confirm").performClick()
        compose.runOnIdle {
            assertEquals(listOf("l2"), state.editor?.lines?.map { it.id })
            assertEquals(SalesSourceActionUi("source-1", "本次不采购该行"), state.editor?.sourceActions?.single())
            assertEquals("exclude_pending", state.editor?.sourceRows?.single()?.status)
            assertEquals(3, state.editor?.version)
        }
    }

    @Test fun changedServerDraftRequiresVisibleReviewBeforeUsingItsVersion() {
        val local = draft().copy(customerOrderNo = "PO-local", lines = draft().lines.map { it.copy(quantity = "250") })
        val latest = draft().copy(version = 4, customerOrderNo = "PO-server")
        var refreshes = 0
        var adopted = 0
        compose.setContent { WorkbenchTheme { SalesAssistantScreen(SalesAssistantUiState(
            session = SalesSessionUi("s1", "订单"), draft = latest, editor = local, editorOpen = true),
            AssistantActions(refreshSales = { refreshes++ }, adoptLatestSalesDraftVersion = { adopted++ })) } }
        compose.onNodeWithContentDescription("刷新助手").performClick()
        assertEquals(1, refreshes)
        compose.onNodeWithTag("sales-review-latest-draft").performScrollTo().performClick()
        compose.onNodeWithText("服务器第 4 版").assertExists()
        compose.onNodeWithText("手机本地编辑").assertExists()
        compose.onNodeWithText("客户 青岛烁然 · 客户订单号 PO-server · 交期 空").assertExists()
        compose.onNodeWithText("客户 青岛烁然 · 客户订单号 PO-local · 交期 空").assertExists()
        assertEquals(0, adopted)
        compose.onNodeWithTag("sales-adopt-latest-version").performClick()
        assertEquals(1, adopted)
    }

    @Test fun purchaseConversationRetainsInputWhileCapabilityUnavailable() {
        var state by mutableStateOf(PurchaseAssistantUiState())
        var sends = 0
        compose.setContent { WorkbenchTheme { PurchaseAssistantScreen(state, AssistantActions(
            updatePurchaseInput = { state = state.copy(input = it) }, sendPurchaseMessage = { sends++ })) } }
        compose.onNodeWithTag("purchase-toggle-chat").performClick()
        compose.onNodeWithTag("purchase-input").performTextInput("M6 要 8.8 级，限价 0.8")
        compose.onNodeWithTag("purchase-send").assertIsNotEnabled()
        compose.onNodeWithText("助手服务待接入").assertIsDisplayed()
        compose.runOnIdle { state = state.copy(conversationEnabled = true) }
        compose.onNodeWithTag("purchase-send").performClick()
        assertEquals(1, sends)
    }

    @Test fun browserQueueShowsCurrentStepAndManualHold() {
        var refreshed = 0
        val operations = listOf(WebOperationUi("download-1", "采购未交下载", "needs_human",
            "等待人工验证", "保留原浏览器会话", 2, 4, "2026-10-07T07:40:00+08:00"))
        compose.setContent { WorkbenchTheme { PurchaseAssistantScreen(PurchaseAssistantUiState(),
            AssistantActions(refreshWebOperations = { refreshed++ }), webOperations = operations) } }
        compose.onNodeWithTag("web-operation-queue").assertExists()
        compose.onNodeWithText("采购未交下载 · 等待人工验证").assertExists()
        compose.onNodeWithTag("refresh-web-operations").performClick()
        assertEquals(1, refreshed)
    }

    @Test fun purchaseJoinWaitsForServerThenMovesIntoProcessingWithProgress() {
        var state by mutableStateOf(PurchaseAssistantUiState(planId = "plan1", generatedAt = "2026-10-06 16:00",
            sourceVersion = "data1", whitelistVersion = "rules1", items = listOf(purchase())))
        var enrolled: Pair<String, Int>? = null
        compose.setContent { WorkbenchTheme { PurchaseAssistantScreen(state, AssistantActions(enrollPurchaseItem = { id, version -> enrolled = id to version })) } }
        compose.onNodeWithText("采购计划 1 项").assertIsDisplayed()
        compose.onNodeWithTag("purchase-enroll:p1").performClick()
        assertEquals("p1" to 7, enrolled)
        compose.onNodeWithTag("purchase-enroll:p1").assertIsDisplayed()
        screenshot("assistant-purchase-whitelist-390.png")
        compose.runOnIdle { state = state.copy(items = listOf(purchase().copy(state = "in_progress", canEnroll = false,
            job = AssistantJobUi("job1", "running", "正在准备采购草稿", completed = 1, total = 3, detail = "正在核对平台规格和限价")))) }
        compose.onNodeWithTag("purchase-section:processing").performClick()
        compose.onNodeWithText("正在准备采购草稿").assertIsDisplayed()
        compose.onNodeWithText("1 / 3").assertIsDisplayed()
        compose.onNodeWithTag("purchase-enroll:p1").assertDoesNotExist()
        screenshot("assistant-purchase-processing-390.png")
    }

    @Test fun stagedWhitelistCountIsDistinctFromCurrentPurchasePlanAndChatToggle() {
        val counts = PurchaseWhitelistCounts(candidateTotal = 385, verifiedTotal = 0,
            reviewTotal = 385, matchedDemandTotal = 0)
        compose.setContent { WorkbenchTheme { PurchaseAssistantScreen(
            PurchaseAssistantUiState(whitelistCounts = counts), AssistantActions()) } }
        compose.onNodeWithTag("purchase-plan-count").assertTextEquals("采购计划 0 项")
        compose.onNodeWithTag("purchase-whitelist-counts")
            .assertTextEquals("白名单候选 385 项 · 已核对 0 项 · 待核对 385 项")
        compose.onNodeWithText("当前需求命中白名单 0 项").assertIsDisplayed()
        compose.onNodeWithText("查看白名单").assertDoesNotExist()
        compose.onNodeWithTag("purchase-toggle-chat").performClick()
        compose.onNodeWithText("返回计划").assertIsDisplayed()
    }

    @Test fun whitelistBlockedOrServerDisabledItemNeverAllowsPurchase() {
        val blocked = purchase().copy(id = "p2", eligible = false, canEnroll = false, reason = "商品单位待核对")
        compose.setContent { WorkbenchTheme { PurchaseAssistantScreen(PurchaseAssistantUiState(items = listOf(purchase().copy(canEnroll = false), blocked)), AssistantActions()) } }
        compose.onNodeWithTag("purchase-enroll:p1").assertIsNotEnabled()
        compose.onNodeWithTag("purchase-section:review").performClick()
        compose.onNodeWithText("商品单位待核对").assertIsDisplayed()
        compose.onNodeWithTag("purchase-enroll:p2").assertIsNotEnabled()
    }

    @Test fun legacyWaitingWithoutPlanIsWaitingAndNeverShowsAutomaticExecutionProgress() {
        val waiting = purchase().copy(state = "waiting")
        compose.setContent { WorkbenchTheme { PurchaseAssistantScreen(PurchaseAssistantUiState(items = listOf(waiting),
            job = AssistantJobUi("ghost-job", "running", "自动执行中")), AssistantActions()) } }
        compose.onNodeWithText("处理中 0").assertIsDisplayed()
        compose.onNodeWithTag("assistant-job:ghost-job").assertDoesNotExist()
        compose.onNodeWithTag("purchase-section:review").performClick()
        compose.onNodeWithText("等待").assertIsDisplayed()
        compose.onNodeWithText("已加入计划").assertDoesNotExist()
        compose.onNodeWithTag("purchase-enroll:p1").assertIsEnabled()
    }

    @Test fun knownPlanWithMissingJobIsStillEnrolledButDoesNotInventProgress() {
        val enrolled = purchase().copy(state = "waiting", canEnroll = false, planId = "plan-1")
        compose.setContent { WorkbenchTheme { PurchaseAssistantScreen(PurchaseAssistantUiState(items = listOf(enrolled)), AssistantActions()) } }
        compose.onNodeWithText("处理中 1").assertIsDisplayed()
        compose.onNodeWithTag("purchase-section:processing").performClick()
        compose.onNodeWithText("已加入计划").assertIsDisplayed()
        compose.onNodeWithText("已加入计划，执行任务正在核对").assertIsDisplayed()
        compose.onNodeWithTag("purchase-enroll:p1").assertDoesNotExist()
    }

    @Test fun refreshedCandidateKeepsFailureDetailAndJoinUsesItsCurrentVersion() {
        var joined: Pair<String, Int>? = null
        val refreshed = purchase().copy(version = 8, lastFailure = "上次加入失败：任务已更新")
        compose.setContent { WorkbenchTheme { PurchaseAssistantScreen(PurchaseAssistantUiState(items = listOf(refreshed)),
            AssistantActions(enrollPurchaseItem = { id, version -> joined = id to version })) } }
        compose.onNodeWithTag("purchase-failure:p1").assertTextEquals("上次加入失败：任务已更新")
        compose.onNodeWithTag("purchase-enroll:p1").performClick()
        assertEquals("p1" to 8, joined)
    }

    @Test fun assistantEntryHasRolePermissionAndPreservesFourTabs() {
        var opened = 0
        var state by mutableStateOf(boardState(ROLE_SALES))
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state), actions(),
            assistantActions = AssistantActions(openSales = { opened++ })) }
        compose.onNodeWithTag("open-sales-assistant").performClick()
        assertEquals(1, opened)
        compose.onNodeWithText("销售录单").assertDoesNotExist()
        listOf("任务", "订单", "库存", "设备").forEach { compose.onNodeWithText(it).assertIsDisplayed() }
        compose.runOnIdle { state = state.copy(me = CurrentUser("u1", "pack", "包装", listOf(ROLE_STOCK_PACKING))) }
        compose.onNodeWithTag("open-sales-assistant").assertDoesNotExist()
    }

    @Test fun overlayPausesBoardPollingAndClosingKeepsTaskScope() {
        var visible by mutableStateOf(true)
        val values = mutableListOf<Boolean>()
        val state = boardState(ROLE_SALES).copy(boardKeyword = "M6", boardSection = "in_progress")
        compose.setContent { WorkbenchTheme { DailyWorkScreen(state, actions().copy(dailyWorkVisible = { values += it }), visible = visible) } }
        compose.runOnIdle { assertTrue(values.last()); visible = false }
        compose.runOnIdle { assertFalse(values.last()); visible = true }
        compose.runOnIdle { assertTrue(values.last()) }
        compose.onNodeWithText("处理中 0").assertIsDisplayed()
    }

    @Test @Config(qualifiers = "w320dp-h740dp-mdpi") fun narrowLargeFontDraftStillHasAccessibleConfirmation() {
        compose.setContent { CompositionLocalProvider(LocalDensity provides Density(1f, 1.3f)) {
            WorkbenchTheme { SalesAssistantScreen(SalesAssistantUiState(session = SalesSessionUi("s1", "青岛烁然"), draft = draft()), AssistantActions()) }
        } }
        compose.onNodeWithTag("sales-confirm-draft").performScrollTo().assertIsDisplayed()
        screenshot("assistant-sales-draft-320-large.png")
    }

    private fun draft() = SalesDraftUi("d1", 3, customerName = "青岛烁然", salesOwnerName = "销售一", customerOrderNo = "客户单 001",
        lines = listOf(SalesDraftLineUi("l1", "UL0006", "内六角圆柱头螺钉", "M6×30 · 8.8", "100", "件", "0.8", "主仓")), canConfirm = true)
    private fun productPlan() = SalesNewProductPlanUi("plan-1", "ready_for_confirmation", "new-1", "codex-1", "result-1",
        "plan-hash", "非标螺栓", "M6×35", "8.8级", "NEW-001", "支", "非标件 / 螺栓", canConfirm = true)
    private fun purchase() = PurchasePlanItemUi("p1", 7, "UL0006", "内六角圆柱头螺钉", "M6×30 · 8.8",
        quantity = "100", unit = "件", warehouse = "主仓", platformName = "采购平台", priceLimit = "0.80 元／件",
        sourceOrderCount = 2, eligible = true, canEnroll = true)
    private fun boardState(role: String) = DailyWorkState(me = CurrentUser("u1", "worker", "员工", listOf(role)), selectedRole = role,
        boardSection = defaultTaskBoardSection(role), board = TaskBoardPage(dataVersion = "d1", taskVersion = "t1"))
    private fun actions() = WorkbenchActions(navigate = {}, search = { _, _ -> }, changeScope = { _, _ -> }, toggleSelection = {}, selectAll = {}, loadMore = {},
        printSelected = {}, printAll = {}, cancelPrint = {}, requestSplit = {}, dismissSplit = {}, editingChanged = {},
        scanPrinters = {}, connectPrinter = {}, disconnectPrinter = {}, saveServer = {}, checkServer = {}, clearMessage = {})
    private fun screenshot(name: String, dialog: Boolean = false) {
        val target = File("build/test-screenshots/$name"); target.parentFile?.mkdirs()
        compose.runOnIdle {
            val activityView = compose.activity.window.decorView
            val view = if (dialog) WindowInspector.getGlobalWindowViews().last { it !== activityView && it.width > 0 && it.height > 0 } else activityView
            val bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
            view.draw(Canvas(bitmap))
            target.outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
        }
    }
}
