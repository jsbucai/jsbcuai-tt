package com.ul.workbench

import android.app.Application
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.ul.workbench.data.*
import com.ul.workbench.device.BlePrinter
import com.ul.workbench.device.PrinterClient
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
import kotlinx.coroutines.delay
import kotlinx.coroutines.async
import kotlinx.coroutines.yield
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.time.OffsetDateTime
import java.util.UUID

class WorkbenchViewModel @JvmOverloads constructor(
    app: Application,
    providedRepository: WorkbenchRepository? = null,
    val printer: PrinterClient = BlePrinter(app),
    providedAdjustmentRepository: WorkbenchRepository? = null
) : AndroidViewModel(app) {
    companion object {
        const val ADJUSTMENT_SERVER = LAN_ADJUSTMENT_SERVER
        const val WAREHOUSE_ID = "29871293290508390"
    }
    private val preferences = app.getSharedPreferences("workbench-settings-v1", 0)
    private val initialUrl = preferences.getString("server", null).let { saved ->
        if (saved == null || saved.trimEnd('/') in setOf("https://ul.yingyinghome.com", "https://ul.yingyinghome.com:443")) {
            "https://ul.yingyinghome.com:8443".also { preferences.edit().putString("server", it).apply() }
        } else saved
    }
    private val deviceId = preferences.getString("device-id", null)?.takeIf(String::isNotBlank) ?: UUID.randomUUID().toString().also {
        preferences.edit().putString("device-id", it).commit()
    }
    private val mutableState = MutableStateFlow(WorkbenchState(destination = Destination.Tasks, serverUrl = initialUrl, connectionRequired = true,
        adjustmentUpload = false, deviceId = deviceId))
    val state = mutableState.asStateFlow()
    private val sessionVault = SessionVault(app)
    private fun httpRepository(url: () -> String) = HttpWorkbenchRepository(sessionVault, { origin ->
        viewModelScope.launch {
            if (state.value.serverUrl == origin) mutableState.update {
                it.copy(loginRequired = true, connectionRequired = false, serverOnline = false, serverStatus = "请重新登录")
            }
        }
    }, url)
    private val repository: WorkbenchRepository = providedRepository ?: httpRepository { state.value.serverUrl }
    private var assistantProfileId = ""
    private var assistantProfileOrigin = ""
    private val assistant = AssistantController(repository, viewModelScope,
        context = { state.value.serverUrl + "\n" + state.value.dailyWork.me?.id.orEmpty() +
            if (state.value.loginRequired || state.value.dailyWork.me?.active != true) "\nunauthorized" else "" },
        user = { state.value.dailyWork.me },
        profileId = { assistantProfileId.takeIf { assistantProfileOrigin == state.value.serverUrl }.orEmpty() },
        load = { key -> withContext(Dispatchers.IO) { preferences.getString("assistant:$key", null) } },
        store = { key, value -> withContext(Dispatchers.IO) { preferences.edit().let { editor ->
            if (value == null) editor.remove("assistant:$key") else editor.putString("assistant:$key", value)
        }.commit() } },
        readAttachment = { file -> withContext(Dispatchers.IO) {
            val uri = Uri.parse(file.uri)
            require(uri.scheme in setOf("content", "file")) { "附件地址无效" }
            app.contentResolver.openInputStream(uri)?.use { input ->
                val output = java.io.ByteArrayOutputStream()
                val buffer = ByteArray(32 * 1024)
                while (true) {
                    val count = input.read(buffer); if (count < 0) break
                    require(output.size() + count <= 10 * 1024 * 1024) { "附件超过 10 MB" }
                    output.write(buffer, 0, count)
                }
                output.toByteArray()
            } ?: error("无法读取附件，请重新选择")
        } }, viewAttachment = { _, file, stillCurrent ->
            com.ul.workbench.ui.screens.SalesAttachmentViewer.open(app, file, stillCurrent)
        })
    val assistantState = assistant.state
    val assistantActions = assistant.actions
    private val purchaseUnfinished = PurchaseUnfinishedController(repository, viewModelScope,
        read = { state.value.dailyWork.purchaseUnfinished },
        update = { transform -> mutableState.update { current -> current.copy(dailyWork = current.dailyWork.copy(
            purchaseUnfinished = transform(current.dailyWork.purchaseUnfinished))) } },
        context = { state.value.serverUrl + "\n" + state.value.dailyWork.me?.id.orEmpty() },
        authorized = { state.value.dailyWork.me?.let { it.active && (it.isAdmin || ROLE_PURCHASE in it.roles) } == true },
        loadDraft = { key -> preferences.getString("purchase-reply:$key", null) },
        writeDraft = { key, value -> preferences.edit().let { editor ->
            if (value == null) editor.remove("purchase-reply:$key") else editor.putString("purchase-reply:$key", value)
        }.commit() })
    private val adjustmentRepository: WorkbenchRepository = providedAdjustmentRepository ?: providedRepository ?: httpRepository { ADJUSTMENT_SERVER }
    private val adjustmentOutbox = AdjustmentOutbox(preferences, viewModelScope,
        { origin -> if (origin == ADJUSTMENT_SERVER) adjustmentRepository else
            providedRepository ?: httpRepository { origin } },
        { value -> mutableState.update { it.copy(adjustments = value) } },
        { products -> refreshInventorySubset(products) })
    private var queryJob: Job? = null
    private var queryGeneration = 0
    private var queryRevision = 0
    private var pendingPage: Int? = null
    private var pendingAutoSelect = true
    private val workspaces = mutableMapOf<Destination, QueryWorkspace>()
    private var healthGeneration = 0
    private var adjustmentProfile = "default"
    private var adjustmentOrigin = initialUrl
    private var updatePoll: Job? = null
    private var dailyWorkPoll: Job? = null
    private var dailyWorkRequest: Job? = null
    private var dailyActionReplay: Job? = null
    private var printRecovery: Job? = null
    private var salesStartUndoExpiry: Job? = null
    private var salesStartUndoRequest: PendingDailyAction? = null
    private var dailyWorkVisible = false
    private var dailyWorkGeneration = 0
    private val boardPageDepths = mutableMapOf<String, Int>()
    private val boardPages = linkedMapOf<String, TaskBoardPage>()
    private var initialTaskNavigationPending = true
    private val refreshWithoutSelection = mutableSetOf<Destination>()
    private var pendingPublication: Pair<String, String>? = null
    private var appliedPublication = ""
    private var splitAll = false
    private val pendingDailyActionsKey = "pending-daily-actions-v1"
    @Volatile private var cancelRequested = false
    var editing = false
        private set

    private fun storedDailyActions(): List<PendingDailyAction> = runCatching {
        val values = JSONArray(preferences.getString(pendingDailyActionsKey, "[]") ?: "[]")
        (0 until values.length()).mapNotNull { index -> values.optJSONObject(index)?.let { row ->
            PendingDailyAction(row.optString("idempotency_key"), row.optString("origin"), row.optString("user_id"),
                row.optString("task_id"), row.optString("action"), row.optString("note"),
                row.optString("next_follow_up_date"), row.optInt("version"), row.optString("created_at"),
                row.optString("last_error"), row.optBoolean("requires_review"), row.optString("waiting_for"), row.optString("frozen_request"))
        } }.filter { it.idempotencyKey.isNotBlank() && it.taskId.isNotBlank() }
    }.getOrDefault(emptyList())

    private fun persistDailyActions(values: List<PendingDailyAction>): Boolean {
        val json = JSONArray(values.map { item -> JSONObject()
            .put("idempotency_key", item.idempotencyKey).put("origin", item.origin).put("user_id", item.userId)
            .put("task_id", item.taskId).put("action", item.action).put("note", item.note)
            .put("next_follow_up_date", item.nextFollowUpDate).put("version", item.version)
            .put("created_at", item.createdAt).put("last_error", item.lastError)
            .put("requires_review", item.requiresReview).put("waiting_for", item.waitingFor).put("frozen_request", item.frozenRequest)
        })
        return preferences.edit().putString(pendingDailyActionsKey, json.toString()).commit()
    }

    private fun pendingFor(user: CurrentUser): List<PendingDailyAction> = storedDailyActions().filter {
        it.origin == state.value.serverUrl && it.userId == user.id
    }

    private fun persistPendingPrint(jobId: String, cancelKey: String, phase: String, claimToken: String = ""): Boolean =
        preferences.edit().putString("pending-job", jobId)
            .putString("pending-action", cancelKey) // Kept for upgrades from the earlier client.
            .putString("pending-cancel-key", cancelKey)
            .putString("pending-print-origin", state.value.serverUrl).putString("pending-print-user", state.value.dailyWork.me?.id ?: preferences.getString("pending-print-user", "").orEmpty())
            .putString("pending-print-state", phase)
            .putString("pending-claim-token", claimToken)
            .remove("pending-prepare-key").remove("pending-prepare-request")
            .commit()

    private fun persistPreparingPrint(prepareKey: String, cancelKey: String, frozenRequest: String): Boolean =
        preferences.edit().remove("pending-job").remove("pending-claim-token")
            .putString("pending-action", cancelKey).putString("pending-cancel-key", cancelKey)
            .putString("pending-prepare-key", prepareKey).putString("pending-prepare-request", frozenRequest)
            .putString("pending-print-origin", state.value.serverUrl).putString("pending-print-user", state.value.dailyWork.me?.id ?: preferences.getString("pending-print-user", "").orEmpty())
            .putString("pending-print-state", "preparing").commit()

    private fun clearPendingPrint() {
        preferences.edit().remove("pending-job").remove("pending-action").remove("pending-cancel-key")
            .remove("pending-print-state").remove("pending-claim-token")
            .remove("pending-prepare-key").remove("pending-prepare-request")
            .remove("pending-print-origin").remove("pending-print-user").apply()
    }

    private fun recoverInterruptedPrint() {
        val storedIdentity = preferences.getString("pending-job", null)?.takeIf(String::isNotBlank)
        val frozenRequest = preferences.getString("pending-prepare-request", null)?.takeIf(String::isNotBlank)
        if (storedIdentity == null && frozenRequest == null) return
        if (printRecovery?.isActive == true) return
        val cancelKey = preferences.getString("pending-cancel-key", null)
            ?: preferences.getString("pending-action", null).orEmpty()
        val claimToken = preferences.getString("pending-claim-token", null).orEmpty()
        val origin = state.value.serverUrl
        printRecovery = viewModelScope.launch {
            try {
                val recordedOrigin = preferences.getString("pending-print-origin", "").orEmpty()
                val recordedUser = preferences.getString("pending-print-user", "").orEmpty()
                if (recordedOrigin.isNotBlank() && recordedOrigin != origin)
                    throw IllegalStateException("请回到建立该打印任务的服务器核对")
                if (recordedUser.isNotBlank()) {
                    val currentUser = state.value.dailyWork.me ?: repository.me()
                    if (currentUser.id != recordedUser) throw IllegalStateException("请使用建立该打印任务的原账号核对")
                }
                val identity = if (storedIdentity == null) {
                    val prepared = repository.replayPrepare(frozenRequest!!)
                    if (!persistPendingPrint(prepared.id, cancelKey, "prepared"))
                        throw IllegalStateException("无法保存找回的打印任务")
                    prepared.id
                } else storedIdentity
                val remoteJob = repository.printJob(identity)
                when (val remote = remoteJob.status) {
                    "revoked" -> {
                        clearPendingPrint()
                        if (state.value.serverUrl == origin) mutableState.update { it.copy(uncertainPrint = false,
                            error = null, receipt = "原打印任务已由管理员撤销，请刷新后重新选择；不会恢复旧发送") }
                    }
                    "prepared" -> {
                        if (cancelKey.length !in 16..128) throw IllegalStateException("缺少安全取消标识")
                        repository.cancelPreparedPrint(identity, cancelKey, deviceId, "App中断后确认尚未领取打印数据")
                        clearPendingPrint()
                        if (state.value.serverUrl == origin) mutableState.update { it.copy(uncertainPrint = false,
                            error = null, receipt = "上次中断的未发送打印任务已安全释放") }
                    }
                    "cancelled", "failed" -> {
                        if (remoteJob.bytesSent == 0) {
                            clearPendingPrint()
                            if (state.value.serverUrl == origin) mutableState.update { it.copy(uncertainPrint = false,
                                error = null, receipt = "上次打印任务已确认未发送") }
                        } else {
                            persistPendingPrint(identity, cancelKey, "review", claimToken)
                            if (state.value.serverUrl == origin) mutableState.update { it.copy(uncertainPrint = true,
                                error = "上次打印虽已结束，但没有明确零字节发送记录；请核对实际出纸，不会自动重打。") }
                        }
                    }
                    "claimed" -> {
                        if (claimToken.isNotBlank()) {
                            repository.report(identity, claimToken, "unknown", "App进程中断，无法确认是否已经出纸", 0, deviceId)
                        } else {
                            if (cancelKey.length !in 16..128) throw IllegalStateException("打印数据已领取，但本机缺少恢复标识")
                            repository.recoverClaimedPrint(identity, cancelKey, deviceId,
                                "领取响应丢失，无法确认是否进入发送步骤")
                        }
                        persistPendingPrint(identity, cancelKey, "review", claimToken)
                        if (state.value.serverUrl == origin) mutableState.update { it.copy(uncertainPrint = true,
                            error = "上次打印在领取数据后中断，已转为待核对；不会自动重打。") }
                    }
                    "submitted", "unknown" -> {
                        persistPendingPrint(identity, cancelKey, "review", claimToken)
                        if (state.value.serverUrl == origin) mutableState.update { it.copy(uncertainPrint = true,
                            error = "上次打印结果为${if (remote == "submitted") "已提交" else "待核对"}，请检查实际出纸；不会自动重打。") }
                    }
                    else -> throw IllegalStateException("打印任务状态 $remote 需要人工核对")
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (failure is ServiceException && failure.code == "print_job_revoked") {
                    clearPendingPrint()
                    if (state.value.serverUrl == origin) mutableState.update { it.copy(uncertainPrint = false,
                        error = null, receipt = "原打印任务已撤销，请刷新后重新选择") }
                    return@launch
                }
                val safeUncreated = storedIdentity == null && failure is ServiceException &&
                    failure.code in setOf("data_version_conflict", "invalid_request", "invalid_id", "print_data_incomplete")
                if (safeUncreated) {
                    clearPendingPrint()
                    if (state.value.serverUrl == origin) {
                        mutableState.update { it.copy(uncertainPrint = false,
                            error = "上次打印准备未在服务器建立：${readable(failure)}；已解除本机占用，请刷新后重新选择。",
                            receipt = "上次打印准备未建立，手机占用已解除") }
                        if (failure.code == "data_version_conflict" && state.value.destination in
                            setOf(Destination.Orders, Destination.Inventory)) load(1, false)
                    }
                } else if (state.value.serverUrl == origin) mutableState.update { it.copy(uncertainPrint = true,
                    error = "上次打印任务 ${(storedIdentity ?: "准备中").take(8)} 尚未安全释放：${readable(failure)}。请保持待核对，不要重复打印。") }
            }
        }
    }

    init {
        viewModelScope.launch {
            state.map { Triple(it.serverUrl, it.dailyWork.me?.let { me ->
                listOf(me.id, me.active.toString(), me.isAdmin.toString(), me.roles.sorted().joinToString(",")) }, it.loginRequired) }
                .distinctUntilChanged().collect { assistant.contextChanged(force = true) }
        }
        adjustmentOutbox.useProfileBucket("default", ADJUSTMENT_SERVER,
            listOf(ADJUSTMENT_SERVER, initialUrl, "https://ul.yingyinghome.com:8443", "https://ul.yingyinghome.com"))
        adjustmentOutbox.holdBatchesOutsideServer(initialUrl)
        val previous = preferences.getString("pending-job", null)
            ?: preferences.getString("pending-prepare-key", null)?.let { "准备中" }
        if (previous != null) mutableState.update { it.copy(uncertainPrint = true,
            error = "上次打印流程中断（任务 ${previous.take(8)}），请先核对标签；不会自动重发。") }
        viewModelScope.launch {
            printer.state.collect { device ->
                val unknown = device.lastAttempt?.takeIf { it.status == "unknown" }
                mutableState.update { it.copy(printerStatus = device.status, printerConnected = device.connected,
                    printerName = device.connectedDevice?.name.orEmpty(),
                    printers = device.devices.map { found -> NearbyPrinter(found.address, found.name) },
                    uncertainPrint = it.uncertainPrint || unknown != null,
                    error = unknown?.message ?: it.error) }
            }
        }
        checkServer()
    }

    fun editingChanged(value: Boolean) { editing = value }
    val acceptsScan get() = !editing && !state.value.updateDetailsOpen && !state.value.loginRequired &&
        !state.value.connectionRequired && !state.value.printBusy && !state.value.splitRequested &&
        state.value.destination in setOf(Destination.Orders, Destination.Inventory)

    fun navigate(destination: Destination) {
        if (state.value.printBusy || destination == state.value.destination) return
        initialTaskNavigationPending = false
        val previous = state.value
        if (previous.destination in setOf(Destination.Orders, Destination.Inventory)) {
            workspaces[previous.destination] = previous.workspace(pendingPage)
            if (pendingPage != null && !pendingAutoSelect) refreshWithoutSelection += previous.destination
        }
        queryJob?.cancel(); queryGeneration++
        pendingPage = null
        splitAll = false
        editing = false
        val restored = workspaces[destination] ?: QueryWorkspace()
        mutableState.update {
            if (destination in setOf(Destination.Orders, Destination.Inventory)) it.withWorkspace(restored).copy(destination = destination)
            else it.copy(destination = destination, loading = false, splitRequested = false)
        }
        if (destination in setOf(Destination.Orders, Destination.Inventory) && restored.pendingPage != null) load(restored.pendingPage, !refreshWithoutSelection.remove(destination))
        if (destination == Destination.Devices) refreshAdjustments()
        if (destination == Destination.Tasks) {
            resumeTaskBoard()
            if (dailyWorkVisible) startDailyWorkPolling()
        } else dailyWorkPoll?.cancel()
    }

    fun scanned(code: String) {
        if (!acceptsScan) return
        when (code.uppercase()) {
            "PALL01" -> printSelected(null)
            "PBOX01" -> if (state.value.destination == Destination.Inventory) printSelected(null) else requestSplit()
            else -> if (state.value.destination == Destination.Inventory && code.uppercase().matches(Regex("[A-Z]{2}[0-9]{4}"))) {
                searchInventoryCode(code)
            } else search(code.uppercase(), "sku")
        }
    }

    fun search(value: String, mode: String) {
        if (state.value.printBusy) return
        editing = false
        splitAll = false
        mutableState.update { it.copy(query = value.trim(), queryMode = mode, inventoryFilters = InventoryFilters(), receipt = null) }
        load(page = 1)
    }

    fun searchInventory(value: String, filters: InventoryFilters) {
        if (state.value.printBusy || state.value.destination != Destination.Inventory) return
        val cleaned = filters.trimmed()
        if (value.isBlank() && cleaned.parameters().isEmpty()) return
        editing = false
        splitAll = false
        mutableState.update { it.copy(query = value.trim(), queryMode = "sku", inventoryFilters = cleaned, receipt = null) }
        load(1)
    }

    fun searchInventoryCode(value: String) {
        if (state.value.printBusy || state.value.destination != Destination.Inventory) return
        val code = value.trim().uppercase()
        if (!code.matches(Regex("[A-Z]{2}[0-9]{4}"))) {
            message("商品编号应为2位字母加4位数字，例如 LS0001")
            return
        }
        editing = false
        splitAll = false
        mutableState.update { it.copy(query = code, queryMode = "sku_exact", inventoryFilters = InventoryFilters(sku = code), receipt = null) }
        load(1)
    }

    fun clearQuery() {
        if (state.value.printBusy || state.value.destination == Destination.Devices) return
        queryJob?.cancel(); queryGeneration++
        pendingPage = null
        splitAll = false
        editing = false
        val blank = QueryWorkspace(scope = state.value.scope, logistics = state.value.logistics, revision = ++queryRevision)
        workspaces.remove(state.value.destination)
        mutableState.update { it.withWorkspace(blank) }
    }

    fun changeScope(scope: String, logistics: String) {
        if (state.value.printBusy) return
        mutableState.update { it.copy(scope = scope, logistics = logistics, selectedIds = emptySet()) }
        if (state.value.queried) load(1)
    }

    fun changeOrderFilters(filters: OrderFilters) {
        if (state.value.printBusy || state.value.destination != Destination.Orders) return
        mutableState.update { it.copy(orderFilters = filters, selectedIds = emptySet()) }
        load(1, false)
    }

    private fun load(page: Int, selectResults: Boolean = true) {
        queryJob?.cancel()
        val generation = ++queryGeneration
        val requested = state.value
        pendingPage = page
        pendingAutoSelect = selectResults
        if (page == 1) queryRevision++
        mutableState.update { it.copy(loading = true, error = if (it.uncertainPrint) it.error else null, selectedIds = if (page == 1) emptySet() else it.selectedIds,
            queryRevision = if (page == 1) queryRevision else it.queryRevision,
            items = if (page == 1) emptyList() else it.items, queried = true,
            total = if (page == 1) 0 else it.total, dataVersion = if (page == 1) "" else it.dataVersion,
            hasMore = if (page == 1) false else it.hasMore,
            demand = if (page == 1) null else it.demand, available = if (page == 1) null else it.available, stock = if (page == 1) null else it.stock) }
        queryJob = viewModelScope.launch {
            try {
                val result = if (requested.destination == Destination.Orders) repository.queryOrders(
                    requested.query, requested.queryMode, requested.scope, requested.logistics, page, requested.orderFilters,
                    if (page > 1) requested.dataVersion else "") else repository.query(
                    requested.destination, requested.query, requested.queryMode, requested.scope, requested.logistics, page,
                    requested.inventoryFilters, if (page > 1) requested.dataVersion else "")
                if (generation != queryGeneration) return@launch
                pendingPage = null
                if (page > 1 && result.dataVersion != requested.dataVersion) {
                    load(1, false)
                    return@launch
                }
                mutableState.update {
                    val combined = if (page == 1) result.items else (it.items + result.items).distinctBy(WorkItem::id)
                    it.copy(items = combined, total = result.total, page = page, hasMore = combined.size < result.total,
                        demand = result.demand, available = result.available, stock = result.stock, productName = result.productName,
                        dataVersion = result.dataVersion, sourceUpdatedAt = result.sourceUpdatedAt, loading = false, serverOnline = true,
                        printStateCounts = result.printStateCounts,
                        customerOptions = if (requested.destination == Destination.Orders) result.customerOptions else emptyList(),
                        selectedIds = when {
                            page != 1 -> it.selectedIds
                            !selectResults -> emptySet()
                            requested.destination == Destination.Inventory && (result.total != 1 || result.items.size != 1) -> emptySet()
                            else -> result.items.filter { it.isPrintEligible(requested.destination) }.map(WorkItem::id).toSet()
                        })
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (generation == queryGeneration) {
                    pendingPage = null
                    if (page > 1 && error is ServiceException && error.code == "data_version_changed") {
                        load(1, false)
                        return@launch
                    }
                    mutableState.update { it.copy(loading = false, error = if (it.uncertainPrint) it.error else readable(error)) }
                }
            }
        }
    }

    fun loadMore() { if (!state.value.loading && state.value.hasMore && !state.value.printBusy) load(state.value.page + 1) }
    fun toggleSelection(id: String) {
        if (!state.value.printBusy && state.value.items.any { it.id == id && it.printable && !it.requiresSettlement }) mutableState.update { it.copy(selectedIds = if (id in it.selectedIds) it.selectedIds - id else it.selectedIds + id) }
    }
    fun selectAll() {
        if (!state.value.printBusy) mutableState.update {
            val printable = it.items.filter { row -> row.isPrintEligible(it.destination) }.map(WorkItem::id).toSet()
            it.copy(selectedIds = if (it.selectedIds == printable) emptySet() else printable)
        }
    }
    fun requestSplit() = openSplit(all = false)
    private fun openSplit(all: Boolean) {
        if (state.value.loading || state.value.printBusy) return
        if (state.value.total <= 0) { splitAll = false; message("先扫描商品，再输入每箱数量"); return }
        splitAll = all
        mutableState.update { it.copy(splitRequested = true) }
    }
    fun dismissSplit() { splitAll = false; editing = false; mutableState.update { it.copy(splitRequested = false) } }
    fun printSelected(maximum: Int?) = print(splitAll, maximum)
    fun printAll(maximum: Int?) = print(true, maximum)

    private fun print(all: Boolean, maximum: Int?, snapshotOverride: WorkbenchState? = null, previewRequest: JSONObject? = null,
                      completed: (Boolean, Boolean) -> Unit = { _, _ -> }): Job? {
        val snapshot = snapshotOverride ?: state.value
        if (snapshotOverride == null && state.value.dailyWork.boardPrinting) return null
        val affectedIds = if (all) snapshot.items.map(WorkItem::id).toSet() else snapshot.selectedIds
        val affectedOrders = snapshot.items.filter { it.id in affectedIds }.map { it.orderNo }.filter(String::isNotBlank).toSet()
        fun reflects(row: WorkItem) = row.id in affectedIds || (row.orderNo.isNotBlank() && row.orderNo in affectedOrders)
        if (snapshot.printBusy || snapshot.loading) return null
        if (snapshot.uncertainPrint) { message("请先核对上次打印结果，并点击“已核对出纸”后再操作"); return null }
        if (!snapshot.printerConnected) { message("请先到设备页连接 A300E 打印机"); return null }
        if (snapshot.total <= 0 || (!all && snapshot.selectedIds.isEmpty())) { message("请先查询并选择要打印的记录"); return null }
        if (snapshot.destination == Destination.Orders && snapshot.items.any { it.requiresSettlement && (all || it.id in snapshot.selectedIds) }) {
            message("列表包含已出库待结存订单；这些订单无需打印，请筛选“未打印”后再全部打印")
            return null
        }
        if (snapshot.items.any { !it.printable && (all || it.id in snapshot.selectedIds) }) { message("部分记录缺少打印字段，请先处理详情中的提示"); return null }
        if (maximum != null && maximum <= 0) { message("每箱数量必须大于 0"); return null }
        // Consume the split scope only after preflight accepts this print request.
        splitAll = false
        editing = false
        cancelRequested = false
        mutableState.update { it.copy(printBusy = true, splitRequested = false, error = null, receipt = null, printMessage = "正在准备标签…") }
        return viewModelScope.launch {
            var succeeded = false
            var capacityRejected = false
            var jobId: String? = null
            var token: String? = null
            val actionKey = UUID.randomUUID().toString()
            val prepareKey = UUID.randomUUID().toString()
            var frozenPrepareRequest = ""
            var preparePersisted = false
            var transportStarted = false
            suspend fun releasePrepared(identity: String, reason: String): Boolean {
                return try {
                    repository.cancelPreparedPrint(identity, actionKey, snapshot.deviceId, reason)
                    clearPendingPrint()
                    true
                } catch (_: Exception) {
                    // A first claim may reject stale business data and release the
                    // reservation in the same server transaction. Confirm that
                    // terminal state before deciding this is still unsafe.
                    val terminal = runCatching { repository.printJob(identity) }.getOrNull()
                    if (terminal?.status in setOf("cancelled", "failed") && terminal?.bytesSent == 0) {
                        clearPendingPrint()
                        true
                    } else false
                }
            }
            suspend fun finishBeforeTransport(identity: String, claimToken: String, message: String): Boolean {
                return try {
                    repository.report(identity, claimToken, "cancelled", message, 0, snapshot.deviceId)
                    clearPendingPrint()
                    true
                } catch (_: Exception) { false }
            }
            fun showUnsafe(message: String) {
                mutableState.update { it.copy(uncertainPrint = true, error = message,
                    printMessage = "打印占用待核对", selectedIds = emptySet()) }
            }
            try {
                frozenPrepareRequest = previewRequest?.put("prepare_idempotency_key", prepareKey)?.put("device_id", snapshot.deviceId)?.toString()
                    ?: repository.freezePrepareRequest(snapshot, all, maximum, prepareKey)
                if (frozenPrepareRequest.isNotBlank()) {
                    if (!persistPreparingPrint(prepareKey, actionKey, frozenPrepareRequest)) {
                        mutableState.update { it.copy(error = "手机无法保存打印准备信息，尚未向服务器创建任务",
                            printMessage = "未开始打印") }
                        return@launch
                    }
                    preparePersisted = true
                }
                val prepared = try {
                    if (previewRequest != null) repository.replayPrepare(frozenPrepareRequest)
                    else repository.prepare(snapshot, all, maximum, prepareKey)
                } catch (firstFailure: Exception) {
                    if (frozenPrepareRequest.isBlank()) throw firstFailure
                    try { repository.replayPrepare(frozenPrepareRequest) }
                    catch (retryFailure: Exception) {
                        retryFailure.addSuppressed(firstFailure)
                        throw retryFailure
                    }
                }
                jobId = prepared.id
                if (!persistPendingPrint(prepared.id, actionKey, "prepared")) {
                    if (releasePrepared(prepared.id, "手机无法保存未发送打印任务")) {
                        mutableState.update { it.copy(error = "手机无法保存打印进度，任务已安全取消",
                            printMessage = "未发送，任务已取消") }
                    } else showUnsafe("手机无法保存打印进度，服务端占用状态待核对；请不要重复打印。")
                    return@launch
                }
                if (cancelRequested) {
                    if (releasePrepared(prepared.id, "用户在领取打印数据前取消")) {
                        mutableState.update { it.copy(printMessage = "已取消，未发送标签", receipt = "已取消，未发送标签") }
                    } else showUnsafe("取消请求未得到服务器确认，订单仍可能处于打印占用；请保持待核对。")
                    return@launch
                }
                val claim = repository.claim(prepared.id, actionKey, snapshot.deviceId)
                token = claim.token
                if (!persistPendingPrint(prepared.id, actionKey, "claimed", claim.token)) {
                    if (finishBeforeTransport(prepared.id, claim.token, "手机无法保存领取结果，未开始发送")) {
                        mutableState.update { it.copy(error = "手机无法保存打印进度，任务已安全取消",
                            printMessage = "未发送，任务已取消") }
                    } else showUnsafe("打印数据已领取，但手机无法保存结果令牌；请人工核对，不能重复打印。")
                    return@launch
                }
                if (cancelRequested) {
                    if (finishBeforeTransport(prepared.id, claim.token, "用户在发送前取消")) {
                        mutableState.update { it.copy(printMessage = "已取消，未发送标签", receipt = "已取消，未发送标签") }
                    } else showUnsafe("取消结果未同步到服务器，打印任务需要核对；不会自动重发。")
                    return@launch
                }
                if (!persistPendingPrint(prepared.id, actionKey, "sending", claim.token)) {
                    if (finishBeforeTransport(prepared.id, claim.token, "发送前无法持久化打印状态")) {
                        mutableState.update { it.copy(error = "手机无法保存打印进度，任务已安全取消",
                            printMessage = "未发送，任务已取消") }
                    } else showUnsafe("发送前无法持久化打印状态，任务需要人工核对；不会自动重发。")
                    return@launch
                }
                mutableState.update { it.copy(printMessage = "正在发送 ${prepared.labels} 张标签…") }
                transportStarted = true
                val outcome = printer.sendPages(claim.pages) { cancelRequested }
                var recorded = true
                try { repository.report(prepared.id, claim.token, outcome.status, outcome.message, outcome.bytesSent, snapshot.deviceId) }
                catch (_: Exception) { recorded = false }
                if (outcome.status == "submitted" && recorded) {
                    succeeded = true
                    clearPendingPrint()
                    mutableState.update {
                        val changed = it.items.map { row -> if (reflects(row)) row.copy(printState = "printed") else row }
                        val hide = it.orderFilters.printState == "unprinted" && it.destination == Destination.Orders
                        val shown = if (hide) changed.filterNot(::reflects) else changed
                        val removedRows = if (hide) changed.count(::reflects) else 0
                        val affectedOrderCount = if (snapshot.destination == Destination.Orders)
                            affectedOrders.size.coerceAtLeast(if (affectedIds.isEmpty()) 0 else 1) else 0
                        it.copy(receipt = "已发送 ${prepared.labels} 张标签，可以继续扫码",
                            printMessage = "${prepared.labels} 张标签已发送", selectedIds = emptySet(), error = null,
                            items = shown, total = (it.total - removedRows).coerceAtLeast(0),
                            printStateCounts = it.printStateCounts.copy(
                                unprinted = (it.printStateCounts.unprinted - affectedOrderCount).coerceAtLeast(0),
                                printed = it.printStateCounts.printed + affectedOrderCount))
                    }
                } else if (outcome.status == "submitted") {
                    persistPendingPrint(prepared.id, actionKey, "review", claim.token)
                    mutableState.update { current ->
                        val changed = current.items.map { row -> if (reflects(row)) row.copy(printState = "uncertain") else row }
                        val hide = current.orderFilters.printState == "unprinted" && current.destination == Destination.Orders
                        val removedRows = if (hide) changed.count(::reflects) else 0
                        current.copy(error = "标签已出纸，但服务端记录未同步。请核对订单后手动恢复状态。",
                            uncertainPrint = true, printMessage = "打印结果待核对", selectedIds = emptySet(),
                            items = if (hide) changed.filterNot(::reflects) else changed,
                            total = (current.total - removedRows).coerceAtLeast(0))
                    }
                } else {
                    val uncertain = outcome.status == "unknown" || outcome.bytesSent > 0 || !recorded
                    if (uncertain) persistPendingPrint(prepared.id, actionKey, "review", claim.token) else clearPendingPrint()
                    mutableState.update { current ->
                        val changed = if (uncertain) current.items.map { row -> if (reflects(row)) row.copy(printState = "uncertain") else row } else current.items
                        val hide = uncertain && current.orderFilters.printState == "unprinted" && current.destination == Destination.Orders
                        val removedRows = if (hide) changed.count(::reflects) else 0
                        current.copy(error = if (recorded) outcome.message else "打印结果未能同步到服务器，请核对实际出纸；不会自动重发。",
                            uncertainPrint = uncertain, printMessage = if (recorded) outcome.message else "打印结果待核对",
                            items = if (hide) changed.filterNot(::reflects) else changed,
                            total = (current.total - removedRows).coerceAtLeast(0))
                    }
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                val identity = jobId
                val deterministicPrepareFailure = failure is ServiceException &&
                    failure.status?.let { it in 400..499 } == true
                val safelyClosed = when {
                    identity == null && (!preparePersisted || deterministicPrepareFailure) -> {
                        if (preparePersisted) clearPendingPrint()
                        true
                    }
                    identity == null -> false
                    token == null -> {
                        val released = releasePrepared(identity, "领取打印数据失败，客户端未发送")
                        if (released) true else try {
                            repository.recoverClaimedPrint(identity, actionKey, snapshot.deviceId,
                                "领取响应丢失，无法确认是否进入发送步骤")
                            persistPendingPrint(identity, actionKey, "review")
                            false
                        } catch (_: Exception) { false }
                    }
                    !transportStarted -> finishBeforeTransport(identity, token, "客户端未开始发送")
                    else -> try {
                        repository.report(identity, token, "unknown", "打印传输异常，无法确认是否出纸", 0, snapshot.deviceId)
                        persistPendingPrint(identity, actionKey, "review", token)
                        false
                    } catch (_: Exception) { false }
                }
                capacityRejected = identity == null && safelyClosed && failure is ServiceException &&
                    failure.status == 400 && failure.code == "invalid_request" &&
                    (failure.message.orEmpty().contains("拆箱后超过100张") || failure.message.orEmpty().contains("一次最多生成 100 张标签"))
                val code = (failure as? ServiceException)?.code
                val message = readable(failure)
                if (!safelyClosed) {
                    if (identity != null) persistPendingPrint(identity, actionKey, "review", token.orEmpty())
                    showUnsafe("$message；打印任务尚未安全释放，请核对实际出纸和订单状态，不能重复打印。")
                } else if (code == "data_version_conflict") {
                    mutableState.update { it.copy(selectedIds = emptySet(), printMessage = "数据已变化，正在刷新当前列表…") }
                    load(1, false)
                    mutableState.update { it.copy(error = "$message；已刷新当前列表", printMessage = "未发送，当前列表已刷新") }
                } else mutableState.update { it.copy(error = message,
                    printMessage = if (code == "order_lifecycle_conflict") "未发送，订单状态已变化" else "未发送，请检查提示",
                    selectedIds = if (code == "order_lifecycle_conflict") emptySet() else it.selectedIds) }
            } finally {
                mutableState.update { it.copy(printBusy = false) }
                pendingPublication?.let { (version, time) -> publishToWorkspace(version, time) }
                completed(succeeded, capacityRejected)
            }
        }
    }

    fun cancelPrint() { cancelRequested = true; printer.cancelSending() }
    fun message(value: String) { mutableState.update { it.copy(error = value) } }
    fun clearMessage() {
        val printOrigin = preferences.getString("pending-print-origin", "").orEmpty()
        val printUser = preferences.getString("pending-print-user", "").orEmpty()
        if ((printOrigin.isNotBlank() && printOrigin != state.value.serverUrl) ||
            (printUser.isNotBlank() && printUser != state.value.dailyWork.me?.id)) {
            message("请回到建立打印任务的服务器，并使用原账号核对出纸记录")
            return
        }
        if (!printer.acknowledgeUnknownAttempt()) return
        val pendingPhase = preferences.getString("pending-print-state", "").orEmpty()
        if ((preferences.contains("pending-job") || preferences.contains("pending-prepare-request")) &&
            pendingPhase in setOf("preparing", "prepared", "claimed", "sending")) {
            recoverInterruptedPrint()
            mutableState.update { it.copy(error = "服务器尚未确认打印占用已结束，已重新核对；当前不能清除后重打。") }
            return
        }
        clearPendingPrint()
        mutableState.update { it.copy(error = null, receipt = null, uncertainPrint = false) }
    }

    private fun taskOperationInFlight(): Boolean = state.value.dailyWork.let {
        it.boardPrinting || it.boardSelectionBusy || it.actionBusyId != null || it.purchaseUnfinished.saving
    }

    fun saveServer(value: String) {
        if (state.value.printBusy || state.value.updateBusy || state.value.loginBusy || taskOperationInFlight()) return
        if (preferences.contains("pending-job") || preferences.contains("pending-prepare-request")) {
            message("仍有打印任务待核对，处理完成前不能切换服务器地址")
            return
        }
        try {
            val normalized = ServerAddress.normalize(value)
            if (normalized == state.value.serverUrl) { editing = false; checkServer(); return }
            queryJob?.cancel(); queryGeneration++
            updatePoll?.cancel()
            dailyWorkPoll?.cancel(); dailyWorkRequest?.cancel(); dailyWorkGeneration++
            appliedPublication = ""; pendingPublication = null; refreshWithoutSelection.clear()
            pendingPage = null
            workspaces.clear()
            preferences.edit().putString("server", normalized).apply()
            initialTaskNavigationPending = true
            mutableState.update { it.withWorkspace(QueryWorkspace(revision = ++queryRevision)).copy(serverUrl = normalized, serverOnline = false,
                        loginRequired = false, connectionRequired = true, apiUpdates = false, logisticsWrite = false, adjustmentUpload = false, updateBusy = false, updateMessage = "",
                        updateTask = null, updateDetailsOpen = false, resolvingFreight = false,
                        dailyWork = DailyWorkState()) }
            editing = false
            checkServer()
        } catch (failure: Exception) { message(failure.message ?: "服务地址无效") }
    }

    fun addAdjustment(item: WorkItem, type: AdjustmentType, quantity: String, note: String): AdjustmentSaveResult {
        val current = state.value
        if (current.printBusy) return AdjustmentSaveResult(false, "正在打印，请等待打印结束后再加入清单")
        val fromOrderDetail = current.destination == Destination.Orders && current.productDetail.item == item
        if (current.destination != Destination.Inventory && !fromOrderDetail)
            return AdjustmentSaveResult(false, "商品页面已变化，请重新打开库存详情后加入清单")
        val result = adjustmentOutbox.add(item, type, quantity, note)
        if (result.saved) {
            mutableState.update { it.copy(receipt = if (it.adjustmentUpload) "已加入${type.label}清单，到设备页提交给局域网上传电脑" else "已保存${type.label}草稿，连接公司局域网后可上传") }
        }
        return result
    }
    fun removeAdjustment(type: AdjustmentType, sku: String) = adjustmentOutbox.remove(type, sku)
    fun uploadAdjustments(type: AdjustmentType) {
        if (!state.value.adjustmentUpload) { message("局域网上传电脑暂不可用，草稿仍保存在手机"); return }
        adjustmentOutbox.upload(type)
    }
    fun retryAdjustment(id: String) {
        if (!state.value.adjustmentUpload) { message("局域网上传电脑暂不可用，清单已保留"); return }
        adjustmentOutbox.retry(id)
    }
    fun restoreAdjustment(id: String) = adjustmentOutbox.restore(id)
    fun confirmAdjustment(id: String) = adjustmentOutbox.confirm(id)
    fun refreshAdjustments() { if (state.value.adjustmentUpload) adjustmentOutbox.refresh() }

    private fun checkAdjustmentServer(generation: Int) {
        viewModelScope.launch {
            try {
                val health = adjustmentRepository.health()
                if (generation == healthGeneration) {
                    val available = health.adjustmentUpload
                    mutableState.update { it.copy(adjustmentUpload = available,
                        adjustmentServerStatus = if (available) "局域网上传电脑已连接" else "局域网上传电脑的报损报溢服务未启用") }
                    if (available) adjustmentOutbox.refresh()
                }
            } catch (_: Exception) {
                if (generation == healthGeneration) mutableState.update { it.copy(adjustmentUpload = false,
                    adjustmentServerStatus = "暂未连接 192.168.1.5:5191；请连接公司 Wi-Fi 并启动电脑服务，草稿保存在手机") }
            }
        }
    }

    fun checkServer() {
        val generation = ++healthGeneration
        val origin = state.value.serverUrl
        mutableState.update { it.copy(serverStatus = "正在连接…") }
        checkAdjustmentServer(generation)
        viewModelScope.launch {
            try {
                val health = repository.health()
                if (generation == healthGeneration) {
                    assistantProfileId = health.profileId
                    assistantProfileOrigin = origin
                    health.profileId.takeIf(String::isNotBlank)?.let { profile ->
                        if (profile != adjustmentProfile || origin != adjustmentOrigin) {
                            adjustmentProfile = profile
                            adjustmentOrigin = origin
                            adjustmentOutbox.useProfileBucket(profile, ADJUSTMENT_SERVER,
                                listOf(ADJUSTMENT_SERVER, initialUrl, state.value.serverUrl, "https://ul.yingyinghome.com:8443"))
                            adjustmentOutbox.holdBatchesOutsideServer(origin)
                        }
                    }
                    mutableState.update { it.copy(serverOnline = true, connectionRequired = false, loginRequired = false, apiUpdates = health.apiUpdates,
                        logisticsWrite = health.logisticsWrite,
                        serverStatus = if (health.apiUpdates && health.updatedAt.isBlank()) "服务已连接 · 尚未完成首次数据同步" else "服务已连接 · ${health.orders} 条订单",
                        sourceUpdatedAt = health.updatedAt) }
                    recoverInterruptedPrint()
                    if (initialTaskNavigationPending || state.value.destination == Destination.Tasks) refreshDailyWork()
                    if (health.apiUpdates && updatePoll?.isActive != true) {
                        try {
                            val pendingId = preferences.getString("pending-update:${state.value.serverUrl}", null)
                            val task = if (pendingId == null) repository.latestUpdate() else try {
                                repository.updateStatus(pendingId)
                            } catch (failure: ServiceException) {
                                if (failure.code !in setOf("not_found", "update_not_found", "job_not_found")) throw failure
                                repository.latestUpdate()
                            }
                            if (generation == healthGeneration && task != null) observeUpdate(task)
                        } catch (cancelled: CancellationException) { throw cancelled }
                        catch (failure: Exception) {
                            if (generation == healthGeneration) mutableState.update { it.copy(updateMessage = "暂时无法取得更新进度，可重新测试连接恢复查看") }
                        }
                    }
                }
            } catch (failure: Exception) {
                if (generation == healthGeneration) mutableState.update { it.copy(serverOnline = false,
                    connectionRequired = !it.loginRequired && it.connectionRequired,
                    serverStatus = readable(failure)) }
            }
        }
    }

    private var productDetailGeneration = 0

    fun openProductDetail(item: WorkItem) {
        if (state.value.printBusy || state.value.destination != Destination.Inventory) return
        loadProductDetail(item)
    }

    fun openOrderProductDetail(order: WorkItem) {
        if (state.value.printBusy || state.value.destination != Destination.Orders) return
        val generation = ++productDetailGeneration
        val origin = state.value.serverUrl
        mutableState.update { it.copy(productDetail = ProductDetailState(loading = true)) }
        viewModelScope.launch {
            try {
                require(order.sku.isNotBlank()) { "订单没有商品编号，无法定位库存商品" }
                require(listOf(order.profileId, order.warehouseId, order.productId, order.skuId, order.unitId)
                    .all(String::isNotBlank)) { "订单缺少完整商品、SKU、仓库或单位标识，请到库存页核对图纸" }
                val inventory = mutableListOf<WorkItem>()
                var page = 1
                var version = ""
                var more: Boolean
                do {
                    val result = repository.query(Destination.Inventory, "", "sku_exact", "all", "", page,
                        InventoryFilters(sku = order.sku), version)
                    if (page == 1) version = result.dataVersion
                    inventory += result.items
                    page++
                    more = result.items.isNotEmpty() && inventory.size < result.total
                } while (more)
                val matches = inventory.filter { candidate ->
                    candidate.sku.equals(order.sku, ignoreCase = true) &&
                        candidate.profileId == order.profileId && candidate.warehouseId == order.warehouseId &&
                        candidate.productId == order.productId && candidate.skuId == order.skuId &&
                        candidate.unitId == order.unitId
                }
                require(matches.size == 1) {
                    if (matches.isEmpty()) "没有与订单商品、仓库和单位一致的库存记录" else
                        "找到 ${matches.size} 条库存记录，无法确定对应图纸；请到库存页核对"
                }
                if (generation == productDetailGeneration && state.value.serverUrl == origin &&
                    state.value.destination == Destination.Orders) loadProductDetail(matches.single())
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (generation == productDetailGeneration && state.value.serverUrl == origin)
                    mutableState.update { it.copy(productDetail = ProductDetailState(error = readable(failure))) }
            }
        }
    }

    private fun loadProductDetail(item: WorkItem) {
        val generation = ++productDetailGeneration
        mutableState.update { it.copy(productDetail = ProductDetailState(item = item, loading = true)) }
        val origin = state.value.serverUrl
        viewModelScope.launch {
            val history = async { runCatching { repository.inventoryHistory(item.id, 90, 30, 0) } }
            val images = async { runCatching { if (item.productId.isBlank()) emptyList<ProductImage>() else repository.productImages(item.productId) } }
            val historyResult = history.await()
            val imageResult = images.await()
            val list = imageResult.getOrDefault(emptyList())
            val bytesResult = list.firstOrNull()?.let { runCatching { repository.productImage(it) } }
            if (generation == productDetailGeneration && state.value.serverUrl == origin && state.value.productDetail.item?.id == item.id) {
                val failures = listOf(historyResult.exceptionOrNull()?.let(::readable), imageResult.exceptionOrNull()?.let(::readable),
                    bytesResult?.exceptionOrNull()?.let(::readable)).filterNotNull()
                mutableState.update { it.copy(productDetail = ProductDetailState(item, false, historyResult.getOrNull(), list,
                    bytesResult?.getOrNull(), failures.joinToString("；").takeIf(String::isNotBlank))) }
            }
        }
    }

    fun closeProductDetail() {
        productDetailGeneration++
        mutableState.update { it.copy(productDetail = ProductDetailState()) }
    }

    fun setInventoryHistoryDays(days: Int) {
        val detail = state.value.productDetail
        val item = detail.item ?: return
        if (days !in setOf(30, 90, 180) || detail.historyDays == days || detail.historyBusy || detail.loading) return
        val generation = productDetailGeneration
        val origin = state.value.serverUrl
        mutableState.update { it.copy(productDetail = it.productDetail.copy(historyBusy = true, error = null)) }
        viewModelScope.launch {
            try {
                val history = repository.inventoryHistory(item.id, days, 30, 0)
                if (generation == productDetailGeneration && state.value.serverUrl == origin &&
                    state.value.productDetail.item?.id == item.id)
                    mutableState.update { it.copy(productDetail = it.productDetail.copy(
                        history = history, historyDays = days, historyBusy = false)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (generation == productDetailGeneration)
                    mutableState.update { it.copy(productDetail = it.productDetail.copy(
                        historyBusy = false, error = "库存台账读取失败：${readable(failure)}")) }
            }
        }
    }

    fun loadMoreInventoryHistory() {
        val detail = state.value.productDetail
        val item = detail.item ?: return
        val shown = detail.history ?: return
        if (!shown.hasMore || detail.historyBusy || detail.loading) return
        val generation = productDetailGeneration
        val origin = state.value.serverUrl
        mutableState.update { it.copy(productDetail = it.productDetail.copy(historyBusy = true, error = null)) }
        viewModelScope.launch {
            try {
                val next = repository.inventoryHistory(item.id, detail.historyDays, 30, shown.movements.size)
                if (generation == productDetailGeneration && state.value.serverUrl == origin &&
                    state.value.productDetail.item?.id == item.id && state.value.productDetail.historyDays == detail.historyDays)
                    mutableState.update { current ->
                        val previous = current.productDetail.history ?: shown
                        val rows = (previous.movements + next.movements).distinctBy { movement ->
                            movement.id.ifBlank { "${movement.postedAt}|${movement.documentNo}|${movement.quantity}" }
                        }
                        current.copy(productDetail = current.productDetail.copy(
                            history = next.copy(movements = rows,
                                hasMore = next.hasMore && (next.total < 0 || rows.size < next.total)),
                            historyBusy = false))
                    }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (generation == productDetailGeneration)
                    mutableState.update { it.copy(productDetail = it.productDetail.copy(
                        historyBusy = false, error = "更多台账读取失败：${readable(failure)}")) }
            }
        }
    }

    fun resetOrderPrintState(orderNo: String) {
        if (orderNo.isBlank() || state.value.printBusy) return
        val row = state.value.items.firstOrNull { it.orderNo == orderNo } ?: return
        if (row.requiresSettlement) {
            message("已出库待结存订单无需重置打印状态")
            return
        }
        viewModelScope.launch {
            try {
                repository.resetOrderPrintState(orderNo, row.profileId, deviceId)
                mutableState.update { it.copy(receipt = "订单 $orderNo 已恢复为未打印") }
                load(1, false)
            } catch (failure: Exception) { message(readable(failure)) }
        }
    }

    private fun refreshInventorySubset(products: Set<String>) {
        if (products.isEmpty()) {
            mutableState.update { it.copy(receipt = "ERP 草稿已建立，尚未过账；下次更新时同步可销售库存") }
            return
        }
        viewModelScope.launch {
            try {
                repository.refreshInventorySubset(products, UUID.randomUUID().toString())
                mutableState.update { it.copy(receipt = "ERP 草稿已建立，尚未过账；正在刷新相关商品的可销售库存") }
            } catch (_: Exception) {
                mutableState.update { it.copy(receipt = "ERP 草稿已建立，尚未过账；相关库存等待下次完整更新") }
            }
        }
    }

    fun login(password: String) = login("", password)

    fun login(username: String, password: String) {
        if (state.value.loginBusy || password.isBlank() || state.value.printBusy || taskOperationInFlight() || printRecovery?.isActive == true) return
        mutableState.update { it.copy(loginBusy = true, error = null) }
        viewModelScope.launch {
            try {
                repository.login(username.trim(), password)
                initialTaskNavigationPending = false
                mutableState.update { it.copy(loginRequired = false, destination = Destination.Tasks,
                    dailyWork = DailyWorkState()) }
                checkServer(); refreshAdjustments(); refreshDailyWork()
            } catch (failure: Exception) { message(readable(failure)) }
            finally { mutableState.update { it.copy(loginBusy = false) } }
        }
    }

    fun logout() {
        if (state.value.printBusy || taskOperationInFlight() || printRecovery?.isActive == true) return
        viewModelScope.launch {
            try {
                repository.logout(); queryJob?.cancel(); updatePoll?.cancel(); dailyWorkPoll?.cancel(); dailyWorkRequest?.cancel(); workspaces.clear()
                mutableState.update { it.withWorkspace(QueryWorkspace()).copy(destination = Destination.Tasks,
                    loginRequired = true, serverOnline = false, updateBusy = false, dailyWork = DailyWorkState()) }
            } catch (failure: Exception) { message(readable(failure)) }
        }
    }

    fun updateData() {
        if (state.value.dailyWork.me?.isAdmin == false || state.value.updateBusy || state.value.printBusy || !state.value.apiUpdates) return
        val origin = state.value.serverUrl
        val slot = "pending-update:$origin"
        val id = preferences.getString(slot, null) ?: UUID.randomUUID().toString().also {
            preferences.edit().putString(slot, it).putBoolean("$slot:write", state.value.logisticsWrite).commit()
        }
        mutableState.update { it.copy(updateBusy = true, updateMessage = "正在提交更新任务…") }
        viewModelScope.launch {
            try {
                val task = repository.startUpdate(id, preferences.getBoolean("$slot:write", false))
                if (state.value.serverUrl == origin) {
                    preferences.edit().putString(slot, task.id).apply()
                    observeUpdate(task)
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (state.value.serverUrl == origin) {
                    val rejected = failure is ServiceException && failure.code in setOf("invalid_mode", "invalid_id", "invalid_request", "logistics_write_disabled", "api_unavailable")
                    if (rejected) preferences.edit().remove(slot).remove("$slot:write").apply()
                    mutableState.update {
                        it.copy(updateBusy = false, updateMessage = readable(failure) +
                            if (rejected) "" else "；任务标识已保留，再次点击可恢复查看")
                    }
                }
            }
        }
    }

    private fun observeUpdate(first: UpdateTask) {
        val origin = state.value.serverUrl
        updatePoll?.cancel()
        updatePoll = viewModelScope.launch {
            var task = first
            try {
                while (state.value.serverUrl == origin) {
                    val active = task.active
                    mutableState.update { it.copy(updateBusy = active, updateMessage = task.message, updateTask = task) }
                    if (task.published && task.dataVersion.isNotBlank()) publishToWorkspace(task.dataVersion, task.publishedAt)
                    if (!active) {
                        val slot = "pending-update:$origin"
                        if (preferences.getString(slot, null) == task.id) preferences.edit().remove(slot).remove("$slot:write").apply()
                        // Compatibility with old services that do not expose publication fields.
                        if (!task.published && task.status in setOf("succeeded", "partial", "unknown")) {
                            val health = repository.health()
                            if (state.value.serverUrl == origin && health.version.isNotBlank()) publishToWorkspace(health.version, health.updatedAt)
                        }
                        break
                    }
                    delay(2000)
                    task = repository.updateStatus(task.id)
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (state.value.serverUrl == origin) mutableState.update { it.copy(updateBusy = false,
                    updateMessage = "暂时无法取得进度；后台任务继续，可测试连接恢复查看") }
            }
        }
    }

    private fun publishToWorkspace(version: String, updatedAt: String) {
        if (appliedPublication == version) {
            if (updatedAt.isNotBlank()) mutableState.update { it.copy(sourceUpdatedAt = updatedAt) }
            return
        }
        if (state.value.printBusy) { pendingPublication = version to updatedAt; return }
        appliedPublication = version; pendingPublication = null
        workspaces.replaceAll { destination, old ->
            if (old.queried && old.dataVersion != version) {
                refreshWithoutSelection += destination
                old.copy(items = emptyList(), selectedIds = emptySet(), dataVersion = "", pendingPage = 1,
                    total = 0, hasMore = false, sourceUpdatedAt = updatedAt)
            } else old
        }
        val active = state.value
        mutableState.update { it.copy(sourceUpdatedAt = updatedAt.ifBlank { it.sourceUpdatedAt }) }
        if (active.destination in setOf(Destination.Orders, Destination.Inventory) && active.queried && active.dataVersion != version) {
            load(1, false)
        }
        if (active.destination == Destination.Tasks) refreshDailyWork()
    }

    fun dailyWorkVisible(visible: Boolean) {
        dailyWorkVisible = visible
        if (visible && state.value.destination == Destination.Tasks && !state.value.loginRequired && !state.value.connectionRequired) {
            resumeTaskBoard()
            startDailyWorkPolling()
        } else dailyWorkPoll?.cancel()
    }

    private fun resumeTaskBoard() {
        if (state.value.dailyWork.me?.isAdmin == true && state.value.dailyWork.snapshot.tasksCurrent == false)
            refreshDailyWork()
        else refreshDailyWork(silent = state.value.dailyWork.board.items.isNotEmpty(), reuseCache = true)
    }

    private fun startDailyWorkPolling() {
        dailyWorkPoll?.cancel()
        dailyWorkPoll = viewModelScope.launch {
            while (dailyWorkVisible && state.value.destination == Destination.Tasks) {
                delay(30_000)
                if (state.value.dailyWork.adminDashboardOpen && !state.value.dailyWork.adminDashboardBusy)
                    loadAdminDashboard(silent = true)
                if (!state.value.loginRequired && !state.value.connectionRequired && dailyWorkRequest?.isActive != true &&
                    state.value.dailyWork.actionBusyId == null) {
                    try {
                        val latest = repository.dailyWorkVersion()
                        val shown = state.value.dailyWork.snapshot
                        val changed = latest.taskVersion != shown.taskVersion || latest.dataVersion != shown.dataVersion ||
                            latest.printStateVersion != shown.printStateVersion || latest.businessDate != shown.businessDate ||
                            (latest.tasksCurrent != null && latest.tasksCurrent != shown.tasksCurrent)
                        val legacyPrintPoll = usesTwoStageTaskBoard(state.value.dailyWork.selectedRole) &&
                            (latest.printStateVersion.isBlank() || shown.printStateVersion.isBlank())
                        if (changed || legacyPrintPoll) refreshDailyWork(silent = true, knownVersion = latest)
                    } catch (_: Exception) {
                        // Polling is advisory. Keep the last usable task list and let manual refresh report failures.
                    }
                }
            }
        }
    }

    fun refreshDailyWork() {
        val daily = state.value.dailyWork
        if (boardInteractionActive(daily)) {
            boardRefreshDeferred = true
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(message = "当前选择或操作已保留；结束后更新任务")) }
            return
        }
        if (daily.me?.isAdmin == true && daily.snapshot.tasksCurrent == false) {
            if (dailyWorkRequest?.isActive == true || daily.actionBusyId != null) return
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(refreshing = true, error = null)) }
            dailyWorkRequest = viewModelScope.launch {
                try {
                    repository.refreshDailyWork(UUID.randomUUID().toString())
                    dailyWorkRequest = null
                    refreshDailyWork(silent = false)
                } catch (failure: Exception) {
                    mutableState.update { it.copy(dailyWork = it.dailyWork.copy(refreshing = false,
                        error = "任务安排重建失败：${readable(failure)}")) }
                }
            }
        } else refreshDailyWork(silent = false)
    }

    private var boardRefreshDeferred = false
    private var stockSettingsScope = ""
    private var stockSelectionRequest: Job? = null
    private val stockSectionPages = mutableMapOf<String, Int>()

    private fun stockSettingsKey(userId: String, role: String) = "stock-packing-query:${state.value.serverUrl}:$userId:$role"
    private fun stockSettings(daily: DailyWorkState, user: CurrentUser): DailyWorkState {
        if (!usesPackingTaskBoard(daily.selectedRole)) return daily
        val key = stockSettingsKey(user.id, daily.selectedRole)
        if (stockSettingsScope == key) return daily
        stockSettingsScope = key
        val value = runCatching { JSONObject(preferences.getString(key, "{}").orEmpty()) }.getOrDefault(JSONObject())
        val filter = StockPackingFilters(value.optString("customer"), value.optString("sku"), value.optString("order_no"),
            value.optString("order_from"), value.optString("order_to"), value.optString("print_state", "all"),
            value.optString("sort", "default"), value.optInt("page_size", 50), value.optString("customer_key"),
            value.optString("supplier"), value.optString("receipt_from"), value.optString("receipt_to")).takeIf { it.valid() } ?: StockPackingFilters()
        stockSectionPages.clear()
        value.optJSONObject("section_pages")?.let { pages -> setOf("todo", "in_progress", "exception").forEach { section ->
            stockSectionPages[section] = pages.optInt(section, 1).coerceAtLeast(1)
        } }
        val restoredPage = stockSectionPages[daily.boardSection] ?: value.optInt("page", 1).coerceAtLeast(1)
        return daily.copy(stockFilters = filter, stockPage = restoredPage,
            boardDueFrom = value.optString("due_from"), boardDueTo = value.optString("due_to"))
    }
    private fun saveStockSettings(daily: DailyWorkState) {
        val user = daily.me ?: return
        val filter = daily.stockFilters
        stockSectionPages[daily.boardSection] = daily.stockPage
        preferences.edit().putString(stockSettingsKey(user.id, daily.selectedRole), JSONObject().put("customer", filter.customer)
            .put("sku", filter.sku).put("order_no", filter.orderNo).put("order_from", filter.orderDateFrom)
            .put("order_to", filter.orderDateTo).put("print_state", filter.printState).put("sort", filter.sort)
            .put("customer_key", filter.customerKey).put("supplier", filter.supplier).put("receipt_from", filter.receiptFrom).put("receipt_to", filter.receiptTo)
            .put("page_size", filter.pageSize).put("page", daily.stockPage).put("section_pages", JSONObject(stockSectionPages.toMap())).put("due_from", daily.boardDueFrom)
            .put("due_to", daily.boardDueTo).toString()).apply()
    }

    private fun boardInteractionActive(daily: DailyWorkState): Boolean =
        daily.boardSelectedIds.isNotEmpty() || daily.boardSelectedTaskIds.isNotEmpty() ||
            daily.boardSelection != null || daily.boardSelectionAll || daily.boardSelectionBusy ||
            daily.boardPrinting || daily.boardExportBusy || daily.boardExport != null ||
            daily.boardDetail != null || daily.actionBusyId != null || daily.packingPrintPreview != null ||
            daily.packingPrintPreviewBusy || daily.packingResetBusy

    private fun refreshDailyWork(silent: Boolean, afterAction: Boolean = false,
        reuseCache: Boolean = false, knownVersion: DailyWorkVersion? = null) {
        if (state.value.loginRequired || state.value.connectionRequired || dailyWorkRequest?.isActive == true ||
            state.value.dailyWork.actionBusyId != null) return
        if (!afterAction && boardInteractionActive(state.value.dailyWork)) {
            boardRefreshDeferred = true
            return
        }
        val generation = ++dailyWorkGeneration
        mutableState.update { current -> current.copy(dailyWork = current.dailyWork.copy(
            loading = !silent && current.dailyWork.board.items.isEmpty(),
            refreshing = current.dailyWork.board.items.isNotEmpty() || silent,
            error = if (silent) current.dailyWork.error else null,
            actionSucceededId = null)) }
        dailyWorkRequest = viewModelScope.launch {
            try {
                val before = state.value.dailyWork
                val currentUser = before.me ?: repository.me()
                if (initialTaskNavigationPending) {
                    initialTaskNavigationPending = false
                    mutableState.update { it.copy(destination = Destination.Tasks) }
                }
                mutableState.update { current -> current.copy(dailyWork = current.dailyWork.copy(
                    me = currentUser, pendingActions = pendingFor(currentUser),
                    pendingReview = pendingFor(currentUser).firstOrNull { it.requiresReview }?.let {
                        "任务 ${it.taskId.take(8)} 有一条未提交记录需要人工核对"
                    })) }
                val view = before.view.takeIf { it in setOf("mine", "role") } ?: "mine"
                val allowedRoles = if (currentUser.isAdmin) DAILY_WORK_ROLES else currentUser.roles
                val role = before.selectedRole.takeIf { it in allowedRoles } ?: allowedRoles.firstOrNull().orEmpty()
                val section = resolvedBoardSection(before, role)
                val requested = stockSettings(before.copy(me = currentUser, selectedRole = role, view = view, boardSection = section), currentUser)
                val cached = boardPages[boardQueryKey(requested)].takeIf { reuseCache && !afterAction }
                if (cached != null && generation == dailyWorkGeneration) mutableState.update { current -> current.copy(
                    dailyWork = requested.copy(board = cached, loading = false, refreshing = true, boardVerified = false)) }
                val head = if (cached != null) knownVersion ?: try { repository.dailyWorkVersion() }
                    catch (cancelled: CancellationException) { throw cancelled }
                    catch (_: Exception) { null } else null
                val validCache = cached?.takeIf { page -> head != null && cacheMatches(page, head) }
                var page = validCache ?: repository.taskBoard(boardParameters(requested,
                    if (usesPackingTaskBoard(role)) requested.stockPage else 1))
                if (usesPackingTaskBoard(role)) {
                    val lastPage = ((page.total + requested.stockFilters.pageSize - 1) / requested.stockFilters.pageSize).coerceAtLeast(1)
                    if (page.page > lastPage) page = repository.taskBoard(boardParameters(requested.copy(board = page), lastPage, versions = true))
                }
                val depth = boardPageDepths[boardQueryKey(requested)] ?: 1
                val frozen = requested.copy(board = page)
                val rows = page.items.toMutableList()
                while (!usesPackingTaskBoard(role) && page.hasMore && page.page < depth) {
                    val next = repository.taskBoard(boardParameters(frozen, page.page + 1, versions = true))
                    if (next.dataVersion != frozen.board.dataVersion || next.taskVersion != frozen.board.taskVersion)
                        throw ServiceException("task_version_changed", "任务已变化，请重新读取清单", 409)
                    if (next.printStateVersion != frozen.board.printStateVersion)
                        throw ServiceException("print_state_version_changed", "打印状态已变化，请重新读取清单", 409)
                    rows += next.items
                    page = next
                }
                page = page.copy(items = rows.distinctBy(TaskBoardItem::id))
                val keptDetail = before.boardDetail?.let { previous ->
                    repository.taskBoardDetail(previous.item.id,
                        boardParameters(requested.copy(board = page), versions = true).filterKeys { it != "page" }).also { detail ->
                        if (detail.printStateVersion != page.printStateVersion)
                            throw ServiceException("print_state_version_changed", "打印状态已变化，请重新读取清单", 409)
                    }
                }
                val assignmentEnabled = page.assignmentEnabled ?: before.snapshot.assignmentEnabled
                if (generation != dailyWorkGeneration) return@launch
                if (!afterAction && boardInteractionActive(state.value.dailyWork)) {
                    boardRefreshDeferred = true
                    mutableState.update { current -> current.copy(dailyWork = current.dailyWork.copy(
                        loading = false, refreshing = false,
                        message = "当前选择或填写内容已保留；结束后更新任务")) }
                    return@launch
                }
                mutableState.update { current ->
                    val shown = current.dailyWork.board
                    val sameVersion = shown.items.isNotEmpty() && shown.dataVersion == page.dataVersion &&
                        shown.taskVersion == page.taskVersion && shown.printStateVersion == page.printStateVersion
                    val changedWhileSelected = !sameVersion && boardInteractionActive(current.dailyWork)
                    current.copy(dailyWork = current.dailyWork.copy(
                    me = currentUser, selectedRole = role, view = view, boardSection = section,
                    boardSectionsByRole = if (isTaskBoardHistorySection(section)) current.dailyWork.boardSectionsByRole
                        else current.dailyWork.boardSectionsByRole + (role to section),
                    // Printer reservations may change card membership without changing data/task versions.
                    // Publish the complete freshly fetched range only after interaction checks above.
                    board = page, boardVerified = true, boardDetail = keptDetail, stockPage = if (usesPackingTaskBoard(role)) page.page else current.dailyWork.stockPage,
                    stockFilters = requested.stockFilters, boardDueFrom = requested.boardDueFrom, boardDueTo = requested.boardDueTo,
                    boardSelectedIds = if (changedWhileSelected) emptySet() else current.dailyWork.boardSelectedIds,
                    boardSelectedTaskIds = if (changedWhileSelected) emptySet() else current.dailyWork.boardSelectedTaskIds,
                    boardSelection = if (changedWhileSelected) null else current.dailyWork.boardSelection,
                    boardSelectionAll = if (changedWhileSelected) false else current.dailyWork.boardSelectionAll,
                    message = if (changedWhileSelected) "任务数据已更新，请重新选择" else current.dailyWork.message,
                    snapshot = current.dailyWork.snapshot.copy(businessDate = page.businessDate, dataVersion = page.dataVersion,
                        taskVersion = page.taskVersion, printStateVersion = page.printStateVersion,
                        sourceUpdatedAt = page.sourceUpdatedAt, tasksCurrent = page.tasksCurrent, stale = page.stale,
                        roles = allowedRoles, assignmentEnabled = assignmentEnabled, summary = page.summary, todayEvents = page.todayEvents),
                    loading = false, refreshing = false, error = null)) }
                rememberBoardPage(requested, page)
                if (isTaskBoardHistorySection(state.value.dailyWork.boardSection)) loadTaskHistory(1, append = false)
                schedulePendingDailyReplay(currentUser)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (generation == dailyWorkGeneration) mutableState.update { current -> current.copy(dailyWork = current.dailyWork.copy(
                    loading = false, refreshing = false, error = readable(failure))) }
            }
        }
    }

    private fun cacheMatches(page: TaskBoardPage, version: DailyWorkVersion): Boolean =
        page.printStateVersion.isNotBlank() && version.printStateVersion.isNotBlank() &&
            page.dataVersion == version.dataVersion && page.taskVersion == version.taskVersion &&
            page.printStateVersion == version.printStateVersion && page.businessDate == version.businessDate &&
            (version.sourceUpdatedAt.isBlank() || page.sourceUpdatedAt == version.sourceUpdatedAt) &&
            (version.tasksCurrent == null || page.tasksCurrent == version.tasksCurrent)

    private fun rememberBoardPage(daily: DailyWorkState, page: TaskBoardPage) {
        val key = boardQueryKey(daily)
        boardPages.remove(key)
        boardPages[key] = page
        // Bound the cache to phone-sized lists; evicted queries still retain their loaded page depth.
        while (boardPages.size > 12 || boardPages.values.sumOf { it.items.size } > 2_000) {
            boardPages.remove(boardPages.keys.first())
        }
    }

    private fun schedulePendingDailyReplay(user: CurrentUser) {
        if (dailyActionReplay?.isActive == true || pendingFor(user).none { !it.requiresReview }) return
        val origin = state.value.serverUrl
        dailyActionReplay = viewModelScope.launch {
            // Publish the readable list first. A slow submission must not delay its first paint.
            yield()
            if (state.value.dailyWork.me?.id != user.id || state.value.serverUrl != origin ||
                state.value.dailyWork.actionBusyId != null) return@launch
            val changed = retryPendingDailyActions(user)
            if (changed && state.value.dailyWork.me?.id == user.id && state.value.serverUrl == origin)
                refreshDailyWork(silent = true)
        }
    }

    fun loadBusinessConfirmations() {
        if (state.value.dailyWork.me?.isAdmin != true) return
        val origin = state.value.serverUrl
        val actor = state.value.dailyWork.me?.id
        viewModelScope.launch {
            try {
                val rows = repository.businessConfirmations()
                if (state.value.serverUrl == origin && state.value.dailyWork.me?.id == actor)
                    mutableState.update { it.copy(businessConfirmations = rows, businessConfirmationError = null) }
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { mutableState.update { it.copy(businessConfirmationError = "待确认事项暂不可用：${readable(e)}") } }
        }
    }

    private var packingOptionsGeneration = 0
    fun loadPackingFilterOptions(view: String) {
        val requestId = ++packingOptionsGeneration
        val actor = state.value.dailyWork.me?.id
        val role = state.value.dailyWork.selectedRole
        if (!usesPackingTaskBoard(role)) return
        val origin = state.value.serverUrl
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingOptionsBusy = true, packingOptionsError = null, packingOptions = PackingFilterOptions())) }
        viewModelScope.launch {
            try {
                val options = repository.packingFilterOptions(role, view)
                if (requestId == packingOptionsGeneration && state.value.serverUrl == origin && state.value.dailyWork.selectedRole == role && state.value.dailyWork.me?.id == actor)
                    mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingOptions = options, packingOptionsBusy = false)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (e: Exception) { if (requestId == packingOptionsGeneration) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingOptionsBusy = false, packingOptionsError = readable(e))) } }
        }
    }

    private fun boardParameters(daily: DailyWorkState, page: Int = 1, versions: Boolean = false): Map<String, String> =
        linkedMapOf("role" to daily.selectedRole, "view" to daily.view, "keyword" to daily.boardKeyword,
            "status" to daily.boardStatus, "page" to page.toString(), "page_size" to "50",
            "due_from" to daily.boardDueFrom, "due_to" to daily.boardDueTo, "assignee_id" to daily.boardAssigneeId).apply {
            if (usesPackingTaskBoard(daily.selectedRole)) {
                val filters = daily.stockFilters
                put("packing_mode", "print"); put("page_size", filters.pageSize.toString())
                put("customer_key", filters.customerKey); put("supplier", filters.supplier); put("receipt_from", filters.receiptFrom); put("receipt_to", filters.receiptTo)
                put("order_no_match", "contains"); put("compact", "1")
                put("customer", if (filters.customerKey.isBlank()) filters.customer.trim() else ""); put("sku", filters.sku.trim()); put("order_no", filters.orderNo.trim())
                put("order_date_from", filters.orderDateFrom); put("order_date_to", filters.orderDateTo)
                put("print_state", filters.printState); put("sort", filters.sort)
            }
            if (usesTwoStageTaskBoard(daily.selectedRole)) put("work_state",
                if (daily.boardSection in setOf("todo", "in_progress", "exception")) daily.boardSection
                else daily.boardSectionsByRole[daily.selectedRole] ?: "todo")
            if (versions) {
                put("data_version", daily.board.dataVersion); put("task_version", daily.board.taskVersion)
                if (daily.board.printStateVersion.isNotBlank()) put("print_state_version", daily.board.printStateVersion)
            }
        }

    private fun boardQueryKey(daily: DailyWorkState): String = state.value.serverUrl + "\u0000" + daily.me?.id.orEmpty() +
        "\u0000" + boardParameters(daily).filterKeys { it != "page" }
            .entries.joinToString("\u0000") { "${it.key}=${it.value}" } +
            if (usesPackingTaskBoard(daily.selectedRole)) "\u0000page=${daily.stockPage}" else ""

    private fun resolvedBoardSection(daily: DailyWorkState, role: String): String {
        val section = if (role == daily.selectedRole) daily.boardSection
            else daily.boardSectionsByRole[role] ?: defaultTaskBoardSection(role)
        if (isTaskBoardHistorySection(section)) return section
        return if (usesTwoStageTaskBoard(role)) section.takeIf { it in (if (role == ROLE_ARRIVAL_PACKING) setOf("todo", "in_progress", "exception") else setOf("todo", "in_progress")) } ?: "todo"
            else "active"
    }

    private fun resetBoardQuery(change: (DailyWorkState) -> DailyWorkState) {
        if (boardInteractionActive(state.value.dailyWork)) return
        dailyWorkRequest?.cancel()
        dailyWorkRequest = null
        dailyWorkGeneration++
        stockSelectionRequest?.cancel()
        boardRefreshDeferred = false
        mutableState.update { current ->
            val changed = change(current.dailyWork)
            val section = resolvedBoardSection(if (changed.selectedRole == current.dailyWork.selectedRole) changed
                else current.dailyWork, changed.selectedRole)
            current.copy(dailyWork = changed.copy(board = boardPages[boardQueryKey(changed.copy(boardSection = section))] ?: TaskBoardPage(), boardVerified = false, boardDetail = null,
            boardPageBusy = false, boardDetailBusy = false, boardSelection = null, boardSelectionAll = false,
            boardSelectedIds = emptySet(), boardSelectedTaskIds = emptySet(), stockSelectionCalculating = false,
            boardSection = section, boardSectionsByRole = if (isTaskBoardHistorySection(section)) changed.boardSectionsByRole
                else changed.boardSectionsByRole + (changed.selectedRole to section),
            taskHistory = TaskHistoryPage(), taskHistoryDetail = null,
            taskHistoryBusy = false, taskHistoryPageBusy = false, error = null)) }
        refreshDailyWork(silent = false, reuseCache = true)
    }

    fun selectDailyRole(role: String) {
        val current = state.value.dailyWork
        if (role == current.selectedRole || role !in current.snapshot.roles) return
        if (usesPackingTaskBoard(current.selectedRole) || usesPackingTaskBoard(role)) stockSettingsScope = ""
        resetBoardQuery { it.copy(selectedRole = role, boardStatus = "all", boardAssigneeId = "") }
    }

    fun selectDailyView(view: String) {
        if (view !in setOf("mine", "role") || view == state.value.dailyWork.view) return
        resetBoardQuery { it.copy(view = view) }
    }

    fun searchTaskBoard(keyword: String) {
        if (keyword.trim() != state.value.dailyWork.boardKeyword) resetBoardQuery { it.copy(boardKeyword = keyword.trim()) }
    }
    fun selectTaskBoardStatus(status: String) {
        if (status !in setOf("all", "todo", "in_progress", "waiting", "packed", "review", "shortage", "packing", "ready_to_ship", "procurement", "arrival")) return
        resetBoardQuery { it.copy(boardStatus = status) }
    }
    fun setTaskBoardFilters(status: String, view: String, dueFrom: String, dueTo: String, assigneeId: String) {
        val current = state.value.dailyWork
        if (status == current.boardStatus && view == current.view && dueFrom == current.boardDueFrom &&
            dueTo == current.boardDueTo && assigneeId == current.boardAssigneeId) return
        if (status !in setOf("all", "todo", "in_progress", "waiting", "packed", "review", "shortage", "packing", "ready_to_ship", "procurement", "arrival") || view !in setOf("mine", "role")) return
        resetBoardQuery { it.copy(boardStatus = status, view = view, boardDueFrom = dueFrom,
            boardDueTo = dueTo, boardAssigneeId = assigneeId) }
    }

    fun setStockPackingFilters(filters: StockPackingFilters, view: String, dueFrom: String, dueTo: String, assigneeId: String) {
        val daily = state.value.dailyWork
        if (!usesPackingTaskBoard(daily.selectedRole) || !filters.valid() || view !in setOf("mine", "role") ||
            (dueFrom.isNotBlank() && dueTo.isNotBlank() && dueFrom > dueTo) || boardInteractionActive(daily)) return
        stockSectionPages.clear()
        resetBoardQuery { it.copy(stockFilters = filters, stockPage = 1, boardStatus = "all", view = view, boardDueFrom = dueFrom,
            boardDueTo = dueTo, boardAssigneeId = assigneeId) }
        saveStockSettings(state.value.dailyWork)
    }

    fun changeStockPackingPage(page: Int) {
        val daily = state.value.dailyWork
        if (!usesPackingTaskBoard(daily.selectedRole) || page < 1 || page == daily.stockPage || daily.loading || daily.refreshing ||
            daily.boardPageBusy || daily.boardPrinting || daily.boardSelectionBusy || daily.boardExportBusy || daily.actionBusyId != null ||
            daily.packingPrintPreview != null || daily.packingPrintPreviewBusy || !daily.board.tasksCurrent) return
        val pages = ((daily.board.total + daily.stockFilters.pageSize - 1) / daily.stockFilters.pageSize).coerceAtLeast(1)
        if (page > pages) return
        val generation = dailyWorkGeneration
        val requested = daily.copy(stockPage = page)
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardPageBusy = true, error = null)) }
        viewModelScope.launch {
            try {
                val cached = boardPages[boardQueryKey(requested)]?.takeIf { it.dataVersion == daily.board.dataVersion &&
                    it.taskVersion == daily.board.taskVersion && it.printStateVersion == daily.board.printStateVersion }
                val next = cached ?: repository.taskBoard(boardParameters(requested, page, true))
                if (next.dataVersion != daily.board.dataVersion || next.taskVersion != daily.board.taskVersion ||
                    next.printStateVersion != daily.board.printStateVersion)
                    throw ServiceException("print_state_version_changed", "任务或打印状态已变化，请重新核对选择", 409)
                if (generation != dailyWorkGeneration || !usesPackingTaskBoard(state.value.dailyWork.selectedRole)) return@launch
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(board = next, stockPage = next.page, boardPageBusy = false)) }
                stockSectionPages[daily.boardSection] = next.page
                rememberBoardPage(requested, next); saveStockSettings(state.value.dailyWork)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (generation == dailyWorkGeneration) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardPageBusy = false,
                    board = if (failure is ServiceException && failure.status == 409) it.dailyWork.board.copy(tasksCurrent = false) else it.dailyWork.board,
                    error = readable(failure))) }
            }
        }
    }

    private fun calculateStockSelection() {
        val daily = state.value.dailyWork
        if (!usesPackingTaskBoard(daily.selectedRole)) return
        stockSelectionRequest?.cancel()
        if (daily.boardSelectedIds.isEmpty() && daily.boardSelectedTaskIds.isEmpty()) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(stockSelectionCalculating = false)) }; return
        }
        val request = boardSelectionRequest(daily).toString()
        val origin = state.value.serverUrl
        val generation = dailyWorkGeneration
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(stockSelectionCalculating = true)) }
        stockSelectionRequest = viewModelScope.launch {
            try {
                delay(200) // Aggregate a drag gesture before persisting its frozen server selection.
                val selected = repository.taskBoardSelection(request)
                if (generation != dailyWorkGeneration || state.value.serverUrl != origin || state.value.dailyWork.me?.id != daily.me?.id ||
                    state.value.dailyWork.boardSelectedIds != daily.boardSelectedIds ||
                    state.value.dailyWork.boardSelectedTaskIds != daily.boardSelectedTaskIds) return@launch
                if (selected.dataVersion != daily.board.dataVersion || selected.taskVersion != daily.board.taskVersion ||
                    selected.printStateVersion != daily.board.printStateVersion)
                    throw ServiceException("print_state_version_changed", "任务或打印状态已变化，请重新核对选择", 409)
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSelection = selected, stockSelectionCalculating = false)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) { if (generation == dailyWorkGeneration && state.value.serverUrl == origin) mutableState.update {
                it.copy(dailyWork = it.dailyWork.copy(stockSelectionCalculating = false,
                    board = if (failure is ServiceException && failure.status == 409) it.dailyWork.board.copy(tasksCurrent = false) else it.dailyWork.board,
                    error = readable(failure))) } }
        }
    }

    fun selectStockPackingPage() {
        val daily = state.value.dailyWork
        if (!usesPackingTaskBoard(daily.selectedRole) || daily.boardPrinting || daily.boardSelectionBusy || !daily.board.tasksCurrent) return
        val ids = daily.board.items.filter { it.canPrint && it.printState !in setOf("reserved", "printing") }.map { it.id }.toSet()
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSelectedIds = it.dailyWork.boardSelectedIds + ids,
            boardSelection = null, boardSelectionAll = false)) }
        calculateStockSelection()
    }

    fun loadMoreTaskBoard() {
        val daily = state.value.dailyWork
        if (usesPackingTaskBoard(daily.selectedRole)) return
        if (!daily.board.hasMore || daily.boardPageBusy || daily.loading || daily.refreshing) return
        val generation = dailyWorkGeneration
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardPageBusy = true)) }
        viewModelScope.launch {
            try {
                val next = repository.taskBoard(boardParameters(daily, daily.board.page + 1, true))
                if (generation != dailyWorkGeneration) return@launch
                if (next.dataVersion != daily.board.dataVersion || next.taskVersion != daily.board.taskVersion)
                    throw ServiceException("data_version_changed", "任务数据已更新")
                if (next.printStateVersion != daily.board.printStateVersion)
                    throw ServiceException("print_state_version_changed", "打印状态已变化，请重新读取清单", 409)
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(board = next.copy(
                    items = (it.dailyWork.board.items + next.items).distinctBy(TaskBoardItem::id)), boardPageBusy = false)) }
                boardPageDepths[boardQueryKey(daily)] = next.page
                rememberBoardPage(daily, state.value.dailyWork.board)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (generation != dailyWorkGeneration) return@launch
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardPageBusy = false, error = readable(failure))) }
                if (failure is ServiceException && failure.status == 409 ||
                    failure is ServiceException && failure.code in setOf("data_version_changed", "task_version_changed", "print_state_version_changed")) {
                    if (boardInteractionActive(state.value.dailyWork)) {
                        boardRefreshDeferred = true
                        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                            board = it.dailyWork.board.copy(tasksCurrent = false),
                            error = "任务已变化；当前选择保留，取消多选后更新")) }
                    } else resetBoardQuery { it.copy(message = "任务已变化，已重新加载第一页") }
                }
            }
        }
    }

    fun openTaskBoardItem(id: String) {
        val daily = state.value.dailyWork
        if (daily.boardDetailBusy || daily.boardPrinting || daily.boardSelectionBusy || daily.boardExportBusy ||
            daily.actionBusyId != null || id.isBlank()) return
        if (usesPackingTaskBoard(daily.selectedRole) && (daily.boardSelectedIds.isNotEmpty() || daily.boardSelectionAll)) {
            toggleTaskBoardItem(id); return
        }
        val generation = dailyWorkGeneration
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardDetailBusy = true, error = null,
            boardSelection = null, boardSelectionAll = false, boardSelectedIds = emptySet(), boardSelectedTaskIds = emptySet())) }
        viewModelScope.launch {
            try {
                val detail = repository.taskBoardDetail(id, boardParameters(daily, versions = true))
                if (detail.dataVersion != daily.board.dataVersion || detail.taskVersion != daily.board.taskVersion)
                    throw ServiceException("data_version_changed", "任务数据已变化，请刷新后查看详情", 409)
                if (detail.printStateVersion != daily.board.printStateVersion)
                    throw ServiceException("print_state_version_changed", "打印状态已变化，请刷新后查看详情", 409)
                if (generation == dailyWorkGeneration) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    boardDetail = detail, boardDetailBusy = false)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (generation == dailyWorkGeneration) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    boardDetailBusy = false, error = readable(failure))) }
            }
        }
    }
    fun closeTaskBoardDetail() {
        if (state.value.dailyWork.boardPrinting || state.value.dailyWork.boardSelectionBusy) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardDetail = null,
            boardSelection = null, boardSelectionAll = false, boardSelectedTaskIds = emptySet())) }
        if (boardRefreshDeferred && !boardInteractionActive(state.value.dailyWork)) {
            boardRefreshDeferred = false
            refreshDailyWork(silent = true)
        }
    }
    fun toggleTaskBoardItem(id: String) {
        val daily = state.value.dailyWork
        if (daily.boardSelectionBusy || daily.boardExportBusy || daily.actionBusyId != null || daily.boardPrinting || daily.packingPrintPreview != null ||
            daily.board.items.none { it.id == id && (if (usesPackingTaskBoard(daily.selectedRole)) it.canPrint else it.canAct) }) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSelection = null, boardSelectionAll = false,
            boardSelectedIds = if (id in it.dailyWork.boardSelectedIds) it.dailyWork.boardSelectedIds - id else it.dailyWork.boardSelectedIds + id)) }
        calculateStockSelection()
    }
    fun toggleTaskBoardLine(id: String) {
        val daily = state.value.dailyWork
        if (daily.boardSelectionBusy || daily.boardExportBusy || daily.actionBusyId != null || daily.boardPrinting || daily.packingPrintPreview != null ||
            daily.boardDetail?.lines?.none { it.taskId == id && (if (usesPackingTaskBoard(daily.selectedRole)) it.canPrint else it.canAct) } != false) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSelection = null, boardSelectionAll = false,
            boardSelectedTaskIds = if (id in it.dailyWork.boardSelectedTaskIds) it.dailyWork.boardSelectedTaskIds - id else it.dailyWork.boardSelectedTaskIds + id)) }
        calculateStockSelection()
    }
    fun clearTaskBoardSelection() {
        if (state.value.dailyWork.boardPrinting || state.value.dailyWork.boardSelectionBusy ||
            state.value.dailyWork.boardExportBusy || state.value.dailyWork.actionBusyId != null) return
        stockSelectionRequest?.cancel()
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSelection = null, boardSelectionAll = false,
            boardSelectedIds = emptySet(), boardSelectedTaskIds = emptySet(), stockSelectionCalculating = false, packingPrintPreview = null)) }
        if (boardRefreshDeferred) {
            boardRefreshDeferred = false
            refreshDailyWork(silent = true)
        }
    }

    fun exportTaskBoardImages() {
        val daily = state.value.dailyWork
        if (daily.boardExportBusy || daily.boardPrinting || daily.boardSelectionBusy || daily.actionBusyId != null ||
            !daily.board.tasksCurrent || daily.board.dataVersion.isBlank()) return
        val generation = dailyWorkGeneration
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardExportBusy = true, boardExport = null, error = null)) }
        viewModelScope.launch {
            try {
                val ids = when {
                    daily.boardDetail != null -> listOf(daily.boardDetail.item.id)
                    daily.boardSelectionAll -> {
                        val collected = mutableListOf<TaskBoardItem>()
                        var pageNumber = 1
                        do {
                            val page = repository.taskBoard(boardParameters(daily, pageNumber, true))
                            if (page.dataVersion != daily.board.dataVersion || page.taskVersion != daily.board.taskVersion)
                                throw ServiceException("task_version_changed", "任务已变化，请重新选择", 409)
                            if (page.printStateVersion != daily.board.printStateVersion)
                                throw ServiceException("print_state_version_changed", "打印状态已变化，请重新选择", 409)
                            collected += page.items
                            pageNumber++
                        } while (page.hasMore)
                        (if (usesPackingTaskBoard(daily.selectedRole)) collected else collected.filter(TaskBoardItem::canAct)).map(TaskBoardItem::id)
                    }
                    else -> if (usesPackingTaskBoard(daily.selectedRole)) daily.boardSelectedIds.toList()
                        else daily.board.items.filter { it.id in daily.boardSelectedIds }.map(TaskBoardItem::id)
                }
                require(ids.isNotEmpty()) { "请先选择需要导出的事项" }
                val exported = mutableListOf<BoardExportCard>()
                var snapshotId = ""
                var exportedAt = ""
                ids.chunked(500).forEach { batch ->
                    val body = JSONObject().put("role", daily.selectedRole).put("view", daily.view)
                        .put("ids", JSONArray(batch)).put("expected_data_version", daily.board.dataVersion)
                        .put("expected_task_version", daily.board.taskVersion)
                    if (usesPackingTaskBoard(daily.selectedRole)) boardParameters(daily).forEach { (key, value) -> body.put(key, value) }
                    if (usesPackingTaskBoard(daily.selectedRole)) body.put("purpose", "image_export")
                    if (daily.board.printStateVersion.isNotBlank()) body.put("expected_print_state_version", daily.board.printStateVersion)
                    if (usesTwoStageTaskBoard(daily.selectedRole)) body.put("work_state", boardParameters(daily)["work_state"])
                    val snapshot = repository.exportTaskBoardSnapshot(body.toString())
                    if (snapshot.dataVersion != daily.board.dataVersion || snapshot.taskVersion != daily.board.taskVersion)
                        throw ServiceException("task_version_changed", "任务已变化，请重新选择", 409)
                    if (snapshot.printStateVersion != daily.board.printStateVersion)
                        throw ServiceException("print_state_version_changed", "打印状态已变化，请重新选择", 409)
                    if (snapshotId.isBlank()) {
                        snapshotId = snapshot.id
                        exportedAt = snapshot.exportedAt
                    }
                    exported += snapshot.items
                }
                val selectedOrders = daily.boardDetail?.takeIf { daily.boardSelectedTaskIds.isNotEmpty() }
                    ?.lines?.filter { it.taskId in daily.boardSelectedTaskIds }?.map(TaskBoardLine::orderNo)?.toSet()
                val visible = if (selectedOrders == null) exported else exported.map { entry ->
                    entry.copy(lines = entry.lines.filter { it.orderNo in selectedOrders },
                        orders = entry.orders.filter { it.orderNo in selectedOrders })
                }
                if (generation == dailyWorkGeneration) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    boardExport = BoardExportSnapshot(snapshotId, exportedAt, daily.board.dataVersion,
                        daily.board.taskVersion, visible, daily.board.printStateVersion), boardExportBusy = false)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (generation == dailyWorkGeneration) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    boardExportBusy = false, error = "图片导出失败：${readable(failure)}")) }
            }
        }
    }

    fun clearTaskBoardExport() {
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardExport = null)) }
    }

    private var taskHistoryGeneration = 0

    fun selectTaskBoardSection(section: String) {
        val daily = state.value.dailyWork
        val allowed = if (daily.selectedRole == ROLE_ARRIVAL_PACKING) setOf("todo", "in_progress", "exception", "processed", "archived")
            else if (usesTwoStageTaskBoard(daily.selectedRole)) setOf("todo", "in_progress", "processed", "archived")
            else setOf("active", "processed", "archived")
        if (section !in allowed || boardInteractionActive(daily)) return
        if (section == state.value.dailyWork.boardSection) return
        taskHistoryGeneration++
        if (!isTaskBoardHistorySection(section)) {
            if (usesPackingTaskBoard(daily.selectedRole)) stockSectionPages[daily.boardSection] = daily.stockPage
            resetBoardQuery { it.copy(boardSection = section,
                stockPage = if (usesPackingTaskBoard(daily.selectedRole)) stockSectionPages[section] ?: 1 else it.stockPage) }
            return
        }
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSection = section,
            taskHistoryDetail = null, error = null)) }
        loadTaskHistory(1, append = false)
    }

    private fun loadTaskHistory(page: Int, append: Boolean) {
        val daily = state.value.dailyWork
        if (!isTaskBoardHistorySection(daily.boardSection) || daily.selectedRole.isBlank()) return
        val generation = ++taskHistoryGeneration
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
            taskHistoryBusy = !append, taskHistoryPageBusy = append, error = null)) }
        viewModelScope.launch {
            try {
                val result = if (daily.selectedRole == ROLE_SALES)
                    repository.salesHistory(daily.view, daily.boardSection, page)
                else repository.taskHistory(daily.selectedRole, daily.view, daily.boardSection, page)
                if (generation == taskHistoryGeneration && state.value.dailyWork.boardSection == daily.boardSection &&
                    state.value.dailyWork.selectedRole == daily.selectedRole && state.value.dailyWork.view == daily.view)
                    mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                        taskHistory = if (append) result.copy(items =
                            (it.dailyWork.taskHistory.items + result.items).distinctBy(DailyWorkItem::id)) else result,
                        taskHistoryBusy = false, taskHistoryPageBusy = false)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (generation == taskHistoryGeneration) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    taskHistoryBusy = false, taskHistoryPageBusy = false, error = readable(failure))) }
            }
        }
    }

    fun loadMoreTaskHistory() {
        val daily = state.value.dailyWork
        val detail = daily.taskHistoryDetail
        if (daily.taskHistoryBusy || daily.taskHistoryPageBusy) return
        if (detail != null) {
            if (!detail.hasMore) return
            val generation = taskHistoryGeneration
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(taskHistoryPageBusy = true)) }
            viewModelScope.launch {
                try {
                    val next = if (daily.selectedRole == ROLE_SALES)
                        repository.salesHistoryDetail(detail.item.id, daily.view, detail.page + 1)
                    else repository.taskHistoryDetail(detail.item.id, daily.selectedRole, daily.view, detail.page + 1)
                    if (generation == taskHistoryGeneration && state.value.dailyWork.taskHistoryDetail?.item?.id == detail.item.id)
                        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(taskHistoryDetail = next.copy(
                            events = it.dailyWork.taskHistoryDetail!!.events + next.events), taskHistoryPageBusy = false)) }
                } catch (failure: Exception) {
                    mutableState.update { it.copy(dailyWork = it.dailyWork.copy(taskHistoryPageBusy = false, error = readable(failure))) }
                }
            }
        } else if (daily.taskHistory.hasMore) loadTaskHistory(daily.taskHistory.page + 1, append = true)
    }

    fun openTaskHistoryItem(id: String) {
        val daily = state.value.dailyWork
        if (id.isBlank() || daily.taskHistoryBusy || daily.taskHistoryPageBusy || !isTaskBoardHistorySection(daily.boardSection)) return
        val generation = ++taskHistoryGeneration
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(taskHistoryBusy = true, error = null)) }
        viewModelScope.launch {
            try {
                val detail = if (daily.selectedRole == ROLE_SALES)
                    repository.salesHistoryDetail(id, daily.view, 1)
                else repository.taskHistoryDetail(id, daily.selectedRole, daily.view, 1)
                if (generation == taskHistoryGeneration && state.value.dailyWork.boardSection == daily.boardSection)
                    mutableState.update { it.copy(dailyWork = it.dailyWork.copy(taskHistoryDetail = detail, taskHistoryBusy = false)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (generation == taskHistoryGeneration) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    taskHistoryBusy = false, error = readable(failure))) }
            }
        }
    }

    fun closeTaskHistoryDetail() {
        taskHistoryGeneration++
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(taskHistoryDetail = null, taskHistoryBusy = false)) }
    }

    private var adminDashboardGeneration = 0
    private var dashboardCompletionsGeneration = 0

    fun openDashboardCompletions(role: String, actorId: String, source: String) {
        if (state.value.dailyWork.me?.isAdmin != true || role !in DAILY_WORK_ROLES || source !in setOf("human", "erp")) return
        dashboardCompletionsGeneration++
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(dashboardCompletionsOpen = true,
            dashboardCompletions = null, dashboardCompletionsBusy = false, dashboardCompletionsError = null,
            dashboardCompletionsRole = role, dashboardCompletionsActor = actorId, dashboardCompletionsSource = source)) }
        loadMoreDashboardCompletions()
    }

    fun loadMoreDashboardCompletions() {
        val daily = state.value.dailyWork
        if (!daily.dashboardCompletionsOpen || daily.dashboardCompletionsBusy || daily.me?.isAdmin != true ||
            daily.dashboardCompletions?.hasMore == false) return
        val generation = dashboardCompletionsGeneration
        val page = (daily.dashboardCompletions?.page ?: 0) + 1
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(dashboardCompletionsBusy = true, dashboardCompletionsError = null)) }
        viewModelScope.launch {
            try {
                val result = repository.dashboardCompletions(daily.dashboardCompletionsRole, daily.dashboardCompletionsActor,
                    daily.dashboardCompletionsSource, page)
                if (generation == dashboardCompletionsGeneration) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    dashboardCompletions = result.copy(items = daily.dashboardCompletions?.items.orEmpty() + result.items),
                    dashboardCompletionsBusy = false)) }
            } catch (failure: Exception) {
                if (failure is CancellationException) throw failure
                if (generation == dashboardCompletionsGeneration) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    dashboardCompletionsBusy = false, dashboardCompletionsError = readable(failure))) }
            }
        }
    }

    fun closeDashboardCompletions() {
        dashboardCompletionsGeneration++
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(dashboardCompletionsOpen = false, dashboardCompletionsBusy = false)) }
    }

    fun openAdminDashboard() {
        if (state.value.dailyWork.me?.isAdmin != true || boardInteractionActive(state.value.dailyWork)) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(adminDashboardOpen = true, error = null)) }
        loadAdminDashboard(silent = false)
    }

    private fun loadAdminDashboard(silent: Boolean) {
        val daily = state.value.dailyWork
        if (!daily.adminDashboardOpen || daily.me?.isAdmin != true || daily.adminDashboardBusy) return
        val generation = ++adminDashboardGeneration
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(adminDashboardBusy = true,
            error = if (silent) it.dailyWork.error else null)) }
        viewModelScope.launch {
            try {
                val dashboard = repository.adminDashboard()
                if (generation == adminDashboardGeneration && state.value.dailyWork.adminDashboardOpen)
                    mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                        adminDashboard = dashboard, adminDashboardBusy = false)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (generation == adminDashboardGeneration) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    adminDashboardBusy = false, error = "管理看板读取失败：${readable(failure)}")) }
            }
        }
    }

    fun closeAdminDashboard() {
        closeDashboardCompletions()
        adminDashboardGeneration++
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(adminDashboardOpen = false,
            adminDashboardBusy = false)) }
    }

    fun openDashboardRole(role: String, assigneeId: String) {
        val daily = state.value.dailyWork
        if (daily.me?.isAdmin != true || role !in DAILY_WORK_ROLES) return
        closeAdminDashboard()
        resetBoardQuery { it.copy(selectedRole = role, view = "role", boardStatus = "all",
            boardAssigneeId = assigneeId, boardKeyword = "", boardDueFrom = "", boardDueTo = "") }
    }
    private fun boardSelectionRequest(daily: DailyWorkState, all: Boolean = false): JSONObject {
        val params = JSONObject(boardParameters(daily, versions = true))
        if (usesPackingTaskBoard(daily.selectedRole)) params.put("purpose", "print")
        if (daily.board.printStateVersion.isNotBlank()) params.put("expected_print_state_version", daily.board.printStateVersion)
        val detail = daily.boardDetail
        if (detail != null) {
            params.put("board_id", detail.item.id)
            if (all) params.put("all", true)
            else if (daily.boardSelectedTaskIds.isNotEmpty()) params.put("task_ids", JSONArray(daily.boardSelectedTaskIds.toList()))
            else params.put("ids", JSONArray(listOf(detail.item.id)))
        } else if (all) params.put("all", true)
        else {
            require(daily.boardSelectedIds.isNotEmpty()) { "请先选择需要处理的事项" }
            params.put("ids", JSONArray(daily.boardSelectedIds.toList()))
        }
        return params
    }
    private suspend fun freezeBoardSelection(all: Boolean = false): TaskBoardSelection {
        val daily = state.value.dailyWork
        if (!all) daily.boardSelection?.let { return it }
        val generation = dailyWorkGeneration
        val origin = state.value.serverUrl
        return repository.taskBoardSelection(boardSelectionRequest(daily, all).toString()).also { selected ->
            if (generation != dailyWorkGeneration || origin != state.value.serverUrl || state.value.dailyWork.me?.id != daily.me?.id)
                throw IllegalStateException("当前查询范围已变化，请重新选择")
            if (selected.dataVersion != daily.board.dataVersion || selected.taskVersion != daily.board.taskVersion)
                throw ServiceException("task_version_changed", "任务状态已变化，请重新选择", 409)
            if (selected.printStateVersion != daily.board.printStateVersion)
                throw ServiceException("print_state_version_changed", "打印状态已变化，请重新选择", 409)
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSelection = selected, boardSelectionAll = all)) }
        }
    }
    fun selectAllTaskBoard() {
        val daily = state.value.dailyWork
        if (daily.view != "mine" || daily.boardSelectionBusy || daily.boardPrinting || daily.actionBusyId != null) return
        stockSelectionRequest?.cancel()
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSelectionBusy = true, stockSelectionCalculating = false, error = null)) }
        viewModelScope.launch {
            try { freezeBoardSelection(all = true) }
            catch (failure: Exception) { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = readable(failure))) } }
            finally { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSelectionBusy = false)) } }
        }
    }
    fun submitTaskBoardBatch(action: String, result: String, note: String, waitingFor: String, promisedDate: String, nextDate: String) {
        val daily = state.value.dailyWork
        val user = daily.me ?: return
        if (action !in setOf("start", "packed", "followup") || daily.actionBusyId != null || daily.boardPrinting ||
            daily.boardSelectionBusy || !daily.board.tasksCurrent) return
        if (daily.pendingActions.any { it.action == "board_batch" }) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = "请先提交或核对此前保存在手机的批量记录")) }; return
        }
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSelectionBusy = true, error = null)) }
        viewModelScope.launch {
            try {
                val selection = daily.boardSelection
                val key = UUID.randomUUID().toString()
                val body = JSONObject().put("idempotency_key", key)
                    .put("action", action).put("result", result).put("note", note.trim()).put("waiting_for", waitingFor.trim())
                    .put("promised_date", promisedDate).put("next_follow_up_date", nextDate)
                if (selection != null) body.put("selection_id", selection.id)
                else body.put("selection_request", boardSelectionRequest(daily))
                val pending = PendingDailyAction(key, state.value.serverUrl, user.id, selection?.id ?: "batch-$key", "board_batch",
                    note.trim(), nextDate, 0, OffsetDateTime.now().toString(), frozenRequest = body.toString())
                check(persistDailyActions(storedDailyActions() + pending)) { "手机无法保存批量记录，本次没有发送" }
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionBusyId = pending.taskId)) }
                if (sendPendingDailyAction(pending, showBoardSelection = true)) {
                    mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                        message = "批量处理已记录",
                        boardSelectedIds = if (action == "start") it.dailyWork.boardSelectedIds else emptySet(),
                        boardSelectedTaskIds = if (action == "start") it.dailyWork.boardSelectedTaskIds else emptySet(),
                        boardSelectionAll = action == "start" && it.dailyWork.boardSelectionAll,
                        boardSelection = if (action == "start") it.dailyWork.boardSelection else null)) }
                    refreshDailyWork(silent = true, afterAction = true)
                }
            } catch (failure: Exception) { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = readable(failure))) } }
            finally { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSelectionBusy = false)) } }
        }
    }

    fun printTaskBoardSelection(maximum: Int?) {
        val daily = state.value.dailyWork
        if (usesPackingTaskBoard(daily.selectedRole)) { previewStockPackingPrint(maximum?.toString().orEmpty()); return }
        if (state.value.printBusy || daily.boardPrinting || daily.boardSelectionBusy || daily.actionBusyId != null ||
            !daily.board.tasksCurrent || daily.selectedRole !in setOf(ROLE_STOCK_PACKING, ROLE_ARRIVAL_PACKING)) return
        if (state.value.uncertainPrint || !state.value.printerConnected) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = if (it.uncertainPrint)
                "请先核对上次打印结果，不能重复打印" else "请先到设备页连接 A300E 打印机")) }; return
        }
        cancelRequested = false
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardPrinting = true, boardSelectionBusy = true, error = null)) }
        viewModelScope.launch {
            var sent = 0
            try {
                val selection = freezeBoardSelection()
                require(selection.orderNos.isNotEmpty()) { "所选事项没有可打印的订单" }
                val chunks = java.util.ArrayDeque(selection.orderNos.distinct().chunked(100))
                while (chunks.isNotEmpty()) {
                    val orders = chunks.removeFirst()
                    if (cancelRequested || state.value.uncertainPrint) break
                    val printState = state.value.copy(destination = Destination.Orders, loading = false, printBusy = false,
                        scope = "all", logistics = "", dataVersion = selection.dataVersion, total = orders.size,
                        items = orders.map { no -> WorkItem(no, "", "", orderNo = no, profileId = selection.profileId) },
                        selectedIds = orders.toSet())
                    var success = false
                    var capacity = false
                    val segment = print(false, maximum, printState) { ok, tooLarge -> success = ok; capacity = tooLarge } ?: break
                    segment.join()
                    if (!success) {
                        if (capacity && orders.size > 1) {
                            val half = orders.size / 2
                            chunks.addFirst(orders.drop(half)); chunks.addFirst(orders.take(half))
                            continue
                        }
                        if (capacity) mutableState.update { it.copy(error = "订单 ${orders.single()} 单次超过 100 张标签，请调整拆箱数量；未拆分整单打印锁") }
                        break
                    }
                    sent += orders.size
                }
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(message = "已发送 $sent / ${selection.orderNos.size} 条订单的标签",
                    error = it.error, boardSelection = null, boardSelectionAll = false, boardSelectedIds = emptySet(), boardSelectedTaskIds = emptySet())) }
            } catch (failure: Exception) {
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = readable(failure))) }
            } finally {
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardPrinting = false, boardSelectionBusy = false)) }
                refreshDailyWork(silent = true)
            }
        }
    }

    fun previewStockPackingPrint(maximum: String) {
        val daily = state.value.dailyWork
        if (!usesPackingTaskBoard(daily.selectedRole) || daily.boardPrinting || daily.boardSelectionBusy ||
            daily.stockSelectionCalculating || daily.packingPrintPreviewBusy || !daily.board.tasksCurrent) return
        if (state.value.uncertainPrint) {
            recoverInterruptedPrint()
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingPrintRecoveryRequired = true,
                error = "上次打印先恢复并核对实际出纸，确认后才能预览新打印")) }; return
        }
        val max = maximum.trim()
        if (max.isNotBlank() && (max.toBigDecimalOrNull()?.signum() ?: 0) <= 0) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = "每张标签数量必须为正数")) }; return
        }
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingPrintPreview = null, packingPrintPreviewBusy = true, error = null)) }
        viewModelScope.launch {
            try {
                val selection = freezeBoardSelection()
                val body = JSONObject().put("selection_id", selection.id).put("device_id", deviceId)
                    .put("expected_data_version", selection.dataVersion)
                if (max.isNotBlank()) body.put("max_per_package", max)
                val preview = repository.packingPrintPreview(body.toString()).copy(dataVersion = selection.dataVersion, maxPerPackage = max)
                require(preview.valid()) { "打印预览不完整，请重新预览" }
                if (state.value.dailyWork.boardSelection?.id != selection.id || !usesPackingTaskBoard(state.value.dailyWork.selectedRole)) return@launch
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingPrintPreview = preview, packingPrintPreviewBusy = false)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingPrintPreviewBusy = false,
                error = readable(failure))) } }
            finally { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingPrintPreviewBusy = false)) } }
        }
    }

    fun dismissStockPackingPrint() {
        if (!state.value.dailyWork.boardPrinting) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
            packingPrintPreview = null, packingPrintRecoveryRequired = false)) }
    }

    fun acknowledgeStockPackingPrint() {
        val origin = state.value.serverUrl
        val daily = state.value.dailyWork
        if (!daily.packingPrintRecoveryRequired || daily.boardPrinting || daily.packingPrintPreviewBusy) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingPrintPreviewBusy = true)) }
        viewModelScope.launch {
            try {
                val storedOrigin = preferences.getString("pending-print-origin", "").orEmpty()
                val storedUser = preferences.getString("pending-print-user", "").orEmpty()
                require((storedOrigin.isBlank() || storedOrigin == origin) && (storedUser.isBlank() || storedUser == daily.me?.id)) {
                    "请使用建立打印任务的原服务器与账号核对" }
                val id = preferences.getString("pending-job", "").orEmpty()
                if (id.isBlank() && preferences.contains("pending-prepare-request")) {
                    recoverInterruptedPrint(); throw IllegalStateException("旧打印准备仍在恢复，请等待恢复完成后再确认")
                }
                if (id.isNotBlank() && repository.printJob(id).status !in setOf("submitted", "unknown", "cancelled", "failed")) {
                    recoverInterruptedPrint(); throw IllegalStateException("旧打印仍在恢复或发送，请等待结果后再确认")
                }
                if (state.value.serverUrl != origin || state.value.dailyWork.me?.id != daily.me?.id) return@launch
                clearMessage()
                if (state.value.uncertainPrint) throw IllegalStateException("本机打印结果尚未完成核对")
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingPrintRecoveryRequired = false, error = null,
                    message = "已人工核对旧打印；重新预览时仍需确认待核对订单的重打")) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = readable(failure))) } }
            finally { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingPrintPreviewBusy = false)) } }
        }
    }

    fun confirmStockPackingPrint(reprint: Boolean, uncertain: Boolean) {
        val daily = state.value.dailyWork
        val preview = daily.packingPrintPreview ?: return
        if (!usesPackingTaskBoard(daily.selectedRole) || !preview.valid() || daily.boardPrinting || daily.packingPrintPreviewBusy ||
            daily.stockSelectionCalculating || !daily.board.tasksCurrent || preview.dataVersion != daily.board.dataVersion ||
            state.value.uncertainPrint || (preview.reprintCount > 0 && !reprint) || (preview.uncertainCount > 0 && !uncertain)) return
        if (!state.value.printerConnected) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = "请先到设备页连接 A300E 打印机")) }; return
        }
        cancelRequested = false
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardPrinting = true, packingPrintPreview = null, error = null)) }
        viewModelScope.launch {
            var sent = 0
            try {
                for (segment in preview.segments) {
                    if (cancelRequested || state.value.uncertainPrint) break
                    val orders = preview.orders.filter { it.orderNo in segment.orderNos }
                    val body = JSONObject().put("kind", "order").put("preview_id", preview.id).put("preview_hash", preview.hash)
                        .put("segment_index", segment.index).put("device_id", deviceId)
                    if (orders.any { it.printState == "printed" }) body.put("confirm_reprint", reprint)
                    if (orders.any { it.printState == "uncertain" }) body.put("confirm_uncertain", uncertain)
                    val snapshot = state.value.copy(destination = Destination.Orders, loading = false, printBusy = false,
                        dataVersion = preview.dataVersion, total = orders.size,
                        items = orders.map { WorkItem(it.orderNo, "", "", orderNo = it.orderNo, profileId = it.profileId) },
                        selectedIds = orders.map { it.orderNo }.toSet())
                    var ok = false
                    val job = print(false, null, snapshot, body) { succeeded, _ -> ok = succeeded } ?: break
                    job.join()
                    if (!ok) break
                    sent += orders.size
                }
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(message = "已发送 $sent / ${preview.orderCount} 单标签",
                    error = it.error, boardSelection = null, boardSelectionAll = false, boardSelectedIds = emptySet(), boardSelectedTaskIds = emptySet())) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = readable(failure))) } }
            finally {
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardPrinting = false)) }
                refreshDailyWork(silent = true, afterAction = true)
            }
        }
    }

    fun resetStockPackingPrint(taskId: String, reason: String) {
        val daily = state.value.dailyWork
        val line = daily.boardDetail?.lines?.firstOrNull { it.taskId == taskId } ?: return
        if (!usesPackingTaskBoard(daily.selectedRole) || daily.packingResetBusy || daily.boardPrinting || reason.trim().isBlank() ||
            line.stateRevision.isBlank() || line.labelFingerprint.isBlank() || line.printState in setOf("reserved", "printing")) return
        val origin = state.value.serverUrl
        val slot = "packing-reset:$origin:${daily.me?.id}:${daily.boardDetail.item.profileId}:${line.orderNo}"
        val body = JSONObject().put("profile_id", daily.boardDetail.item.profileId).put("device_id", deviceId)
            .put("reason", reason.trim()).put("expected_state_revision", line.stateRevision)
            .put("expected_label_fingerprint", line.labelFingerprint).put("idempotency_key", UUID.randomUUID().toString())
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingResetBusy = true, error = null)) }
        viewModelScope.launch {
            try {
                val old = preferences.getString(slot, null)
                val frozen = old ?: body.toString().also { require(preferences.edit().putString(slot, it).commit()) { "无法保存恢复请求，请稍后重试" } }
                if (old != null && JSONObject(old).optString("reason") != reason.trim())
                    throw IllegalStateException("此前恢复请求结果待核对，请使用原原因重试：${JSONObject(old).optString("reason")}")
                repository.resetPackingPrint(line.orderNo, frozen)
                preferences.edit().remove(slot).commit()
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardDetail = null, boardSelectedIds = emptySet(),
                    boardSelectedTaskIds = emptySet(), boardSelection = null, message = "已恢复为未打印；原打印记录保留")) }
                refreshDailyWork(silent = true, afterAction = true)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                val rejected = failure is ServiceException && failure.status in 400..499 && failure.code in setOf(
                    "order_print_conflict", "role_forbidden", "daily_work_owner_required", "task_projection_conflict",
                    "invalid_request", "invalid_id", "admin_required")
                if (rejected) preferences.edit().remove(slot).commit()
                val explanation = readable(failure) + if (rejected) "；原请求未执行，原因已保留。请核对更新后的状态，再点击确认恢复。" else ""
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = explanation,
                    message = if (rejected) explanation else it.dailyWork.message)) }
                if (rejected) refreshDailyWork(silent = true, afterAction = true)
            }
            finally { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(packingResetBusy = false)) } }
        }
    }

    fun selectCustomerOrderFilter(filter: String) {
        if (filter !in setOf("all", "shortage", "packing", "ready_to_ship", "review")) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(customerOrderFilter = filter)) }
        refreshDailyWork(silent = true)
    }

    fun searchCustomerOrders(keyword: String) {
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(customerOrderKeyword = keyword.trim())) }
        refreshDailyWork(silent = true)
    }

    fun loadMoreCustomerOrders() {
        val daily = state.value.dailyWork
        if (!daily.customerOrders.hasMore || daily.customerOrderPageBusy || daily.customerOrders.dataVersion.isBlank()) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(customerOrderPageBusy = true)) }
        viewModelScope.launch {
            try {
                val next = repository.customerOrders(daily.selectedRole, daily.view,
                    daily.customerOrderFilter, daily.customerOrderKeyword, daily.customerOrders.page + 1)
                mutableState.update { current ->
                    val shown = current.dailyWork.customerOrders
                    if (shown.dataVersion != next.dataVersion || shown.taskVersion != next.taskVersion ||
                        shown.page + 1 != next.page) current.copy(dailyWork = current.dailyWork.copy(
                            customerOrderPageBusy = false, message = "订单数据已更新，正在重新加载第一页"))
                    else current.copy(dailyWork = current.dailyWork.copy(
                        customerOrders = next.copy(items = shown.items + next.items), customerOrderPageBusy = false))
                }
                if (state.value.dailyWork.customerOrders.dataVersion != next.dataVersion ||
                    state.value.dailyWork.customerOrders.taskVersion != next.taskVersion) refreshDailyWork(silent = true)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                customerOrderPageBusy = false, error = "下一页未加载：${readable(error)}")) } }
        }
    }

    fun openCustomerOrder(id: String) {
        val daily = state.value.dailyWork
        if (id.isBlank() || daily.customerOrderDetailBusy || daily.customerOrders.items.none { it.id == id } && daily.board.items.none { it.id == id && it.kind == "sales" } && daily.boardDetail?.item?.id != id) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(customerOrderDetailBusy = true, error = null)) }
        viewModelScope.launch {
            try {
                val detail = repository.customerOrderDetail(id, daily.selectedRole, if (daily.view == "role") "all" else daily.view)
                if (detail.group.id != id || detail.group.status.isBlank()) throw IllegalStateException("客户订单明细无效")
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    customerOrderDetail = detail, customerOrderDetailBusy = false)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                customerOrderDetailBusy = false, error = "客户订单未打开：${readable(error)}")) } }
        }
    }

    fun closeCustomerOrder() {
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(customerOrderDetail = null)) }
    }

    fun startTaskBoardSales(id: String) = submitTaskBoardSalesAction(id, "start")
    fun resetTaskBoardSales(id: String) = submitTaskBoardSalesAction(id, "reset_to_todo")
    fun saveTaskBoardSalesNote(id: String, note: String) = submitTaskBoardSalesAction(id, "note", note)

    private fun submitTaskBoardSalesAction(id: String, action: String, note: String = "") {
        val daily = state.value.dailyWork
        val user = daily.me ?: return
        if (daily.selectedRole != ROLE_SALES || daily.actionBusyId != null || daily.boardPrinting ||
            daily.boardSelectionBusy || daily.boardExportBusy || daily.boardSelectedIds.isNotEmpty() ||
            daily.boardSelectionAll || id.isBlank() || (action == "note" && note.isBlank())) return
        val item = daily.boardDetail?.item?.takeIf { it.id == id } ?: daily.board.items.firstOrNull { it.id == id }
        if (item == null || !item.canAct || !daily.board.tasksCurrent) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = id,
                actionError = "订单范围或权限已变化，请刷新后再处理；填写内容已保留")) }
            return
        }
        if (daily.pendingActions.any { it.taskId == id }) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = id,
                actionError = "这项处理已保存在手机，请先提交或核对此前记录")) }
            return
        }
        if ((action == "start" && item.workState == "in_progress") ||
            (action == "reset_to_todo" && item.workState == "todo")) return
        val key = UUID.randomUUID().toString()
        val body = JSONObject().put("idempotency_key", key).put("action", action).put("version", item.version)
            .put("expected_data_version", daily.board.dataVersion).put("expected_task_version", daily.board.taskVersion)
            .put("note", note.trim())
        enqueueSalesGroupAction(PendingDailyAction(key, state.value.serverUrl, user.id, id,
            "sales_group_action", note.trim(), "", item.version, OffsetDateTime.now().toString(),
            frozenRequest = body.toString()))
    }

    private fun enqueueSalesGroupAction(pending: PendingDailyAction) {
        if (!persistDailyActions(storedDailyActions() + pending)) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = pending.taskId,
                actionError = "手机未能保存待提交记录，本次没有发送；填写内容已保留")) }
            return
        }
        // An earlier poll must not publish its pre-action response after this frozen action is sent.
        dailyWorkRequest?.cancel()
        dailyWorkRequest = null
        dailyWorkGeneration++
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionBusyId = pending.taskId,
            actionErrorId = null, actionError = null, actionSucceededId = null,
            refreshing = false, loading = false,
            pendingActions = it.dailyWork.me?.let(::pendingFor).orEmpty())) }
        viewModelScope.launch {
            if (sendPendingDailyAction(pending)) refreshDailyWork(silent = true, afterAction = true)
        }
    }

    fun undoTaskBoardSalesStart() {
        val pending = salesStartUndoRequest ?: return
        if (state.value.dailyWork.actionBusyId != null || !matchesPendingContext(pending) ||
            state.value.dailyWork.pendingActions.any { it.taskId == pending.taskId }) return
        val key = UUID.randomUUID().toString()
        val body = JSONObject(pending.frozenRequest).put("idempotency_key", key)
        clearSalesStartUndo()
        enqueueSalesGroupAction(pending.copy(idempotencyKey = key, createdAt = OffsetDateTime.now().toString(),
            frozenRequest = body.toString()))
    }

    private fun clearSalesStartUndo() {
        salesStartUndoExpiry?.cancel()
        salesStartUndoRequest = null
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(undoSalesStartGroupId = "", undoSalesStartUntil = 0)) }
    }

    private fun applySalesGroupResult(pending: PendingDailyAction, result: SalesGroupActionResult) {
        val action = JSONObject(pending.frozenRequest).getString("action")
        if (action == "start") {
            clearSalesStartUndo()
            val undoBody = JSONObject(pending.frozenRequest).put("action", "reset_to_todo")
                .put("version", result.version).put("expected_task_version", result.taskVersion).put("note", "")
            salesStartUndoRequest = pending.copy(version = result.version, note = "", frozenRequest = undoBody.toString())
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(undoSalesStartGroupId = pending.taskId,
                undoSalesStartUntil = System.currentTimeMillis() + 5_000)) }
            salesStartUndoExpiry = viewModelScope.launch { delay(5_000); clearSalesStartUndo() }
        } else if (action == "reset_to_todo") clearSalesStartUndo()
        mutableState.update { current ->
            val daily = current.dailyWork
            fun updated(item: TaskBoardItem) = if (item.id != pending.taskId) item else item.copy(
                version = result.version, workState = result.workState, hasNote = result.hasNote, latestNote = result.latestNote,
                workLabel = if (result.workState == "todo") "待处理" else "处理中")
            val rows = daily.board.items.map(::updated).filter { !usesTwoStageTaskBoard(daily.selectedRole) ||
                isTaskBoardHistorySection(daily.boardSection) || it.workState.isBlank() || it.workState == daily.boardSection }
            val detail = daily.boardDetail?.let { old -> old.copy(item = updated(old.item), taskVersion = result.taskVersion) }
                ?.takeUnless { action != "note" && it.item.id == pending.taskId && it.item.workState != daily.boardSection }
            current.copy(dailyWork = daily.copy(board = daily.board.copy(items = rows), boardDetail = detail,
                salesActionSavedId = pending.taskId, salesActionRevision = daily.salesActionRevision + 1,
                message = when (action) { "start" -> "已进入处理中"; "reset_to_todo" -> "已退回待处理"; else -> "备注已保存" }))
        }
    }

    fun recordCustomerOrderFollowup(id: String, note: String, waitingFor: String, nextDate: String, version: Int) {
        val daily = state.value.dailyWork
        val person = daily.me ?: return
        if (id.isBlank() || note.isBlank() || daily.actionBusyId != null || daily.customerOrders.tasksCurrent == false) return
        if (daily.pendingActions.any { it.taskId == id }) return
        val pending = PendingDailyAction(UUID.randomUUID().toString(), state.value.serverUrl, person.id, id,
            "group_followup", note.trim(), nextDate, version, OffsetDateTime.now().toString(),
            waitingFor = waitingFor.trim())
        if (!persistDailyActions(storedDailyActions() + pending)) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = "手机未能保存整单跟进，本次没有发送")) }
            return
        }
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionBusyId = id)) }
        viewModelScope.launch {
            if (sendPendingDailyAction(pending)) refreshDailyWork(silent = true)
        }
    }

    fun transferCustomerOrder(id: String, assigneeId: String, reason: String, version: Int) {
        val daily = state.value.dailyWork
        val person = daily.me ?: return
        if (!person.isAdmin || id.isBlank() || assigneeId.isBlank() || reason.isBlank() ||
            daily.actionBusyId != null || daily.customerOrders.tasksCurrent == false) return
        val pending = PendingDailyAction(UUID.randomUUID().toString(), state.value.serverUrl, person.id, id,
            "group_transfer", reason.trim(), assigneeId, version, OffsetDateTime.now().toString())
        if (!persistDailyActions(storedDailyActions() + pending)) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = "手机未能保存整单转交，本次没有发送")) }
            return
        }
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionBusyId = id)) }
        viewModelScope.launch {
            if (sendPendingDailyAction(pending)) refreshDailyWork(silent = true)
        }
    }

    fun submitDailyAction(taskId: String, action: String, note: String, nextFollowUpDate: String, version: Int) {
        val allowed = setOf("start", "progress", "wait", "resolve", "packed", "reopen")
        if (taskId.isBlank() || action !in allowed || state.value.dailyWork.actionBusyId != null) return
        val before = state.value.dailyWork
        if (before.snapshot.tasksCurrent == false) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = taskId,
                actionError = "任务安排未跟上当前订单数据，请刷新后再处理")) }
            return
        }
        val user = before.me ?: return
        if (before.pendingActions.any { it.taskId == taskId }) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = taskId,
                actionError = "这项处理已保存在手机，正在等待提交")) }
            return
        }
        if (before.snapshot.items.none { it.id == taskId && it.version == version } &&
            before.board.items.none { it.id == taskId && it.version == version } &&
            before.boardDetail?.lines?.none { it.taskId == taskId && it.version == version } != false) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = taskId,
                actionError = "任务已变化，请刷新后再处理")) }
            return
        }
        val pending = PendingDailyAction(UUID.randomUUID().toString(), state.value.serverUrl, user.id, taskId,
            action, note.trim(), nextFollowUpDate, version, OffsetDateTime.now().toString())
        val stored = storedDailyActions() + pending
        if (!persistDailyActions(stored)) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = taskId,
                actionError = "手机无法保存待提交记录，本次没有发送")) }
            return
        }
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionBusyId = taskId,
            actionErrorId = null, actionError = null, actionSucceededId = null)) }
        viewModelScope.launch {
            val sent = sendPendingDailyAction(pending)
            if (sent) {
                refreshDailyWork(silent = true)
            }
        }
    }

    fun retryDailyActions() {
        val user = state.value.dailyWork.me ?: return
        if (state.value.dailyWork.actionBusyId != null || dailyWorkRequest?.isActive == true ||
            dailyActionReplay?.isActive == true) return
        viewModelScope.launch {
            retryPendingDailyActions(user)
            refreshDailyWork(silent = true)
        }
    }

    private suspend fun retryPendingDailyActions(user: CurrentUser): Boolean {
        var changed = false
        val origin = state.value.serverUrl
        for (pending in pendingFor(user).filterNot { it.requiresReview }) {
            if (state.value.dailyWork.me?.id != user.id || state.value.serverUrl != origin) break
            if (state.value.dailyWork.actionBusyId != null && state.value.dailyWork.actionBusyId != pending.taskId) break
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionBusyId = pending.taskId,
                actionErrorId = null, actionError = null)) }
            val sent = sendPendingDailyAction(pending)
            changed = changed || sent
            if (!sent) {
                val retained = pendingFor(user).firstOrNull { it.idempotencyKey == pending.idempotencyKey }
                if (retained != null && !retained.requiresReview) break
            }
        }
        if (state.value.dailyWork.me?.id == user.id && state.value.serverUrl == origin) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionBusyId = null,
            pendingActions = pendingFor(user))) }
        return changed
    }

    private fun matchesPendingContext(pending: PendingDailyAction) =
        pending.origin == state.value.serverUrl && pending.userId == state.value.dailyWork.me?.id

    private suspend fun sendPendingDailyAction(pending: PendingDailyAction, showBoardSelection: Boolean = false): Boolean {
        if (!matchesPendingContext(pending)) return false
        return try {
            var salesResult: SalesGroupActionResult? = null
            if (pending.action == "board_batch") {
                val body = JSONObject(pending.frozenRequest)
                if (!body.has("selection_id")) {
                    val selection = repository.taskBoardSelection(body.getJSONObject("selection_request").toString())
                    body.remove("selection_request")
                    body.put("selection_id", selection.id)
                    check(persistDailyActions(storedDailyActions().map { if (it.idempotencyKey == pending.idempotencyKey)
                        it.copy(frozenRequest = body.toString()) else it })) { "无法保存固定选择，本次没有提交" }
                    // A restart replays the frozen request without creating a selection in the new screen/role.
                    if (showBoardSelection && matchesPendingContext(pending))
                        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(boardSelection = selection)) }
                }
                if (!matchesPendingContext(pending)) return false
                repository.taskBoardBatch(body.toString())
            }
            else if (pending.action == "sales_group_action") salesResult = repository.salesGroupAction(pending.taskId, pending.frozenRequest)
            else if (pending.action in setOf("customer_owner", "customer_deputy")) repository.saveCustomerOwner(
                pending.frozenRequest, pending.action == "customer_deputy")
            else if (pending.action == "group_followup") repository.recordCustomerOrderFollowup(
                pending.taskId, pending.idempotencyKey, pending.version, pending.note,
                pending.waitingFor, pending.nextFollowUpDate)
            else if (pending.action == "group_transfer") repository.transferCustomerOrder(
                pending.taskId, pending.idempotencyKey, pending.version,
                pending.nextFollowUpDate, pending.note)
            else if (pending.action == "transfer") repository.transferDailyTask(pending.taskId, pending.nextFollowUpDate,
                pending.note, pending.idempotencyKey, pending.version)
            else if (pending.action in setOf("urgent_on", "urgent_off")) repository.setDailyTaskUrgent(
                pending.taskId, pending.action == "urgent_on", pending.note, pending.idempotencyKey, pending.version)
            else repository.submitDailyAction(pending.taskId, pending.action, pending.note, pending.nextFollowUpDate,
                pending.idempotencyKey, pending.version)
            persistDailyActions(storedDailyActions().filterNot { it.idempotencyKey == pending.idempotencyKey })
            if (!matchesPendingContext(pending)) return true
            salesResult?.let { applySalesGroupResult(pending, it) }
            mutableState.update { current -> current.copy(dailyWork = current.dailyWork.copy(
                actionBusyId = null, actionErrorId = null, actionError = null,
                actionSucceededId = pending.taskId,
                customerOwnersSaveRevision = current.dailyWork.customerOwnersSaveRevision +
                    if (pending.action in setOf("customer_owner", "customer_deputy")) 1 else 0,
                customerOwnersSavedKey = if (pending.action in setOf("customer_owner", "customer_deputy"))
                    pending.taskId else current.dailyWork.customerOwnersSavedKey,
                pendingActions = current.dailyWork.me?.let(::pendingFor).orEmpty())) }
            if (pending.action in setOf("customer_owner", "customer_deputy")) loadCustomerOwners()
            true
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (failure: Exception) {
            val retryable = when (failure) {
                is ServiceException -> failure.status in setOf(401, 429) || (failure.status ?: 0) >= 500 ||
                    failure.code in setOf("http_401", "http_429", "authentication_required", "auth_required", "unauthorized") ||
                    failure.code.startsWith("http_5")
                is java.io.IOException -> true
                else -> false
            }
            if (!retryable) {
                val reason = readable(failure)
                val updated = storedDailyActions().map { if (it.idempotencyKey == pending.idempotencyKey)
                    it.copy(lastError = reason, requiresReview = true) else it }
                persistDailyActions(updated)
                mutableState.update { current -> current.copy(dailyWork = current.dailyWork.copy(
                    actionBusyId = null, actionErrorId = pending.taskId,
                    actionError = "服务器拒绝了这条记录，已保留在手机，请人工核对：$reason",
                    pendingReview = "任务 ${pending.taskId.take(8)} 的待提交记录被服务器拒绝，需要重新核对",
                    pendingActions = current.dailyWork.me?.let(::pendingFor).orEmpty())) }
            } else {
                val readable = readable(failure)
                val updated = storedDailyActions().map { if (it.idempotencyKey == pending.idempotencyKey)
                    it.copy(lastError = readable) else it }
                persistDailyActions(updated)
                mutableState.update { current -> current.copy(dailyWork = current.dailyWork.copy(
                    actionBusyId = null, actionErrorId = pending.taskId,
                    actionError = "尚未提交，已安全保存在手机：$readable",
                    pendingActions = current.dailyWork.me?.let(::pendingFor).orEmpty())) }
            }
            false
        }
    }

    fun discardDailyActionReview(idempotencyKey: String) {
        val user = state.value.dailyWork.me ?: return
        val target = pendingFor(user).firstOrNull { it.idempotencyKey == idempotencyKey && it.requiresReview } ?: return
        if (!persistDailyActions(storedDailyActions().filterNot { it.idempotencyKey == target.idempotencyKey })) return
        mutableState.update { current ->
            val pending = pendingFor(user)
            current.copy(dailyWork = current.dailyWork.copy(pendingActions = pending,
                pendingReview = pending.firstOrNull { it.requiresReview }?.let {
                    "任务 ${it.taskId.take(8)} 有一条未提交记录需要人工核对"
                }, actionErrorId = null, actionError = null))
        }
    }

    fun openDailyOrder(orderNo: String) {
        if (orderNo.isBlank() || state.value.printBusy) return
        if (state.value.dailyWork.snapshot.tasksCurrent == false) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                error = "任务安排未跟上当前订单数据，请刷新任务后再进入包装")) }
            return
        }
        navigate(Destination.Orders)
        search(orderNo, "order_no")
    }

    fun resetDailyOrderPrintState(orderNo: String, profileId: String) {
        val daily = state.value.dailyWork
        if (orderNo.isBlank() || state.value.printBusy) return
        if (profileId.isBlank()) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = "任务缺少账套信息，无法恢复打印状态")) }
            return
        }
        if (daily.snapshot.tasksCurrent == false) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = "任务安排未跟上当前订单数据，请刷新后再恢复打印状态")) }
            return
        }
        viewModelScope.launch {
            try {
                repository.resetOrderPrintState(orderNo, profileId, deviceId)
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(message = "订单 $orderNo 已恢复为未打印", error = null)) }
                refreshDailyWork(silent = true)
                delay(3_500)
                mutableState.update { current -> current.copy(dailyWork = current.dailyWork.copy(
                    message = current.dailyWork.message.takeUnless { it == "订单 $orderNo 已恢复为未打印" })) }
            } catch (failure: Exception) {
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(error = "打印状态未恢复：${readable(failure)}")) }
            }
        }
    }

    fun setAssignmentEnabled(enabled: Boolean) {
        val daily = state.value.dailyWork
        if (daily.me?.isAdmin != true || daily.settingsSaving || daily.snapshot.assignmentEnabled == enabled) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(settingsSaving = true, error = null)) }
        viewModelScope.launch {
            try {
                val settings = repository.setDailyWorkSettings(enabled)
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(settingsSaving = false,
                    snapshot = it.dailyWork.snapshot.copy(assignmentEnabled = settings.assignmentEnabled))) }
                refreshDailyWork(silent = true)
            } catch (failure: Exception) {
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(settingsSaving = false,
                    error = "自动派工设置未保存：${readable(failure)}")) }
            }
        }
    }

    fun setDailyTaskUrgent(taskId: String, urgent: Boolean, reason: String, version: Int) {
        val daily = state.value.dailyWork
        if (daily.me?.isAdmin != true || taskId.isBlank() || reason.isBlank() || daily.actionBusyId != null) return
        if (daily.snapshot.tasksCurrent == false) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = taskId,
                actionError = "任务安排未跟上当前订单数据，请刷新后再调整急单")) }
            return
        }
        if (daily.pendingActions.any { it.taskId == taskId }) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = taskId,
                actionError = "这项急单调整已保存在手机，正在等待提交")) }
            return
        }
        val pending = PendingDailyAction(UUID.randomUUID().toString(), state.value.serverUrl, daily.me.id, taskId,
            if (urgent) "urgent_on" else "urgent_off", reason.trim(), "", version, OffsetDateTime.now().toString())
        if (!persistDailyActions(storedDailyActions() + pending)) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = taskId,
                actionError = "手机无法保存急单调整，本次没有发送")) }
            return
        }
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionBusyId = taskId,
            actionErrorId = null, actionError = null, actionSucceededId = null)) }
        viewModelScope.launch {
            val sent = sendPendingDailyAction(pending)
            if (sent) refreshDailyWork(silent = true)
        }
    }

    fun transferDailyTask(taskId: String, assigneeId: String, reason: String, version: Int) {
        val daily = state.value.dailyWork
        if (daily.me?.isAdmin != true || taskId.isBlank() || assigneeId.isBlank() || reason.isBlank() ||
            daily.actionBusyId != null) return
        if (daily.snapshot.tasksCurrent == false) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = taskId,
                actionError = "任务安排未跟上当前订单数据，请刷新后再转交")) }
            return
        }
        if (daily.pendingActions.any { it.taskId == taskId }) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = taskId,
                actionError = "这项转交已保存在手机，正在等待提交")) }
            return
        }
        val pending = PendingDailyAction(UUID.randomUUID().toString(), state.value.serverUrl, daily.me.id, taskId,
            "transfer", reason.trim(), assigneeId, version, OffsetDateTime.now().toString())
        if (!persistDailyActions(storedDailyActions() + pending)) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionErrorId = taskId,
                actionError = "手机无法保存转交记录，本次没有发送")) }
            return
        }
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(actionBusyId = taskId,
            actionErrorId = null, actionError = null, actionSucceededId = null)) }
        viewModelScope.launch {
            val sent = sendPendingDailyAction(pending)
            if (sent) {
                refreshDailyWork(silent = true)
            }
        }
    }

    fun openPurchaseUnfinished() = purchaseUnfinished.open()
    fun closePurchaseUnfinished() = purchaseUnfinished.close()
    fun refreshPurchaseUnfinished() = purchaseUnfinished.refresh()
    fun openPurchaseSupplier(supplier: PurchaseSupplier) = purchaseUnfinished.supplier(supplier)
    fun closePurchaseSupplier() = purchaseUnfinished.suppliers()
    fun loadMorePurchaseUnfinished() = purchaseUnfinished.more()
    fun editPurchasePromise(line: PurchaseUnfinishedLine) = purchaseUnfinished.edit(line)
    fun resumePurchasePromise() = purchaseUnfinished.resume()
    fun closePurchasePromise() = purchaseUnfinished.hideEditor()
    fun discardPurchasePromise() = purchaseUnfinished.discard()
    fun updatePurchasePromise(date: String, note: String) = purchaseUnfinished.change(date, note)
    fun savePurchasePromise() = purchaseUnfinished.save()
    fun reviewPurchasePromise() = purchaseUnfinished.review()
    fun acceptPurchasePromiseReview() = purchaseUnfinished.acceptReview()

    fun loadCustomerOwners() {
        if (state.value.dailyWork.me?.isAdmin != true || state.value.dailyWork.customerOwnersLoading) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(customerOwnersLoading = true,
            customerOwnersError = null)) }
        viewModelScope.launch {
            try {
                val owners = repository.customerOwners()
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(customerOwners = owners,
                    customerOwnersLoading = false)) }
            } catch (failure: Exception) {
                if (failure is CancellationException) throw failure
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(customerOwnersLoading = false,
                    customerOwnersError = "客户归属读取失败：${readable(failure)}")) }
            }
        }
    }

    fun saveCustomerOwner(draft: CustomerOwnerDraft) {
        val daily = state.value.dailyWork
        val person = daily.me ?: return
        if (!person.isAdmin || daily.customerOwnersSaving || daily.actionBusyId != null ||
            draft.profileId.isBlank() || draft.customer.isBlank() || draft.reason.isBlank()) return
        val taskId = "customer-owner:${draft.profileId}:${draft.customer}"
        if (daily.pendingActions.any { it.taskId == taskId }) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                customerOwnersError = "这个客户已有待提交的归属变更，请先处理保留的记录。")) }
            return
        }
        val key = UUID.randomUUID().toString()
        val body = JSONObject().put("profile_id", draft.profileId).put("customer", draft.customer)
            .put("scope", "customer").put("version", draft.version).put("idempotency_key", key)
            .put("reason", draft.reason.trim())
        if (draft.deputy) body.put("deputy_id", draft.personId).put("deputy_until", draft.deputyUntil)
        else body.put("owner_id", draft.personId)
        val pending = PendingDailyAction(key, state.value.serverUrl, person.id, taskId,
            if (draft.deputy) "customer_deputy" else "customer_owner", draft.reason.trim(), "",
            draft.version, OffsetDateTime.now().toString(), frozenRequest = body.toString())
        if (!persistDailyActions(storedDailyActions() + pending)) {
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(customerOwnersError = "手机未能保存归属变更，本次没有发送。")) }
            return
        }
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(customerOwnersSaving = true,
            customerOwnersError = null, actionBusyId = taskId)) }
        viewModelScope.launch {
            val sent = sendPendingDailyAction(pending)
            mutableState.update { it.copy(dailyWork = it.dailyWork.copy(customerOwnersSaving = false,
                customerOwnersError = if (sent) null else it.dailyWork.actionError)) }
            if (sent) refreshDailyWork(silent = true)
        }
    }

    fun loadStaff() {
        val current = state.value.dailyWork
        if (current.me?.isAdmin != true || current.staffLoading) return
        loadCustomerOwners()
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(staffLoading = true, staffError = null,
            attendanceError = null, rulesLoading = true, rulesError = null)) }
        viewModelScope.launch {
            try {
                val peopleRequest = async { repository.staff() }
                val attendanceRequest = async { runCatching { repository.attendance() } }
                val rulesRequest = async { runCatching { repository.assignmentRules() } }
                val people = peopleRequest.await()
                val attendance = attendanceRequest.await()
                val rules = rulesRequest.await()
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(staff = people, staffLoading = false,
                    attendance = attendance.getOrDefault(emptyMap()), attendanceAvailable = attendance.isSuccess,
                    attendanceError = attendance.exceptionOrNull()?.let { failure -> "今日在岗暂不可用：${readable(failure)}" },
                    assignmentRules = rules.getOrDefault(emptyList()), rulesLoading = false,
                    rulesError = rules.exceptionOrNull()?.let { failure -> "负责人规则暂不可用：${readable(failure)}" })) }
            } catch (failure: Exception) {
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(staffLoading = false, rulesLoading = false,
                    staffError = readable(failure))) }
            }
        }
    }

    fun saveAssignmentRule(role: String, matchValue: String, staffId: String) {
        val daily = state.value.dailyWork
        if (daily.me?.isAdmin != true || daily.rulesSaving || role != ROLE_PURCHASE ||
            matchValue.isBlank() || staffId.isBlank()) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(rulesSaving = true, rulesError = null)) }
        viewModelScope.launch {
            try {
                val rules = repository.saveAssignmentRule(role, matchValue, staffId)
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(assignmentRules = rules,
                    rulesSaving = false, rulesSaveRevision = it.dailyWork.rulesSaveRevision + 1)) }
                val rebuilt = runCatching { repository.refreshDailyWork(UUID.randomUUID().toString()) }
                if (rebuilt.isFailure) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    rulesError = "规则已保存，现有未分配任务暂未重算：${readable(rebuilt.exceptionOrNull()!!)}")) }
                refreshDailyWork(silent = true)
            } catch (failure: Exception) {
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(rulesSaving = false,
                    rulesError = "负责人规则未保存：${readable(failure)}")) }
            }
        }
    }

    fun deleteAssignmentRule(id: String) {
        val daily = state.value.dailyWork
        if (daily.me?.isAdmin != true || daily.rulesSaving || id.isBlank()) return
        if (daily.assignmentRules.firstOrNull { it.id == id }?.role != ROLE_PURCHASE) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(rulesSaving = true, rulesError = null)) }
        viewModelScope.launch {
            try {
                repository.deleteAssignmentRule(id)
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    assignmentRules = it.dailyWork.assignmentRules.filterNot { rule -> rule.id == id }, rulesSaving = false,
                    rulesSaveRevision = it.dailyWork.rulesSaveRevision + 1)) }
                refreshDailyWork(silent = true)
            } catch (failure: Exception) {
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(rulesSaving = false,
                    rulesError = "负责人规则未删除：${readable(failure)}")) }
            }
        }
    }

    fun createStaff(draft: StaffDraft) = saveStaff {
        repository.createStaff(draft)
    }

    fun updateStaff(id: String, draft: StaffDraft) = saveStaff {
        repository.updateStaff(id, draft)
    }

    fun resetStaffPassword(id: String, password: String) {
        if (id.isBlank() || password.isBlank() || state.value.dailyWork.staffSaving) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(staffSaving = true, staffError = null)) }
        viewModelScope.launch {
            try {
                repository.resetStaffPassword(id, password)
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(staffSaving = false,
                    staffSaveRevision = it.dailyWork.staffSaveRevision + 1)) }
            } catch (failure: Exception) {
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(staffSaving = false, staffError = readable(failure))) }
            }
        }
    }

    private fun saveStaff(operation: suspend () -> StaffMember) {
        if (state.value.dailyWork.me?.isAdmin != true || state.value.dailyWork.staffSaving) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(staffSaving = true, staffError = null)) }
        viewModelScope.launch {
            try {
                val saved = operation()
                val refreshed = runCatching { repository.staff() }.getOrElse { current ->
                    (state.value.dailyWork.staff.filterNot { it.id == saved.id } + saved).sortedBy { it.displayName }
                }
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(staff = refreshed, staffSaving = false,
                    staffSaveRevision = it.dailyWork.staffSaveRevision + 1)) }
                val taskRefresh = runCatching { repository.refreshDailyWork(UUID.randomUUID().toString()) }
                if (taskRefresh.isFailure) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    staffError = "人员已保存，任务安排暂未刷新：${readable(taskRefresh.exceptionOrNull()!!)}")) }
                refreshDailyWork(silent = true)
            } catch (failure: Exception) {
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(staffSaving = false, staffError = readable(failure))) }
            }
        }
    }

    fun setStaffAttendance(userId: String, present: Boolean) {
        val daily = state.value.dailyWork
        if (daily.me?.isAdmin != true || !daily.attendanceAvailable || daily.attendanceSavingId != null) return
        mutableState.update { it.copy(dailyWork = it.dailyWork.copy(attendanceSavingId = userId, attendanceError = null)) }
        viewModelScope.launch {
            try {
                repository.setAttendance(userId, present)
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(attendance = it.dailyWork.attendance + (userId to present),
                    attendanceSavingId = null)) }
                val taskRefresh = runCatching { repository.refreshDailyWork(UUID.randomUUID().toString()) }
                if (taskRefresh.isFailure) mutableState.update { it.copy(dailyWork = it.dailyWork.copy(
                    attendanceError = "在岗状态已保存，任务安排暂未刷新：${readable(taskRefresh.exceptionOrNull()!!)}")) }
                refreshDailyWork(silent = true)
            } catch (failure: Exception) {
                mutableState.update { it.copy(dailyWork = it.dailyWork.copy(attendanceSavingId = null,
                    attendanceError = "今日在岗未保存：${readable(failure)}")) }
            }
        }
    }

    fun showUpdateDetails() {
        if (!state.value.printBusy && state.value.dailyWork.me?.isAdmin != false)
            mutableState.update { it.copy(updateDetailsOpen = true) }
    }
    fun dismissUpdateDetails() { if (!state.value.resolvingFreight) mutableState.update { it.copy(updateDetailsOpen = false) } }
    fun resolveFreight(id: String, resolution: String, note: String) {
        if (state.value.dailyWork.me?.isAdmin == false || state.value.resolvingFreight || state.value.updateBusy ||
            resolution !in setOf("applied", "not_applied") || note.isBlank()) return
        val task = state.value.updateTask ?: return
        if (task.attempts.none { it.id == id && it.status == "unknown" }) return
        val origin = state.value.serverUrl
        mutableState.update { it.copy(resolvingFreight = true) }
        viewModelScope.launch {
            try {
                repository.resolveFreight(id, resolution, note.trim())
                val refreshed = repository.updateStatus(task.id)
                if (state.value.serverUrl == origin) {
                    mutableState.update { it.copy(updateTask = refreshed, updateMessage = refreshed.message, receipt = "已保存物流核对结论") }
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) { if (state.value.serverUrl == origin) message(readable(failure)) }
            finally { if (state.value.serverUrl == origin) mutableState.update { it.copy(resolvingFreight = false) } }
        }
    }

    private fun readable(failure: Throwable): String = when (failure) {
        is ServiceException -> failure.message ?: "服务请求失败"
        is java.net.SocketTimeoutException -> "连接超时，请检查网络后重试"
        is java.net.UnknownHostException -> "服务地址暂不可用，请确认地址正确且服务器已启用"
        is java.net.ConnectException -> "暂时连不上服务，请检查网络和服务器状态"
        else -> failure.message?.take(160) ?: "操作未完成，请稍后再试"
    }

    override fun onCleared() { printer.close() }
}
