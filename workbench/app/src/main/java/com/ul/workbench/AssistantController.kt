package com.ul.workbench

import com.ul.workbench.data.*
import com.ul.workbench.ui.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.util.UUID

/** Assistant drafts/jobs have their own storage and never participate in task-list replay. */
internal class AssistantController(
    private val repository: WorkbenchRepository,
    private val scope: CoroutineScope,
    private val context: () -> String,
    private val user: () -> CurrentUser?,
    private val profileId: () -> String,
    private val load: suspend (String) -> String?,
    private val store: suspend (String, String?) -> Boolean,
    private val readAttachment: suspend (AssistantLocalAttachment) -> ByteArray,
    private val parserDispatcher: CoroutineDispatcher = Dispatchers.Default,
    private val viewAttachment: suspend (String, SalesAttachmentFile, () -> Boolean) -> Unit = { _, _, _ -> error("原附件查看尚未配置") },
    private val codexPollIntervalMs: Long = 5_000
) {
    private val mutableState = MutableStateFlow(AssistantUiState())
    val state = mutableState.asStateFlow()
    private var boundContext = ""
    private var generation = 0
    private var salesRead: Job? = null
    private var codexPoll: Job? = null
    private var customerSearch: Job? = null
    private var customerSearchSerial = 0
    private var purchaseRead: Job? = null
    private var webRead: Job? = null
    private var attachmentRead: Job? = null
    private var localSave: Job? = null
    private val localWriteMutex = Mutex()
    private var uploads: Job? = null
    private val localFiles = linkedMapOf<String, AssistantLocalAttachment>()
    private val sessionVersions = mutableMapOf<String, Int>()
    private val salesReadTokens = mutableMapOf<String, String>()
    private var salesListReadToken = ""
    private var editorOpenedReadToken = ""
    private val lineIdentities = mutableMapOf<String, Pair<String, JSONObject>>()
    private var purchaseSnapshotId = ""
    private var salesSubmitEnabled = false
    private var attachmentUploadEnabled = false

    val actions = AssistantActions(
        openSales = ::openSales, openPurchase = ::openPurchase, close = ::close,
        refreshSales = ::refreshSales, newSalesSession = ::newSalesSession,
        selectSalesSession = ::selectSalesSession, searchSalesCustomers = ::searchSalesCustomers,
        updateSalesInput = { value -> mutableState.update { it.copy(sales = it.sales.copy(input = value.take(10_000))) }; persistLocal() },
        attachSalesFile = ::attachSalesFile,
        attachmentError = { value -> salesError(value) }, removeSalesAttachment = ::removeSalesAttachment,
        retrySalesAttachment = ::retrySalesAttachment,
        sendSalesMessage = ::sendSalesMessage,
        confirmSalesDraft = { id, version -> salesDraftAction(id, version, false) },
        submitSalesDraft = { id, version -> salesDraftAction(id, version, true) },
        beginSalesDraftEdit = ::beginSalesDraftEdit, updateSalesDraftEdit = ::updateSalesDraftEdit,
        saveSalesDraftEdit = ::saveSalesDraftEdit, adoptLatestSalesDraftVersion = ::adoptLatestSalesDraftVersion,
        closeSalesDraftEditor = ::closeSalesDraftEditor, discardSalesDraftEdit = ::discardSalesDraftEdit,
        retryUnknownSalesDraftSave = ::retryUnknownSalesDraftSave,
        adoptSalesDocument = ::adoptSalesDocument, retryUnknownSalesDocumentAdoption = ::retryUnknownSalesDocumentAdoption,
        viewSalesAttachment = ::viewSalesAttachment,
        adoptSalesCodexDraft = ::adoptSalesCodexDraft,
        retryUnknownSalesCodexOperation = ::retryUnknownSalesCodexOperation,
        prepareSalesNewProductSuggestion = ::prepareSalesNewProductSuggestion,
        updateSalesNewProductReference = { id, value ->
            if (id.isNotBlank() && value.length <= 100) {
                mutableState.update { it.copy(sales = it.sales.copy(newProductReferences =
                    it.sales.newProductReferences + (id to value))) }; persistLocal()
            }
        },
        confirmSalesNewProduct = ::confirmSalesNewProduct,
        retryUnknownSalesNewProductOperation = ::retryUnknownSalesNewProductOperation,
        refreshPurchase = ::refreshPurchase, enrollPurchaseItem = ::enrollPurchaseItem,
        updatePurchaseInput = { value -> mutableState.update { it.copy(purchase = it.purchase.copy(input = value.take(20_000))) } },
        sendPurchaseMessage = { purchaseError("采购对话助手服务待接入；输入已保留，采购计划可独立使用") },
        refreshJob = ::refreshJob, refreshWebOperations = ::refreshWebOperations
    )

    fun contextChanged(force: Boolean = false) {
        if (!force && boundContext == context()) return
        generation++; salesRead?.cancel(); codexPoll?.cancel(); customerSearch?.cancel(); customerSearchSerial++
        purchaseRead?.cancel(); webRead?.cancel(); attachmentRead?.cancel(); uploads?.cancel()
        boundContext = context(); localFiles.clear(); sessionVersions.clear(); salesReadTokens.clear(); salesListReadToken = ""
        editorOpenedReadToken = ""; lineIdentities.clear(); purchaseSnapshotId = ""
        salesSubmitEnabled = false; attachmentUploadEnabled = false
        mutableState.value = AssistantUiState()
    }

    private fun authorized(role: String) = user()?.let { it.active && (it.isAdmin || role in it.roles) } == true
    private fun current(key: String, run: Int) = key == context() && key == boundContext && run == generation
    private fun pathId(value: String) = URLEncoder.encode(value, "UTF-8").replace("+", "%20")
    private fun draftSlot(key: String, id: String) = "sales:$key:$id:draft"
    private fun documentSlot(key: String, id: String) = "sales:$key:$id:document"
    private fun codexSlot(key: String, id: String, kind: String) = "sales:$key:$id:$kind"
    private fun unknownDraftSlot(saved: JSONObject) = saved.optBoolean("pending_unknown") ||
        !saved.optBoolean("requires_review") ||
        saved.optString("review_reason") !in setOf("version_conflict", "owner_review", "rejected")
    private fun frozenRequest(saved: JSONObject) = JSONObject(saved.toString()).also { request ->
        for (field in listOf("requires_review", "review_reason", "review_read_token", "review_message", "pending_unknown", "codex_job_id", "new_product_plan_id"))
            request.remove(field)
    }
    private data class DraftRecovery(val editor: SalesDraftUi?, val warning: String = "")
    private fun recoverSavedDraftEditor(local: SalesDraftUi?, frozen: JSONObject,
                                        committed: JSONObject, readback: JSONObject): DraftRecovery {
        if (local == null) return DraftRecovery(null)
        val saved = committed.optJSONObject("session") ?: return DraftRecovery(local, "原保存回执缺少会话，行身份须逐行核对")
        val latest = readback.optJSONObject("session") ?: return DraftRecovery(local, "回查缺少会话，行身份须逐行核对")
        val expected = frozen.optJSONArray("lines") ?: return DraftRecovery(local, "原请求缺少明细，行身份须逐行核对")
        val actual = saved.optJSONObject("draft")?.optJSONArray("lines")
            ?: return DraftRecovery(local, "原保存回执缺少明细，行身份须逐行核对")
        val current = latest.optJSONObject("draft")?.optJSONArray("lines")
            ?: return DraftRecovery(local, "回查缺少明细，行身份须逐行核对")
        if (local.version != frozen.optInt("version") ||
            saved.optInt("version") != frozen.optInt("version") + 1 ||
            latest.optInt("version") != saved.optInt("version") ||
            expected.length() != actual.length() || actual.length() != current.length())
            return DraftRecovery(local, "服务器明细版本或行数已变化，行身份须逐行核对")
        val mapped = linkedMapOf<String, String>()
        val serverIds = mutableSetOf<String>()
        for (index in 0 until expected.length()) {
            val requestLine = expected.optJSONObject(index)
            val savedLine = actual.optJSONObject(index)
            val currentLine = current.optJSONObject(index)
            val requestId = requestLine?.optString("id").orEmpty()
            val serverId = savedLine?.optString("id").orEmpty()
            val code = requestLine?.optString("product_code").orEmpty()
            if (requestId.isBlank() || serverId.isBlank() || code.isBlank() ||
                savedLine?.optString("product_code") != code ||
                currentLine?.optString("product_code") != code ||
                currentLine?.optString("id") != serverId ||
                (requestLine?.optBoolean("new_line") != true && requestId != serverId) ||
                mapped.put(requestId, serverId) != null || !serverIds.add(serverId))
                return DraftRecovery(local, "原请求和服务器明细无法逐行对应，行身份须逐行核对")
        }
        val frozenActions = frozen.optJSONArray("source_row_actions") ?: JSONArray()
        val completedActions = linkedMapOf<String, String>()
        fun report(rows: JSONArray?, id: String): JSONObject? = rows?.let { values ->
            (0 until values.length()).mapNotNull(values::optJSONObject).singleOrNull { it.optString("id") == id }
        }
        for (index in 0 until frozenActions.length()) {
            val action = frozenActions.optJSONObject(index)
            val id = action?.optString("source_row_id").orEmpty()
            val reason = action?.optString("reason").orEmpty().trim()
            val savedReport = report(saved.optJSONObject("draft")?.optJSONArray("source_rows"), id)
            val currentReport = report(latest.optJSONObject("draft")?.optJSONArray("source_rows"), id)
            if (id.isBlank() || reason.isBlank() || action?.optString("action") != "exclude" ||
                completedActions.put(id, reason) != null ||
                savedReport?.optString("status") != "excluded_by_user" ||
                currentReport?.optString("status") != "excluded_by_user" ||
                savedReport?.optString("exclusion_reason") != reason ||
                currentReport?.optString("exclusion_reason") != reason ||
                savedReport?.optString("excluded_by") != user()?.id)
                return DraftRecovery(local, "来源排除回查不完整，原动作仍保留待人工核对")
        }
        if (local.sourceActions.any { action -> completedActions[action.id]?.let { it != action.reason.trim() } == true })
            return DraftRecovery(local, "本地来源排除原因在原保存后改变，须人工核对")
        val lines = local.lines.map { line -> line.copy(id = mapped[line.id] ?: line.id) }
        if (lines.map { it.id }.toSet().size != lines.size)
            return DraftRecovery(local, "本地新增行与服务器行身份冲突，须逐行核对")
        return DraftRecovery(local.copy(lines = lines,
            sourceActions = local.sourceActions.filterNot { it.id in completedActions.keys },
            sourceRows = local.sourceRows.map { source -> if (source.id in completedActions.keys)
                source.copy(status = "excluded_by_user") else source }))
    }
    private fun text(error: Exception) = error.message?.take(200)?.takeIf(String::isNotBlank) ?: "连接失败，请稍后重试"
    private fun salesError(value: String) = mutableState.update { it.copy(sales = it.sales.copy(error = value)) }
    private fun purchaseError(value: String) = mutableState.update { it.copy(purchase = it.purchase.copy(error = value)) }
    private fun bind(role: String): Boolean {
        contextChanged()
        if (authorized(role)) return true
        if (role == ROLE_SALES) salesError("当前账号没有销售权限") else purchaseError("当前账号没有采购权限")
        return false
    }

    private fun openSales() {
        if (!bind(ROLE_SALES)) return
        mutableState.update { it.copy(panel = AssistantPanel.Sales, sales = it.sales.copy(accountLabel = profileId())) }
        refreshSales()
        refreshWebOperations()
    }
    private fun openPurchase() {
        if (!bind(ROLE_PURCHASE)) return
        mutableState.update { it.copy(panel = AssistantPanel.Purchase) }
        refreshPurchase()
        refreshWebOperations()
    }
    private fun close() {
        persistLocal(); webRead?.cancel(); codexPoll?.cancel(); customerSearch?.cancel(); customerSearchSerial++
        mutableState.update { it.copy(panel = null) }
    }

    private fun refreshWebOperations() {
        if (state.value.panel == null) return
        val key = context(); val run = generation
        webRead?.cancel()
        webRead = scope.launch {
            try {
                val response = repository.assistantRequest("/assistant/web-operations?limit=20")
                val items = withContext(parserDispatcher) { AssistantJson.webOperations(response) }
                if (current(key, run) && state.value.panel != null) mutableState.update {
                    it.copy(webOperations = items, webOperationsError = null) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (current(key, run) && state.value.panel != null) mutableState.update {
                    it.copy(webOperationsError = "网页进度读取失败；原列表保留：${text(error)}") }
            }
        }
    }

    private fun persistLocal() {
        val session = state.value.sales.session ?: return
        val key = context(); val sales = state.value.sales
        val snapshot = JSONObject().put("input", sales.input).put("editor", sales.editor?.let(AssistantJson::draftRecord))
            .put("new_product_references", JSONObject(sales.newProductReferences))
            .put("editor_open", sales.editorOpen).put("has_local_draft_edit", sales.hasLocalDraftEdit)
            .put("files", JSONArray(localFiles.map { (id, file) -> JSONObject().put("id", id).put("uri", file.uri)
                .put("name", file.name).put("mime_type", file.mimeType).put("size", file.sizeBytes) }))
        localSave?.cancel()
        localSave = scope.launch {
            try {
                val saved = localWriteMutex.withLock { store("local:$key:${session.id}", snapshot.toString()) }
                if (!saved && key == context() && state.value.sales.session?.id == session.id)
                    salesError("本地草稿保存失败，请保留当前输入并检查手机存储")
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { if (key == context()) salesError("本地草稿保存失败：${text(error)}") }
        }
    }

    private suspend fun restoreLocal(sessionId: String) {
        val key = context(); val run = generation
        val pending = load(draftSlot(key, sessionId))?.let(::JSONObject)?.let(::unknownDraftSlot) == true
        val pendingDocument = load(documentSlot(key, sessionId))?.let(::JSONObject)?.takeIf { it.optBoolean("pending_unknown") }
            ?.optString("attachment_id").orEmpty()
        val pendingCodex = listOf("codex", "codex-adopt").firstOrNull { kind ->
            load(codexSlot(key, sessionId, kind))?.let(::JSONObject)?.let { saved ->
                saved.optBoolean("pending_unknown") || !saved.optBoolean("requires_review")
            } == true
        }.orEmpty()
        val pendingNewProduct = listOf("new-product-prepare", "new-product-confirm").firstOrNull { kind ->
            load(codexSlot(key, sessionId, kind))?.let(::JSONObject)?.let { saved ->
                saved.optBoolean("pending_unknown") || !saved.optBoolean("requires_review")
            } == true
        }.orEmpty()
        val saved = load("local:$key:$sessionId")?.let(::JSONObject)
        if (!current(key, run) || state.value.sales.session?.id != sessionId) return
        if (saved == null) {
            mutableState.update { it.copy(sales = it.sales.copy(pendingDraftSave = pending, pendingDocumentAdoptionId = pendingDocument,
                pendingCodexOperation = pendingCodex, pendingNewProductOperation = pendingNewProduct)) }
            return
        }
        localFiles.clear()
        val files = saved.optJSONArray("files") ?: JSONArray()
        val attachments = (0 until minOf(files.length(), 5)).mapNotNull { index -> files.optJSONObject(index)?.let { row ->
            val id = row.optString("id"); val uri = row.optString("uri")
            if (id.isBlank() || uri.isBlank()) return@let null
            val file = AssistantLocalAttachment(uri, row.optString("name"), row.optString("mime_type"),
                row.optLong("size").takeIf { it > 0 })
            localFiles[id] = file
            AssistantAttachmentUi(id, file.name, "pending", "已恢复手机本地附件，上传结果尚未确认；可移除后重新选择核对")
        } }
        val editor = saved.optJSONObject("editor")?.let(AssistantJson::draft)?.takeIf { it.id == sessionId }
        val references = saved.optJSONObject("new_product_references")?.let { values -> values.keys().asSequence().take(200)
            .filter { it.isNotBlank() }.associateWith { values.optString(it).take(100) } }.orEmpty()
        mutableState.update { it.copy(sales = it.sales.copy(input = saved.optString("input"), attachments = attachments,
            newProductReferences = references,
            editor = editor, editorOpen = editor != null && saved.optBoolean("editor_open", true),
            hasLocalDraftEdit = editor != null && (saved.optBoolean("has_local_draft_edit") ||
                editor != it.sales.draft), pendingDraftSave = pending, pendingDocumentAdoptionId = pendingDocument,
            pendingCodexOperation = pendingCodex, pendingNewProductOperation = pendingNewProduct, editorError = null)) }
    }

    private fun refreshPurchase() {
        if (!bind(ROLE_PURCHASE) || state.value.purchase.busy) return
        val key = context(); val run = generation
        purchaseRead?.cancel()
        mutableState.update { it.copy(purchase = it.purchase.copy(loading = true, error = null)) }
        purchaseRead = scope.launch {
            try {
                val payload = repository.assistantRequest("/assistant/purchase/plan")
                val result = withContext(parserDispatcher) { AssistantJson.purchase(payload) }
                val failures = load("purchase-failures:$key")?.let(::JSONObject) ?: JSONObject()
                val enriched = result.copy(items = result.items.map { it.copy(lastFailure = purchaseFailure(failures, it.id)) })
                if (!current(key, run)) return@launch
                purchaseSnapshotId = UUID.randomUUID().toString()
                mutableState.update { it.copy(purchase = enriched.copy(input = it.purchase.input,
                    messages = it.purchase.messages, notice = "加入计划只保留采购需求；平台下单和付款尚未开启")) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (current(key, run)) mutableState.update { it.copy(purchase = it.purchase.copy(loading = false,
                    error = "采购计划读取失败，已保留上次数据：${text(error)}")) }
            }
        }
    }

    private fun enrollPurchaseItem(id: String, version: Int) {
        if (!bind(ROLE_PURCHASE) || state.value.purchase.busy || state.value.purchase.loading) return
        val plan = state.value.purchase
        val item = plan.items.firstOrNull { it.id == id } ?: return
        if (item.version != version) { purchaseError("采购候选版本已改变，请刷新后核对"); return }
        if (!item.eligible || !item.canEnroll || plan.sourceVersion.isBlank()) {
            purchaseError(item.reason.ifBlank { "当前采购需求尚未满足加入条件" }); return
        }
        val key = context(); val run = generation
        val snapshotAtClick = purchaseSnapshotId
        val slot = "purchase:$key:$id"
        mutableState.update { it.copy(purchase = it.purchase.copy(busy = true, error = null)) }
        scope.launch {
            try {
                // Persist before network I/O. A timeout retry uses this exact original version/key.
                val saved = load(slot)?.let(::JSONObject)
                val reviewedFreshScope = saved?.optBoolean("requires_review") == true &&
                    purchaseSnapshotId.isNotBlank() && saved.optString("review_snapshot_id") != purchaseSnapshotId &&
                    (saved.optInt("version") != version || saved.optString("source_version") != plan.sourceVersion)
                val frozen = if (saved != null && !reviewedFreshScope) saved else JSONObject()
                    .put("version", version).put("source_version", plan.sourceVersion)
                    .put("idempotency_key", UUID.randomUUID().toString()).also {
                        check(store(slot, it.toString())) { "无法保存采购操作，请检查手机存储后重试" }
                    }
                check(!frozen.optBoolean("requires_review")) { "上次采购操作存在版本冲突，请刷新并人工核对；不会自动重试" }
                if (!current(key, run) || !authorized(ROLE_PURCHASE)) return@launch
                val response = repository.assistantRequest("/assistant/purchase/plan/${pathId(id)}/enroll", frozen, "POST")
                if (!current(key, run)) return@launch
                check(response.optString("plan_id").isNotBlank() && response.optJSONObject("job")?.optString("id").orEmpty().isNotBlank()) {
                    "采购操作响应不完整，原请求已保留，尚未判定成功"
                }
                store(slot, null)
                if (!current(key, run)) return@launch
                val job = response.optJSONObject("job")?.let(AssistantJson::job)
                mutableState.update { it.copy(purchase = it.purchase.copy(busy = false, planId = response.optString("plan_id"), job = job,
                    items = it.purchase.items.map { row -> if (row.id == id) row.copy(state = "in_progress", canEnroll = false,
                        reason = "已有采购计划", job = job, planId = response.optString("plan_id")) else row },
                    notice = "已加入采购计划；平台下单和付款尚未开启")) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (error is ServiceException && error.status == 409) {
                    val failedRequest = load(slot)?.let(::JSONObject)
                    failedRequest?.let { store(slot, it.put("requires_review", true).put("review_snapshot_id", snapshotAtClick).toString()) }
                    val failures = load("purchase-failures:$key")?.let(::JSONObject) ?: JSONObject()
                    val history = failures.optJSONArray(id) ?: JSONArray()
                    history.put(JSONObject().put("version", failedRequest?.optInt("version") ?: version)
                        .put("source_version", failedRequest?.optString("source_version") ?: plan.sourceVersion)
                        .put("error_code", error.code).put("message", text(error)).put("created_at", java.time.OffsetDateTime.now().toString()))
                    // Keep the last ten conflict facts for this candidate, separate from the retry request.
                    val retained = JSONArray((maxOf(0, history.length() - 10) until history.length()).map { history.getJSONObject(it) })
                    failures.put(id, retained)
                    store("purchase-failures:$key", failures.toString())
                    if (current(key, run)) mutableState.update { it.copy(purchase = it.purchase.copy(items = it.purchase.items.map { row ->
                        if (row.id == id) row.copy(lastFailure = purchaseFailure(failures, id)) else row })) }
                }
                if (current(key, run)) mutableState.update { it.copy(purchase = it.purchase.copy(busy = false,
                    error = text(error) + if (error is ServiceException && error.status == 409) "；原操作已保留，请人工核对" else "；操作已保存，再次点击将核对原操作结果")) }
            } finally {
                if (current(key, run)) mutableState.update { it.copy(purchase = it.purchase.copy(busy = false)) }
            }
        }
    }

    private fun purchaseFailure(history: JSONObject, id: String): String {
        val records = history.optJSONArray(id) ?: return ""
        val last = records.optJSONObject(records.length() - 1) ?: return ""
        return "上次加入失败：${last.optString("message")}（任务版本 ${last.optInt("version")}）"
    }

    private fun refreshJob(id: String) {
        if (id.isBlank() || !bind(ROLE_PURCHASE) || state.value.purchase.busy) return
        val key = context(); val run = generation
        scope.launch {
            try {
                val response = repository.assistantRequest("/assistant/jobs/${pathId(id)}")
                val job = response.optJSONObject("job")?.let(AssistantJson::job) ?: error("执行任务响应不完整")
                if (current(key, run)) mutableState.update { it.copy(purchase = it.purchase.copy(job = job,
                    items = it.purchase.items.map { row -> if (row.job?.id == id) row.copy(job = job) else row }, error = null)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { if (current(key, run)) purchaseError("执行状态读取失败：${text(error)}") }
        }
    }

    private fun refreshSales() {
        if (!bind(ROLE_SALES) || state.value.sales.busy || state.value.sales.editorBusy) return
        val key = context(); val run = generation; val selected = state.value.sales.session?.id
        salesRead?.cancel()
        mutableState.update { it.copy(sales = it.sales.copy(loading = true, error = null)) }
        salesRead = scope.launch {
            try {
                val response = repository.assistantRequest("/sales-assistant/sessions")
                val sessions = withContext(parserDispatcher) { AssistantJson.sessions(response) }
                val detail = selected?.let { repository.assistantRequest("/sales-assistant/sessions/${pathId(it)}") }
                if (!current(key, run)) return@launch
                if (selected != null) check(detail?.optJSONObject("session")?.optString("id") == selected) {
                    "录单会话身份不一致" }
                mutableState.update { it.copy(sales = it.sales.copy(loading = false, sessions = sessions,
                    notice = "录单草稿可保存；选择会话后查看网页上传状态")) }
                if (detail != null && acceptSession(detail, preserveEditor = true))
                    salesReadTokens[selected!!] = UUID.randomUUID().toString()
                if (current(key, run)) salesListReadToken = UUID.randomUUID().toString()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(loading = false,
                    error = "销售助手读取失败：${text(error)}；已有输入保留")) }
            }
        }
    }

    private fun searchSalesCustomers(query: String) {
        if (!bind(ROLE_SALES) || state.value.panel != AssistantPanel.Sales) return
        val profile = profileId()
        customerSearch?.cancel()
        val serial = ++customerSearchSerial
        if (profile.isBlank() || query.length > 200) {
            mutableState.update { it.copy(sales = it.sales.copy(customerChoices = emptyList(),
                customerChoicesLoading = false, customerChoicesError = if (profile.isBlank())
                    "账套身份尚未就绪，请先刷新任务页" else "客户关键词最多 200 字")) }
            return
        }
        val key = context(); val run = generation
        mutableState.update { it.copy(sales = it.sales.copy(customerChoicesLoading = true,
            customerChoicesError = null, customerChoices = emptyList(), customerChoicesTotal = 0,
            customerChoicesHasMore = false)) }
        customerSearch = scope.launch {
            try {
                if (query.isNotBlank()) delay(250)
                val response = repository.assistantRequest(
                    "/sales-assistant/customers?profile_id=${pathId(profile)}&q=${pathId(query.trim())}&page=1")
                val rows = response.optJSONArray("items") ?: error("客户候选响应不完整")
                val choices = withContext(parserDispatcher) { (0 until rows.length()).mapNotNull { index ->
                    rows.optJSONObject(index)?.let { item ->
                        item.optString("customer_name").takeIf(String::isNotBlank)?.let { name ->
                            SalesCustomerChoiceUi(name, item.optString("owner_name"))
                        }
                    }
                } }
                if (current(key, run) && serial == customerSearchSerial && state.value.panel == AssistantPanel.Sales)
                    mutableState.update { it.copy(sales = it.sales.copy(customerChoices = choices,
                        customerChoicesLoading = false, customerChoicesTotal = response.optInt("total", choices.size),
                        customerChoicesHasMore = response.optBoolean("has_more", false))) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (current(key, run) && serial == customerSearchSerial && state.value.panel == AssistantPanel.Sales)
                    mutableState.update { it.copy(sales = it.sales.copy(customerChoicesLoading = false,
                        customerChoicesError = "客户名单读取失败：${text(error)}；已输入名称保留")) }
            }
        }
    }

    private fun newSalesSession(customer: String) {
        if (!bind(ROLE_SALES) || state.value.sales.busy || state.value.sales.editorBusy || customer.isBlank()) return
        if (state.value.sales.pendingDocumentAdoptionId.isNotBlank()) { salesError("原资料加入结果尚未确认，请先核对原结果"); return }
        if (profileId().isBlank()) {
            mutableState.update { it.copy(sales = it.sales.copy(error = "账套身份尚未就绪，请先刷新任务页",
                createError = "账套身份尚未就绪，请先刷新任务页")) }; return
        }
        salesMutation("create", "new", 0, JSONObject().put("profile_id", profileId()).put("customer_name", customer.trim()),
            "/sales-assistant/sessions", "POST") { response ->
            localFiles.clear()
            mutableState.update { it.copy(sales = it.sales.copy(input = "", attachments = emptyList(), editor = null, job = null,
                pendingCodexOperation = "", pendingNewProductOperation = "", newProductReferences = emptyMap())) }
            acceptSession(response)
            persistLocal()
        }
    }

    private fun selectSalesSession(id: String) {
        if (!bind(ROLE_SALES) || state.value.sales.busy || state.value.sales.editorBusy || id.isBlank()) return
        val key = context(); val run = generation
        persistLocal()
        salesRead?.cancel(); codexPoll?.cancel(); codexPoll = null
        mutableState.update { it.copy(sales = it.sales.copy(loading = true, error = null)) }
        salesRead = scope.launch {
            try {
                val response = repository.assistantRequest("/sales-assistant/sessions/${pathId(id)}")
                if (!current(key, run)) return@launch
                check(response.optJSONObject("session")?.optString("id") == id) { "录单会话身份不一致" }
                localFiles.clear()
                mutableState.update { it.copy(sales = it.sales.copy(loading = false, input = "", attachments = emptyList(), editor = null)) }
                if (acceptSession(response)) salesReadTokens[id] = UUID.randomUUID().toString()
                localSave?.join()
                restoreLocal(id)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(loading = false, error = text(error))) } }
        }
    }

    private fun sendSalesMessage() {
        val sales = state.value.sales; val session = sales.session ?: return
        if (!sales.codexEnabled) { salesError("N150 Codex 录单尚未就绪；资料和输入已保留"); return }
        if (!salesCodexMutable(sales)) return
        if (sales.codexJob?.running == true) { salesError("Codex 正在处理这份资料，请等待结果"); return }
        if (sales.newProductPlans.any { it.running }) { salesError("新商品正在处理，请等待创建结果后重新核对编码；输入已保留"); return }
        if (sales.pendingCodexOperation.isNotBlank()) { salesError("原 Codex 操作结果未确认，请核对原操作；输入已保留"); return }
        if (sales.pendingNewProductOperation.isNotBlank()) { salesError("原新商品操作结果未确认，请核对原操作；输入已保留"); return }
        if (sales.pendingDraftSave) { salesError("原草稿保存结果未确认，请先重试原保存请求"); return }
        if (sales.pendingDocumentAdoptionId.isNotBlank()) { salesError("原资料加入结果尚未确认，请先核对原结果"); return }
        if (sales.attachments.any { it.status in setOf("pending", "uploading", "failed") }) {
            salesError("附件尚未上传，请重试或移除本地附件；输入已保留"); return
        }
        if (sales.hasLocalDraftEdit) { salesError("请先保存或明确丢弃本地草稿修改，输入已保留"); return }
        if (sales.input.isBlank() && sales.draft?.lines.isNullOrEmpty() && sales.attachments.isEmpty() &&
            session.messages.none { it.attachments.isNotEmpty() } && session.documentAttachments.isEmpty()) return
        if (session.dataVersion.isBlank()) { salesError("资料数据版本尚未就绪，请刷新后再发送"); return }
        val version = sessionVersions[session.id] ?: return
        val input = sales.input
        salesMutation("codex", session.id, version, JSONObject().put("version", version)
            .put("data_version", session.dataVersion).put("text", input),
            "/sales-assistant/sessions/${pathId(session.id)}/codex-jobs", "POST") { response ->
            acceptCodexMutation(response, "codex", input)
        }
    }

    private fun salesCodexMutable(sales: SalesAssistantUiState): Boolean {
        val draft = sales.draft ?: return false
        if (draft.status !in setOf("draft", "confirmed") || draft.erpOrderNo.isNotBlank()) {
            salesError("原订单正在上传或结果待核对，不能重新分析或采用另一份草稿"); return false
        }
        return true
    }

    private suspend fun acceptCodexMutation(response: JSONObject, kind: String, originalInput: String = "",
                                            expectedJobId: String = "", expectedResultHash: String = "", baseVersion: Int = 0) {
        val raw = response.optJSONObject("session") ?: error("Codex 会话回执不完整")
        val job = raw.optJSONObject("codex_job") ?: error("Codex 操作回执缺少任务，原请求保留")
        check(job.optString("id").isNotBlank() && job.optString("status").isNotBlank()) { "Codex 任务身份或状态不完整" }
        if (kind == "codex-adopt") check(job.optString("id") == expectedJobId && job.optString("result_hash") == expectedResultHash &&
            job.optString("status") == "adopted" && raw.optInt("version") > baseVersion) {
            "Codex 草稿采用回执不完整，原请求保留待核对"
        }
        if (acceptSession(response, preserveEditor = true)) {
            mutableState.update { it.copy(sales = it.sales.copy(pendingCodexOperation = "",
                input = if (kind == "codex" && it.sales.input == originalInput) "" else it.sales.input,
                notice = if (kind == "codex-adopt") "已采用 Codex 草稿；核对商品编码、数量、报价与摘要后再确认订单"
                    else "资料已交给 N150 Codex；结果只供核对，不会自动确认或上传")) }
            persistLocal()
        }
    }

    private fun adoptSalesCodexDraft(jobId: String, resultHash: String) {
        val sales = state.value.sales; val session = sales.session ?: return
        val job = sales.codexJob ?: return
        if (!salesCodexMutable(sales)) return
        if (sales.hasLocalDraftEdit || sales.pendingDraftSave || sales.pendingDocumentAdoptionId.isNotBlank() ||
            sales.pendingCodexOperation.isNotBlank() || sales.pendingNewProductOperation.isNotBlank()) {
            salesError("手机上有未保存编辑或待核对操作，先处理后再采用 Codex 草稿"); return
        }
        if (job.id != jobId || job.status != "ready_for_review" || !job.canAdopt || job.newProductSuggestions.isNotEmpty() || resultHash.isBlank() ||
            job.resultHash != resultHash || job.baseVersion != sessionVersions[session.id]) {
            salesError("Codex 结果或草稿版本已变化，请刷新核对；不会采用旧结果"); return
        }
        if (session.dataVersion.isBlank()) { salesError("资料数据版本尚未就绪，请刷新后再核对"); return }
        salesMutation("codex-adopt", session.id, job.baseVersion, JSONObject().put("version", job.baseVersion)
            .put("data_version", session.dataVersion).put("result_hash", resultHash),
            "/sales-assistant/sessions/${pathId(session.id)}/codex-jobs/${pathId(jobId)}/adopt", "POST") {
            acceptCodexMutation(it, "codex-adopt", expectedJobId = jobId, expectedResultHash = resultHash, baseVersion = job.baseVersion)
        }
    }

    private fun retryUnknownSalesCodexOperation() {
        val sales = state.value.sales; val session = sales.session ?: return
        val kind = sales.pendingCodexOperation.takeIf { it in setOf("codex", "codex-adopt") } ?: return
        if (!bind(ROLE_SALES) || sales.busy || sales.loading || sales.editorBusy || sales.hasLocalDraftEdit ||
            sales.pendingDraftSave || sales.pendingDocumentAdoptionId.isNotBlank() || sales.pendingNewProductOperation.isNotBlank()) return
        val key = context(); val run = generation
        scope.launch {
            try {
                val saved = load(codexSlot(key, session.id, kind))?.let(::JSONObject)
                check(saved != null && (saved.optBoolean("pending_unknown") || !saved.optBoolean("requires_review"))) {
                    "原 Codex 请求不存在，请保留资料并核对会话"
                }
                val original = frozenRequest(saved!!).also { it.remove("idempotency_key") }
                val jobId = saved.optString("codex_job_id")
                val path = if (kind == "codex") "/sales-assistant/sessions/${pathId(session.id)}/codex-jobs"
                    else {
                        check(jobId.isNotBlank()) { "原 Codex 采用任务身份不完整，不能重试" }
                        "/sales-assistant/sessions/${pathId(session.id)}/codex-jobs/${pathId(jobId)}/adopt"
                    }
                if (!current(key, run) || state.value.sales.session?.id != session.id) return@launch
                salesMutation(kind, session.id, original.optInt("version"), original, path, "POST") {
                    acceptCodexMutation(it, kind, original.optString("text"), jobId, original.optString("result_hash"), original.optInt("version"))
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { if (current(key, run)) salesError("原 Codex 操作仍未核定：${text(error)}；原请求保留") }
        }
    }

    private fun scheduleCodexPoll(sessionId: String) {
        if (codexPollIntervalMs <= 0 || codexPoll?.isActive == true || state.value.panel != AssistantPanel.Sales ||
            !salesAssistantRunning(state.value.sales)) return
        val key = context(); val run = generation
        codexPoll = scope.launch {
            while (current(key, run) && state.value.panel == AssistantPanel.Sales &&
                state.value.sales.session?.id == sessionId && salesAssistantRunning(state.value.sales)) {
                delay(codexPollIntervalMs)
                if (state.value.sales.busy || state.value.sales.loading || state.value.sales.editorBusy) continue
                try {
                    val response = repository.assistantRequest("/sales-assistant/sessions/${pathId(sessionId)}")
                    if (!current(key, run) || state.value.panel != AssistantPanel.Sales || state.value.sales.session?.id != sessionId) break
                    check(response.optJSONObject("session")?.optString("id") == sessionId) { "Codex 状态会话身份不一致" }
                    if (acceptSession(response, preserveEditor = true)) salesReadTokens[sessionId] = UUID.randomUUID().toString()
                } catch (cancelled: CancellationException) { throw cancelled }
                catch (error: Exception) {
                    if (current(key, run)) salesError("Codex 进度暂未更新：${text(error)}；资料、输入和上次进度保留")
                    break
                }
            }
        }
    }

    private fun salesAssistantRunning(sales: SalesAssistantUiState) = sales.codexJob?.running == true ||
        sales.newProductPlans.any { it.running }

    private fun salesNewProductMutable(sales: SalesAssistantUiState): Boolean {
        if (!salesCodexMutable(sales)) return false
        if (sales.hasLocalDraftEdit || sales.pendingDraftSave || sales.pendingDocumentAdoptionId.isNotBlank() ||
            sales.pendingCodexOperation.isNotBlank() || sales.pendingNewProductOperation.isNotBlank()) {
            salesError("有未保存编辑或待核对原操作，请先处理；新商品资料和输入保留"); return false
        }
        return true
    }

    private fun prepareSalesNewProductSuggestion(jobId: String, resultHash: String, suggestionId: String) {
        val sales = state.value.sales; val session = sales.session ?: return
        val job = sales.codexJob ?: return
        val suggestion = job.newProductSuggestions.firstOrNull { it.id == suggestionId } ?: return
        if (!salesNewProductMutable(sales)) return
        if (job.id != jobId || job.resultHash != resultHash || resultHash.isBlank() || !suggestion.canPrepare ||
            job.baseVersion != sessionVersions[session.id] || job.status == "stale") {
            salesError("新商品建议或资料版本已变化，请刷新核对"); return
        }
        val existing = sales.newProductPlans.firstOrNull { it.suggestionId == suggestionId && it.jobId == jobId &&
            it.sourceResultHash == resultHash }
        if (existing != null && (existing.status !in setOf("blocked", "failed") || !existing.canPrepare)) {
            salesError("这项新商品已有处理计划，请查看原进度，不会重复整理或创建"); return
        }
        val version = sessionVersions[session.id] ?: return
        if (session.dataVersion.isBlank()) { salesError("商品数据版本尚未就绪，请刷新后再整理"); return }
        val body = JSONObject().put("version", version).put("data_version", session.dataVersion)
            .put("job_id", jobId).put("result_hash", resultHash).put("suggestion_id", suggestionId)
        sales.newProductReferences["$jobId:$suggestionId"]?.trim()?.takeIf(String::isNotBlank)?.let { body.put("reference_code", it) }
        salesMutation("new-product-prepare", session.id, version, body,
            "/sales-assistant/sessions/${pathId(session.id)}/new-products/prepare", "POST") {
            acceptNewProductMutation(it, "new-product-prepare", body)
        }
    }

    private fun confirmSalesNewProduct(planId: String, planHash: String) {
        val sales = state.value.sales; val session = sales.session ?: return
        val plan = sales.newProductPlans.firstOrNull { it.id == planId } ?: return
        if (user()?.let { it.active && it.isAdmin } != true || !sales.newProductAdmin) {
            salesError("只有管理员可以确认新建编码"); return
        }
        if (!salesNewProductMutable(sales)) return
        if (!plan.canConfirm || plan.status != "ready_for_confirmation" || planHash.isBlank() || plan.planHash != planHash) {
            salesError("新商品方案尚未通过预检查或已变化，请刷新核对，不会创建"); return
        }
        val version = sessionVersions[session.id] ?: return
        if (session.dataVersion.isBlank()) { salesError("商品数据版本尚未就绪，请刷新后再核对"); return }
        val body = JSONObject().put("version", version).put("data_version", session.dataVersion).put("plan_hash", planHash)
        salesMutation("new-product-confirm", session.id, version, body,
            "/sales-assistant/sessions/${pathId(session.id)}/new-products/${pathId(planId)}/confirm", "POST") {
            acceptNewProductMutation(it, "new-product-confirm", body, planId)
        }
    }

    private suspend fun acceptNewProductMutation(response: JSONObject, kind: String, request: JSONObject, expectedPlanId: String = "") {
        val raw = response.optJSONObject("session") ?: error("新商品会话回执不完整")
        val planId = response.optString("plan_id")
        check(planId.isNotBlank() && (expectedPlanId.isBlank() || planId == expectedPlanId)) { "新商品计划回执身份不一致，原请求保留" }
        val plan = AssistantJson.newProductPlans(raw).singleOrNull { it.id == planId }
            ?: error("新商品计划回执缺少明确计划，原请求保留")
        check(plan.status in setOf("preparing", "needs_review", "ready_for_confirmation", "queued", "creating", "created", "unknown", "blocked", "failed", "stale") &&
            (plan.status != "created" || plan.created)) { "新商品计划状态或创建凭据不完整，原请求保留" }
        if (kind == "new-product-prepare") check(plan.suggestionId == request.optString("suggestion_id") &&
            plan.jobId == request.optString("job_id") && plan.sourceResultHash == request.optString("result_hash")) {
            "新商品建议回执来源不一致，原请求保留"
        } else check(plan.planHash == request.optString("plan_hash")) { "新商品确认回执方案哈希不一致，原请求保留" }
        if (acceptSession(response, preserveEditor = true)) {
            mutableState.update { it.copy(sales = it.sales.copy(pendingNewProductOperation = "",
                notice = if (kind == "new-product-prepare") "正在整理新商品方案；不会写入 ERP，管理员确认后才可新建编码"
                    else "新商品确认已收到；以创建进度为准，不会自动录单")) }
            persistLocal()
        }
    }

    private fun retryUnknownSalesNewProductOperation() {
        val sales = state.value.sales; val session = sales.session ?: return
        val kind = sales.pendingNewProductOperation.takeIf { it in setOf("new-product-prepare", "new-product-confirm") } ?: return
        if (!bind(ROLE_SALES) || sales.busy || sales.loading || sales.editorBusy || sales.hasLocalDraftEdit ||
            sales.pendingDraftSave || sales.pendingDocumentAdoptionId.isNotBlank() || sales.pendingCodexOperation.isNotBlank()) return
        if (kind == "new-product-confirm" && user()?.let { it.active && it.isAdmin } != true) {
            salesError("原新建编码确认只能由管理员核对，原请求保留"); return
        }
        val key = context(); val run = generation
        scope.launch {
            try {
                val saved = load(codexSlot(key, session.id, kind))?.let(::JSONObject)
                check(saved != null && (saved.optBoolean("pending_unknown") || !saved.optBoolean("requires_review"))) { "没有待核对的新商品原请求" }
                val original = frozenRequest(saved!!).also { it.remove("idempotency_key") }
                val planId = saved.optString("new_product_plan_id")
                val path = if (kind == "new-product-prepare") "/sales-assistant/sessions/${pathId(session.id)}/new-products/prepare"
                    else { check(planId.isNotBlank()) { "原新建编码计划身份不完整，不能重试" }
                        "/sales-assistant/sessions/${pathId(session.id)}/new-products/${pathId(planId)}/confirm" }
                if (!current(key, run) || state.value.sales.session?.id != session.id) return@launch
                salesMutation(kind, session.id, original.optInt("version"), original, path, "POST") {
                    acceptNewProductMutation(it, kind, original, planId)
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { if (current(key, run)) salesError("新商品原操作仍未核定：${text(error)}；原请求和资料保留") }
        }
    }

    private fun salesDraftAction(id: String, version: Int, submit: Boolean) {
        val draft = state.value.sales.draft ?: return
        if (state.value.sales.codexJob?.blocksConfirmation(draft.version) == true) {
            salesError("请先采用当前 Codex 建议或重新整理资料，再核对确认订单；旧草稿不会自动上传"); return
        }
        if (state.value.sales.hasLocalDraftEdit || state.value.sales.pendingDraftSave || state.value.sales.pendingDocumentAdoptionId.isNotBlank() ||
            state.value.sales.pendingCodexOperation.isNotBlank() || state.value.sales.pendingNewProductOperation.isNotBlank()) {
            salesError("手机上还有未核对的编辑或保存请求；请先恢复、保存或明确丢弃后再确认")
            return
        }
        if (draft.id != id || draft.version != version || sessionVersions[id] != version) {
            salesError("确认稿版本已改变，请重新核对"); return
        }
        if (submit && (!salesSubmitEnabled || !draft.canSubmit || draft.confirmedVersion != version)) {
            salesError("网页上传暂不可用，已确认草稿保留"); return
        }
        if (!submit && !draft.canConfirm) { salesError("草稿存在待核对项，暂不能确认"); return }
        val action = if (submit) "submit" else "confirm"
        salesMutation(action, id, version, JSONObject().put("version", version),
            "/sales-assistant/sessions/${pathId(id)}/$action", "POST") { acceptSession(it) }
    }

    private fun salesMutation(kind: String, id: String, version: Int, body: JSONObject, path: String,
        method: String, success: suspend (JSONObject) -> Unit) {
        if (!bind(ROLE_SALES) || state.value.sales.busy || state.value.sales.editorBusy || state.value.sales.loading) return
        val key = context(); val run = generation; val slot = "sales:$key:$id:$kind"
        val readTokenAtClick = if (kind == "create") salesListReadToken else salesReadTokens[id].orEmpty()
        mutableState.update { it.copy(sales = it.sales.copy(busy = true, error = null,
            editorBusy = kind == "draft", editorError = if (kind == "draft") null else it.sales.editorError,
            createError = if (kind == "create") null else it.sales.createError)) }
        scope.launch {
            var requestSent = false
            var requestAcknowledged = false
            try {
                localSave?.join()
                val saved = load(slot)?.let(::JSONObject)
                // Older app versions stored a review flag without recording which
                // read the user had seen. Require one new read before replacing it.
                if (saved?.optBoolean("requires_review") == true && !saved.has("review_read_token")) {
                    saved.put("review_read_token", readTokenAtClick)
                    check(store(slot, saved.toString())) { "无法保存核对状态，请检查手机存储" }
                }
                if (kind == "draft" && saved != null && unknownDraftSlot(saved))
                    error("原草稿保存结果未确认，请使用“重试原保存请求”核对，不能生成新请求")
                val comparable = saved?.let { frozenRequest(it).also { value -> value.remove("idempotency_key") } }
                val reviewedAgain = saved?.optBoolean("requires_review") == true && readTokenAtClick.isNotBlank() &&
                    readTokenAtClick != saved?.optString("review_read_token")
                val frozen = if (saved == null || reviewedAgain) {
                    JSONObject(body.toString()).put("idempotency_key", UUID.randomUUID().toString()).also {
                        if (kind == "codex-adopt") it.put("codex_job_id", java.net.URLDecoder.decode(
                            path.substringBeforeLast("/adopt").substringAfterLast('/'), "UTF-8"))
                        if (kind == "new-product-confirm") it.put("new_product_plan_id", java.net.URLDecoder.decode(
                            path.substringBeforeLast("/confirm").substringAfterLast('/'), "UTF-8"))
                        check(store(slot, it.toString())) { "无法保存录单操作，请检查手机存储" }
                    }
                } else {
                    check(!saved.optBoolean("requires_review")) {
                        val originalReason = saved.optString("review_message").takeIf(String::isNotBlank)?.let { "$it；" }.orEmpty()
                        originalReason + when (saved.optString("review_reason")) {
                            "version_conflict" -> "原操作版本已失效；请刷新并核对服务器草稿，当前输入已保留"
                            "owner_review" -> "客户固定销售需核对；请刷新并检查客户名称，当前输入已保留"
                            "rejected" -> "原操作被服务器拒绝；请刷新并核对输入，当前输入已保留"
                            else -> "原操作结果需核对；请刷新会话，当前输入已保留"
                        }
                    }
                    check(comparable != null && AssistantJson.sameObject(comparable, body)) { "上次操作结果尚未确认；请保留原输入并核对会话，不能改成另一份请求" }
                    saved
                }
                if (!current(key, run) || !authorized(ROLE_SALES)) return@launch
                requestSent = true
                var response = repository.assistantRequest(path, frozenRequest(frozen), method)
                requestAcknowledged = true
                if (!current(key, run)) return@launch
                val sessionId = response.optJSONObject("session")?.optString("id").orEmpty()
                check(sessionId.isNotBlank() && (kind == "create" || sessionId == id)) { "录单操作响应不完整，原请求已保留" }
                // Stored idempotent results may be an earlier session version. Read back current data.
                if (response.optBoolean("replayed")) {
                    val returnedPlanId = response.optString("plan_id")
                    response = repository.assistantRequest("/sales-assistant/sessions/${pathId(sessionId)}")
                    if (kind in setOf("new-product-prepare", "new-product-confirm") && returnedPlanId.isNotBlank())
                        response.put("plan_id", returnedPlanId)
                    if (!current(key, run)) return@launch
                }
                success(response)
                if (current(key, run)) store(slot, null)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                val unknownDraft = kind == "draft" && requestSent &&
                    (requestAcknowledged || error !is ServiceException || error.status !in setOf(400, 403, 409, 422))
                val unknownCodex = kind in setOf("codex", "codex-adopt") && requestSent &&
                    (requestAcknowledged || error !is ServiceException || error.status !in setOf(400, 403, 409, 422, 503))
                val unknownNewProduct = kind in setOf("new-product-prepare", "new-product-confirm") && requestSent &&
                    (requestAcknowledged || error !is ServiceException || error.status !in setOf(400, 403, 409, 422, 503))
                if (unknownDraft || unknownCodex || unknownNewProduct) {
                    load(slot)?.let { saved -> store(slot, JSONObject(saved).put("pending_unknown", true).toString()) }
                } else if (error is ServiceException && error.status in setOf(400, 403, 409, 422, 503)) {
                    val reason = when {
                        error.code == "sales_draft_version_changed" -> "version_conflict"
                        error.code in setOf("customer_owner_review", "task_owner_conflict") -> "owner_review"
                        error.status in setOf(400, 403, 422) -> "rejected"
                        else -> "unavailable"
                    }
                    load(slot)?.let { saved -> store(slot, JSONObject(saved).put("requires_review", true)
                        .also { if (kind in setOf("codex", "codex-adopt", "new-product-prepare", "new-product-confirm")) it.put("pending_unknown", false) }
                        .put("review_reason", reason).put("review_message", text(error)).put("review_read_token", readTokenAtClick).toString()) }
                }
                val unknownSubmission = kind == "submit" && requestSent &&
                    (error !is ServiceException || error.status !in setOf(400, 403, 409, 422, 503))
                val guidance = if (unknownSubmission) "；导入结果待核对，勿重复上传"
                    else if (unknownNewProduct) "；原新商品请求已保留，请核对原操作，不会自动重试或重复创建"
                    else if (unknownCodex) "；原 Codex 请求已保留，请核对原操作，不会自动重试" else when ((error as? ServiceException)?.code) {
                    "sales_draft_version_changed" -> "；输入已保留，请刷新并对照服务器草稿后重试"
                    "customer_owner_review", "task_owner_conflict" -> "；客户归属需核对，输入已保留。请检查客户名称与固定销售后刷新"
                    else -> when ((error as? ServiceException)?.status) {
                    400, 403, 422 -> "；输入已保留，请刷新、核对并修改后重试"
                    503 -> "；服务暂不可用，原操作已保留待核对"
                    else -> ""
                    }
                }
                if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(error = text(error) + guidance,
                    pendingDraftSave = it.sales.pendingDraftSave || unknownDraft,
                    pendingCodexOperation = if (unknownCodex) kind else if (kind in setOf("codex", "codex-adopt") &&
                        error is ServiceException && error.status in setOf(400, 403, 409, 422, 503)) "" else it.sales.pendingCodexOperation,
                    pendingNewProductOperation = if (unknownNewProduct) kind else if (kind in setOf("new-product-prepare", "new-product-confirm") &&
                        error is ServiceException && error.status in setOf(400, 403, 409, 422, 503)) "" else it.sales.pendingNewProductOperation,
                    editorError = if (kind == "draft") text(error) + guidance + "；编辑内容已保留" else it.sales.editorError,
                    createError = if (kind == "create") text(error) + guidance else it.sales.createError)) }
            } finally {
                if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(busy = false, editorBusy = false)) }
            }
        }
    }

    private suspend fun acceptSession(response: JSONObject, preserveEditor: Boolean = false): Boolean {
        val key = context(); val run = generation
        val raw = response.optJSONObject("session") ?: error("录单会话响应不完整")
        check(raw.optString("id").isNotBlank() && raw.optInt("version") > 0 && raw.optString("profile_id").isNotBlank()) {
            "录单会话身份或版本不完整，原操作保留待核对"
        }
        val (session, draft, codexJob) = withContext(parserDispatcher) {
            Triple(AssistantJson.session(raw), AssistantJson.draft(raw), AssistantJson.codexJob(raw))
        }
        val newProductPlans = withContext(parserDispatcher) { AssistantJson.newProductPlans(raw) }
        if (!current(key, run)) return false
        if (state.value.sales.session?.id != session.id) { codexPoll?.cancel(); codexPoll = null }
        sessionVersions[session.id] = raw.optInt("version")
        lineIdentities.clear()
        val rawDraft = raw.optJSONObject("draft")
        rawDraft?.optJSONArray("lines")?.let { lines -> for (index in 0 until lines.length()) {
            val line = lines.optJSONObject(index) ?: continue
            val identity = line.optJSONObject("resolved_identity") ?: line.optJSONObject("identity") ?: continue
            lineIdentities[line.optString("id").ifBlank { index.toString() }] = line.optString("product_code") to JSONObject(identity.toString())
        } }
        // Only an affirmative server capability permits submit; confirmation alone never grants it.
        salesSubmitEnabled = raw.optBoolean("submit_available", false)
        attachmentUploadEnabled = true
        if (!preserveEditor) editorOpenedReadToken = ""
        mutableState.update { state ->
            val previous = state.sales
            val retainLocal = preserveEditor && previous.hasLocalDraftEdit && previous.editor?.id == session.id
            val editor = when {
                retainLocal -> previous.editor
                preserveEditor && previous.editorOpen -> draft
                else -> null
            }
            state.copy(sales = previous.copy(session = session, draft = draft,
                codexEnabled = raw.opt("codex_enabled") == true, codexJob = codexJob,
                newProductAdmin = user()?.let { it.active && it.isAdmin } == true, newProductPlans = newProductPlans,
                sessions = (listOf(session) + previous.sessions).distinctBy(SalesSessionUi::id),
                editor = editor, editorOpen = preserveEditor && previous.editorOpen && editor != null,
                hasLocalDraftEdit = retainLocal, pendingDraftSave = preserveEditor && previous.pendingDraftSave,
                editorError = if (retainLocal) previous.editorError else null,
                error = null, notice = if (salesSubmitEnabled || raw.optString("status") in setOf("submitting", "submitted", "submission_unknown", "submission_blocked")) null
                    else "录单确认稿已保存；网页上传暂不可用"))
        }
        scheduleCodexPoll(session.id)
        return true
    }
    private fun beginSalesDraftEdit() {
        if (state.value.sales.busy || state.value.sales.loading) return
        if (state.value.sales.pendingCodexOperation.isNotBlank() || state.value.sales.pendingNewProductOperation.isNotBlank()) {
            salesError("原助手操作结果尚未确认，请先核对；原输入保留"); return
        }
        if (state.value.sales.pendingDocumentAdoptionId.isNotBlank()) { salesError("原资料加入结果尚未确认，请先核对原结果；原输入保留"); return }
        editorOpenedReadToken = state.value.sales.draft?.id?.let { salesReadTokens[it] }.orEmpty()
        mutableState.update { state ->
            val sales = state.sales
            val editor = sales.editor?.takeIf { sales.hasLocalDraftEdit && it.id == sales.draft?.id }
                ?: sales.draft?.copy()
            state.copy(sales = sales.copy(editor = editor, editorOpen = editor != null,
                editorError = null))
        }
        persistLocal()
    }
    private fun updateSalesDraftEdit(value: SalesDraftUi) {
        val editor = state.value.sales.editor ?: return
        if (state.value.sales.busy || state.value.sales.editorBusy || editor.id != value.id || editor.version != value.version) return
        val checked = value.copy(lines = value.lines.map { line ->
            val previous = editor.lines.firstOrNull { it.id == line.id }
            if (previous != null && previous.productCode != line.productCode)
                line.copy(sourceMatchConfirmed = false, sourceReview = null)
            else line
        })
        mutableState.update { it.copy(sales = it.sales.copy(editor = checked,
            hasLocalDraftEdit = checked != it.sales.draft, editorError = null)) }
        persistLocal()
    }
    private fun saveSalesDraftEdit() {
        val editor = state.value.sales.editor ?: return
        if (state.value.sales.pendingCodexOperation.isNotBlank() || state.value.sales.pendingNewProductOperation.isNotBlank()) {
            mutableState.update { it.copy(sales = it.sales.copy(editorError = "原 Codex 操作结果尚未确认；编辑已保留，请先核对原操作")) }; return
        }
        if (state.value.sales.pendingDocumentAdoptionId.isNotBlank()) {
            mutableState.update { it.copy(sales = it.sales.copy(editorError = "原资料加入结果尚未确认；编辑保留，请先核对原结果")) }; return
        }
        if (state.value.sales.pendingDraftSave) {
            mutableState.update { it.copy(sales = it.sales.copy(editorError =
                "原草稿保存结果未确认，请先重试原保存请求；新编辑已保留")) }
            return
        }
        if (editor.version != sessionVersions[editor.id]) {
            mutableState.update { it.copy(sales = it.sales.copy(editorError = "草稿版本已改变；编辑内容已保留，请先核对当前版本")) }; return
        }
        val body = JSONObject().put("version", editor.version).put("customer_name", editor.customerName)
            .put("customer_order_no", editor.customerOrderNo).put("delivery_date", editor.dueDate).put("note", editor.note)
            .put("tax_basis", editor.taxBasis).put("tax_rate", "13")
            .put("source_row_actions", JSONArray(editor.sourceActions.map { action -> JSONObject()
                .put("source_row_id", action.id).put("action", "exclude").put("reason", action.reason) }))
            .put("lines", JSONArray(editor.lines.map { line -> JSONObject().put("id", line.id).put("product_code", line.productCode)
                .put("quantity", line.quantity).put("unit_price", line.unitPrice).put("unit", line.unit)
                .put("tax_basis", line.taxBasis).put("tax_rate", "13")
                .put("warehouse", line.warehouse).put("delivery_date", line.dueDate).also { row ->
                    val original = state.value.sales.draft?.lines?.firstOrNull { it.id == line.id }
                    if (original == null) row.put("new_line", true)
                    else if (original.productCode != line.productCode) row.put("replace_product_code", true)
                    if (line.summaryMetadataConfirmed) row.put("summary_metadata_confirmed", true)
                        .put("summary_dimension", line.summaryDimension).put("project_no", line.projectNo)
                        .put("drawing_no", line.drawingNo).put("grade", line.grade).put("surface", line.surface)
                        .also { if (line.summaryDimensionSource.isNotBlank()) it.put("summary_dimension_source", line.summaryDimensionSource) }
                    if (line.sourceMatchConfirmed && line.sourceReview?.evidenceHash?.isNotBlank() == true)
                        row.put("source_match_confirmed", true).put("source_match_evidence_hash", line.sourceReview.evidenceHash)
                    lineIdentities[line.id]?.takeIf { it.first == line.productCode }?.second?.let { row.put("identity", it) }
                } }))
        salesMutation("draft", editor.id, editor.version, body, "/sales-assistant/sessions/${pathId(editor.id)}/draft", "PUT") {
            acceptSession(it); persistLocal()
        }
    }
    private fun adoptLatestSalesDraftVersion() {
        val sales = state.value.sales; val editor = sales.editor ?: return; val latest = sales.draft ?: return
        if (sales.pendingDraftSave) {
            mutableState.update { it.copy(sales = it.sales.copy(editorError =
                "原草稿保存结果未确认，请先重试原保存请求；本地编辑已保留")) }
            return
        }
        if (editor.lines.associate { it.id to it.productCode } != latest.lines.associate { it.id to it.productCode }) {
            mutableState.update { it.copy(sales = it.sales.copy(editorError =
                "明细行或商品已变化，不能直接覆盖服务器草稿；本地输入已保留，请逐行核对后重新编辑")) }
            return
        }
        val token = salesReadTokens[editor.id].orEmpty()
        if (sales.busy || sales.loading || latest.id != editor.id || sales.session?.id != editor.id ||
            latest.version <= editor.version || latest.erpOrderNo.isNotBlank() || latest.status !in setOf("draft", "confirmed") ||
            token.isBlank() || token == editorOpenedReadToken || sessionVersions[editor.id] != latest.version) {
            mutableState.update { it.copy(sales = it.sales.copy(editorError =
                "请先刷新并核对服务器草稿；本地编辑内容已保留")) }
            return
        }
        editorOpenedReadToken = token
        mutableState.update { it.copy(sales = it.sales.copy(editor = editor.copy(version = latest.version),
            editorError = "已保留本地编辑并改用服务器第 ${latest.version} 版；请再次核对各行后保存")) }
        persistLocal()
    }
    private fun closeSalesDraftEditor() {
        if (!state.value.sales.editorBusy) {
            editorOpenedReadToken = ""
            mutableState.update { it.copy(sales = it.sales.copy(editorOpen = false)) }
        }
        persistLocal()
    }
    private fun discardSalesDraftEdit() {
        val sales = state.value.sales; val id = sales.session?.id ?: return
        if (!bind(ROLE_SALES) || sales.busy || sales.loading || sales.editorBusy) return
        val key = context(); val run = generation; val slot = draftSlot(key, id)
        mutableState.update { it.copy(sales = it.sales.copy(busy = true, editorError = null)) }
        scope.launch {
            try {
                localSave?.join()
                val saved = load(slot)?.let(::JSONObject)
                check(saved == null || !unknownDraftSlot(saved)) {
                    "原草稿保存结果未确认，不能丢弃；请先重试原保存请求"
                }
                if (saved != null) check(store(slot, null)) { "无法清理已拒绝的原请求，编辑仍保留" }
                if (!current(key, run) || state.value.sales.session?.id != id) return@launch
                editorOpenedReadToken = ""
                mutableState.update { it.copy(sales = it.sales.copy(editor = null, editorOpen = false,
                    hasLocalDraftEdit = false, pendingDraftSave = false, editorError = null)) }
                persistLocal()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(editorError = text(error))) }
            } finally {
                if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(busy = false)) }
            }
        }
    }
    private fun retryUnknownSalesDraftSave() {
        val sales = state.value.sales; val id = sales.session?.id ?: return
        if (!bind(ROLE_SALES) || sales.busy || sales.loading || sales.editorBusy) return
        val key = context(); val run = generation; val slot = draftSlot(key, id)
        mutableState.update { it.copy(sales = it.sales.copy(busy = true, editorBusy = true,
            editorError = null, error = null)) }
        scope.launch {
            var requestSent = false
            var requestAcknowledged = false
            try {
                localSave?.join()
                val saved = load(slot)?.let(::JSONObject)
                check(saved != null && unknownDraftSlot(saved)) { "没有待重试的原草稿保存请求" }
                val frozen = frozenRequest(saved)
                check(frozen.optString("idempotency_key").isNotBlank() && frozen.optInt("version") > 0 &&
                    frozen.optJSONArray("lines") != null) { "原草稿保存请求不完整，不能重试" }
                if (!current(key, run) || !authorized(ROLE_SALES) || state.value.sales.session?.id != id) return@launch
                requestSent = true
                val response = repository.assistantRequest(
                    "/sales-assistant/sessions/${pathId(id)}/draft", frozen, "PUT")
                requestAcknowledged = true
                check(response.optJSONObject("session")?.optString("id") == id) {
                    "原草稿保存响应会话身份不一致，继续保留原请求"
                }
                val readback = repository.assistantRequest("/sales-assistant/sessions/${pathId(id)}")
                check(readback.optJSONObject("session")?.optString("id") == id) {
                    "原草稿保存回查身份不一致，继续保留原请求"
                }
                val recovery = recoverSavedDraftEditor(state.value.sales.editor, frozen, response, readback)
                if (!acceptSession(readback, preserveEditor = true)) return@launch
                salesReadTokens[id] = UUID.randomUUID().toString()
                check(store(slot, null)) { "原请求回查成功，但本地状态未能保存，请再次核对" }
                if (!current(key, run) || state.value.sales.session?.id != id) return@launch
                mutableState.update { state -> state.copy(sales = state.sales.copy(
                    editor = recovery.editor ?: state.sales.draft?.copy(),
                    hasLocalDraftEdit = true, pendingDraftSave = false,
                    editorError = if (recovery.warning.isBlank())
                        "原保存请求已按相同标识读回；本地编辑仍保留，请核对服务器稿后明确采用或丢弃"
                    else "原保存已读回；${recovery.warning}。本地编辑已保留")) }
                persistLocal()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                val knownRejection = requestSent && !requestAcknowledged && error is ServiceException &&
                    error.status in setOf(400, 403, 409, 422)
                if (knownRejection) {
                    load(slot)?.let { saved -> store(slot, JSONObject(saved).put("pending_unknown", false)
                        .put("requires_review", true).put("review_reason", if (error.code == "sales_draft_version_changed")
                            "version_conflict" else "rejected")
                        .put("review_message", text(error))
                        .put("review_read_token", salesReadTokens[id].orEmpty()).toString()) }
                } else if (requestSent) {
                    load(slot)?.let { saved -> store(slot, JSONObject(saved).put("pending_unknown", true).toString()) }
                }
                if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(
                    pendingDraftSave = !knownRejection,
                    editorError = "原草稿保存仍未核定：${text(error)}；本地编辑和原请求均已保留")) }
            } finally {
                if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(busy = false, editorBusy = false)) }
            }
        }
    }
    private fun attachSalesFile(value: AssistantLocalAttachment) {
        if (!bind(ROLE_SALES) || state.value.sales.session == null || state.value.sales.loading ||
            state.value.sales.pendingCodexOperation.isNotBlank() || state.value.sales.codexJob?.running == true ||
            state.value.sales.pendingNewProductOperation.isNotBlank() || state.value.sales.newProductPlans.any { it.running } ||
            state.value.sales.draft?.status !in setOf("draft", "confirmed") || state.value.sales.draft?.erpOrderNo?.isNotBlank() == true ||
            state.value.sales.pendingDraftSave || state.value.sales.hasLocalDraftEdit || state.value.sales.pendingDocumentAdoptionId.isNotBlank() ||
            (state.value.sales.busy && uploads?.isActive != true)) return
        if ((value.sizeBytes ?: 0) > 10 * 1024 * 1024 || state.value.sales.attachments.size >= 5) {
            salesError("每次最多 5 个附件，每个不得超过 10 MB"); return
        }
        val id = UUID.randomUUID().toString(); localFiles[id] = value
        mutableState.update { it.copy(sales = it.sales.copy(attachments = it.sales.attachments +
            AssistantAttachmentUi(id, value.name, "pending", "手机本地附件，尚未上传"), error = null)) }
        persistLocal()
        if (!attachmentUploadEnabled) return
        uploadPendingFiles()
    }
    private fun retrySalesAttachment(id: String) {
        if (!bind(ROLE_SALES) || state.value.sales.busy || id !in localFiles) return
        mutableState.update { it.copy(sales = it.sales.copy(attachments = it.sales.attachments.map { row ->
            if (row.id == id && row.status in setOf("failed", "pending")) row.copy(status = "pending", detail = "等待核对原上传结果") else row })) }
        uploadPendingFiles()
    }
    private fun uploadPendingFiles() {
        if (uploads?.isActive == true || !attachmentUploadEnabled) return
        val sessionId = state.value.sales.session?.id ?: return
        val key = context(); val run = generation
        mutableState.update { it.copy(sales = it.sales.copy(busy = true)) }
        uploads = scope.launch {
            while (current(key, run) && authorized(ROLE_SALES) && state.value.sales.session?.id == sessionId) {
                val row = state.value.sales.attachments.firstOrNull { it.status == "pending" && it.id in localFiles } ?: break
                val id = row.id; val value = localFiles[id] ?: break
                val version = sessionVersions[sessionId] ?: break
                mutableState.update { it.copy(sales = it.sales.copy(attachments = it.sales.attachments.map { file ->
                    if (file.id == id) file.copy(status = "uploading", detail = "正在上传") else file })) }
                try {
                    val bytes = readAttachment(value)
                    check(bytes.isNotEmpty() && bytes.size <= 10 * 1024 * 1024) { "附件为空或超过 10 MB" }
                    val hash = java.security.MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
                    val slot = "attachment:$key:$sessionId:${value.uri}"
                    val existing = load(slot)?.let(::JSONObject)
                    val frozen = existing ?: JSONObject().put("version", version).put("idempotency_key", id)
                        .put("sha256", hash).put("name", value.name).put("mime_type", value.mimeType).also {
                            check(store(slot, it.toString())) { "无法保存附件上传标识，未发送文件" }
                        }
                    check(frozen.optString("sha256") == hash && frozen.optString("name") == value.name &&
                        frozen.optString("mime_type") == value.mimeType) { "原附件上传结果未确认，文件内容已改变，请先核对会话" }
                    if (!current(key, run) || state.value.sales.session?.id != sessionId || !authorized(ROLE_SALES)) break
                    var response = repository.uploadSalesAttachment(sessionId, frozen.optInt("version"), frozen.optString("idempotency_key"), value.name, value.mimeType, bytes)
                    if (!current(key, run) || state.value.sales.session?.id != sessionId) break
                    check(response.optJSONObject("session")?.optString("id") == sessionId) { "附件响应会话身份不一致，原上传保留待核对" }
                    if (response.optBoolean("replayed")) {
                        response = repository.assistantRequest("/sales-assistant/sessions/${pathId(sessionId)}")
                        if (!current(key, run)) break
                    }
                    if (!acceptSession(response) || !current(key, run)) break
                    store(slot, null)
                    if (!current(key, run)) break
                    localFiles.remove(id)
                    mutableState.update { it.copy(sales = it.sales.copy(attachments = it.sales.attachments.map { file ->
                        if (file.id == id) file.copy(status = "needs_review", detail = "已上传，解析和识别结果待核对") else file })) }
                    persistLocal()
                } catch (cancelled: CancellationException) { throw cancelled }
                catch (error: Exception) {
                    if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(error = "附件上传未确认：${text(error)}；本地文件已保留",
                        attachments = it.sales.attachments.map { file -> if (file.id == id) file.copy(status = "failed", detail = "本地附件，上传未确认") else file })) }
                    // An unknown response may have consumed the session version. Pause the queue.
                    break
                }
            }
            if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(busy = false)) }
        }
    }
    private fun removeSalesAttachment(id: String) {
        if (state.value.sales.busy) return
        localFiles.remove(id)
        mutableState.update { it.copy(sales = it.sales.copy(attachments = it.sales.attachments.filterNot { file -> file.id == id })) }
        persistLocal()
    }

    private fun viewSalesAttachment(attachmentId: String) {
        if (!bind(ROLE_SALES) || state.value.sales.viewingAttachmentId.isNotBlank()) return
        val session = state.value.sales.session ?: return
        val attachment = session.documentAttachments.firstOrNull { it.id == attachmentId } ?: return
        if (attachment.mimeType !in setOf("application/pdf", "image/png", "image/jpeg", "image/webp") ||
            !attachment.sha256.matches(Regex("[0-9a-f]{64}")) || attachment.sizeBytes !in 1..10L * 1024 * 1024) {
            salesError("原附件身份或类型尚未核实，请刷新会话后再查看"); return
        }
        val key = context(); val run = generation
        fun stillCurrent() = current(key, run) && authorized(ROLE_SALES) && state.value.sales.session?.id == session.id &&
            state.value.panel == AssistantPanel.Sales
        mutableState.update { it.copy(sales = it.sales.copy(viewingAttachmentId = attachmentId, error = null)) }
        attachmentRead = scope.launch {
            try {
                val file = repository.downloadSalesAttachment(session.id, attachmentId)
                if (!stillCurrent()) return@launch
                val hash = withContext(parserDispatcher) { java.security.MessageDigest.getInstance("SHA-256").digest(file.bytes)
                    .joinToString("") { "%02x".format(it) } }
                check(file.bytes.size.toLong() == attachment.sizeBytes && hash == attachment.sha256 && file.mimeType == attachment.mimeType) {
                    "原附件完整性校验不一致，未打开，请刷新会话核对"
                }
                if (stillCurrent()) viewAttachment(attachment.name, file, ::stillCurrent)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { if (stillCurrent()) salesError("原附件未打开：${text(error)}；草稿和附件保留") }
            finally { if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(viewingAttachmentId = "")) } }
        }
    }

    private fun adoptSalesDocument(attachmentId: String, resultHash: String) {
        val sales = state.value.sales
        val attachment = sales.session?.documentAttachments?.firstOrNull { it.id == attachmentId } ?: return
        val processing = attachment.documentProcessing ?: return
        if (sales.hasLocalDraftEdit || sales.pendingDraftSave || sales.pendingDocumentAdoptionId.isNotBlank() ||
            sales.pendingCodexOperation.isNotBlank() || sales.pendingNewProductOperation.isNotBlank() || sales.codexJob?.running == true) {
            salesError("手机上有未保存编辑或待核对操作，先处理后再加入识别结果"); return
        }
        if (processing.status != "ready" || processing.unreadyPages.isNotEmpty() || resultHash.isBlank() || resultHash != processing.resultHash) {
            salesError("附件识别结果尚未完整就绪，请刷新核对；不会加入草稿"); return
        }
        runSalesDocumentAdoption(attachmentId, resultHash, retryOriginal = false)
    }

    private fun retryUnknownSalesDocumentAdoption() {
        val attachmentId = state.value.sales.pendingDocumentAdoptionId
        if (attachmentId.isBlank()) return
        runSalesDocumentAdoption(attachmentId, "", retryOriginal = true)
    }

    /** A lost reply is retried only by an explicit click, with the original body and key. */
    private fun runSalesDocumentAdoption(attachmentId: String, resultHash: String, retryOriginal: Boolean) {
        if (!bind(ROLE_SALES) || state.value.sales.busy || state.value.sales.editorBusy || state.value.sales.loading) return
        val sales = state.value.sales
        val session = sales.session ?: return
        if (sales.hasLocalDraftEdit || sales.pendingDraftSave) { salesError("请先保存或明确丢弃手机上的草稿修改，资料原请求仍保留"); return }
        val key = context(); val run = generation; val slot = documentSlot(key, session.id)
        mutableState.update { it.copy(sales = it.sales.copy(busy = true, error = null)) }
        scope.launch {
            var requestSent = false
            var acknowledged = false
            try {
                localSave?.join()
                val saved = load(slot)?.let(::JSONObject)
                val frozen = if (retryOriginal) {
                    check(saved?.optBoolean("pending_unknown") == true && saved.optString("attachment_id") == attachmentId) {
                        "原资料请求不存在或身份不一致，请保留输入并核对会话"
                    }
                    saved!!
                } else {
                    check(saved == null || !saved.optBoolean("pending_unknown")) { "原资料加入结果未确认，请先核对原结果" }
                    check(session.dataVersion.isNotBlank()) { "资料数据版本尚未就绪，请刷新后再核对" }
                    JSONObject().put("attachment_id", attachmentId).put("pending_unknown", false)
                        .put("request", JSONObject().put("version", sessionVersions[session.id])
                            .put("data_version", session.dataVersion).put("result_hash", resultHash)
                            .put("idempotency_key", UUID.randomUUID().toString())).also {
                            check(store(slot, it.toString())) { "资料操作无法落盘，尚未发送" }
                        }
                }
                if (!current(key, run) || state.value.sales.session?.id != session.id || !authorized(ROLE_SALES)) return@launch
                val request = frozen.getJSONObject("request")
                requestSent = true
                val response = repository.assistantRequest("/sales-assistant/sessions/${pathId(session.id)}/attachments/${pathId(attachmentId)}/adopt", request, "POST")
                acknowledged = true
                if (!current(key, run) || state.value.sales.session?.id != session.id) return@launch
                fun adopted(raw: JSONObject): Boolean {
                    val returned = raw.optJSONObject("session") ?: return false
                    if (returned.optString("id") != session.id) return false
                    val files = returned.optJSONArray("attachments") ?: return false
                    return (0 until files.length()).mapNotNull(files::optJSONObject).any { attachment ->
                        val processing = attachment.optJSONObject("document_processing") ?: return@any false
                        attachment.optString("id") == attachmentId && processing.optString("status") == "adopted" &&
                            processing.optString("result_hash") == request.optString("result_hash") &&
                            processing.optInt("adopted_version") > request.optInt("version")
                    }
                }
                check(adopted(response)) { "资料加入回执不完整，原请求保留待核对" }
                val readback = repository.assistantRequest("/sales-assistant/sessions/${pathId(session.id)}")
                if (!current(key, run) || state.value.sales.session?.id != session.id) return@launch
                check(adopted(readback)) { "资料加入回查不一致，原请求保留待核对" }
                // Never overwrite edits that appeared during an asynchronous readback.
                if (!acceptSession(readback, preserveEditor = true)) return@launch
                check(store(slot, null)) { "资料已加入，手机请求清理失败；原结果仍需核对" }
                mutableState.update { it.copy(sales = it.sales.copy(pendingDocumentAdoptionId = "",
                    notice = "识别结果已加入草稿；请核对每行原文、商品编码和数量后保存确认")) }
                salesReadTokens[session.id] = UUID.randomUUID().toString()
                persistLocal()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                val unknown = (retryOriginal && !requestSent) || (requestSent &&
                    (acknowledged || error !is ServiceException || error.status !in setOf(400, 403, 409, 422, 503)))
                load(slot)?.let { saved -> store(slot, JSONObject(saved).put("pending_unknown", unknown).toString()) }
                if (current(key, run) && state.value.sales.session?.id == session.id) mutableState.update {
                    it.copy(sales = it.sales.copy(pendingDocumentAdoptionId = if (unknown) attachmentId else "",
                        error = text(error) + if (unknown) "；原请求已保留，请点击核对原加入结果，不会自动重试"
                        else "；识别资料和草稿保留，请刷新后核对")) }
            } finally {
                if (current(key, run)) mutableState.update { it.copy(sales = it.sales.copy(busy = false)) }
            }
        }
    }
}

internal object AssistantJson {
    private fun rows(value: JSONObject, field: String): List<JSONObject> = value.optJSONArray(field)?.let { array ->
        (0 until array.length()).mapNotNull(array::optJSONObject)
    }.orEmpty()
    private fun issueMessage(value: String, fallback: String = "信息需核对"): String = when {
        value.isBlank() -> fallback
        !value.matches(Regex("[A-Za-z][A-Za-z0-9_]*")) -> value
        else -> when (value) {
            "delivery_missing" -> "请填写整单交期或本行交期"
            "delivery_date_invalid" -> "交期需为 YYYY-MM-DD"
            "delivery_before_order_date" -> "交期早于当前录单日期，请更新交期"
            "quantity_invalid" -> "数量须为正数"
            "unit_price_missing_or_invalid" -> "请确认销售单价，不自动猜价"
            "tax_basis_invalid" -> "报价须注明含税或未税"
            "tax_rate_invalid" -> "销售税率固定为 13%，请核对报价"
            "product_identity_review" -> "商品编码或 SKU／仓库／单位身份不唯一，请核对"
            "unit_mismatch" -> "填写的单位与 ERP 基本单位不符"
            "source_product_match_unconfirmed" -> "原文与商品资料有差异，请核对后明确使用此商品编码"
            "source_match_evidence_changed" -> "原文或目录已变化，请重新核对商品对应"
            else -> fallback
        }
    }
    private fun issues(value: JSONObject, field: String = "issues") = value.optJSONArray(field)?.let { array ->
        (0 until array.length()).mapNotNull { index -> when (val item = array.opt(index)) {
            is JSONObject -> issueMessage(item.optString("message").ifBlank { item.optString("code") })
            is String -> issueMessage(item)
            else -> null
        } }
    }.orEmpty()
    private fun lineIssues(line: JSONObject, draft: JSONObject, ordinal: Int): String {
        val messages = rows(draft, "issues")
        val problems = line.optJSONArray("issues") ?: return ""
        return (0 until problems.length()).mapNotNull { index ->
            val raw = problems.opt(index)
            val code = if (raw is JSONObject) raw.optString("code") else raw as? String ?: return@mapNotNull null
            val ownMessage = (raw as? JSONObject)?.optString("message").orEmpty()
            val matching = messages.firstOrNull { issue -> issue.optString("code") == code &&
                ((issue.has("line_id") && issue.optString("line_id") == line.optString("id")) ||
                    issue.optInt("line") == ordinal) }?.optString("message").orEmpty()
            issueMessage(ownMessage.ifBlank { matching.ifBlank { code } }, "本行信息需核对")
        }.distinct().joinToString("；")
    }
    fun sessions(value: JSONObject) = rows(value, "items").map(::session)
    fun newProductPlans(session: JSONObject) = rows(session, "new_product_plans").mapNotNull { value ->
        val id = value.optString("id").takeIf(String::isNotBlank) ?: return@mapNotNull null
        val status = value.optString("status")
        val hash = value.opt("plan_hash") as? String ?: ""
        val classification = when (val path = value.opt("classification_path")) {
            is JSONArray -> (0 until path.length()).mapNotNull { path.opt(it) as? String }.filter(String::isNotBlank).joinToString(" / ")
            is String -> path
            else -> ""
        }
        val productName = value.optString("product_name")
        val code = value.optString("code")
        val unit = value.optString("base_unit")
        SalesNewProductPlanUi(id, status, value.optString("suggestion_id"), value.optString("job_id"),
            value.optString("source_result_hash"), hash, productName, value.optString("spec"), value.optString("model"), code,
            unit, classification, issues(value), status == "ready_for_confirmation" && value.opt("can_confirm") == true &&
                hash.isNotBlank() && productName.isNotBlank() && code.isNotBlank() && unit.isNotBlank() && classification.isNotBlank(),
            value.opt("can_prepare") == true, value.optString("created_code"), value.optString("error_code"), value.optString("material"))
    }
    fun codexJob(session: JSONObject): SalesCodexJobUi? {
        val raw = session.optJSONObject("codex_job") ?: return null
        val id = raw.optString("id").takeIf(String::isNotBlank) ?: return null
        val status = raw.optString("status")
        val baseVersion = raw.optInt("base_version")
        val preview = raw.optJSONObject("preview")?.let { value ->
            draft(JSONObject().put("id", session.optString("id")).put("version", baseVersion)
                .put("status", "codex_preview").put("customer_name", session.optString("customer_name"))
                .put("fixed_sales_owner_name", session.optString("fixed_sales_owner_name"))
                .put("draft", JSONObject(value.toString())))?.copy(canConfirm = false, canSubmit = false)
        }
        return SalesCodexJobUi(id, status, baseVersion, raw.optString("result_hash"), raw.optString("assistant_text"),
            issues(raw), preview, status == "ready_for_review" && raw.opt("can_adopt") == true &&
                baseVersion > 0 && baseVersion == session.optInt("version") && raw.optString("result_hash").isNotBlank() &&
                rows(raw, "new_product_suggestions").isEmpty(),
            rows(raw, "new_product_suggestions").mapIndexed { index, value -> SalesNewProductSuggestionUi(
                value.optString("id").ifBlank { "$id:$index" }, value.optString("product_name"),
                value.optString("specification").ifBlank { value.optString("spec") },
                value.optString("proposed_code").ifBlank { value.optString("product_code") },
                value.optString("reason"), value.optString("status").ifBlank { "pending_review" },
                value.opt("can_prepare") == true && value.optString("id").isNotBlank(), value.optString("material")) })
    }
    fun webOperations(value: JSONObject) = rows(value, "items").mapNotNull { row ->
        val id = row.optString("id")
        if (id.isBlank()) null else WebOperationUi(id, row.optString("label"), row.optString("status"),
            row.optString("stage"), row.optString("detail"), row.optInt("step"),
            row.optInt("total_steps"), row.optString("created_at"))
    }
    fun session(value: JSONObject): SalesSessionUi {
        val attachments = rows(value, "attachments").associate { row -> row.optString("id") to
            AssistantAttachmentUi(row.optString("id"), row.optString("name"), row.optString("status"),
                when (row.optJSONObject("document_processing")?.optString("status")) {
                    "queued" -> "已保存附件，等待识别"
                    "running" -> "正在识别，原件已保存"
                    "ready" -> "已提取，待逐行核对"
                    "adopted" -> "已加入草稿，仍需确认"
                    "failed" -> "识别未完成，原件仍保留"
                    else -> if (row.optString("status") == "needs_processing") "已保存附件，识别进度见下方" else "已上传，待核对"
                },
                row.optJSONObject("document_processing")?.let { processing -> SalesDocumentProcessingUi(
                    processing.optString("status"), processing.optString("message"), processing.optString("error_code"),
                    processing.optInt("page_count"), processing.optInt("line_count"), processing.optInt("candidate_count"),
                    processing.optJSONArray("unready_pages")?.let { pages -> (0 until pages.length()).map { index ->
                        (pages.opt(index) as? Number)?.toInt()?.takeIf { it > 0 } ?: 0 } }.orEmpty(), processing.optString("result_hash"),
                    processing.optInt("adopted_version").takeIf { it > 0 }) }, row.optString("mime_type"), row.optString("sha256"), row.optLong("size")) }
        return SalesSessionUi(value.optString("id"), value.optString("customer_name").ifBlank { "录单草稿" },
            value.optString("updated_at"), rows(value, "messages").map { row -> AssistantMessageUi(row.optString("id"),
                row.optString("role"), row.optString("text"), listOfNotNull(attachments[row.optString("attachment_id")])) },
            value.optString("profile_id"), attachments.values.filter { it.documentProcessing != null }, value.optString("data_version"))
    }
    private fun sourceLabel(value: JSONObject, source: JSONObject): String {
        val file = rows(value, "attachments").firstOrNull { it.optString("id") == source.optString("source_id") }?.optString("name").orEmpty()
        val page = source.optInt("page").takeIf { it > 0 }?.let { "第 $it 页" }.orEmpty()
        val row = source.optInt("row").takeIf { it > 0 } ?: source.optInt("line").takeIf { it > 0 }
        return listOf(file, source.optString("sheet"), page, row?.let { "第 $it 行" }.orEmpty())
            .filter { it.isNotBlank() }.joinToString(" · ")
    }
    private fun sourceProduct(value: JSONObject?) = SalesSourceProductUi(value?.optString("product_name").orEmpty(),
        value?.optString("spec").orEmpty(), value?.optString("model").orEmpty(), value?.optString("grade").orEmpty(), value?.optString("surface").orEmpty(),
        value?.optString("standard").orEmpty())
    private fun sourceReview(value: JSONObject?) = value?.let { review -> SalesSourceReviewUi(review.optBoolean("required"),
        review.optJSONArray("reasons")?.let { values -> (0 until values.length()).mapNotNull { values.optString(it).takeIf(String::isNotBlank) } }.orEmpty(),
        review.optBoolean("confirmed"), review.optString("evidence_hash"), sourceProduct(review.optJSONObject("original")), sourceProduct(review.optJSONObject("catalog"))) }
    private fun sourceProductRecord(value: SalesSourceProductUi) = JSONObject().put("product_name", value.productName)
        .put("spec", value.spec).put("model", value.model).put("grade", value.grade).put("surface", value.surface).put("standard", value.standard)
    private fun sourceReviewRecord(value: SalesSourceReviewUi) = JSONObject().put("required", value.required)
        .put("reasons", JSONArray(value.reasons)).put("confirmed", value.confirmed).put("evidence_hash", value.evidenceHash)
        .put("original", sourceProductRecord(value.original)).put("catalog", sourceProductRecord(value.catalog))
    private fun matchCandidates(value: JSONObject) = rows(value, "match_candidates").map { candidate -> SalesMatchCandidateUi(
        candidate.optString("product_code"), candidate.optString("product_name"), candidate.optString("spec"), candidate.optString("model"),
        candidate.optString("method"), candidate.optInt("evidence_count").coerceAtLeast(0), candidate.optString("last_order_date"),
        candidate.opt("available") == true, candidate.optString("message")) }
    private fun matchStatus(value: JSONObject?) = value?.let { SalesMatchStatusUi(it.optString("status"), it.optString("message"),
        it.optString("covered_from"), it.optString("covered_to")) }
    private fun matchCandidateRecord(value: SalesMatchCandidateUi) = JSONObject().put("product_code", value.productCode)
        .put("product_name", value.productName).put("spec", value.spec).put("model", value.model).put("method", value.method)
        .put("evidence_count", value.evidenceCount).put("last_order_date", value.lastOrderDate).put("available", value.available).put("message", value.message)
    private fun matchStatusRecord(value: SalesMatchStatusUi) = JSONObject().put("status", value.status).put("message", value.message)
        .put("covered_from", value.coveredFrom).put("covered_to", value.coveredTo)
    fun draft(value: JSONObject): SalesDraftUi? {
        val raw = value.optJSONObject("draft") ?: return null
        val status = value.optString("status"); val version = value.optInt("version")
        val confirmed = value.optInt("confirmed_version").takeIf { it > 0 }
        val notes = issues(raw)
        val defaultBasis = if (raw.has("tax_basis")) raw.optString("tax_basis") else "tax_inclusive"
        val taxRate = raw.optString("tax_rate").ifBlank { "13" }
        val lines = rows(raw, "lines").mapIndexed { index, row -> SalesDraftLineUi(row.optString("id").ifBlank { index.toString() },
            row.optString("product_code"), row.optString("product_name"), row.optString("specification").ifBlank {
                row.optString("summary_dimension").ifBlank { row.optString("spec") } },
            row.optString("quantity"), row.optString("unit"), row.optString("quoted_unit_price").ifBlank { row.optString("unit_price") }, row.optString("warehouse"),
            lineIssues(row, raw, index + 1), row.optString("delivery_date"),
            if (row.has("tax_basis")) row.optString("tax_basis") else defaultBasis,
            row.optString("tax_rate").ifBlank { taxRate }, row.optString("untaxed_unit_price"), row.optString("untaxed_line_amount"),
            row.optString("summary_dimension"), row.optString("project_no"), row.optString("drawing_no"),
            row.optString("grade"), row.optString("surface"), row.optBoolean("summary_metadata_confirmed"),
            row.optString("summary_dimension_source"), row.optString("source_label").ifBlank {
                row.optJSONObject("source_ref")?.let { source ->
                    sourceLabel(value, source)
                }.orEmpty()
            }, row.optString("source_preview").ifBlank { row.optString("source_quote").ifBlank {
                row.optJSONObject("source_ref")?.optString("preview").orEmpty() } },
            sourceReview(row.optJSONObject("source_review")), row.optBoolean("source_match_confirmed"),
            row.optString("source_attachment_id").ifBlank { row.optJSONObject("source_ref")?.takeIf { it.optString("kind") == "document" }
                ?.optString("source_id").orEmpty() }, matchCandidates(row), matchStatus(row.optJSONObject("match_status")),
            row.optJSONObject("source_product")?.let(::sourceProduct) ?: if (row.has("original_name") || row.has("original_spec"))
                SalesSourceProductUi(row.optString("original_name"), row.optString("original_spec"), grade = row.optString("grade"),
                    surface = row.optString("surface"), standard = row.optString("standard")) else null,
            row.optJSONObject("catalog_product")?.let(::sourceProduct), row.optString("customer_line_no").ifBlank { row.optString("line_no") },
            row.optString("original_material").ifBlank { row.optJSONObject("source_product")?.optString("material").orEmpty() },
            row.optJSONObject("catalog_product")?.optString("area").orEmpty().ifBlank { row.optString("catalog_area") },
            row.optJSONObject("catalog_product")?.optString("material").orEmpty().ifBlank { row.optString("catalog_material") }) }
        return SalesDraftUi(value.optString("id"), version, status, raw.optString("customer_name").ifBlank { value.optString("customer_name") },
            value.optString("fixed_sales_owner_name"), raw.optString("customer_order_no"), lines, notes,
            canConfirm = status == "draft" && lines.isNotEmpty() && notes.isEmpty() && lines.none { it.issue.isNotBlank() ||
                (it.sourceReview?.required == true && !it.sourceReview.confirmed) },
            canSubmit = status == "confirmed" && confirmed == version && value.optBoolean("submit_available", false),
            confirmedVersion = confirmed.takeIf { status == "confirmed" }, erpOrderNo = value.optString("erp_order_no"), dueDate = raw.optString("delivery_date"),
            taxBasis = defaultBasis, taxRate = taxRate,
            sourceRows = rows(raw, "source_rows").map { source ->
                val label = source.optString("source_label").ifBlank {
                    sourceLabel(value, source)
                }
                SalesSourceRowUi(source.optString("id"), label, source.optString("preview"), source.optString("status"),
                    source.optString("exclusion_reason").ifBlank { source.optString("reason") }, source.optString("line_id"),
                    when {
                        source.has("exclude_allowed") -> source.optBoolean("exclude_allowed")
                        source.has("can_exclude") -> source.optBoolean("can_exclude") // local records from this client
                        else -> source.optString("kind") !in setOf("document_page", "page_coverage", "document_coverage") &&
                            source.optString("scope") !in setOf("attachment_coverage", "page_coverage")
                    })
            }, sourceActions = rows(raw, "source_row_actions").map { SalesSourceActionUi(it.optString("source_row_id"), it.optString("reason")) },
            note = raw.optString("note"), submissionCompletionBasis = value.optString("submission_completion_basis"),
            erpReadbackPerformed = value.opt("erp_readback_performed") == true,
            erpOrderNumbers = value.optJSONArray("erp_order_numbers")?.let { values ->
                (0 until values.length()).map { values.opt(it) as? String ?: "" } }.orEmpty(),
            erpDocumentCount = (value.opt("erp_document_count") as? Number)?.let { count ->
                count.toLong().takeIf { it in 0..Int.MAX_VALUE.toLong() && count.toDouble() == it.toDouble() }?.toInt() } ?: 0,
            erpReferenceScope = value.optString("erp_reference_scope"))
    }
    fun draftRecord(value: SalesDraftUi) = JSONObject().put("id", value.id).put("version", value.version).put("status", value.status)
        .put("submission_completion_basis", value.submissionCompletionBasis).put("erp_readback_performed", value.erpReadbackPerformed)
        .put("erp_order_no", value.erpOrderNo).put("erp_order_numbers", JSONArray(value.erpOrderNumbers))
        .put("erp_document_count", value.erpDocumentCount).put("erp_reference_scope", value.erpReferenceScope)
        .put("customer_name", value.customerName).put("draft", JSONObject().put("customer_name", value.customerName)
            .put("customer_order_no", value.customerOrderNo).put("delivery_date", value.dueDate).put("note", value.note)
            .put("tax_basis", value.taxBasis).put("tax_rate", value.taxRate)
            .put("source_rows", JSONArray(value.sourceRows.map { source -> JSONObject().put("id", source.id)
                .put("source_label", source.label).put("preview", source.preview).put("status", source.status)
                .put("reason", source.reason).put("line_id", source.lineId).put("can_exclude", source.canExclude) }))
            .put("source_row_actions", JSONArray(value.sourceActions.map { action -> JSONObject()
                .put("source_row_id", action.id).put("action", "exclude").put("reason", action.reason) }))
            .put("lines", JSONArray(value.lines.map { row -> JSONObject().put("id", row.id).put("product_code", row.productCode)
                .put("product_name", row.productName).put("specification", row.specification).put("quantity", row.quantity)
                .put("unit", row.unit).put("unit_price", row.unitPrice).put("quoted_unit_price", row.unitPrice)
                .put("tax_basis", row.taxBasis).put("tax_rate", row.taxRate)
                .put("untaxed_unit_price", row.untaxedUnitPrice).put("untaxed_line_amount", row.untaxedLineAmount)
                .put("summary_dimension", row.summaryDimension).put("project_no", row.projectNo).put("drawing_no", row.drawingNo)
                .put("grade", row.grade).put("surface", row.surface).put("summary_metadata_confirmed", row.summaryMetadataConfirmed)
                .put("summary_dimension_source", row.summaryDimensionSource).put("source_label", row.sourceLabel).put("source_preview", row.sourcePreview)
                .put("source_review", row.sourceReview?.let(::sourceReviewRecord)).put("source_match_confirmed", row.sourceMatchConfirmed)
                .put("source_attachment_id", row.sourceAttachmentId)
                .put("source_product", row.previewOriginal?.let(::sourceProductRecord))
                .put("catalog_product", row.previewCatalog?.let(::sourceProductRecord)).put("customer_line_no", row.customerLineNo)
                .put("original_material", row.originalMaterial).put("catalog_area", row.catalogArea).put("catalog_material", row.catalogMaterial)
                .put("match_candidates", JSONArray(row.matchCandidates.map(::matchCandidateRecord)))
                .put("match_status", row.matchStatus?.let(::matchStatusRecord))
                .put("warehouse", row.warehouse).put("delivery_date", row.dueDate) })))
    fun sameObject(left: JSONObject, right: JSONObject) = canonical(left) == canonical(right)
    private fun canonical(value: Any?): String = when (value) {
        is JSONObject -> value.keys().asSequence().toList().sorted().joinToString(prefix = "{", postfix = "}") { key ->
            JSONObject.quote(key) + ":" + canonical(value.opt(key)) }
        is JSONArray -> (0 until value.length()).joinToString(prefix = "[", postfix = "]") { canonical(value.opt(it)) }
        is String -> JSONObject.quote(value)
        null, JSONObject.NULL -> "null"
        else -> value.toString()
    }
    fun job(value: JSONObject): AssistantJobUi {
        val status = value.optString("status")
        val labels = mapOf("waiting_configuration" to "等待平台配置", "waiting_price" to "等待价格资料",
            "awaiting_price_source" to "等待价格资料", "awaiting_platform_check" to "等待平台核验",
            "queued" to "等待处理", "running" to "处理中", "requires_review" to "待核对", "completed" to "已完成",
            "failed" to "执行失败", "awaiting_payment" to "待付款", "awaiting_confirmation" to "等待人工确认")
        return AssistantJobUi(value.optString("id"), status, value.optString("label").ifBlank { labels[status] ?: status },
            value.optInt("completed"), value.optInt("total"), value.optString("detail"),
            value.optBoolean("requires_review") || status in setOf("requires_review", "failed", "uncertain"))
    }
    private fun whitelistCounts(value: JSONObject): PurchaseWhitelistCounts? {
        val raw = value.optJSONObject("whitelist_counts") ?: return null
        fun count(name: String): Int? = (raw.opt(name) as? Number)?.toLong()?.takeIf { it in 0..Int.MAX_VALUE.toLong() }?.toInt()
        return PurchaseWhitelistCounts(count("candidate_total") ?: return null,
            count("verified_total") ?: return null, count("review_total") ?: return null,
            count("matched_demand_total") ?: return null)
    }
    fun purchase(value: JSONObject) = PurchaseAssistantUiState(sourceVersion = value.optString("source_version"),
        whitelistVersion = value.optString("whitelist_version"), generatedAt = value.optString("generated_at"),
        whitelistCounts = whitelistCounts(value),
        items = rows(value, "items").map { row -> PurchasePlanItemUi(row.optString("id"), row.optInt("version"),
            row.optString("product_code"), row.optString("product_name"), row.optString("specification"), row.optString("supplier_name"),
            row.optString("quantity"), row.optString("unit"), row.optString("warehouse"), row.optString("platform"), row.optString("price_limit"),
            row.optInt("source_order_count"), row.optString("state").ifBlank { "review" },
            row.optBoolean("eligible"), row.optBoolean("can_enroll"), row.optString("reason"), row.optJSONObject("job")?.let(::job),
            planId = row.optString("plan_id")) })
}
