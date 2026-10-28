package com.ul.workbench.ui.screens

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.ul.workbench.ui.*
import com.ul.workbench.ui.components.*
import com.ul.workbench.ui.theme.WorkbenchColors
import com.ul.workbench.ui.theme.WorkbenchTokens
import java.util.UUID

private data class DraftConfirmation(val draft: SalesDraftUi, val submit: Boolean, val sessionId: String?)
private data class NewProductConfirmation(val plan: SalesNewProductPlanUi, val sessionId: String?, val version: Int, val dataVersion: String)
private data class CodexDraftReview(val job: SalesCodexJobUi, val sessionId: String?, val version: Int, val dataVersion: String)

/** Input stays in its scoped controller. Closing the panel never clears the draft. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SalesAssistantScreen(state: SalesAssistantUiState, actions: AssistantActions,
                         modifier: Modifier = Modifier, onPickAttachment: () -> Unit = {},
                         webOperations: List<WebOperationUi> = emptyList(), webOperationsError: String? = null) {
    if (state.editor != null && state.editorOpen) {
        SalesDraftEditor(state.editor, state.draft, state.editorBusy || state.loading,
            state.editorError ?: state.error, actions, modifier, state.pendingDraftSave)
        return
    }
    var sessionsOpen by remember { mutableStateOf(false) }
    var newSessionOpen by rememberSaveable { mutableStateOf(false) }
    var newSessionCustomer by rememberSaveable { mutableStateOf("") }
    var creatingSession by rememberSaveable { mutableStateOf(false) }
    var previousSessionId by rememberSaveable { mutableStateOf<String?>(null) }
    var confirmation by remember { mutableStateOf<DraftConfirmation?>(null) }
    var discardOpen by remember { mutableStateOf(false) }
    var newProductConfirmation by remember { mutableStateOf<NewProductConfirmation?>(null) }
    var codexDraftReview by remember { mutableStateOf<CodexDraftReview?>(null) }
    val uploadLocked = state.draft?.let { it.status !in setOf("draft", "confirmed") || it.erpOrderNo.isNotBlank() } == true
    val codexWaiting = state.pendingCodexOperation.isNotBlank() || state.pendingNewProductOperation.isNotBlank() ||
        state.codexJob?.running == true || state.newProductPlans.any { it.running }
    val codexBlocksConfirmation = state.pendingCodexOperation.isNotBlank() || state.pendingNewProductOperation.isNotBlank() ||
        state.draft?.let { state.codexJob?.blocksConfirmation(it.version) } == true
    LaunchedEffect(newSessionOpen, newSessionCustomer) {
        if (newSessionOpen) actions.searchSalesCustomers(newSessionCustomer.trim())
    }
    LaunchedEffect(state.session?.id, state.busy) {
        if (creatingSession && state.session != null && state.session.id != previousSessionId) {
            creatingSession = false; newSessionOpen = false; newSessionCustomer = ""
        }
    }
    val listState = rememberLazyListState()
    Column(modifier.fillMaxSize().testTag("sales-assistant")) {
        AssistantHeader("销售录单", actions.close, actions.refreshSales, state.loading || state.busy)
        Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.weight(1f)) {
                TextButton(onClick = { sessionsOpen = true }, enabled = !state.busy) {
                    Text(state.session?.title?.ifBlank { "当前会话" } ?: "选择会话", maxLines = 1,
                        overflow = TextOverflow.Ellipsis)
                }
                DropdownMenu(sessionsOpen, { sessionsOpen = false }) {
                    if (state.sessions.isEmpty()) DropdownMenuItem(text = { Text("还没有录单会话") }, onClick = {}, enabled = false)
                    state.sessions.forEach { session ->
                        DropdownMenuItem(text = { Column {
                            Text(session.title.ifBlank { "录单会话" })
                            if (session.updatedAt.isNotBlank()) Text(session.updatedAt, style = MaterialTheme.typography.labelSmall)
                        } }, onClick = { sessionsOpen = false; actions.selectSalesSession(session.id) })
                    }
                }
            }
            TextButton(onClick = { newSessionOpen = true }, enabled = !state.busy && !state.loading && state.pendingDocumentAdoptionId.isBlank(),
                modifier = Modifier.testTag("sales-new-session")) { Text("新建") }
        }
        if (!newSessionOpen) state.error?.takeIf(String::isNotBlank)?.let {
            Box(Modifier.fillMaxWidth().padding(horizontal = 12.dp).testTag("sales-conversation-error")) { StateNotice(it, error = true) }
        }
        if (state.loading || state.busy) LinearProgressIndicator(Modifier.fillMaxWidth(), color = WorkbenchColors.Red)
        LazyColumn(Modifier.weight(1f).fillMaxWidth().testTag("sales-conversation"), state = listState,
            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 10.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            state.notice?.let { item("notice") { StateNotice(it) } }
            if (state.hasLocalDraftEdit || state.pendingDraftSave) item("local-edit") {
                WorkbenchCard {
                    Text(if (state.pendingDraftSave) "原保存结果待核对，本地输入已保留。" else "有尚未保存的编辑，可以继续修改。",
                        style = MaterialTheme.typography.bodyMedium)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TextButton(onClick = actions.beginSalesDraftEdit, enabled = !state.busy,
                            modifier = Modifier.testTag("sales-resume-local-edit")) { Text("继续编辑") }
                        if (state.pendingDraftSave) TextButton(onClick = actions.retryUnknownSalesDraftSave, enabled = !state.busy,
                            modifier = Modifier.testTag("sales-reconcile-draft-save")) { Text("核对原保存结果") }
                        else TextButton(onClick = { discardOpen = true }, enabled = !state.busy) { Text("丢弃本地修改") }
                    }
                }
            }
            if (state.pendingDocumentAdoptionId.isNotBlank()) item("document-adoption-pending") {
                WorkbenchCard {
                    Text("原资料加入结果尚未确认；原请求已保留，不会自动重复加入。", style = MaterialTheme.typography.bodySmall)
                    TextButton(onClick = actions.retryUnknownSalesDocumentAdoption, enabled = !state.busy && !state.hasLocalDraftEdit && !state.pendingDraftSave,
                        modifier = Modifier.testTag("sales-reconcile-document-adoption")) { Text("核对原加入结果") }
                }
            }
            if (state.pendingCodexOperation.isNotBlank()) item("codex-operation-pending") {
                WorkbenchCard(Modifier.testTag("sales-codex-pending")) {
                    Text("原 Codex 操作结果待核对；资料和原请求已保存，不会自动重新分析或采用。", style = MaterialTheme.typography.bodySmall)
                    TextButton(onClick = actions.retryUnknownSalesCodexOperation,
                        enabled = !state.busy && !state.loading && !state.hasLocalDraftEdit && !state.pendingDraftSave && state.pendingDocumentAdoptionId.isBlank() && state.pendingNewProductOperation.isBlank(),
                        modifier = Modifier.testTag("sales-codex-reconcile")) { Text("核对原 Codex 操作") }
                }
            }
            if (state.pendingNewProductOperation.isNotBlank()) item("new-product-operation-pending") {
                WorkbenchCard(Modifier.testTag("sales-new-product-pending")) {
                    Text("原新商品操作结果待核对；请求和参考编码已保留，不会自动重复整理或创建。", style = MaterialTheme.typography.bodySmall)
                    TextButton(onClick = actions.retryUnknownSalesNewProductOperation,
                        enabled = !state.busy && !state.loading && !state.hasLocalDraftEdit && !state.pendingDraftSave &&
                            state.pendingDocumentAdoptionId.isBlank() && state.pendingCodexOperation.isBlank() &&
                            (state.pendingNewProductOperation != "new-product-confirm" || state.newProductAdmin),
                        modifier = Modifier.testTag("sales-new-product-reconcile")) { Text("核对原新商品操作") }
                }
            }
            if (webOperations.isNotEmpty() || webOperationsError != null) item("web-operations") {
                WebOperationProgress(webOperations, webOperationsError, actions.refreshWebOperations)
            }
            if (state.session?.messages.isNullOrEmpty()) item("empty") {
                WorkbenchCard {
                    Text("把客户订单发到这里", style = MaterialTheme.typography.titleMedium)
                    Text("发送文字、照片、Excel 或 PDF，N150 Codex 会按原录单流程整理并匹配商品；缺少的信息在这里补充。采用草稿后再核对和确认上传。",
                        style = MaterialTheme.typography.bodyMedium, color = WorkbenchColors.Muted)
                    if (state.session == null) Text("点“新建”开始；以前的会话和草稿会保留。",
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                }
            }
            items(state.session?.messages.orEmpty(), key = { "message:${it.id}" }) { message ->
                MessageCard(message)
            }
            state.codexJob?.let { job -> item("codex:${job.id}") { SalesCodexCard(job, state, actions, uploadLocked) {
                codexDraftReview = CodexDraftReview(job, state.session?.id, state.draft?.version ?: job.baseVersion,
                    state.session?.dataVersion.orEmpty())
            } } }
            items(state.newProductPlans, key = { "new-product:${it.id}" }) { plan ->
                SalesNewProductPlanCard(plan, state, actions, uploadLocked) {
                    newProductConfirmation = NewProductConfirmation(plan, state.session?.id,
                        state.draft?.version ?: 0, state.session?.dataVersion.orEmpty())
                }
            }
            items(state.session?.documentAttachments.orEmpty(), key = { "document:${it.id}" }) { attachment ->
                SalesDocumentCard(attachment, state, actions)
            }
            state.job?.let { job -> item("job:${job.id}") { AssistantJobCard(job, actions.refreshJob) } }
            state.draft?.let { draft -> item("draft:${draft.id}") {
                SalesDraftCard(draft.copy(canConfirm = draft.canConfirm && !state.hasLocalDraftEdit && !state.pendingDraftSave && state.pendingDocumentAdoptionId.isBlank() && !codexBlocksConfirmation,
                    canSubmit = draft.canSubmit && !state.hasLocalDraftEdit && !state.pendingDraftSave && state.pendingDocumentAdoptionId.isBlank() && !codexBlocksConfirmation), state.busy || state.pendingDocumentAdoptionId.isNotBlank(), onEdit = actions.beginSalesDraftEdit,
                    onConfirm = { confirmation = DraftConfirmation(draft, false, state.session?.id) },
                    onSubmit = { confirmation = DraftConfirmation(draft, true, state.session?.id) })
            } }
        }
        Surface(color = WorkbenchColors.Paper, border = BorderStroke(0.5.dp, WorkbenchColors.Line)) {
            Column(Modifier.fillMaxWidth().padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Column(Modifier.fillMaxWidth().heightIn(max = 150.dp).verticalScroll(rememberScrollState())) {
                state.attachments.forEach { attachment ->
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text(attachment.name, style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            Text(attachment.detail.ifBlank { attachmentStatus(attachment.status) },
                                style = MaterialTheme.typography.labelSmall,
                                color = if (attachment.status == "failed") WorkbenchColors.Error else WorkbenchColors.Muted)
                        }
                        if (attachment.status in setOf("pending", "failed")) TextButton(onClick = { actions.retrySalesAttachment(attachment.id) },
                            enabled = !state.busy, modifier = Modifier.testTag("retry-attachment:${attachment.id}")) { Text("重试") }
                        TextButton(onClick = { actions.removeSalesAttachment(attachment.id) }, enabled = !state.busy,
                            modifier = Modifier.testTag("remove-attachment:${attachment.id}")) { Text("移除") }
                    }
                }
                }
                OutlinedTextField(state.input, actions.updateSalesInput, Modifier.fillMaxWidth().testTag("sales-input"),
                    label = { Text("告诉录单助手，或补充它询问的信息") }, minLines = 1, maxLines = 4,
                    enabled = state.session != null && !state.busy && !uploadLocked)
                if (state.session != null && !state.codexEnabled) Text("N150 Codex 录单尚未就绪，输入和资料会保留。",
                    style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted, modifier = Modifier.testTag("sales-codex-unavailable"))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = onPickAttachment, enabled = state.session != null && !state.busy && !state.hasLocalDraftEdit && !state.pendingDraftSave && state.pendingDocumentAdoptionId.isBlank() && !uploadLocked && !codexWaiting,
                        modifier = Modifier.testTag("sales-attach")) { Text("照片／文件") }
                    Spacer(Modifier.weight(1f))
                    Button(onClick = actions.sendSalesMessage,
                        enabled = state.session != null && state.codexEnabled && !state.busy && !state.loading && !state.hasLocalDraftEdit && !state.pendingDraftSave && state.pendingDocumentAdoptionId.isBlank() && !uploadLocked && !codexWaiting &&
                            (state.input.isNotBlank() || state.attachments.isNotEmpty() || !state.draft?.lines.isNullOrEmpty() ||
                                !state.session.documentAttachments.isEmpty() || state.session.messages.any { it.attachments.isNotEmpty() }) &&
                            state.attachments.none { it.status in setOf("uploading", "failed", "pending") },
                        modifier = Modifier.testTag("sales-send"), shape = WorkbenchTokens.ControlShape) { Text(if (state.busy) "处理中…" else "发送给 Codex") }
                }
            }
        }
    }
    if (newSessionOpen) Dialog(onDismissRequest = { if (!state.busy) { newSessionOpen = false; creatingSession = false } },
        properties = DialogProperties(usePlatformDefaultWidth = false, dismissOnClickOutside = !state.busy)) {
        Surface(Modifier.padding(18.dp).fillMaxWidth().heightIn(max = 480.dp), shape = WorkbenchTokens.CardShape,
            color = WorkbenchColors.Paper) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Column(Modifier.weight(1f, fill = false).verticalScroll(rememberScrollState()).testTag("sales-new-session-form"),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("新建销售录单", style = MaterialTheme.typography.titleLarge)
            if (state.accountLabel.isNotBlank()) Text("账套：${state.accountLabel}", style = MaterialTheme.typography.bodySmall)
            OutlinedTextField(newSessionCustomer, { newSessionCustomer = it }, Modifier.fillMaxWidth().testTag("sales-new-customer"),
                label = { Text("搜索或填写完整客户名称") }, enabled = !state.busy && !state.loading, singleLine = true)
            Text("从已确认固定归属的客户中选择完整名称；手输名称仍会由服务器核验。",
                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            state.createError?.let { StateNotice(it, error = true) }
            TextButton(onClick = { creatingSession = false; actions.refreshSales() },
                enabled = !state.busy && !state.loading,
                modifier = Modifier.testTag("sales-refresh-create")) { Text("刷新核对已有录单（保留客户名称）") }
            if (state.customerChoicesLoading) LinearProgressIndicator(Modifier.fillMaxWidth())
            state.customerChoicesError?.let { StateNotice(it, error = true) }
            if (!state.customerChoicesLoading && state.customerChoicesError == null) {
                if (state.customerChoices.isEmpty()) Text("没有匹配的已确认客户；请核对关键词或完整名称。",
                    style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                else {
                    Text("已确认客户 ${state.customerChoicesTotal} 项${if (state.customerChoicesHasMore) "，继续输入关键词缩小范围" else ""}",
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    state.customerChoices.take(8).forEach { choice ->
                        TextButton(onClick = { newSessionCustomer = choice.customerName },
                            enabled = !state.busy && !state.loading,
                            modifier = Modifier.fillMaxWidth().testTag("sales-customer-choice:${choice.customerName}")) {
                            Column(Modifier.fillMaxWidth()) {
                                Text(choice.customerName, maxLines = 2, overflow = TextOverflow.Ellipsis)
                                if (choice.ownerName.isNotBlank()) Text("固定销售：${choice.ownerName}",
                                    style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                            }
                        }
                    }
                    if (state.customerChoices.size > 8) Text("仅显示前 8 项，请继续输入关键词查找。",
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                }
            }
            state.sessions.filter { it.title == newSessionCustomer.trim() }.take(3).forEach { session ->
                TextButton(onClick = { actions.selectSalesSession(session.id); newSessionOpen = false; creatingSession = false },
                    enabled = !state.busy && !state.loading,
                    modifier = Modifier.testTag("sales-open-existing:${session.id}")) {
                    Text("打开近期同名会话：${session.updatedAt.ifBlank { session.title }}")
                }
            }
        }
            HorizontalDivider(color = WorkbenchColors.Line)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            TextButton(onClick = { newSessionOpen = false; creatingSession = false }, enabled = !state.busy && !state.loading,
                modifier = Modifier.testTag("sales-cancel-create")) { Text("取消") }
            TextButton(onClick = {
            previousSessionId = state.session?.id; creatingSession = true; actions.newSalesSession(newSessionCustomer.trim())
            }, enabled = !state.busy && !state.loading && newSessionCustomer.isNotBlank(),
                modifier = Modifier.testTag("sales-create-session")) { Text(if (state.busy) "核验中…" else "开始录单") }
            }
        }
        }
    }
    confirmation?.let { frozen ->
        val draft = frozen.draft
        val current = state.draft
        val sameVersion = current != null && current.id == draft.id && current.version == draft.version && state.session?.id == frozen.sessionId
        val allowed = current != null && sameVersion && !state.busy && !state.hasLocalDraftEdit && !state.pendingDraftSave && state.pendingDocumentAdoptionId.isBlank() && !codexBlocksConfirmation && if (frozen.submit)
            current.canSubmit && current.confirmedVersion == draft.version else current.canConfirm
        AlertDialog(onDismissRequest = { confirmation = null }, title = { Text(if (frozen.submit) "自动上传这份已确认订单？" else "确认这份草稿？") },
            text = { LazyColumn(Modifier.heightIn(max = 420.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (!sameVersion) item { StateNotice("草稿已经更新，请关闭后核对最新版本。", error = true) }
                item { DraftReviewHeader("第 ${draft.version} 版", draft) }
                item { Text(if (frozen.submit) "使用已确认的这一版生成表格，校验通过后自动网页上传；不执行 ERP 回查。" else "确认后固定这一版；再修改内容时，需要重新确认。") }
                items(draft.lines, key = { it.id }) { line -> DraftReviewLine(line) }
                items(draft.sourceRows.filter { it.status == "excluded_by_user" }, key = { "excluded:${it.id}" }) { source ->
                    Text("排除资料：${source.label} · ${source.reason}", style = MaterialTheme.typography.bodySmall)
                }
            } }, confirmButton = {
                TextButton(onClick = {
                    if (allowed) {
                        if (frozen.submit) actions.submitSalesDraft(draft.id, draft.version)
                        else actions.confirmSalesDraft(draft.id, draft.version)
                        confirmation = null
                    }
                }, enabled = allowed, modifier = Modifier.testTag("sales-confirm-frozen")) { Text(if (frozen.submit) "确认上传" else "确认第 ${draft.version} 版") }
            }, dismissButton = { TextButton(onClick = { confirmation = null }) { Text("继续核对") } })
    }
    if (discardOpen) AlertDialog(onDismissRequest = { discardOpen = false }, title = { Text("丢弃本地修改？") },
        text = { Text("尚未保存的修改会移除，之后重新打开服务器上的草稿。") },
        confirmButton = { TextButton(onClick = { actions.discardSalesDraftEdit(); discardOpen = false }) { Text("确认丢弃") } },
        dismissButton = { TextButton(onClick = { discardOpen = false }) { Text("保留修改") } })
    codexDraftReview?.let { frozen ->
        val current = state.codexJob
        val same = current != null && current.id == frozen.job.id && current.resultHash == frozen.job.resultHash &&
            state.session?.id == frozen.sessionId && state.session?.dataVersion == frozen.dataVersion &&
            (state.draft?.version ?: current.baseVersion) == frozen.version && current.baseVersion == frozen.version
        val allowed = same && current?.canAdopt == true && current.status == "ready_for_review" &&
            current.newProductSuggestions.isEmpty() && !state.busy && !state.loading && !state.hasLocalDraftEdit &&
            !state.pendingDraftSave && state.pendingDocumentAdoptionId.isBlank() && state.pendingCodexOperation.isBlank() &&
            state.pendingNewProductOperation.isBlank() && state.newProductPlans.none { it.running } && !uploadLocked
        Dialog(onDismissRequest = { codexDraftReview = null }, properties = DialogProperties(usePlatformDefaultWidth = false)) {
            Surface(Modifier.fillMaxWidth().fillMaxHeight(0.92f).padding(horizontal = 12.dp), shape = WorkbenchTokens.CardShape,
                color = WorkbenchColors.Paper) {
                Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("核对 Codex 草稿", style = MaterialTheme.typography.titleLarge)
                    Text("请对照原资料和选定编码；采用表示已核对本次匹配与订单信息。之后仍需确认订单和上传。",
                        style = MaterialTheme.typography.bodySmall)
                    LazyColumn(Modifier.weight(1f).testTag("sales-codex-review-lines"), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                        if (!same) item { StateNotice("结果或数据已经变化，请返回核对最新草稿。", error = true) }
                        frozen.job.preview?.let { preview ->
                            item {
                                Text("${preview.customerName.ifBlank { state.session?.title.orEmpty() }} · ${preview.lines.size} 行", style = MaterialTheme.typography.titleSmall)
                                if (preview.customerOrderNo.isNotBlank()) Text("客户订单号：${preview.customerOrderNo}")
                                if (preview.dueDate.isNotBlank()) Text("整单交期：${preview.dueDate}")
                                Text("报价口径：${codexTaxLabel(preview.taxBasis)} · 税率 ${preview.taxRate}%", style = MaterialTheme.typography.bodySmall)
                                if (preview.note.isNotBlank()) Text("人工说明：${preview.note}", style = MaterialTheme.typography.bodySmall)
                            }
                            items(preview.lines.mapIndexed { index, line -> index to line }, key = { it.first }) { (index, line) ->
                                CodexReviewLine(line, index + 1, preview.dueDate)
                            }
                        }
                    }
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                        TextButton(onClick = { codexDraftReview = null }, modifier = Modifier.weight(1f).testTag("sales-codex-review-dismiss")) { Text("返回继续核对") }
                        Button(onClick = { if (allowed) { actions.adoptSalesCodexDraft(frozen.job.id, frozen.job.resultHash); codexDraftReview = null } },
                            enabled = allowed, modifier = Modifier.weight(1f).testTag("sales-codex-adopt-frozen")) { Text("确认采用这份草稿") }
                    }
                }
            }
        }
    }
    newProductConfirmation?.let { frozen ->
        val current = state.newProductPlans.firstOrNull { it.id == frozen.plan.id }
        val same = current != null && current.planHash == frozen.plan.planHash && current.status == "ready_for_confirmation" &&
            state.session?.id == frozen.sessionId && state.session?.dataVersion == frozen.dataVersion && state.draft?.version == frozen.version
        val allowed = same && current?.canConfirm == true && state.newProductAdmin && !state.busy && !state.loading &&
            !state.hasLocalDraftEdit && !state.pendingDraftSave && state.pendingDocumentAdoptionId.isBlank() &&
            state.pendingCodexOperation.isBlank() && state.pendingNewProductOperation.isBlank() && !uploadLocked
        AlertDialog(onDismissRequest = { newProductConfirmation = null }, title = { Text("确认新建这个商品编码？") },
            text = { Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (!same) StateNotice("方案或数据已经变化，请关闭后重新核对。", error = true)
                NewProductPlanFields(frozen.plan)
                Text("只创建这一项商品，不会自动录单。创建成功后重新核对编码，再采用和确认订单草稿。")
            } }, confirmButton = { TextButton(onClick = {
                if (allowed) { actions.confirmSalesNewProduct(frozen.plan.id, frozen.plan.planHash); newProductConfirmation = null }
            }, enabled = allowed, modifier = Modifier.testTag("sales-new-product-confirm-frozen")) { Text("确认新建编码") } },
            dismissButton = { TextButton(onClick = { newProductConfirmation = null }) { Text("继续核对") } })
    }
}

@Composable
private fun SalesCodexCard(job: SalesCodexJobUi, state: SalesAssistantUiState, actions: AssistantActions, uploadLocked: Boolean, onReview: () -> Unit) {
    WorkbenchCard(Modifier.testTag("sales-codex:${job.id}")) {
        Text("N150 Codex 录单助手", style = MaterialTheme.typography.titleMedium)
        Text(when (job.status) {
            "queued" -> "已排队 · 等待整理资料"
            "running" -> "正在分析资料并匹配商品"
            "needs_clarification" -> "请补充信息 · 在下方回复"
            "ready_for_review" -> "草稿建议已就绪 · 尚未采用"
            "failed" -> "本次分析未完成 · 原资料仍保留"
            "stale" -> "资料或草稿已变化 · 旧结果不能采用"
            "adopted" -> "已采用到草稿 · 尚需确认订单"
            else -> "处理状态待核对"
        }, style = MaterialTheme.typography.bodyMedium,
            color = if (job.status in setOf("failed", "stale")) WorkbenchColors.Error else WorkbenchColors.Muted)
        if (job.running) LinearProgressIndicator(Modifier.fillMaxWidth())
        if (job.assistantText.isNotBlank()) Text(job.assistantText, style = MaterialTheme.typography.bodyMedium)
        job.issues.forEach { issue -> Text(issue, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error) }
        job.preview?.let { preview ->
            Text("建议 ${preview.lines.size} 行${preview.customerOrderNo.takeIf(String::isNotBlank)?.let { " · 客户订单号 $it" }.orEmpty()}",
                style = MaterialTheme.typography.bodySmall)
            preview.lines.take(5).forEach { line ->
                Text("${line.productCode.ifBlank { "编码待匹配" }} · ${line.productName.ifBlank { "商品待核对" }} ${line.specification} · ${line.quantity}${line.unit} · ${line.unitPrice}",
                    style = MaterialTheme.typography.bodySmall)
            }
            TextButton(onClick = onReview, modifier = Modifier.testTag("sales-codex:${job.id}:preview")) { Text("查看全部 ${preview.lines.size} 行及原资料") }
        }
        job.newProductSuggestions.forEach { suggestion ->
            val plan = state.newProductPlans.firstOrNull { it.suggestionId == suggestion.id && it.jobId == job.id && it.sourceResultHash == job.resultHash }
            val referenceKey = "${job.id}:${suggestion.id}"
            val canPrepare = suggestion.canPrepare && (plan == null || (plan.status in setOf("blocked", "failed") && plan.canPrepare)) &&
                !state.busy && !state.loading && !state.hasLocalDraftEdit && !state.pendingDraftSave &&
                state.pendingDocumentAdoptionId.isBlank() && state.pendingCodexOperation.isBlank() && state.pendingNewProductOperation.isBlank() && !uploadLocked
            Surface(color = WorkbenchColors.Tinted, shape = WorkbenchTokens.ControlShape,
                modifier = Modifier.fillMaxWidth().testTag("sales-codex-new-product:${suggestion.id}")) {
                Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                    Text(if (plan?.created == true) "已新建编码 · 请重新核对" else "新商品建议 · 待管理员确认", style = MaterialTheme.typography.labelMedium)
                    Text(listOf(suggestion.productName, suggestion.specification).filter(String::isNotBlank).joinToString(" · "),
                        style = MaterialTheme.typography.bodySmall)
                    Text("材质：${suggestion.material.ifBlank { "待补充" }}", style = MaterialTheme.typography.bodySmall)
                    if (suggestion.proposedCode.isNotBlank()) Text("建议编码：${suggestion.proposedCode}", style = MaterialTheme.typography.bodySmall)
                    if (suggestion.reason.isNotBlank()) Text(suggestion.reason, style = MaterialTheme.typography.bodySmall)
                    Text(if (plan?.created == true) "已创建编码：${plan.createdCode}" else "建议尚未创建到 ERP。", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                    OutlinedTextField(state.newProductReferences[referenceKey].orEmpty(),
                        { actions.updateSalesNewProductReference(referenceKey, it.take(100)) }, Modifier.fillMaxWidth()
                            .testTag("sales-new-product-reference:${suggestion.id}"),
                        label = { Text("相近现有商品编码（可选）") }, singleLine = true, enabled = canPrepare)
                    Text("只参考现有分类和编码前缀；不会直接创建商品。", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                    OutlinedButton(onClick = { actions.prepareSalesNewProductSuggestion(job.id, job.resultHash, suggestion.id) },
                        enabled = canPrepare, modifier = Modifier.testTag("sales-new-product-prepare:${suggestion.id}")) {
                        Text(if (plan?.status in setOf("blocked", "failed")) "重新整理编码方案" else "整理编码方案")
                    }
                }
            }
        }
        if (job.status == "ready_for_review") {
            Text("采用只更新录单草稿；不会确认订单或上传 ERP。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            if (job.newProductSuggestions.isNotEmpty()) Text("新商品先完成管理员确认和创建，再重新核对编码，才能采用完整草稿。",
                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            OutlinedButton(onClick = onReview,
                enabled = job.canAdopt && job.preview != null && job.newProductSuggestions.isEmpty() && !state.busy && !state.loading && !state.hasLocalDraftEdit && !state.pendingDraftSave &&
                    state.pendingDocumentAdoptionId.isBlank() && state.pendingCodexOperation.isBlank() && state.pendingNewProductOperation.isBlank() && !uploadLocked,
                modifier = Modifier.testTag("sales-codex:${job.id}:adopt")) { Text("核对后采用草稿") }
        }
        TextButton(onClick = actions.refreshSales, enabled = !state.busy && !state.loading,
            modifier = Modifier.testTag("sales-codex:${job.id}:refresh")) { Text("刷新助手进度") }
    }
}

private fun codexTaxLabel(basis: String) = when (basis) {
    "tax_inclusive" -> "含税原报价"
    "tax_exclusive", "untaxed" -> "未税原报价"
    else -> "原报价口径待核对"
}

@Composable
private fun CodexReviewLine(line: SalesDraftLineUi, rowNumber: Int, fallbackDueDate: String) {
    var expanded by rememberSaveable(line.id, rowNumber) { mutableStateOf(false) }
    val original = line.previewOriginal ?: line.sourceReview?.original ?: SalesSourceProductUi(line.productName, line.specification,
        grade = line.grade, surface = line.surface)
    val catalog = line.previewCatalog ?: line.sourceReview?.catalog
    WorkbenchCard(Modifier.testTag("sales-codex-review-line:$rowNumber")) {
        Text("第 $rowNumber 行 · 原资料", style = MaterialTheme.typography.labelMedium)
        Text("${original.productName.ifBlank { "品名待核对" }} · ${original.spec.ifBlank { "规格待核对" }}", style = MaterialTheme.typography.bodyMedium)
        Text("原资料材质：${line.originalMaterial.ifBlank { "未注明" }}", style = MaterialTheme.typography.bodySmall)
        Text(listOf("标准" to original.standard, "等级" to original.grade, "表面处理" to original.surface)
            .filter { it.second.isNotBlank() }.joinToString(" · ") { "${it.first}：${it.second}" }.ifBlank { "标准、等级、表面处理未注明" },
            style = MaterialTheme.typography.bodySmall)
        Text("选定 ERP 编码：${line.productCode.ifBlank { "待匹配" }}", style = MaterialTheme.typography.titleSmall)
        Text(listOf(catalog?.productName ?: line.productName, catalog?.spec ?: line.specification)
            .filter(String::isNotBlank).joinToString(" · "), style = MaterialTheme.typography.bodySmall)
        Text("数量：${line.quantity} ${line.unit.ifBlank { "支" }} · 原报价：${line.unitPrice.ifBlank { "未注明" }} · ${codexTaxLabel(line.taxBasis)} · ${line.taxRate}%",
            style = MaterialTheme.typography.bodySmall)
        Text("交期：${line.dueDate.ifBlank { fallbackDueDate }.ifBlank { "未注明" }}", style = MaterialTheme.typography.bodySmall)
        if (line.customerLineNo.isNotBlank()) Text("客户行号：${line.customerLineNo}", style = MaterialTheme.typography.bodySmall)
        if (line.projectNo.isNotBlank()) Text("项目号：${line.projectNo}", style = MaterialTheme.typography.bodySmall)
        if (line.drawingNo.isNotBlank()) Text("图号：${line.drawingNo}", style = MaterialTheme.typography.bodySmall)
        if (line.issue.isNotBlank()) Text(line.issue, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
        TextButton(onClick = { expanded = !expanded }, modifier = Modifier.testTag("sales-codex-review-line:$rowNumber:expand")) {
            Text(if (expanded) "收起原文及编码详情" else "展开原文及编码详情")
        }
        if (expanded) {
            if (line.sourceLabel.isNotBlank()) Text("来源：${line.sourceLabel}", style = MaterialTheme.typography.bodySmall)
            Text("原文：${line.sourcePreview.ifBlank { "未提供原文片段，请对照附件" }}", style = MaterialTheme.typography.bodySmall)
            catalog?.let { value ->
                Text(listOf("型号" to value.model, "标准" to value.standard, "等级" to value.grade, "表面处理" to value.surface)
                    .filter { it.second.isNotBlank() }.joinToString(" · ") { "编码${it.first}：${it.second}" }.ifBlank { "编码属性未注明" },
                    style = MaterialTheme.typography.bodySmall)
            }
            if (line.catalogMaterial.isNotBlank()) Text("ERP 资料 Area／材质字段原值：${line.catalogMaterial}", style = MaterialTheme.typography.bodySmall)
            if (line.catalogArea.isNotBlank()) Text("ERP 资料 Area：${line.catalogArea}", style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun NewProductPlanFields(plan: SalesNewProductPlanUi) {
    Text(plan.productName.ifBlank { "商品名称待核对" }, style = MaterialTheme.typography.titleSmall)
    if (plan.specification.isNotBlank()) Text("规格：${plan.specification}", style = MaterialTheme.typography.bodySmall)
    if (plan.model.isNotBlank()) Text("型号：${plan.model}", style = MaterialTheme.typography.bodySmall)
    Text("方案编码：${plan.code.ifBlank { "待整理" }}", style = MaterialTheme.typography.bodySmall)
    Text("基本单位：${plan.baseUnit.ifBlank { "待核对" }}", style = MaterialTheme.typography.bodySmall)
    Text("材质：${plan.material.ifBlank { "待补充" }}", style = MaterialTheme.typography.bodySmall)
    Text("分类：${plan.classificationPath.ifBlank { "待管理员核对" }}", style = MaterialTheme.typography.bodySmall)
}

@Composable
private fun SalesNewProductPlanCard(plan: SalesNewProductPlanUi, state: SalesAssistantUiState, actions: AssistantActions,
                                    uploadLocked: Boolean, onConfirm: () -> Unit) {
    WorkbenchCard(Modifier.testTag("sales-new-product:${plan.id}")) {
        Text("新商品编码方案", style = MaterialTheme.typography.titleMedium)
        Text(when {
            plan.created -> "已新建编码：${plan.createdCode}"
            plan.status == "created" -> "创建凭据待核对 · 请勿重复创建"
            plan.status == "preparing" -> "正在整理分类和编码方案 · 尚未创建"
            plan.status == "needs_review" -> "分类或编码需管理员核对 · 尚未创建"
            plan.status == "ready_for_confirmation" -> "方案已通过预检查 · 待管理员确认"
            plan.status == "queued" -> "确认已收到 · 等待创建预检查"
            plan.status == "creating" -> "正在新建编码 · 请等待结果"
            plan.status == "unknown" -> "创建结果待核对 · 请勿重复创建"
            plan.status == "blocked" -> "预检查未通过 · 请核对方案"
            plan.status == "failed" -> "本次处理未完成 · 原资料保留"
            plan.status == "stale" -> "资料已经变化 · 原方案不能提交"
            else -> "处理状态待核对"
        }, style = MaterialTheme.typography.bodyMedium, color = if (plan.status in setOf("unknown", "blocked", "failed", "stale"))
            WorkbenchColors.Error else WorkbenchColors.Muted)
        if (plan.running) LinearProgressIndicator(Modifier.fillMaxWidth())
        NewProductPlanFields(plan)
        plan.issues.forEach { Text(it, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error) }
        if (plan.errorCode.isNotBlank()) Text("处理提示：${plan.errorCode}", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Error)
        if (plan.status == "ready_for_confirmation") {
            if (state.newProductAdmin) OutlinedButton(onClick = onConfirm,
                enabled = plan.canConfirm && !state.busy && !state.loading && !state.hasLocalDraftEdit && !state.pendingDraftSave &&
                    state.pendingDocumentAdoptionId.isBlank() && state.pendingCodexOperation.isBlank() && state.pendingNewProductOperation.isBlank() && !uploadLocked,
                modifier = Modifier.testTag("sales-new-product:${plan.id}:confirm")) { Text("核对并确认新建编码") }
            else Text("请管理员进入这份会话，核对并确认新建编码。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        }
        if (plan.created) TextButton(onClick = actions.sendSalesMessage,
            enabled = state.codexEnabled && !state.busy && !state.loading && !state.hasLocalDraftEdit && !state.pendingDraftSave &&
                state.pendingDocumentAdoptionId.isBlank() && state.pendingCodexOperation.isBlank() && state.pendingNewProductOperation.isBlank() &&
                state.codexJob?.running != true && state.newProductPlans.none { it.running } && !uploadLocked,
            modifier = Modifier.testTag("sales-new-product:${plan.id}:rematch")) { Text("重新核对编码") }
        TextButton(onClick = actions.refreshSales, enabled = !state.busy && !state.loading,
            modifier = Modifier.testTag("sales-new-product:${plan.id}:refresh")) { Text("刷新新商品进度") }
    }
}

@Composable
private fun SalesDocumentCard(attachment: AssistantAttachmentUi, state: SalesAssistantUiState, actions: AssistantActions) {
    val processing = attachment.documentProcessing ?: return
    WorkbenchCard(Modifier.testTag("sales-document:${attachment.id}")) {
        Text(attachment.name, style = MaterialTheme.typography.titleMedium)
        TextButton(onClick = { actions.viewSalesAttachment(attachment.id) },
            enabled = state.viewingAttachmentId.isBlank() && attachment.mimeType in setOf("application/pdf", "image/png", "image/jpeg", "image/webp") &&
                attachment.sha256.isNotBlank() && attachment.sizeBytes > 0,
            modifier = Modifier.testTag("sales-document:${attachment.id}:view-original")) {
            Text(if (state.viewingAttachmentId == attachment.id) "读取原附件…" else "查看原附件")
        }
        Text(when (processing.status) {
            "queued" -> "已上传 · 等待识别"
            "running" -> "正在识别 · 尚未加入草稿"
            "ready" -> "识别结果待逐行核对"
            "failed" -> "识别未完成 · 附件仍保留"
            "adopted" -> "已加入草稿${processing.adoptedVersion?.let { " · 第 $it 版" }.orEmpty()}"
            else -> "识别状态待核对"
        }, style = MaterialTheme.typography.bodyMedium,
            color = if (processing.status == "failed") WorkbenchColors.Error else WorkbenchColors.Muted)
        if (processing.message.isNotBlank()) Text(processing.message, style = MaterialTheme.typography.bodySmall)
        if (processing.errorCode.isNotBlank()) Text("原因：${processing.errorCode}", style = MaterialTheme.typography.labelSmall,
            color = WorkbenchColors.Error)
        if (processing.pageCount > 0 || processing.lineCount > 0 || processing.candidateCount > 0)
            Text("${processing.pageCount} 页 · ${processing.lineCount} 条原文 · ${processing.candidateCount} 条候选明细",
                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        if (processing.unreadyPages.isNotEmpty()) StateNotice("第 ${processing.unreadyPages.joinToString("、")} 页未完整识别，不能加入草稿。", error = true)
        if (processing.status in setOf("queued", "running", "failed")) TextButton(onClick = actions.refreshSales,
            enabled = !state.busy && !state.loading, modifier = Modifier.testTag("sales-document:${attachment.id}:refresh")) { Text("刷新识别状态") }
        if (processing.status == "ready") {
            Text(if (state.codexEnabled) "原附件已保存；点下方“发送给 Codex”一起整理和匹配商品。"
                else "这里只读取附件；加入后仍须核对原文、商品和数量，才能确认订单。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            if (state.hasLocalDraftEdit || state.pendingDraftSave) Text("先保存或明确丢弃本地草稿修改，再加入这份结果。", style = MaterialTheme.typography.bodySmall,
                color = WorkbenchColors.Error)
            if (!state.codexEnabled) OutlinedButton(onClick = { actions.adoptSalesDocument(attachment.id, processing.resultHash) },
                enabled = !state.busy && !state.loading && !state.hasLocalDraftEdit && !state.pendingDraftSave && state.pendingDocumentAdoptionId.isBlank() &&
                    state.pendingCodexOperation.isBlank() && state.codexJob?.running != true &&
                    processing.unreadyPages.isEmpty() && processing.resultHash.isNotBlank(),
                modifier = Modifier.testTag("sales-document:${attachment.id}:adopt")) { Text("加入草稿逐行核对") }
        }
    }
}

@Composable
private fun MessageCard(message: AssistantMessageUi, assistantLabel: String = "录单助手") {
    val mine = message.role in setOf("user", "human")
    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (mine) Arrangement.End else Arrangement.Start) {
        Surface(color = if (mine) WorkbenchColors.Tinted else WorkbenchColors.Paper,
            shape = WorkbenchTokens.CardShape, modifier = Modifier.widthIn(max = 340.dp)) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                Text(if (mine) "你" else assistantLabel, style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                if (message.text.isNotBlank()) Text(message.text, style = MaterialTheme.typography.bodyMedium)
                message.attachments.forEach { attachment ->
                    Text("${attachment.name} · ${attachment.detail.ifBlank { attachmentStatus(attachment.status) }}",
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                }
            }
        }
    }
}

@Composable
private fun SalesDraftCard(draft: SalesDraftUi, busy: Boolean, onEdit: () -> Unit, onConfirm: () -> Unit, onSubmit: () -> Unit) {
    var documentNumbersOpen by remember(draft.id, draft.erpOrderNumbers, draft.erpDocumentCount, draft.erpReferenceScope) { mutableStateOf(false) }
    WorkbenchCard(Modifier.testTag("sales-draft")) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("订单草稿 · 第 ${draft.version} 版", Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
            SmallTag(salesDraftStatus(draft))
        }
        Text(draft.customerName.ifBlank { "客户待核对" }, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.SemiBold)
        if (draft.salesOwnerName.isNotBlank()) Text("销售：${draft.salesOwnerName}", style = MaterialTheme.typography.bodySmall)
        if (draft.customerOrderNo.isNotBlank()) Text("客户单号：${draft.customerOrderNo}", style = MaterialTheme.typography.bodySmall)
        if (draft.dueDate.isNotBlank()) Text("交期：${draft.dueDate}", style = MaterialTheme.typography.bodySmall)
        Text("销售税率 ${draft.taxRate}% · 未注明未税的客户报价按含税", style = MaterialTheme.typography.bodySmall,
            color = WorkbenchColors.Muted)
        draft.lines.forEachIndexed { index, line ->
            HorizontalDivider(color = WorkbenchColors.Line)
            Text("${index + 1}. ${line.productCode} ${line.productName}", style = MaterialTheme.typography.bodyMedium)
            if (line.specification.isNotBlank()) Text(line.specification, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            Text("${line.quantity.ifBlank { "数量待核对" }} ${line.unit}" +
                (if (line.warehouse.isNotBlank()) " · ${line.warehouse}" else ""), style = MaterialTheme.typography.bodySmall)
            if (line.unitPrice.isNotBlank()) {
                Text("客户原报价 ${line.unitPrice}（${taxBasisLabel(line.taxBasis)}）", style = MaterialTheme.typography.bodySmall)
                Text(if (line.untaxedUnitPrice.isNotBlank() && line.untaxedLineAmount.isNotBlank())
                    "未税单价 ${line.untaxedUnitPrice} · 未税行金额 ${line.untaxedLineAmount}"
                    else "未税单价及行金额待服务端核算", style = MaterialTheme.typography.bodySmall,
                    color = WorkbenchColors.Muted)
            }
            if (line.issue.isNotBlank()) Text(line.issue, color = WorkbenchColors.Error, style = MaterialTheme.typography.bodySmall)
            if (line.matchCandidates.isNotEmpty()) Text("有历史候选，编辑核对", style = MaterialTheme.typography.labelSmall,
                color = WorkbenchColors.SecondaryInk)
            if (line.dueDate.isNotBlank()) Text("本行交期 ${line.dueDate}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        }
        draft.issues.forEach { Text(it, color = WorkbenchColors.Error, style = MaterialTheme.typography.bodySmall) }
        if (draft.erpReferenceScope == "first_line_document") {
            if (draft.hasCompleteLineDocumentReceipt) {
                Text("共 ${draft.erpDocumentCount} 张 ERP 单据", style = MaterialTheme.typography.bodyMedium,
                    color = WorkbenchColors.SecondaryInk, modifier = Modifier.testTag("sales-erp-document-count"))
                Text("首条单号：${draft.erpOrderNo}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.SecondaryInk)
                TextButton(onClick = { documentNumbersOpen = true }, modifier = Modifier.testTag("sales-erp-document-numbers")) { Text("查看全部单号") }
            } else {
                Text("单据清单待核对", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
                if (draft.erpOrderNo.isNotBlank()) Text("首条参考单号：${draft.erpOrderNo}", style = MaterialTheme.typography.bodySmall,
                    color = WorkbenchColors.Muted)
            }
        } else if (draft.erpOrderNo.isNotBlank()) Text("单号：${draft.erpOrderNo}", style = MaterialTheme.typography.bodyMedium, color = WorkbenchColors.SecondaryInk)
        if (draft.status in setOf("submission_unknown", "uncertain")) Text("导入结果待核对，勿重复上传", style = MaterialTheme.typography.bodySmall,
            color = WorkbenchColors.Error)
        else if (draft.status == "submitting") Text("正在自动网页上传，请查看执行进度。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        if (draft.erpOrderNo.isBlank() && draft.status !in setOf("submitting", "submitted", "submission_unknown", "submission_blocked", "uncertain")) {
            TextButton(onClick = onEdit, enabled = !busy, modifier = Modifier.testTag("sales-edit-draft")) { Text("编辑客户／商品明细") }
            Text("修改并保存后需要重新确认。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = onConfirm,
                    enabled = !busy && draft.canConfirm && draft.lines.isNotEmpty() && draft.issues.isEmpty() && draft.lines.none { it.issue.isNotBlank() },
                    modifier = Modifier.weight(1f).testTag("sales-confirm-draft"), shape = WorkbenchTokens.ControlShape) { Text("确认草稿") }
                Button(onClick = onSubmit,
                    enabled = !busy && draft.canSubmit && draft.confirmedVersion == draft.version,
                    modifier = Modifier.weight(1f).testTag("sales-submit-draft"), shape = WorkbenchTokens.ControlShape) { Text("自动网页上传") }
            }
            if (!draft.canSubmit && draft.confirmedVersion == draft.version) Text("草稿已确认；正式提交暂不可用。",
                color = WorkbenchColors.Muted, style = MaterialTheme.typography.bodySmall)
        }
    }
    if (documentNumbersOpen && draft.hasCompleteLineDocumentReceipt) AlertDialog(onDismissRequest = { documentNumbersOpen = false },
        title = { Text("本次导入的 ${draft.erpDocumentCount} 张 ERP 单据") },
        text = { LazyColumn(Modifier.heightIn(max = 440.dp).testTag("sales-erp-document-number-list"), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            item { Text("每条原订单明细对应一张单据；同一文件批量导入。首条单号只代表第一张。", style = MaterialTheme.typography.bodySmall) }
            items(draft.erpOrderNumbers.mapIndexed { index, number -> index to number }, key = { it.first }) { (index, number) ->
                Text("${index + 1}. $number", style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.testTag("sales-erp-document-number:${index + 1}"))
            }
        } }, confirmButton = { TextButton(onClick = { documentNumbersOpen = false }) { Text("关闭") } })
}

private fun SalesDraftLineUi.withProductCode(value: String): SalesDraftLineUi =
    if (productCode == value) this else copy(productCode = value, productName = "", specification = "", summaryDimension = "", projectNo = "", drawingNo = "",
        grade = "", surface = "", summaryMetadataConfirmed = false, summaryDimensionSource = "", untaxedUnitPrice = "", untaxedLineAmount = "",
        sourceMatchConfirmed = false, sourceReview = null)

private fun recommendationMessage(value: String, fallback: String): String =
    value.takeIf { it.isNotBlank() && !it.matches(Regex("[A-Za-z][A-Za-z0-9_]*")) } ?: fallback

@Composable
private fun SalesMatchCandidates(line: SalesDraftLineUi, busy: Boolean, onSelect: (String) -> Unit) {
    Column(Modifier.fillMaxWidth().testTag("sales-candidates:${line.id}"), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        if (line.matchCandidates.isEmpty()) {
            val fallback = when (line.matchStatus?.status) {
                null, "", "not_loaded", "pending" -> "历史候选尚未加载，可先核对商品编码。"
                "queued", "running", "loading" -> "正在查询历史候选。"
                "failed", "unavailable" -> "历史候选暂不可用，请核对商品编码。"
                else -> "暂无历史候选，请核对商品编码。"
            }
            Text(recommendationMessage(line.matchStatus?.message.orEmpty(), fallback), style = MaterialTheme.typography.bodySmall,
                color = WorkbenchColors.Muted)
        } else {
            Text("历史候选，需要核对", style = MaterialTheme.typography.labelMedium, color = WorkbenchColors.SecondaryInk)
            Text("选用只修改本行编码；保存后仍需核对目录商品及原文。", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
            line.matchCandidates.take(5).forEachIndexed { index, candidate ->
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("${candidate.productCode.ifBlank { "编码待核对" }} · ${candidate.productName.ifBlank { "商品名称待核对" }}",
                            style = MaterialTheme.typography.bodySmall)
                        val dimensions = listOf(candidate.spec, candidate.model).filter(String::isNotBlank).joinToString(" · ")
                        if (dimensions.isNotBlank()) Text(dimensions, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                        val source = when (candidate.method) {
                            "customer_part_exact" -> "同客户料号历史"
                            "same_customer_history_attributes" -> "同客户规格历史"
                            else -> "推荐来源待核对"
                        }
                        Text(source + (if (candidate.evidenceCount > 0) " · ${candidate.evidenceCount} 次" else "") +
                            (if (candidate.lastOrderDate.isNotBlank()) " · 最近 ${candidate.lastOrderDate}" else ""),
                            style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                        if (candidate.message.isNotBlank() || !candidate.available) Text(
                            recommendationMessage(candidate.message, if (candidate.available) "历史候选，需要核对" else "该候选暂不可选，请核对目录。"),
                            style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                    }
                    TextButton(onClick = { if (candidate.available && candidate.productCode.isNotBlank()) onSelect(candidate.productCode) },
                        enabled = !busy && candidate.available && candidate.productCode.isNotBlank() && candidate.productCode != line.productCode,
                        modifier = Modifier.testTag("edit-line:${line.id}:candidate:$index")) {
                        Text(if (candidate.productCode.isNotBlank() && candidate.productCode == line.productCode) "当前编码" else "选用")
                    }
                }
            }
        }
        line.matchStatus?.let { status ->
            if (status.coveredFrom.isNotBlank() && status.coveredTo.isNotBlank()) Text("历史覆盖：${status.coveredFrom} 至 ${status.coveredTo}",
                style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
        }
    }
}

@Composable
private fun SourceProductReview(line: SalesDraftLineUi, review: SalesSourceReviewUi, busy: Boolean, onConfirm: () -> Unit) {
    Column(Modifier.fillMaxWidth().testTag("source-product-review:${line.id}"), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text("原文与目录商品需核对", style = MaterialTheme.typography.labelMedium, color = WorkbenchColors.Error)
        Text("客户原文：${review.original.productName.ifBlank { "名称未提供" }}", style = MaterialTheme.typography.bodySmall)
        Text("原文规格：${review.original.spec.ifBlank { "未提供" }} · 等级：${review.original.grade.ifBlank { "未提供" }} · 表面：${review.original.surface.ifBlank { "未提供" }}",
            style = MaterialTheme.typography.bodySmall)
        Text("目录 ${line.productCode}：${review.catalog.productName.ifBlank { "名称待核对" }}", style = MaterialTheme.typography.bodySmall)
        Text("目录规格：${review.catalog.spec.ifBlank { "未提供" }} · 型号：${review.catalog.model.ifBlank { "未提供" }}",
            style = MaterialTheme.typography.bodySmall)
        Text("目录等级：${review.catalog.grade.ifBlank { "未提供" }} · 表面：${review.catalog.surface.ifBlank { "未提供" }}", style = MaterialTheme.typography.bodySmall)
        if (review.original.standard.isNotBlank() || review.catalog.standard.isNotBlank()) {
            Text("原文标准：${review.original.standard.ifBlank { "未提供" }}", style = MaterialTheme.typography.bodySmall)
            Text("目录标准：${review.catalog.standard.ifBlank { "未提供" }}", style = MaterialTheme.typography.bodySmall)
        }
        if (review.reasons.isNotEmpty()) Text("核对项：${review.reasons.joinToString("、")}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
        when {
            review.confirmed -> Text("已在服务端核对该商品及这份原文", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            line.sourceMatchConfirmed -> Text("已勾选核对，保存后由服务端验证", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            else -> TextButton(onClick = onConfirm, enabled = !busy && review.evidenceHash.isNotBlank() && line.productCode.isNotBlank(),
                modifier = Modifier.testTag("source-product-review:${line.id}:confirm")) { Text("已核对，使用此商品编码") }
        }
    }
}

@Composable
private fun SalesDraftEditor(draft: SalesDraftUi, latest: SalesDraftUi?, busy: Boolean, error: String?,
                             actions: AssistantActions, modifier: Modifier, pendingSave: Boolean = false) {
    var reviewOpen by remember { mutableStateOf(false) }
    var sourcesOpen by rememberSaveable(draft.id) { mutableStateOf(false) }
    var excludeSource by remember { mutableStateOf<SalesSourceRowUi?>(null) }
    var excludeReason by rememberSaveable { mutableStateOf("") }
    Column(modifier.fillMaxSize().testTag("sales-draft-editor")) {
        AssistantHeader("编辑销售草稿", actions.closeSalesDraftEditor, actions.refreshSales, busy)
        Text("税率固定 13%；客户报价默认按含税，单行可选未税。保存后由服务端核算价格并生成新版本，需要重新确认。", Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
            style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        error?.takeIf(String::isNotBlank)?.let {
            Box(Modifier.fillMaxWidth().padding(horizontal = 12.dp).testTag("sales-editor-error")) { StateNotice(it, error = true) }
        }
        LazyColumn(Modifier.weight(1f).fillMaxWidth().testTag("sales-draft-editor-list"), contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)) {
            if (pendingSave) item("pending-save") {
                WorkbenchCard {
                    Text("原保存请求结果未确认。先核对原保存结果，再核对当前编辑。", style = MaterialTheme.typography.bodySmall)
                    TextButton(onClick = actions.retryUnknownSalesDraftSave, enabled = !busy,
                        modifier = Modifier.testTag("sales-reconcile-draft-save")) { Text("核对原保存结果") }
                }
            }
            if (latest != null && latest.id == draft.id && latest.version > draft.version) item("new-version") {
                WorkbenchCard {
                    StateNotice("服务器草稿已到第 ${latest.version} 版；本地编辑仍是第 ${draft.version} 版。请先对照两份内容。", error = true)
                    TextButton(onClick = { reviewOpen = true }, enabled = !busy,
                        modifier = Modifier.testTag("sales-review-latest-draft")) { Text("核对服务器草稿") }
                }
            }
            item("header") { WorkbenchCard {
                DraftField("客户", draft.customerName, "edit-draft-customer", busy) { actions.updateSalesDraftEdit(draft.copy(customerName = it)) }
                DraftField("客户订单号", draft.customerOrderNo, "edit-draft-order-no", busy) { actions.updateSalesDraftEdit(draft.copy(customerOrderNo = it)) }
                DraftField("整单交期 YYYY-MM-DD（明细可覆盖）", draft.dueDate, "edit-draft-due", busy) { actions.updateSalesDraftEdit(draft.copy(dueDate = it)) }
                DraftField("附加说明（选填）", draft.note, "edit-draft-note", busy) { actions.updateSalesDraftEdit(draft.copy(note = it)) }
            } }
            if (draft.sourceRows.isNotEmpty()) item("source-header") {
                TextButton(onClick = { sourcesOpen = !sourcesOpen }, modifier = Modifier.testTag("sales-source-review")) {
                    Text(if (sourcesOpen) "收起资料核对" else "资料核对 · ${draft.sourceRows.count { it.status == "needs_review" }} 行待核对")
                }
            }
            if (sourcesOpen) items(draft.sourceRows.filter { it.status !in setOf("ignored_with_reason", "parsed_for_review") }, key = { "source:${it.id}" }) { source ->
                WorkbenchCard {
                    Text(source.label.ifBlank { "来源行" }, style = MaterialTheme.typography.labelMedium)
                    Text(source.preview, style = MaterialTheme.typography.bodySmall)
                    Text(when (source.status) {
                        "excluded_by_user" -> "已排除：${source.reason}"
                        "exclude_pending" -> "待保存排除：${source.reason}"
                        else -> "请核对该行；属于订单的明细请在下面补齐。"
                    }, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    if (!source.canExclude && source.status !in setOf("excluded_by_user", "exclude_pending"))
                        Text("这项用于核对资料覆盖，不能作为无关行排除。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
                    if (source.canExclude && source.status !in setOf("excluded_by_user", "exclude_pending")) TextButton(onClick = {
                        excludeSource = source; excludeReason = ""
                    }, enabled = !busy, modifier = Modifier.testTag("source-row:${source.id}:exclude")) { Text("不属于本单") }
                }
            }
            items(draft.lines, key = { it.id }) { line ->
                var summaryOpen by rememberSaveable(line.id) { mutableStateOf(false) }
                fun update(changed: SalesDraftLineUi) = actions.updateSalesDraftEdit(draft.copy(lines = draft.lines.map { if (it.id == line.id) changed else it }))
                WorkbenchCard {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text("第 ${draft.lines.indexOfFirst { it.id == line.id } + 1} 行 · ${line.productName.ifBlank { "商品明细" }}",
                            Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
                        TextButton(onClick = {
                            val source = draft.sourceRows.firstOrNull { it.lineId == line.id && it.status != "excluded_by_user" }
                            if (source != null && source.canExclude) { excludeSource = source; excludeReason = "" }
                            else if (source != null) Unit
                            else actions.updateSalesDraftEdit(draft.copy(lines = draft.lines.filterNot { it.id == line.id }))
                        },
                            enabled = !busy, modifier = Modifier.testTag("remove-draft-line:${line.id}")) { Text("移除本行") }
                    }
                    DraftField("商品编码", line.productCode, "edit-line:${line.id}:sku", busy) {
                        update(line.withProductCode(it))
                    }
                    SalesMatchCandidates(line, busy) { update(line.withProductCode(it)) }
                    if (line.specification.isNotBlank()) Text("原商品规格：${line.specification}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    if (line.sourceLabel.isNotBlank()) {
                        Text("资料来源：${line.sourceLabel}", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                        Text(line.sourcePreview, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    }
                    if (line.sourceAttachmentId.isNotBlank()) TextButton(onClick = { actions.viewSalesAttachment(line.sourceAttachmentId) },
                        enabled = !busy, modifier = Modifier.testTag("edit-line:${line.id}:view-original")) { Text("查看对应原图／PDF") }
                    line.sourceReview?.takeIf { it.required }?.let { review ->
                        SourceProductReview(line, review, busy) { update(line.copy(sourceMatchConfirmed = true)) }
                    }
                    Text("更换编码后，保存时重新核对商品和规格。", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                    DraftField("数量", line.quantity, "edit-line:${line.id}:quantity", busy, decimal = true) {
                        update(line.copy(quantity = it, untaxedUnitPrice = "", untaxedLineAmount = ""))
                    }
                    DraftField("单位", line.unit, "edit-line:${line.id}:unit", busy) { update(line.copy(unit = it)) }
                    DraftField("客户报价单价", line.unitPrice, "edit-line:${line.id}:price", busy, decimal = true) {
                        update(line.copy(unitPrice = it, untaxedUnitPrice = "", untaxedLineAmount = ""))
                    }
                    Text("报价口径", style = MaterialTheme.typography.labelMedium)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        FilterChip(line.taxBasis == "tax_inclusive", onClick = {
                            update(line.copy(taxBasis = "tax_inclusive", untaxedUnitPrice = "", untaxedLineAmount = ""))
                        }, label = { Text("含税报价") }, enabled = !busy,
                            modifier = Modifier.testTag("edit-line:${line.id}:tax-inclusive"))
                        FilterChip(line.taxBasis == "tax_exclusive", onClick = {
                            update(line.copy(taxBasis = "tax_exclusive", untaxedUnitPrice = "", untaxedLineAmount = ""))
                        }, label = { Text("未税报价") }, enabled = !busy,
                            modifier = Modifier.testTag("edit-line:${line.id}:tax-exclusive"))
                    }
                    if (line.untaxedUnitPrice.isNotBlank() && line.untaxedLineAmount.isNotBlank())
                        Text("上次保存的未税单价 ${line.untaxedUnitPrice} · 行金额 ${line.untaxedLineAmount}",
                            style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    DraftField("仓库", line.warehouse, "edit-line:${line.id}:warehouse", busy) { update(line.copy(warehouse = it)) }
                    DraftField("本行交期 YYYY-MM-DD（选填）", line.dueDate, "edit-line:${line.id}:due", busy) { update(line.copy(dueDate = it)) }
                    TextButton(onClick = { summaryOpen = !summaryOpen }, enabled = !busy,
                        modifier = Modifier.testTag("edit-line:${line.id}:summary-toggle")) {
                        Text(if (summaryOpen) "收起摘要补充" else "摘要补充（规格／图号等）")
                    }
                    if (summaryOpen) {
                        Text("自动规格无法核对时，在这里填写。项目、图号、等级、表面处理可按客户资料补充；保存后随整单确认。",
                            style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                        DraftField("摘要规格", line.summaryDimension, "edit-line:${line.id}:dimension", busy) {
                            update(line.copy(summaryDimension = it, summaryMetadataConfirmed = true, summaryDimensionSource = "")) }
                        DraftField("项目号（选填）", line.projectNo, "edit-line:${line.id}:project", busy) {
                            update(line.copy(projectNo = it, summaryMetadataConfirmed = true)) }
                        DraftField("图号（选填）", line.drawingNo, "edit-line:${line.id}:drawing", busy) {
                            update(line.copy(drawingNo = it, summaryMetadataConfirmed = true)) }
                        DraftField("等级（选填）", line.grade, "edit-line:${line.id}:grade", busy) {
                            update(line.copy(grade = it, summaryMetadataConfirmed = true)) }
                        DraftField("表面处理（选填）", line.surface, "edit-line:${line.id}:surface", busy) {
                            update(line.copy(surface = it, summaryMetadataConfirmed = true)) }
                    }
                    if (line.issue.isNotBlank()) Text(line.issue, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
                }
            }
            item("add-line") { OutlinedButton(onClick = {
                actions.updateSalesDraftEdit(draft.copy(lines = draft.lines + SalesDraftLineUi(UUID.randomUUID().toString(), "", "",
                    taxBasis = draft.taxBasis.ifBlank { "tax_inclusive" })))
            }, enabled = !busy, modifier = Modifier.fillMaxWidth().testTag("add-draft-line")) { Text("新增商品明细") } }
        }
        Surface(color = WorkbenchColors.Paper) {
            Row(Modifier.fillMaxWidth().padding(12.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = actions.closeSalesDraftEditor, enabled = !busy, modifier = Modifier.weight(1f)) { Text("返回草稿") }
                Button(onClick = actions.saveSalesDraftEdit, enabled = !busy && !pendingSave && draft.customerName.isNotBlank() && draft.lines.isNotEmpty(),
                    modifier = Modifier.weight(1f).testTag("sales-save-draft-edit")) { Text(if (busy) "保存中…" else if (pendingSave) "等待核对" else "保存修改") }
            }
        }
    }
    excludeSource?.let { source ->
        Dialog(onDismissRequest = { excludeSource = null },
            properties = DialogProperties(usePlatformDefaultWidth = false)) {
            Surface(Modifier.padding(horizontal = 18.dp).fillMaxWidth().height(300.dp),
                shape = WorkbenchTokens.CardShape, color = WorkbenchColors.Paper) {
                Column(Modifier.padding(16.dp).verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("从本单排除资料行", style = MaterialTheme.typography.titleLarge)
                    Text("${source.label}\n${source.preview}", style = MaterialTheme.typography.bodySmall,
                        maxLines = 4, overflow = TextOverflow.Ellipsis)
                    Text("原文会保留。填写原因后，保存草稿时一起生效。", style = MaterialTheme.typography.bodySmall,
                        maxLines = 2, overflow = TextOverflow.Ellipsis)
                    OutlinedTextField(excludeReason, { if (it.length <= 200) excludeReason = it }, label = { Text("排除原因") },
                        singleLine = true, modifier = Modifier.fillMaxWidth().testTag("sales-source-exclude-reason"))
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                        TextButton(onClick = { excludeSource = null }) { Text("取消") }
                        TextButton(onClick = {
                            val reason = excludeReason.trim()
                            actions.updateSalesDraftEdit(draft.copy(lines = draft.lines.filterNot { it.id == source.lineId },
                                sourceRows = draft.sourceRows.map { if (it.id == source.id) it.copy(status = "exclude_pending", reason = reason) else it },
                                sourceActions = draft.sourceActions + SalesSourceActionUi(source.id, reason)))
                            excludeSource = null
                        }, enabled = !busy && excludeReason.trim().isNotEmpty(),
                            modifier = Modifier.testTag("sales-source-exclude-confirm")) { Text("确认排除") }
                    }
                }
            }
        }
    }
    if (reviewOpen && latest != null && latest.id == draft.id && latest.version > draft.version) {
        AlertDialog(onDismissRequest = { reviewOpen = false },
            title = { Text("核对第 ${latest.version} 版与本地编辑") },
            text = { LazyColumn(Modifier.heightIn(max = 420.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                item { Text("服务器已保存的内容如下。继续后会用你手机上的编辑内容保存为新版本，请逐行核对。") }
                item { DraftReviewHeader("服务器第 ${latest.version} 版", latest) }
                items(latest.lines) { line -> DraftReviewLine(line) }
                item { HorizontalDivider(); DraftReviewHeader("手机本地编辑", draft) }
                items(draft.lines) { line -> DraftReviewLine(line) }
            } },
            confirmButton = { TextButton(onClick = { actions.adoptLatestSalesDraftVersion(); reviewOpen = false },
                enabled = !busy, modifier = Modifier.testTag("sales-adopt-latest-version")) { Text("保留本地编辑并使用第 ${latest.version} 版") } },
            dismissButton = { TextButton(onClick = { reviewOpen = false }) { Text("继续核对") } })
    }
}

@Composable
private fun DraftReviewHeader(title: String, draft: SalesDraftUi) {
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(title, style = MaterialTheme.typography.titleSmall)
        Text("客户 ${draft.customerName} · 客户订单号 ${draft.customerOrderNo.ifBlank { "空" }} · 交期 ${draft.dueDate.ifBlank { "空" }}",
            style = MaterialTheme.typography.bodySmall)
        Text("${draft.lines.size} 行 · 默认报价口径 ${taxBasisLabel(draft.taxBasis)}", style = MaterialTheme.typography.bodySmall)
        if (draft.note.isNotBlank()) Text("附加说明：${draft.note}", style = MaterialTheme.typography.bodySmall)
        val amounts = draft.lines.map { it.untaxedLineAmount.toBigDecimalOrNull() }
        if (amounts.isNotEmpty() && amounts.all { it != null }) Text("未税合计 ${amounts.filterNotNull().fold(java.math.BigDecimal.ZERO) { total, amount -> total + amount }} · 税率 13%",
            style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun DraftReviewLine(line: SalesDraftLineUi) {
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text("${line.productCode} · ${line.quantity} ${line.unit} · 报价 ${line.unitPrice}（${taxBasisLabel(line.taxBasis)}） · " +
            "仓库 ${line.warehouse} · 交期 ${line.dueDate.ifBlank { "空" }}", style = MaterialTheme.typography.bodySmall)
        Text("明细 ${line.id.takeLast(8)} · 摘要规格 ${line.summaryDimension.ifBlank { "空" }} · 项目 ${line.projectNo.ifBlank { "空" }} · 图号 ${line.drawingNo.ifBlank { "空" }}",
            style = MaterialTheme.typography.bodySmall)
        Text("等级 ${line.grade.ifBlank { "空" }} · 表面 ${line.surface.ifBlank { "空" }}", style = MaterialTheme.typography.bodySmall)
        if (line.sourceLabel.isNotBlank()) Text("资料来源：${line.sourceLabel} · ${line.sourcePreview}", style = MaterialTheme.typography.bodySmall)
    }
}

private fun taxBasisLabel(value: String) = when (value) {
    "tax_inclusive" -> "含税"
    "tax_exclusive" -> "未税"
    else -> "口径待核对"
}

@Composable
private fun DraftField(label: String, value: String, tag: String, busy: Boolean, decimal: Boolean = false, onChange: (String) -> Unit) {
    OutlinedTextField(value, onChange, Modifier.fillMaxWidth().testTag(tag), label = { Text(label) }, enabled = !busy,
        singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = if (decimal) KeyboardType.Decimal else KeyboardType.Text))
}

@Composable
fun PurchaseAssistantScreen(state: PurchaseAssistantUiState, actions: AssistantActions, modifier: Modifier = Modifier,
                            webOperations: List<WebOperationUi> = emptyList(), webOperationsError: String? = null) {
    var section by rememberSaveable { mutableStateOf("todo") }
    var conversationOpen by rememberSaveable { mutableStateOf(false) }
    val pending = state.items.filter { !it.hasPlan && !purchaseReview(it) }
    val processing = state.items.filter { it.hasPlan }
    val review = state.items.filter { !it.hasPlan && purchaseReview(it) }
    val shown = when (section) { "processing" -> processing; "review" -> review; else -> pending }
    Column(modifier.fillMaxSize().testTag("purchase-assistant")) {
        AssistantHeader("采购助手", actions.close, actions.refreshPurchase, state.loading || state.busy)
        Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("采购计划 ${state.items.size} 项", Modifier.weight(1f).testTag("purchase-plan-count"),
                    style = MaterialTheme.typography.titleMedium)
                TextButton(onClick = { conversationOpen = !conversationOpen }, modifier = Modifier.testTag("purchase-toggle-chat")) {
                    Text(if (conversationOpen) "返回计划" else "对话")
                }
            }
            state.whitelistCounts?.let { counts ->
                Text("白名单候选 ${counts.candidateTotal} 项 · 已核对 ${counts.verifiedTotal} 项 · 待核对 ${counts.reviewTotal} 项",
                    Modifier.testTag("purchase-whitelist-counts"), style = MaterialTheme.typography.bodySmall,
                    color = WorkbenchColors.Muted)
                Text("当前需求命中白名单 ${counts.matchedDemandTotal} 项", style = MaterialTheme.typography.bodySmall,
                    color = WorkbenchColors.Muted)
            } ?: Text("白名单统计待同步", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            Text(if (state.generatedAt.isBlank()) "采购需求正在读取" else "生成时间 ${state.generatedAt}",
                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            if (state.whitelistVersion.isNotBlank()) Text("规则 ${state.whitelistVersion} · 数据 ${state.sourceVersion}",
                style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
            Text("加入计划后可查看执行进度；原任务的等待状态单独显示。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        }
        if (!conversationOpen) Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            listOf("todo" to "待采购 ${pending.size}", "processing" to "处理中 ${processing.size}", "review" to "等待／待核对 ${review.size}").forEach { (key, title) ->
                FilterChip(selected = section == key, onClick = { section = key }, label = { Text(title) },
                    modifier = Modifier.weight(1f).testTag("purchase-section:$key"))
            }
        }
        if (state.loading || state.busy) LinearProgressIndicator(Modifier.fillMaxWidth(), color = WorkbenchColors.Red)
        LazyColumn(Modifier.weight(1f).fillMaxWidth().testTag("purchase-plan-list"),
            contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            state.error?.let { item("error") { StateNotice(it, error = true) } }
            state.notice?.let { item("notice") { StateNotice(it) } }
            if (webOperations.isNotEmpty() || webOperationsError != null) item("web-operations") {
                WebOperationProgress(webOperations, webOperationsError, actions.refreshWebOperations)
            }
            state.job?.takeIf { job -> conversationOpen && state.items.any { it.hasPlan && it.job?.id == job.id } }
                ?.let { job -> item("job:${job.id}") { AssistantJobCard(job, actions.refreshJob) } }
            if (conversationOpen && state.messages.isEmpty()) item("chat-empty") {
                WorkbenchCard {
                    Text("告诉我采购要求", style = MaterialTheme.typography.titleMedium)
                    Text(if (state.conversationEnabled) "可以说明平台、规格或限价；采购前仍需核对白名单和计划。" else "助手服务待接入，已选采购计划仍可查看。",
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                }
            }
            if (!conversationOpen && shown.isEmpty() && !state.loading) item("empty") {
                WorkbenchCard {
                    Text(when (section) { "processing" -> "还没有加入采购计划的商品"; "review" -> "没有待核对商品"; else -> "暂没有可采购商品" },
                        style = MaterialTheme.typography.bodyMedium)
                    Text(if (section == "todo") "只有规则和商品身份核验通过的需求才能加入计划。" else "刷新会保留已经加入计划的记录。",
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                }
            }
            if (conversationOpen) items(state.messages, key = { "message:${it.id}" }) { MessageCard(it, "采购助手") }
            else items(shown, key = { it.id }) { item -> PurchasePlanCard(item, state.busy, actions) }
        }
        Surface(color = WorkbenchColors.Paper, border = BorderStroke(0.5.dp, WorkbenchColors.Line)) {
            Column(Modifier.fillMaxWidth().padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                if (!state.conversationEnabled) Text("助手服务待接入", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                OutlinedTextField(state.input, actions.updatePurchaseInput, Modifier.fillMaxWidth().testTag("purchase-input"),
                    label = { Text("说明采购要求") }, minLines = 1, maxLines = 3, enabled = !state.busy)
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    Button(onClick = actions.sendPurchaseMessage, enabled = state.conversationEnabled && !state.busy && state.input.isNotBlank(),
                        modifier = Modifier.testTag("purchase-send"), shape = WorkbenchTokens.ControlShape) { Text("发送") }
                }
            }
        }
    }
}

@Composable
private fun PurchasePlanCard(item: PurchasePlanItemUi, busy: Boolean, actions: AssistantActions) {
    WorkbenchCard(Modifier.testTag("purchase-item:${item.id}")) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(item.productCode, style = MaterialTheme.typography.titleMedium)
                Text(item.productName, style = MaterialTheme.typography.bodyMedium)
            }
            if (item.hasPlan) SmallTag("已加入计划")
            else Button(onClick = { actions.enrollPurchaseItem(item.id, item.version) },
                enabled = !busy && item.eligible && item.canEnroll, shape = WorkbenchTokens.ControlShape,
                modifier = Modifier.testTag("purchase-enroll:${item.id}")) { Text("加入计划") }
        }
        if (!item.hasPlan) purchaseTaskLabel(item.state)?.let { SmallTag(it) }
        if (item.specification.isNotBlank()) Text(item.specification, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        Text("计划 ${item.quantity.ifBlank { "待核对" }} ${item.unit}" + (if (item.warehouse.isNotBlank()) " · ${item.warehouse}" else ""),
            style = MaterialTheme.typography.bodyMedium)
        if (item.sourceOrderCount > 0) Text("关联 ${item.sourceOrderCount} 张销售订单", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        if (item.supplierName.isNotBlank()) Text("供应商 ${item.supplierName}", style = MaterialTheme.typography.bodySmall)
        if (item.platformName.isNotBlank() || item.priceLimit.isNotBlank()) Text(listOfNotNull(
            item.platformName.takeIf { it.isNotBlank() }, item.priceLimit.takeIf { it.isNotBlank() }?.let { "限价 $it" }).joinToString(" · "),
            style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        if (item.reason.isNotBlank()) Text(item.reason, style = MaterialTheme.typography.bodySmall,
            color = if (!item.eligible) WorkbenchColors.Error else WorkbenchColors.Muted)
        if (item.lastFailure.isNotBlank()) Text(item.lastFailure, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted,
            modifier = Modifier.testTag("purchase-failure:${item.id}"))
        if (item.hasPlan) item.job?.let { AssistantJobCard(it, actions.refreshJob) }
            ?: Text("已加入计划，执行任务正在核对", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
    }
}

@Composable
private fun AssistantHeader(title: String, onClose: () -> Unit, onRefresh: () -> Unit, busy: Boolean) {
    Surface(color = WorkbenchColors.Paper) {
        Row(Modifier.fillMaxWidth().heightIn(min = 54.dp).padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onClose) { Icon(WorkbenchIcons.Close, "返回任务") }
            Text(title, Modifier.weight(1f), style = MaterialTheme.typography.titleLarge)
            IconButton(onClick = onRefresh, enabled = !busy) { Icon(WorkbenchIcons.Refresh, "刷新助手") }
        }
    }
}

@Composable
private fun AssistantJobCard(job: AssistantJobUi, onRefresh: (String) -> Unit) {
    Column(Modifier.fillMaxWidth().testTag("assistant-job:${job.id}"), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(job.label.ifBlank { "处理中" }, Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium,
                color = if (job.requiresReview || job.status in setOf("failed", "uncertain")) WorkbenchColors.Error else WorkbenchColors.SecondaryInk)
            TextButton(onClick = { onRefresh(job.id) }, modifier = Modifier.testTag("refresh-job:${job.id}")) { Text("查进度") }
        }
        if (job.total > 0) {
            LinearProgressIndicator(progress = { (job.completed.toFloat() / job.total).coerceIn(0f, 1f) },
                modifier = Modifier.fillMaxWidth(), color = WorkbenchColors.Red)
            Text("${job.completed} / ${job.total}", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
        } else if (job.status in setOf("queued", "running", "processing")) LinearProgressIndicator(Modifier.fillMaxWidth(), color = WorkbenchColors.Red)
        if (job.detail.isNotBlank()) Text(job.detail, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        if (job.requiresReview) Text("需要人工核对后再继续", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
    }
}

@Composable
private fun WebOperationProgress(items: List<WebOperationUi>, error: String?, onRefresh: () -> Unit) {
    WorkbenchCard(Modifier.testTag("web-operation-queue")) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("网页执行进度", Modifier.weight(1f), style = MaterialTheme.typography.titleSmall)
            TextButton(onClick = onRefresh, modifier = Modifier.testTag("refresh-web-operations")) { Text("刷新") }
        }
        error?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error) }
        items.take(5).forEach { operation ->
            Column(Modifier.fillMaxWidth().testTag("web-operation:${operation.id}"), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text("${operation.label} · ${operation.stage}", style = MaterialTheme.typography.bodyMedium,
                    color = if (operation.status in setOf("uncertain", "needs_human", "failed", "blocked")) WorkbenchColors.Error else WorkbenchColors.SecondaryInk)
                if (operation.status == "running" && operation.totalSteps > 0)
                    LinearProgressIndicator(progress = { (operation.step.toFloat() / operation.totalSteps).coerceIn(0f, 1f) },
                        modifier = Modifier.fillMaxWidth(), color = WorkbenchColors.Red)
                Text(operation.detail, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            }
            HorizontalDivider(color = WorkbenchColors.Line)
        }
    }
}

private fun purchaseReview(item: PurchasePlanItemUi) = !item.eligible || item.state !in setOf("todo", "in_progress", "processing")
private fun purchaseTaskLabel(state: String): String? = when (state) {
    "todo" -> null
    "waiting" -> "等待"
    "in_progress", "processing" -> "人工处理"
    else -> "待核对"
}
private fun attachmentStatus(status: String) = when (status) {
    "uploading", "pending" -> "正在上传"; "failed" -> "上传失败，输入已保留"; "needs_review", "review" -> "内容待核对"
    "parsed" -> "已读取"; else -> "已添加"
}
private fun salesDraftStatus(draft: SalesDraftUi) = when {
    draft.status in setOf("submission_unknown", "uncertain") -> "导入结果待核对"
    draft.status == "submitting" -> "网页上传中"
    draft.status == "submission_blocked" -> "上传受阻"
    draft.status == "submitted" && draft.submissionCompletionBasis == "web_import_confirmation" -> "网页已确认导入"
    draft.erpOrderNo.isNotBlank() && draft.erpReadbackPerformed -> "ERP 已回查"
    draft.erpOrderNo.isNotBlank() -> "已有单号"
    draft.status == "submitted" -> "提交记录已保存"
    draft.status == "needs_review" -> "待核对"
    draft.confirmedVersion == draft.version -> "已确认"
    else -> "待确认"
}
