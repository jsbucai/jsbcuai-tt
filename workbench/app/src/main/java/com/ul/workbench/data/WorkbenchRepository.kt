package com.ul.workbench.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URI
import java.net.URLEncoder

data class QueryResult(val items: List<WorkItem>, val total: Int, val page: Int,
    val demand: String?, val available: String?, val stock: String?,
    val productName: String, val dataVersion: String, val sourceUpdatedAt: String,
    val printStateCounts: PrintStateCounts = PrintStateCounts(),
    val customerOptions: List<String> = emptyList())
data class ServiceHealth(val version: String, val updatedAt: String, val orders: Int, val inventory: Int,
    val apiUpdates: Boolean = false, val logisticsWrite: Boolean = false, val adjustmentUpload: Boolean = true,
    val profileId: String = "")
data class FreightAttempt(val id: String, val orderNo: String, val status: String,
    val company: String = "", val number: String = "", val message: String = "")
data class UpdateTask(val id: String, val status: String, val message: String,
    val stage: String = "", val completed: Int = 0, val total: Int = 0,
    val published: Boolean = false, val dataVersion: String = "", val publishedAt: String = "",
    val orders: Int = 0, val inventory: Int = 0, val success: Int = 0, val failed: Int = 0,
    val unknown: Int = 0, val skipped: Int = 0, val attempts: List<FreightAttempt> = emptyList(),
    val queryStatus: String = "", val logisticsStatus: String = "", val blockedOrders: List<String> = emptyList(),
    val source: String = "") {
    val active get() = status in setOf("queued", "running")
}
data class PreparedPrint(val id: String, val labels: Int)
data class ClaimedPrint(val token: String, val pages: List<ByteArray>)
data class PrintJobState(val id: String, val status: String, val bytesSent: Int? = null)
data class SalesAttachmentFile(val bytes: ByteArray, val mimeType: String)

interface WorkbenchRepository {
    suspend fun login(password: String): Unit = error("当前服务不支持登录")
    suspend fun login(username: String, password: String): Unit = login(password)
    suspend fun logout(): Unit = error("当前服务不支持登录")
    suspend fun me(): CurrentUser = error("当前服务不支持人员身份")
    suspend fun dailyWork(role: String, view: String): DailyWorkSnapshot = error("当前服务不支持每日任务")
    suspend fun taskBoard(parameters: Map<String, String>): TaskBoardPage = error("当前服务不支持岗位清单，请升级服务")
    suspend fun taskBoardDetail(id: String, parameters: Map<String, String>): TaskBoardDetail = error("当前服务不支持岗位清单详情")
    suspend fun taskBoardSelection(frozenRequest: String): TaskBoardSelection = error("当前服务不支持批量选择")
    suspend fun packingPrintPreview(frozenRequest: String): PackingPrintPreview = error("当前服务不支持库存包装打印预览，请升级服务")
    suspend fun resetPackingPrint(orderNo: String, frozenRequest: String): Unit = error("当前服务不支持受控恢复打印状态")
    suspend fun taskBoardBatch(frozenRequest: String): Unit = error("当前服务不支持批量处理")
    suspend fun assistantRequest(path: String, body: JSONObject? = null, method: String = "GET"): JSONObject =
        error("助手服务待接入")
    suspend fun uploadSalesAttachment(sessionId: String, version: Int, key: String,
        name: String, mimeType: String, bytes: ByteArray): JSONObject = error("附件上传服务待接入")
    suspend fun downloadSalesAttachment(sessionId: String, attachmentId: String): SalesAttachmentFile = error("原附件查看服务待接入")
    suspend fun salesGroupAction(id: String, frozenRequest: String): SalesGroupActionResult =
        error("当前服务不支持销售开始和备注，请升级服务")
    suspend fun exportTaskBoardSnapshot(frozenRequest: String): BoardExportSnapshot =
        error("当前服务不支持任务图片导出")
    suspend fun taskHistory(role: String, view: String, scope: String, page: Int): TaskHistoryPage =
        error("当前服务不支持历史事项")
    suspend fun taskHistoryDetail(id: String, role: String, view: String, page: Int): TaskHistoryDetail =
        error("当前服务不支持历史事项详情")
    suspend fun salesHistory(view: String, scope: String, page: Int): TaskHistoryPage =
        error("当前服务不支持销售历史")
    suspend fun salesHistoryDetail(id: String, view: String, page: Int): TaskHistoryDetail =
        error("当前服务不支持销售历史详情")
    suspend fun adminDashboard(): AdminDashboard = error("当前服务不支持管理员工作看板")
    suspend fun dashboardCompletions(role: String, actorId: String, source: String, page: Int): DashboardCompletions =
        error("当前服务不支持完成记录明细")
    suspend fun purchaseUnfinishedStatus(): PurchaseSnapshotMeta = error("当前服务不支持采购未交快照")
    suspend fun purchaseSuppliers(): PurchaseSuppliers = error("当前服务不支持采购供应商列表")
    suspend fun purchaseUnfinished(supplierCode: String, page: Int, orderNo: String = ""): PurchaseUnfinishedPage =
        error("当前服务不支持采购未交明细")
    suspend fun purchaseReminder(supplierCode: String): PurchaseReminderGate = error("当前服务不支持催货校验")
    suspend fun savePurchasePromise(lineKey: String, frozenRequest: String): Unit = error("当前服务不支持供应商回复交期")
    suspend fun customerOwners(): List<CustomerOwner> = error("当前服务不支持统一客户归属，请升级服务")
    suspend fun saveCustomerOwner(frozenRequest: String, deputy: Boolean): Unit =
        error("当前服务不支持统一客户归属，请升级服务")

    suspend fun customerOrders(role: String, view: String, status: String, keyword: String, page: Int): CustomerOrderPage =
        error("当前服务不支持客户整单")
    suspend fun customerOrderDetail(id: String, role: String, view: String): CustomerOrderDetail =
        error("当前服务不支持客户整单")
    suspend fun recordCustomerOrderFollowup(id: String, key: String, version: Int, note: String,
                                            waitingFor: String, nextDate: String): Unit = error("当前服务不支持客户整单跟进")
    suspend fun transferCustomerOrder(id: String, key: String, version: Int, assigneeId: String,
                                      reason: String): Unit = error("当前服务不支持客户整单转交")
    suspend fun dailyWorkVersion(): DailyWorkVersion = error("当前服务不支持任务版本查询")
    suspend fun businessConfirmations(): List<String> = emptyList()
    suspend fun packingFilterOptions(role: String, view: String): PackingFilterOptions = PackingFilterOptions()
    suspend fun refreshDailyWork(idempotencyKey: String): Unit = error("当前服务不支持刷新每日任务")
    suspend fun submitDailyAction(taskId: String, action: String, note: String, nextFollowUpDate: String,
                                  idempotencyKey: String, version: Int): Unit = error("当前服务不支持任务处理")
    suspend fun transferDailyTask(taskId: String, assigneeId: String, reason: String,
                                  idempotencyKey: String, version: Int): Unit = error("当前服务不支持任务转交")
    suspend fun dailyWorkSettings(): DailyWorkSettings = error("当前服务不支持任务设置")
    suspend fun setDailyWorkSettings(assignmentEnabled: Boolean): DailyWorkSettings = error("当前服务不支持任务设置")
    suspend fun assignmentRules(): List<AssignmentRule> = error("当前服务不支持负责人规则")
    suspend fun saveAssignmentRule(role: String, matchValue: String, staffId: String): List<AssignmentRule> =
        error("当前服务不支持负责人规则")
    suspend fun deleteAssignmentRule(id: String): Unit = error("当前服务不支持负责人规则")
    suspend fun setDailyTaskUrgent(taskId: String, urgent: Boolean, reason: String,
                                   idempotencyKey: String, version: Int): Unit = error("当前服务不支持急单设置")
    suspend fun staff(): List<StaffMember> = error("当前服务不支持人员管理")
    suspend fun createStaff(draft: StaffDraft): StaffMember = error("当前服务不支持人员管理")
    suspend fun updateStaff(id: String, draft: StaffDraft): StaffMember = error("当前服务不支持人员管理")
    suspend fun resetStaffPassword(id: String, password: String): Unit = error("当前服务不支持人员管理")
    suspend fun attendance(): Map<String, Boolean> = error("当前服务不支持今日在岗")
    suspend fun setAttendance(userId: String, present: Boolean): Unit = error("当前服务不支持今日在岗")
    suspend fun startUpdate(id: String, write: Boolean): UpdateTask = error("当前服务不支持 API 更新")
    suspend fun latestUpdate(): UpdateTask? = null
    suspend fun updateStatus(id: String): UpdateTask = error("当前服务不支持 API 更新")
    suspend fun resolveFreight(id: String, resolution: String, note: String): Unit = error("当前服务不支持物流核对")
    suspend fun health(): ServiceHealth
    suspend fun query(destination: Destination, q: String, mode: String, scope: String, logistics: String, page: Int,
        inventoryFilters: InventoryFilters = InventoryFilters(), expectedDataVersion: String = ""): QueryResult
    suspend fun queryOrders(q: String, mode: String, scope: String, logistics: String, page: Int,
        filters: OrderFilters, expectedDataVersion: String = ""): QueryResult =
        query(Destination.Orders, q, mode, scope, logistics, page, expectedDataVersion = expectedDataVersion)
    suspend fun prepare(state: WorkbenchState, all: Boolean, maximum: Int?): PreparedPrint
    fun freezePrepareRequest(state: WorkbenchState, all: Boolean, maximum: Int?, prepareKey: String): String = ""
    suspend fun prepare(state: WorkbenchState, all: Boolean, maximum: Int?, prepareKey: String): PreparedPrint =
        prepare(state, all, maximum)
    suspend fun replayPrepare(frozenRequest: String): PreparedPrint = error("当前服务不支持恢复准备中的打印任务")
    suspend fun claim(id: String, actionKey: String): ClaimedPrint
    suspend fun claim(id: String, actionKey: String, deviceId: String): ClaimedPrint = claim(id, actionKey)
    suspend fun printJob(id: String): PrintJobState = error("当前服务不支持打印任务恢复")
    suspend fun cancelPreparedPrint(id: String, actionKey: String, deviceId: String, reason: String): Unit =
        error("当前服务不支持取消未发送打印任务")
    suspend fun recoverClaimedPrint(id: String, actionKey: String, deviceId: String, reason: String): Unit =
        error("当前服务不支持恢复领取结果不明的打印任务")
    suspend fun report(id: String, token: String, status: String, message: String, bytes: Int)
    suspend fun report(id: String, token: String, status: String, message: String, bytes: Int, deviceId: String) =
        report(id, token, status, message, bytes)
    suspend fun inventoryHistory(recordId: String): InventoryHistory = error("当前服务不支持库存台账")
    suspend fun inventoryHistory(recordId: String, days: Int, limit: Int, offset: Int): InventoryHistory =
        inventoryHistory(recordId)
    suspend fun productImages(productId: String): List<ProductImage> = emptyList()
    suspend fun productImage(image: ProductImage): ByteArray = error("当前服务不支持产品图片")
    suspend fun resetOrderPrintState(orderNo: String, profileId: String, deviceId: String): Unit =
        error("当前服务不支持重置打印状态")
    suspend fun refreshInventorySubset(productIds: Set<String>, id: String): UpdateTask =
        error("当前服务不支持局部库存刷新")
    suspend fun submitAdjustment(batch: AdjustmentBatch): AdjustmentBatch = error("服务尚未支持库存清单上传")
    suspend fun adjustment(id: String, clientId: String): AdjustmentBatch = error("服务尚未支持库存清单查询")
    suspend fun retryAdjustment(id: String, clientId: String): AdjustmentBatch = error("服务尚未支持库存清单重试")
    suspend fun confirmAdjustment(id: String, clientId: String): AdjustmentBatch = error("服务尚未支持人工核对库存清单")
}

class ServiceException(val code: String, message: String, val status: Int? = null) : Exception(message)

object ServerAddress {
    fun normalize(value: String): String {
        val raw = value.trim().trimEnd('/')
        val uri = runCatching { URI(raw) }.getOrNull() ?: error("请输入完整服务地址")
        require(uri.scheme in setOf("http", "https") && !uri.host.isNullOrBlank() &&
            uri.userInfo == null && uri.rawQuery == null && uri.fragment == null && uri.path.isNullOrEmpty()) {
            "地址格式应为 http://192.168.1.5:5191"
        }
        require(uri.scheme == "https" || uri.host in setOf("192.168.1.5", "10.0.2.2", "127.0.0.1", "localhost")) {
            "当前版本允许工作台 192.168.1.5 的局域网地址；其他服务器请使用 HTTPS"
        }
        require(uri.port == -1 || uri.port in 1..65535) { "端口无效" }
        return raw
    }
}

class HttpWorkbenchRepository(private val sessions: SessionVault? = null,
    private val onUnauthorized: (String) -> Unit = {}, private val baseUrl: () -> String) : WorkbenchRepository {
    private suspend fun request(path: String, body: JSONObject? = null, method: String? = null,
        boundOrigin: String? = null, boundToken: String? = null): JSONObject = withContext(Dispatchers.IO) {
        val origin = boundOrigin ?: ServerAddress.normalize(baseUrl())
        if (boundOrigin != null) check(ServerAddress.normalize(baseUrl()) == boundOrigin && sessions?.load(origin) == boundToken) {
            "账号或服务已变化；原助手操作已保留，请重新核对"
        }
        val connection = URI(origin + "/api/v2" + path).toURL().openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = 6000
            connection.readTimeout = 20000
            connection.instanceFollowRedirects = false
            connection.setRequestProperty("Accept", "application/json")
            if (path != "/auth/login") (if (boundOrigin != null) boundToken else sessions?.load(origin))
                ?.let { connection.setRequestProperty("Authorization", "Bearer $it") }
            if (body != null || method != null) {
                connection.requestMethod = method ?: "POST"
            }
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val status = connection.responseCode
            if (status == 401 && path != "/auth/login" && (boundOrigin == null || sessions?.load(origin) == boundToken)) {
                sessions?.clear(origin); onUnauthorized(origin)
            }
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val raw = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
            val json = runCatching { JSONObject(raw) }.getOrElse {
                throw ServiceException("invalid_response", "服务未返回有效数据（HTTP $status）", status)
            }
            if (status !in 200..299 || !json.optBoolean("ok", true)) {
                throw ServiceException(json.optString("error", "http_$status"),
                    json.optString("message", "请求失败（$status）"), status)
            }
            json
        } finally { connection.disconnect() }
    }

    private suspend fun requestBytes(path: String): ByteArray = withContext(Dispatchers.IO) {
        val origin = ServerAddress.normalize(baseUrl())
        val target = if (path.startsWith("http://") || path.startsWith("https://")) path else origin +
            if (path.startsWith("/api/v2/")) path else "/api/v2${if (path.startsWith('/')) path else "/$path"}"
        val uri = URI(target)
        require(uri.scheme == URI(origin).scheme && uri.host == URI(origin).host && uri.port == URI(origin).port) {
            "图片地址不是当前数据服务"
        }
        val connection = uri.toURL().openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = 6000
            connection.readTimeout = 30000
            connection.instanceFollowRedirects = false
            sessions?.load(origin)?.let { connection.setRequestProperty("Authorization", "Bearer $it") }
            connection.setRequestProperty("Accept", "image/*")
            val status = connection.responseCode
            if (status == 401) { sessions?.clear(origin); onUnauthorized(origin) }
            if (status !in 200..299) throw ServiceException("http_$status", "图片读取失败（HTTP $status）", status)
            require(connection.contentLengthLong <= 20L * 1024 * 1024 || connection.contentLengthLong < 0) { "产品图片超过 20 MB" }
            connection.inputStream.use { input ->
                val bytes = input.readBytes()
                require(bytes.size <= 20 * 1024 * 1024) { "产品图片超过 20 MB" }
                bytes
            }
        } finally { connection.disconnect() }
    }

    override suspend fun login(password: String) = login("", password)
    override suspend fun login(username: String, password: String) {
        val origin = ServerAddress.normalize(baseUrl())
        require(origin.startsWith("https://")) { "登录需要 HTTPS 服务地址" }
        val body = JSONObject().put("password", password)
        username.trim().takeIf(String::isNotBlank)?.let { body.put("username", it) }
        val data = request("/auth/login", body)
        check(origin == ServerAddress.normalize(baseUrl())) { "服务地址已变化，请重新登录" }
        checkNotNull(sessions) { "未配置安全会话存储" }.save(origin, data.getString("token"))
    }
    override suspend fun logout() {
        val origin = ServerAddress.normalize(baseUrl())
        request("/auth/logout", JSONObject())
        sessions?.clear(origin)
    }
    override suspend fun me(): CurrentUser {
        val data = request("/me")
        return parseUser(data.optJSONObject("user") ?: data.optJSONObject("me") ?: data)
    }
    override suspend fun dailyWork(role: String, view: String): DailyWorkSnapshot {
        val params = linkedMapOf("view" to view).apply { role.takeIf(String::isNotBlank)?.let { put("role", it) } }
        val query = params.entries.joinToString("&") { "${it.key}=${URLEncoder.encode(it.value, "UTF-8")}" }
        return parseDailyWork(request("/daily-work?$query"))
    }
    private fun boardQuery(parameters: Map<String, String>) = parameters.entries.joinToString("&") {
        "${it.key}=${URLEncoder.encode(it.value, "UTF-8")}" }
    override suspend fun taskBoard(parameters: Map<String, String>): TaskBoardPage =
        decodeTaskBoard(request("/daily-work/board?${boardQuery(parameters)}"))
    override suspend fun taskBoardDetail(id: String, parameters: Map<String, String>): TaskBoardDetail =
        decodeTaskBoardDetail(request("/daily-work/board/${encodePath(id)}?${boardQuery(parameters)}"))
    override suspend fun taskBoardSelection(frozenRequest: String): TaskBoardSelection =
        parseTaskBoardSelection(request("/daily-work/selections", JSONObject(frozenRequest)))
    override suspend fun packingPrintPreview(frozenRequest: String): PackingPrintPreview =
        parsePackingPrintPreview(request("/print/jobs/preview", JSONObject(frozenRequest)))
    override suspend fun resetPackingPrint(orderNo: String, frozenRequest: String) {
        request("/orders/${encodePath(orderNo)}/print-state/reset", JSONObject(frozenRequest))
    }
    override suspend fun salesGroupAction(id: String, frozenRequest: String): SalesGroupActionResult =
        parseSalesGroupAction(request("/daily-work/groups/${encodePath(id)}/actions", JSONObject(frozenRequest)))
    override suspend fun exportTaskBoardSnapshot(frozenRequest: String): BoardExportSnapshot =
        parseBoardExportSnapshot(request("/daily-work/export-snapshot", JSONObject(frozenRequest)))
    override suspend fun assistantRequest(path: String, body: JSONObject?, method: String): JSONObject {
        require((path.startsWith("/assistant/") || path.startsWith("/sales-assistant/")) && !path.contains("..") &&
            method in setOf("GET", "POST", "PATCH", "PUT")) { "助手请求范围无效" }
        val origin = ServerAddress.normalize(baseUrl())
        val token = sessions?.load(origin)
        return request(path, body, method, boundOrigin = origin, boundToken = token)
    }
    override suspend fun uploadSalesAttachment(sessionId: String, version: Int, key: String,
        name: String, mimeType: String, bytes: ByteArray): JSONObject {
        val origin = ServerAddress.normalize(baseUrl())
        val token = sessions?.load(origin)
        return withContext(Dispatchers.IO) {
        require(bytes.isNotEmpty() && bytes.size <= 10 * 1024 * 1024) { "附件为空或超过 10 MB" }
        require(mimeType.matches(Regex("[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+"))) { "附件类型无效" }
        check(ServerAddress.normalize(baseUrl()) == origin && sessions?.load(origin) == token) { "账号或服务已变化，附件未发送" }
        val target = "$origin/api/v2/sales-assistant/sessions/${encodePath(sessionId)}/attachments"
        val connection = URI(target).toURL().openConnection() as HttpURLConnection
        val boundary = "ul-${java.util.UUID.randomUUID()}"
        try {
            connection.connectTimeout = 6000; connection.readTimeout = 30000
            connection.instanceFollowRedirects = false; connection.requestMethod = "POST"; connection.doOutput = true
            token?.let { connection.setRequestProperty("Authorization", "Bearer $it") }
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
            connection.outputStream.use { output ->
                fun write(value: String) = output.write(value.toByteArray(Charsets.UTF_8))
                for ((field, value) in listOf("version" to version.toString(), "idempotency_key" to key))
                    write("--$boundary\r\nContent-Disposition: form-data; name=\"$field\"\r\n\r\n$value\r\n")
                val safeName = name.replace(Regex("[\\r\\n\"\\\\]"), "_").take(200)
                write("--$boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"$safeName\"\r\nContent-Type: $mimeType\r\n\r\n")
                output.write(bytes); write("\r\n--$boundary--\r\n")
            }
            val status = connection.responseCode
            if (status == 401 && sessions?.load(origin) == token) { sessions?.clear(origin); onUnauthorized(origin) }
            val raw = (if (status in 200..299) connection.inputStream else connection.errorStream)
                ?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
            val response = runCatching { JSONObject(raw) }.getOrElse { throw ServiceException("invalid_response", "附件服务返回无效数据", status) }
            if (status !in 200..299 || !response.optBoolean("ok", true)) throw ServiceException(response.optString("error", "http_$status"),
                response.optString("message", "附件上传失败（$status）"), status)
            response
        } finally { connection.disconnect() }
        }
    }
    override suspend fun downloadSalesAttachment(sessionId: String, attachmentId: String): SalesAttachmentFile {
        require(sessionId.isNotBlank() && attachmentId.isNotBlank()) { "原附件身份无效" }
        val origin = ServerAddress.normalize(baseUrl())
        val token = sessions?.load(origin)
        return withContext(Dispatchers.IO) {
            check(ServerAddress.normalize(baseUrl()) == origin && sessions?.load(origin) == token) { "账号或服务已变化，原附件未读取" }
            val connection = URI("$origin/api/v2/sales-assistant/sessions/${encodePath(sessionId)}/attachments/${encodePath(attachmentId)}")
                .toURL().openConnection() as HttpURLConnection
            try {
                connection.connectTimeout = 6000; connection.readTimeout = 30000; connection.instanceFollowRedirects = false
                token?.let { connection.setRequestProperty("Authorization", "Bearer $it") }
                connection.setRequestProperty("Accept", "application/pdf,image/png,image/jpeg,image/webp")
                val status = connection.responseCode
                if (status == 401 && sessions?.load(origin) == token) { sessions?.clear(origin); onUnauthorized(origin) }
                if (status !in 200..299) throw ServiceException("http_$status", "原附件读取失败（HTTP $status）", status)
                val mime = connection.contentType.orEmpty().substringBefore(';').trim().lowercase()
                require(mime in setOf("application/pdf", "image/png", "image/jpeg", "image/webp")) { "此原附件类型暂不支持查看" }
                require(connection.contentLengthLong <= 10L * 1024 * 1024 || connection.contentLengthLong < 0) { "原附件超过 10 MB" }
                val output = java.io.ByteArrayOutputStream()
                connection.inputStream.use { input ->
                    val buffer = ByteArray(32 * 1024)
                    while (true) {
                        val count = input.read(buffer); if (count < 0) break
                        require(output.size() + count <= 10 * 1024 * 1024) { "原附件超过 10 MB" }
                        output.write(buffer, 0, count)
                    }
                }
                check(ServerAddress.normalize(baseUrl()) == origin && sessions?.load(origin) == token) { "账号或服务已变化，原附件不会打开" }
                require(output.size() > 0) { "原附件为空" }
                SalesAttachmentFile(output.toByteArray(), mime)
            } finally { connection.disconnect() }
        }
    }
    override suspend fun taskHistory(role: String, view: String, scope: String, page: Int): TaskHistoryPage {
        val query = boardQuery(mapOf("role" to role, "view" to view, "scope" to scope, "page" to page.toString()))
        return parseTaskHistory(request("/daily-work/task-history?$query"))
    }
    override suspend fun taskHistoryDetail(id: String, role: String, view: String, page: Int): TaskHistoryDetail {
        val query = boardQuery(mapOf("role" to role, "view" to view, "page" to page.toString()))
        return parseTaskHistoryDetail(request("/daily-work/task-history/${encodePath(id)}?$query"))
    }
    override suspend fun salesHistory(view: String, scope: String, page: Int): TaskHistoryPage {
        val query = boardQuery(mapOf("view" to view, "scope" to scope, "page" to page.toString()))
        return parseSalesHistory(request("/daily-work/groups/history?$query"))
    }
    override suspend fun salesHistoryDetail(id: String, view: String, page: Int): TaskHistoryDetail {
        val query = boardQuery(mapOf("view" to view, "page" to page.toString()))
        return parseSalesHistoryDetail(request("/daily-work/groups/history/${encodePath(id)}?$query"))
    }
    override suspend fun adminDashboard(): AdminDashboard = parseAdminDashboard(request("/daily-work/dashboard"))
    override suspend fun dashboardCompletions(role: String, actorId: String, source: String, page: Int): DashboardCompletions {
        val params = mapOf("role" to role, "actor_id" to actorId, "source" to source, "page" to page.toString())
        val query = params.entries.joinToString("&") { "${it.key}=${URLEncoder.encode(it.value, "UTF-8")}" }
        return parseDashboardCompletions(request("/daily-work/dashboard/completions?$query"))
    }
    override suspend fun purchaseUnfinishedStatus(): PurchaseSnapshotMeta =
        PurchaseUnfinishedJson.meta(request("/purchase-unfinished/status"))
    override suspend fun purchaseSuppliers(): PurchaseSuppliers =
        PurchaseUnfinishedJson.suppliers(request("/purchase-unfinished/suppliers"))
    override suspend fun purchaseUnfinished(supplierCode: String, page: Int, orderNo: String): PurchaseUnfinishedPage {
        val query = boardQuery(mapOf("supplier_code" to supplierCode, "page" to page.toString(), "order_no" to orderNo))
        return PurchaseUnfinishedJson.page(request("/purchase-unfinished?$query"))
    }
    override suspend fun purchaseReminder(supplierCode: String): PurchaseReminderGate =
        PurchaseUnfinishedJson.reminder(request("/purchase-unfinished/suppliers/${encodePath(supplierCode)}/reminder-preview"))
    override suspend fun savePurchasePromise(lineKey: String, frozenRequest: String) {
        request("/purchase-unfinished/promises/${encodePath(lineKey)}", JSONObject(frozenRequest), method = "PUT")
    }
    override suspend fun customerOwners(): List<CustomerOwner> =
        jsonRows(request("/daily-work/customer-owners"), "items").map(::parseCustomerOwner)
    override suspend fun saveCustomerOwner(frozenRequest: String, deputy: Boolean) {
        request(if (deputy) "/daily-work/customer-deputies" else "/daily-work/customer-owners",
            JSONObject(frozenRequest), method = "PUT")
    }
    override suspend fun taskBoardBatch(frozenRequest: String) {
        request("/daily-work/batches", JSONObject(frozenRequest))
    }
    override suspend fun customerOrders(role: String, view: String, status: String, keyword: String, page: Int): CustomerOrderPage {
        val params = linkedMapOf("view" to view, "status" to status, "keyword" to keyword,
            "page" to page.toString()).apply { role.takeIf(String::isNotBlank)?.let { put("role", it) } }
        val query = params.entries.joinToString("&") { "${it.key}=${URLEncoder.encode(it.value, "UTF-8")}" }
        return parseCustomerOrderPage(request("/daily-work/groups?$query"))
    }
    override suspend fun customerOrderDetail(id: String, role: String, view: String): CustomerOrderDetail {
        val params = linkedMapOf("view" to view).apply { role.takeIf(String::isNotBlank)?.let { put("role", it) } }
        val query = params.entries.joinToString("&") { "${it.key}=${URLEncoder.encode(it.value, "UTF-8")}" }
        return parseCustomerOrderDetail(request("/daily-work/groups/${encodePath(id)}?$query"))
    }
    override suspend fun recordCustomerOrderFollowup(id: String, key: String, version: Int, note: String,
                                                     waitingFor: String, nextDate: String) {
        request("/daily-work/groups/${encodePath(id)}/followups", JSONObject()
            .put("idempotency_key", key).put("version", version).put("note", note.trim())
            .put("waiting_for", waitingFor.trim()).put("next_follow_up_date", nextDate))
    }
    override suspend fun transferCustomerOrder(id: String, key: String, version: Int, assigneeId: String, reason: String) {
        request("/daily-work/groups/${encodePath(id)}/transfer", JSONObject()
            .put("idempotency_key", key).put("version", version).put("assignee_id", assigneeId)
            .put("reason", reason.trim()))
    }
    override suspend fun dailyWorkVersion(): DailyWorkVersion = parseDailyWorkVersion(request("/daily-work/version"))
    override suspend fun businessConfirmations(): List<String> = jsonRows(request("/business-confirmations"), "items").map {
        "${it.optString("message")}\n编号 ${it.optString("id")} · ${when(it.optString("status")){ "approved" -> "已确认，待执行核验"; "expired" -> "确认码已过期，事项仍待处理，请联系管理员重新发起"; else -> "待确认" }} · 微信${when(it.optString("delivery")){ "accepted" -> "已发送"; "pending" -> "待发送"; else -> "发送待核对" }}"
    }
    override suspend fun packingFilterOptions(role: String, view: String): PackingFilterOptions {
        val value = request("/daily-work/board/filter-options?${boardQuery(mapOf("role" to role, "view" to view))}")
        return PackingFilterOptions(jsonRows(value, "customers").map { PackingFilterOption(it.getString("id"), it.getString("name")) },
            jsonRows(value, "suppliers").map { PackingFilterOption(it.getString("id"), it.getString("name")) })
    }
    override suspend fun refreshDailyWork(idempotencyKey: String) {
        request("/daily-work/refresh", JSONObject().put("idempotency_key", idempotencyKey))
    }
    override suspend fun submitDailyAction(taskId: String, action: String, note: String, nextFollowUpDate: String,
                                           idempotencyKey: String, version: Int) {
        request("/daily-work/tasks/${encodePath(taskId)}/actions", JSONObject()
            .put("action", action).put("note", note.trim()).put("next_follow_up_date", nextFollowUpDate)
            .put("idempotency_key", idempotencyKey).put("version", version))
    }
    override suspend fun transferDailyTask(taskId: String, assigneeId: String, reason: String,
                                           idempotencyKey: String, version: Int) {
        request("/daily-work/tasks/${encodePath(taskId)}/transfer", JSONObject()
            .put("assignee_id", assigneeId).put("reason", reason.trim())
            .put("idempotency_key", idempotencyKey).put("version", version))
    }
    override suspend fun dailyWorkSettings(): DailyWorkSettings = parseDailyWorkSettings(request("/daily-work/settings"))
    override suspend fun setDailyWorkSettings(assignmentEnabled: Boolean): DailyWorkSettings =
        parseDailyWorkSettings(request("/daily-work/settings", JSONObject().put("assignment_enabled", assignmentEnabled), method = "PUT"))
    override suspend fun assignmentRules(): List<AssignmentRule> {
        val rows = request("/daily-work/assignment-rules").optJSONArray("items") ?: JSONArray()
        return (0 until rows.length()).mapNotNull { rows.optJSONObject(it)?.let(::parseAssignmentRule) }
    }
    override suspend fun saveAssignmentRule(role: String, matchValue: String, staffId: String): List<AssignmentRule> {
        val matchType = if (role == ROLE_SALES) "customer" else "sku"
        val rows = request("/daily-work/assignment-rules", JSONObject().put("role", role).put("match_type", matchType)
            .put("match_value", matchValue.trim()).put("staff_id", staffId).put("priority", 100), method = "PUT")
            .optJSONArray("items") ?: JSONArray()
        return (0 until rows.length()).mapNotNull { rows.optJSONObject(it)?.let(::parseAssignmentRule) }
    }
    override suspend fun deleteAssignmentRule(id: String) {
        request("/daily-work/assignment-rules/${encodePath(id)}", method = "DELETE")
    }
    override suspend fun setDailyTaskUrgent(taskId: String, urgent: Boolean, reason: String,
                                            idempotencyKey: String, version: Int) {
        request("/daily-work/tasks/${encodePath(taskId)}/urgent", JSONObject().put("urgent", urgent)
            .put("reason", reason.trim()).put("idempotency_key", idempotencyKey).put("version", version))
    }
    override suspend fun staff(): List<StaffMember> {
        val data = request("/staff")
        val items = data.optJSONArray("items") ?: data.optJSONArray("staff") ?: JSONArray()
        return (0 until items.length()).mapNotNull { items.optJSONObject(it)?.let(::parseStaff) }
    }
    override suspend fun createStaff(draft: StaffDraft): StaffMember {
        val data = request("/staff", staffPayload(draft, includeIdentity = true))
        return parseStaff(data.optJSONObject("staff") ?: data.optJSONObject("user") ?: data)
    }
    override suspend fun updateStaff(id: String, draft: StaffDraft): StaffMember {
        val data = request("/staff/${encodePath(id)}", staffPayload(draft, includeIdentity = false), method = "PATCH")
        return parseStaff(data.optJSONObject("staff") ?: data.optJSONObject("user") ?: data)
    }
    override suspend fun resetStaffPassword(id: String, password: String) {
        request("/staff/${encodePath(id)}/password", JSONObject().put("password", password))
    }
    override suspend fun attendance(): Map<String, Boolean> {
        val data = request("/daily-work/attendance")
        val items = data.optJSONArray("items") ?: data.optJSONArray("attendance") ?: JSONArray()
        return (0 until items.length()).mapNotNull { index -> items.optJSONObject(index)?.let { row ->
            row.optString("user_id").takeIf(String::isNotBlank)?.let {
                it to if (row.has("on_duty")) row.optBoolean("on_duty") else row.optBoolean("present")
            }
        } }.toMap()
    }
    override suspend fun setAttendance(userId: String, present: Boolean) {
        request("/daily-work/attendance", JSONObject().put("user_id", userId).put("on_duty", present), method = "PUT")
    }
    override suspend fun resolveFreight(id: String, resolution: String, note: String) {
        require(resolution in setOf("applied", "not_applied"))
        request("/freight-attempts/$id/resolve", JSONObject().put("resolution", resolution).put("note", note))
    }
    override suspend fun startUpdate(id: String, write: Boolean) = parseTask(request("/update-jobs",
        JSONObject().put("id", id).put("mode", "sync")).getJSONObject("job"))
    override suspend fun latestUpdate(): UpdateTask? = request("/update-jobs/latest").optJSONObject("job")?.let(::parseTask)
    override suspend fun updateStatus(id: String) = parseTask(request("/update-jobs/$id").getJSONObject("job"))

    override suspend fun health(): ServiceHealth {
        val data = request("/health")
        val counts = data.optJSONObject("counts") ?: JSONObject()
        val sources = data.optJSONArray("sources")
        val updated = data.optString("source_updated_at").ifBlank {
            sources?.let { (0 until it.length()).mapNotNull { n -> it.optJSONObject(n)?.optString("source_mtime")?.takeIf(String::isNotBlank) }.maxOrNull() }.orEmpty()
        }
        val capabilities = data.optJSONObject("capabilities") ?: JSONObject()
        return ServiceHealth(data.optString("data_version"), displayTime(updated), counts.optInt("orders"), counts.optInt("inventory"),
            capabilities.optBoolean("wsgjp_api"), capabilities.optBoolean("logistics_write"),
            capabilities.optBoolean("inventory_batch_upload", !capabilities.optBoolean("wsgjp_api")), data.optString("profile_id"))
    }

    override suspend fun query(destination: Destination, q: String, mode: String, scope: String, logistics: String, page: Int, inventoryFilters: InventoryFilters, expectedDataVersion: String): QueryResult =
        queryInternal(destination, q, mode, scope, logistics, page, inventoryFilters, OrderFilters(), expectedDataVersion)

    override suspend fun queryOrders(q: String, mode: String, scope: String, logistics: String, page: Int,
                                     filters: OrderFilters, expectedDataVersion: String): QueryResult =
        queryInternal(Destination.Orders, q, if (mode == "note") "summary" else mode, scope, logistics, page,
            InventoryFilters(), filters, expectedDataVersion)

    private suspend fun queryInternal(destination: Destination, q: String, mode: String, scope: String, logistics: String, page: Int,
                                      inventoryFilters: InventoryFilters, orderFilters: OrderFilters,
                                      expectedDataVersion: String): QueryResult {
        val params = linkedMapOf("q" to q, "page" to page.toString(), "page_size" to "50")
        if (expectedDataVersion.isNotBlank()) params["expected_data_version"] = expectedDataVersion
        val orders = destination == Destination.Orders
        if (orders) {
            params["count_basis"] = "lines"
            params.putAll(mapOf("mode" to mode, "scope" to scope, "logistics_company" to logistics))
            params.putAll(orderFilters.parameters())
        }
        else {
            params.putAll(inventoryFilters.parameters())
            if (mode == "sku_exact") params["q"] = ""
        }
        val query = params.entries.joinToString("&") { "${it.key}=${URLEncoder.encode(it.value, "UTF-8")}" }
        val data = request("/${if (orders) "orders" else "inventory"}?$query")
        val items = data.optJSONArray("items") ?: JSONArray()
        val parsed = (0 until items.length()).map { n ->
            parseItem(items.getJSONObject(n), orders)
        }
        val inv = data.optJSONObject("inventory")
        val printCounts = data.optJSONObject("line_state_counts") ?: data.optJSONObject("print_state_counts") ?: JSONObject()
        val customerArray = data.optJSONArray("customer_options") ?: JSONArray()
        return QueryResult(parsed, data.optInt("total"), page,
            if (orders) quantity(data, "total_quantity") else null,
            if (orders) quantity(inv, "available_stock", true) else quantity(data, "total_available"),
            if (orders) quantity(inv, "stock_quantity", true) else quantity(data, "total_stock"),
            inv?.optString("product_name").orEmpty(), data.getString("data_version"), displayTime(data.optString("source_updated_at")),
            PrintStateCounts(printCounts.optInt("all"), printCounts.optInt("unprinted"), printCounts.optInt("printed"),
                printCounts.optInt("reserved"), printCounts.optInt("uncertain")),
            (0 until customerArray.length()).mapNotNull { customerArray.optString(it).trim().takeIf(String::isNotBlank) })
    }

    private fun prepareBody(state: WorkbenchState, all: Boolean, maximum: Int?, prepareKey: String): JSONObject {
        val body = JSONObject().put("kind", if (state.destination == Destination.Orders) "order" else "inventory")
            .put("scope", state.scope).put("logistics_company", state.logistics).put("expected_data_version", state.dataVersion)
            .put("device_id", state.deviceId)
        if (prepareKey.isNotBlank()) body.put("prepare_idempotency_key", prepareKey)
        if (all) body.put("all", true).put("query", printQuery(state))
        else if (state.destination == Destination.Orders) {
            val selected = state.items.filter { it.id in state.selectedIds }
            val orders = selected.map { it.orderNo }.filter(String::isNotBlank).distinct()
            selected.map { it.profileId }.filter(String::isNotBlank).distinct().singleOrNull()?.let { body.put("profile_id", it) }
            if (orders.isNotEmpty()) body.put("order_nos", JSONArray(orders)) else body.put("item_ids", JSONArray(state.selectedIds.toList()))
        } else body.put("item_ids", JSONArray(state.selectedIds.toList()))
        if (maximum != null) body.put("max_per_package", maximum)
        return body
    }

    override fun freezePrepareRequest(state: WorkbenchState, all: Boolean, maximum: Int?, prepareKey: String): String =
        prepareBody(state, all, maximum, prepareKey).toString()

    override suspend fun prepare(state: WorkbenchState, all: Boolean, maximum: Int?): PreparedPrint =
        prepare(state, all, maximum, java.util.UUID.randomUUID().toString())

    override suspend fun prepare(state: WorkbenchState, all: Boolean, maximum: Int?, prepareKey: String): PreparedPrint =
        replayPrepare(freezePrepareRequest(state, all, maximum, prepareKey))

    override suspend fun replayPrepare(frozenRequest: String): PreparedPrint {
        val json = request("/print/jobs", JSONObject(frozenRequest))
        return PreparedPrint(json.getString("job_id"), json.getInt("labels"))
    }

    override suspend fun claim(id: String, actionKey: String): ClaimedPrint = claim(id, actionKey, "")
    override suspend fun claim(id: String, actionKey: String, deviceId: String): ClaimedPrint {
        val json = request("/print/jobs/$id/claim", JSONObject().put("transport", "native").put("idempotency_key", actionKey)
            .put("user_initiated", true).put("device_id", deviceId))
        if (json.optBoolean("replayed")) throw ServiceException("already_claimed", "该打印任务已领取，请核对打印记录，不能重复发送")
        val pages = json.getJSONArray("cpcl_pages")
        return ClaimedPrint(json.getString("claim_token"), (0 until pages.length()).map { java.util.Base64.getDecoder().decode(pages.getString(it)) })
    }

    override suspend fun printJob(id: String): PrintJobState {
        val json = request("/print/jobs/${encodePath(id)}")
        return PrintJobState(id, json.optString("status"),
            if (json.has("bytes_sent") && !json.isNull("bytes_sent")) json.optInt("bytes_sent", -1).takeIf { it >= 0 } else null)
    }

    override suspend fun cancelPreparedPrint(id: String, actionKey: String, deviceId: String, reason: String) {
        request("/print/jobs/${encodePath(id)}/cancel", JSONObject()
            .put("device_id", deviceId).put("idempotency_key", actionKey).put("reason", reason))
    }

    override suspend fun recoverClaimedPrint(id: String, actionKey: String, deviceId: String, reason: String) {
        request("/print/jobs/${encodePath(id)}/recover", JSONObject()
            .put("device_id", deviceId).put("idempotency_key", actionKey).put("reason", reason))
    }

    override suspend fun report(id: String, token: String, status: String, message: String, bytes: Int) =
        report(id, token, status, message, bytes, "")
    override suspend fun report(id: String, token: String, status: String, message: String, bytes: Int, deviceId: String) {
        request("/print/jobs/$id/result", JSONObject().put("claim_token", token).put("status", status).put("message", message)
            .put("bytes_sent", bytes).put("device_id", deviceId))
    }

    override suspend fun inventoryHistory(recordId: String): InventoryHistory = inventoryHistory(recordId, 90, 30, 0)

    override suspend fun inventoryHistory(recordId: String, days: Int, limit: Int, offset: Int): InventoryHistory {
        require(days in setOf(30, 90, 180) && limit == 30 && offset >= 0) { "库存台账范围无效" }
        val data = request("/inventory/${encodePath(recordId)}/history?days=$days&limit=$limit&offset=$offset")
        val snapshot = data.optJSONObject("snapshot") ?: JSONObject()
        val coverage = data.optJSONObject("coverage") ?: JSONObject()
        val movements = data.optJSONArray("movements") ?: JSONArray()
        val pending = data.optJSONArray("pending_adjustments") ?: JSONArray()
        val paging = data.optJSONObject("pagination") ?: data
        val nextOffset = coverage.opt("next_offset")
        val hasMore = paging.optBoolean("has_more", nextOffset != null && nextOffset != JSONObject.NULL)
        return InventoryHistory(recordId, data.optString("status", "pending"),
            quantity(snapshot, "physical_quantity"), quantity(snapshot, "saleable_quantity"), displayTime(snapshot.optString("as_of")),
            coverage.optString("from"), coverage.optString("to"), coverage.optInt("positive_inbounds"), coverage.optBoolean("truncated"),
            (0 until movements.length()).mapNotNull { i -> movements.optJSONObject(i)?.let { row ->
                InventoryMovement(row.optString("id"), row.optString("posted_at"), row.optString("date"), row.optString("document_no"),
                    row.optString("document_type"), row.optString("party"), quantity(row, "quantity") ?: "0",
                    row.optString("direction"), quantity(row, "balance_after"), row.optString("reconciliation_status"))
            } }, (0 until pending.length()).mapNotNull { i -> when (val value = pending.opt(i)) {
                is JSONObject -> value.optString("message").ifBlank { value.optString("document_no") }.ifBlank { value.toString() }
                is String -> value
                else -> null
            } }, paging.optInt("total", coverage.optInt("total", if (hasMore) -1 else offset + movements.length())), hasMore)
    }

    override suspend fun productImages(productId: String): List<ProductImage> {
        val data = request("/products/${encodePath(productId)}/images")
        val items = data.optJSONArray("items") ?: JSONArray()
        return (0 until items.length()).mapNotNull { i -> items.optJSONObject(i)?.let { row ->
            ProductImage(row.optString("id"), row.optString("product_id", productId), row.optString("content_url"),
                row.optString("mime_type", "image/jpeg"), row.optInt("width"), row.optInt("height"),
                row.optString("etag"), row.optString("updated_at"))
        } }.take(1)
    }

    override suspend fun productImage(image: ProductImage): ByteArray = requestBytes(image.contentUrl)

    override suspend fun resetOrderPrintState(orderNo: String, profileId: String, deviceId: String) {
        request("/orders/${encodePath(orderNo)}/print-state/reset", JSONObject().put("profile_id", profileId)
            .put("device_id", deviceId).put("reason", "包装人员手动恢复未打印"))
    }

    override suspend fun refreshInventorySubset(productIds: Set<String>, id: String): UpdateTask = parseTask(request("/update-jobs",
        JSONObject().put("id", id).put("mode", "inventory_subset").put("product_ids", JSONArray(productIds.toList()))).getJSONObject("job"))

    override suspend fun submitAdjustment(batch: AdjustmentBatch): AdjustmentBatch =
        AdjustmentJson.batch(request("/adjustment-batches", AdjustmentJson.payload(batch)).getJSONObject("batch"))

    override suspend fun adjustment(id: String, clientId: String): AdjustmentBatch =
        AdjustmentJson.batch(request("/adjustment-batches/$id?client_id=$clientId").getJSONObject("batch"))

    override suspend fun retryAdjustment(id: String, clientId: String): AdjustmentBatch =
        AdjustmentJson.batch(request("/adjustment-batches/$id/retry", JSONObject().put("client_id", clientId)).getJSONObject("batch"))

    override suspend fun confirmAdjustment(id: String, clientId: String): AdjustmentBatch =
        AdjustmentJson.batch(request("/adjustment-batches/$id/confirm", JSONObject().put("client_id", clientId).put("confirmed", true)).getJSONObject("batch"))

    companion object {
        fun parseUser(value: JSONObject): CurrentUser = CurrentUser(
            value.optString("id"), value.optString("username"),
            value.optString("display_name").ifBlank { value.optString("username") },
            stringList(value.optJSONArray("roles")), value.optBoolean("is_admin"),
            value.optBoolean("active", true), value.optInt("packing_limit", 3).coerceAtLeast(0)
        )

        fun parseStaff(value: JSONObject): StaffMember {
            val user = parseUser(value)
            return StaffMember(user.id, user.username, user.displayName, user.roles, user.active, user.packingLimit, user.isAdmin)
        }

        private fun jsonRows(value: JSONObject, key: String): List<JSONObject> {
            val rows = value.optJSONArray(key) ?: JSONArray()
            return (0 until rows.length()).mapNotNull(rows::optJSONObject)
        }
        private fun parseBoardEvents(value: JSONObject, key: String) = jsonRows(value, key).map {
            val type = it.optString("event_type")
            val label = it.optString("title").ifBlank { when (type) {
                "start" -> "开始处理"; "progress" -> "处理进展"; "wait" -> "等待事项"
                "packed" -> "包装完成"; "reopen" -> "重新打开"; "transfer" -> "负责人调整"
                "erp_business_completed" -> "业务完成"; "created" -> "任务建立"
                "reset_to_todo" -> "退回待处理"; "note" -> "备注"
                else -> type.ifBlank { "处理记录" }
            } }
            DailyWorkEvent(it.optString("id"), it.optString("task_id"), type,
                label, it.optString("note"), displayTime(it.optString("created_at")))
        }
        private fun packingSources(v: JSONObject): List<String> = jsonRows(v, "arrival_sources").map {
            "${it.optString("supplier_name")} · ${it.optString("document_no")} · ${it.optString("receipt_date")} · ${it.optString("sku")} ${it.optString("quantity")}支"
        }
        fun parseTaskBoardItem(v: JSONObject) = TaskBoardItem(
            id = v.getString("id"), kind = v.optString("kind"), role = v.optString("role"), title = v.optString("title"),
            subtitle = v.optString("subtitle"), sku = v.optString("sku"), productName = v.optString("product_name"),
            specification = v.optString("specification"), unit = v.optString("unit"), location = v.optString("location"),
            quantity = v.optString("quantity"), shortage = v.optString("shortage"), pending = v.optString("pending"),
            orderCount = v.optInt("order_count"), customerOrderNo = v.optString("customer_order_no"), customer = v.optString("customer"),
            dueDate = v.optString("due_date"), status = v.optString("status"), blockedBy = v.optString("blocked_by"),
            assigneeId = v.optString("assignee_id"), assigneeName = v.optString("assignee_name"),
            ownerNames = stringList(v.optJSONArray("owner_names")), canAct = v.optBoolean("can_act"), urgent = v.optBoolean("urgent"),
            readyCount = v.optInt("ready_count"), missingCount = v.optInt("missing_count"), reviewCount = v.optInt("review_count"),
            packedCount = v.optInt("packed_count"), totalCount = v.optInt("total_count"),
            customerOrderImpact = v.optInt("customer_order_impact"), nearCompleteCustomerOrders = v.optInt("near_complete_customer_orders"),
            waitingFor = v.optString("waiting_for"), promisedDate = v.optString("promised_date"),
            nextFollowUpDate = v.optString("next_follow_up_date"), version = v.optInt("version"),
            missingItems = jsonRows(v, "missing_items").map { row -> DailyMissingItem(row.optString("sku"),
                row.optString("product_id"), row.optString("shortage"), row.optString("pending"), row.optString("order_no")) },
            profileId = v.optString("profile_id"), unitNote = v.optString("unit_note"),
            customerSalesOwners = jsonRows(v, "customer_sales_owners").map { CustomerSalesOwner(
                it.optString("customer"), it.optString("owner_id"), it.optString("owner_name"), it.optBoolean("review_required")) },
            businessStatus = v.optString("business_status"), workState = v.optString("work_state"),
            hasNote = v.optBoolean("has_note"), latestNote = v.optString("latest_note"),
            printState = v.optString("print_state"), workLabel = v.optString("work_label"),
            latestPrintActorName = v.optString("latest_print_actor_name"), latestPrintAt = displayTime(v.optString("latest_print_at")),
            historicalPrint = v.optBoolean("historical_print"), canPrint = v.optBoolean("can_print", v.optBoolean("can_act")),
            printBlockedBy = v.optString("print_blocked_by"), model = v.optString("model"), arrivalSources = packingSources(v),
            arrivalReceived = v.optString("arrival_received"), arrivalRequired = v.optString("arrival_required"),
            arrivalShortage = v.optString("arrival_shortage"), arrivalPartial = v.optBoolean("arrival_partial"), arrivalOverdue = v.optBoolean("arrival_overdue"))
        fun parseSalesGroupAction(v: JSONObject) = SalesGroupActionResult(v.getInt("version"),
            v.getString("task_version"), v.getString("work_state"), v.optBoolean("has_note"), v.optString("latest_note"))
        internal suspend fun decodeTaskBoard(value: JSONObject): TaskBoardPage =
            withContext(Dispatchers.Default) { parseTaskBoard(value) }
        internal suspend fun decodeTaskBoardDetail(value: JSONObject): TaskBoardDetail =
            withContext(Dispatchers.Default) { parseTaskBoardDetail(value) }
        fun parseTaskBoard(value: JSONObject): TaskBoardPage {
            val summary = value.optJSONObject("summary") ?: JSONObject()
            return TaskBoardPage(items = jsonRows(value, "items").map(::parseTaskBoardItem), total = value.optInt("total"),
                page = value.optInt("page", 1), hasMore = value.optBoolean("has_more"), dataVersion = value.optString("data_version"),
                taskVersion = value.optString("task_version"), sourceUpdatedAt = displayTime(value.optString("source_updated_at")),
                tasksCurrent = value.optBoolean("tasks_current", true), businessDate = value.optString("business_date"),
                summary = DailyWorkSummary(todo = summary.optInt("todo"), inProgress = summary.optInt("in_progress"), waiting = summary.optInt("waiting"),
                    packedWaiting = summary.optInt("packed"), unassigned = summary.optInt("unassigned"),
                    todayActions = summary.optInt("actions_completed_today"), businessCompleted = summary.optInt("business_completed_today")),
                todayEvents = parseBoardEvents(value, "today_events"),
                assignees = jsonRows(value.optJSONObject("filter_options") ?: value, "assignees").map { TaskBoardAssignee(it.optString("id"), it.optString("display_name")) },
                stale = value.optBoolean("stale"), assignmentEnabled = value.optBoolean("assignment_enabled").takeIf { value.has("assignment_enabled") },
                workStateCounts = summary.optJSONObject("work_state_counts")?.let { counts ->
                    counts.keys().asSequence().filter { it in setOf("todo", "in_progress", "exception") }.associateWith { counts.optInt(it) }
                }.orEmpty(), printStateVersion = value.optString("print_state_version"), orderCount = value.optInt("order_count"),
                orderWorkStateCounts = summary.optJSONObject("order_work_state_counts")?.let { counts ->
                    counts.keys().asSequence().filter { it in setOf("todo", "in_progress", "exception") }.associateWith { counts.optInt(it) }
                }.orEmpty(), lineCount = value.optInt("line_count"))
        }
        fun parseTaskBoardDetail(value: JSONObject) = TaskBoardDetail(
            item = parseTaskBoardItem(value.getJSONObject("item")),
            lines = jsonRows(value, "lines").map { v -> TaskBoardLine(v.getString("id"), v.optString("task_id"), v.optInt("version"),
                v.optString("order_no"), v.optString("sku"), v.optString("product_name"), v.optString("quantity"),
                v.optString("unit"), v.optString("location"), v.optString("summary"), v.optString("status"),
                v.optString("print_state"), v.optString("assignee_id"), v.optString("assignee_name"), v.optBoolean("can_act"),
                v.optString("blocked_by"), v.optString("customer"), v.optString("due_date"),
                jsonRows(v, "product_lines").map { line -> TaskBoardProductLine(line.optString("record_id"),
                    line.optString("sku"), line.optString("quantity"), line.optString("unit"), line.optString("summary"), line.optString("unit_note")) },
                unitNote = v.optString("unit_note"), fulfillmentLabel = v.optString("fulfillment_label"),
                packingBasis = v.optString("packing_basis"), stateRevision = v.optString("state_revision"),
                labelFingerprint = v.optString("label_fingerprint"), latestPrintActorName = v.optString("latest_print_actor_name"),
                latestPrintAt = displayTime(v.optString("latest_print_at")), historicalPrint = v.optBoolean("historical_print"),
                canPrint = v.optBoolean("can_print", v.optBoolean("can_act")), printBlockedBy = v.optString("print_blocked_by"),
                currentPrintActorName = v.optString("current_print_actor_name"), arrivalSources = packingSources(v)) },
            relatedGroups = jsonRows(value, "related_groups").map { v -> TaskBoardRelatedGroup(v.getString("id"),
                v.optString("customer_order_no"), v.optString("customer"), v.optString("assignee_name"),
                v.optInt("missing_count"), v.optString("due_date")) }, events = parseBoardEvents(value, "events"),
            dataVersion = value.optString("data_version"), taskVersion = value.optString("task_version"),
            tasksCurrent = value.optBoolean("tasks_current", true),
            notes = jsonRows(value, "notes").map { TaskBoardNote(it.optString("note"),
                displayTime(it.optString("created_at")), it.optString("actor_id"), it.optString("event_type")) },
            printStateVersion = value.optString("print_state_version"))
        fun parseTaskBoardSelection(value: JSONObject) = TaskBoardSelection(value.getString("selection_id"),
            value.optInt("card_count", value.optInt("object_count")), stringList(value.optJSONArray("order_nos")), value.optString("data_version"),
            value.optString("task_version"), value.optString("role"), value.optString("profile_id"), value.optInt("excluded_count"),
            value.optString("print_state_version"))

        fun parsePackingPrintPreview(value: JSONObject): PackingPrintPreview = PackingPrintPreview(
            value.optString("preview_id"), value.optString("preview_hash"), value.optInt("order_count"), value.optInt("label_count"),
            value.optInt("reprint_count"), value.optInt("uncertain_count"), jsonRows(value, "orders").map { row ->
                PackingPrintOrder(row.optString("profile_id"), row.optString("order_no"), row.optString("print_state"),
                    row.optString("state_revision"), row.optString("label_fingerprint"), row.optString("latest_print_actor_name"),
                    displayTime(row.optString("latest_print_at")), row.optInt("label_count"),
                    jsonRows(row, "lines").map { line -> PackingPrintLine(line.optString("line_id"), line.optString("sku"),
                        line.optString("quantity"), line.optInt("label_count"),
                        line.optJSONObject("fields")?.optString("name").orEmpty(),
                        line.optJSONObject("fields")?.optString("spec").orEmpty(), stringList(line.optJSONArray("print_warnings"))) }, arrivalSources = packingSources(row)) },
            jsonRows(value, "segments").map { row -> PackingPrintSegment(row.optInt("index"), stringList(row.optJSONArray("order_nos")),
                row.optInt("order_count"), row.optInt("label_count")) },
            lineCount = value.optInt("line_count")).also {
                    if (!it.valid()) throw ServiceException("invalid_print_preview", "打印预览不完整，请重新预览")
                }

        fun parseBoardExportSnapshot(value: JSONObject): BoardExportSnapshot {
            val exported = jsonRows(value, "items").map { entry ->
                val card = parseTaskBoardItem(entry.getJSONObject("card"))
                val lines = jsonRows(entry, "lines").map { row ->
                    BoardExportLine(row.optString("order_no"), row.optString("customer"), row.optString("sku"),
                        row.optString("product_name"), row.optString("quantity"), row.optString("unit"),
                        row.optString("location"), row.optString("summary"), row.optString("task_id"),
                        row.optString("specification"), row.optString("due_date"))
                }
                val orders = jsonRows(entry, "orders").map { order ->
                    val number = order.optString("order_no")
                    BoardExportOrder(number, jsonRows(order, "items").map { row ->
                        BoardExportLine(number, lines.firstOrNull { it.orderNo == number }?.customer.orEmpty(),
                            row.optString("sku"), row.optString("product_name"), row.optString("quantity"),
                            row.optString("unit"), row.optString("location"), row.optString("summary"),
                            specification = row.optString("specification"), deliveryDate = row.optString("delivery_date"))
                    })
                }
                BoardExportCard(card, lines, orders)
            }
            return BoardExportSnapshot(value.getString("snapshot_id"), displayTime(value.optString("exported_at")),
                value.optString("data_version"), value.optString("task_version"), exported, value.optString("print_state_version"))
        }

        fun parseTaskHistory(value: JSONObject) = TaskHistoryPage(
            jsonRows(value, "items").map(::parseDailyItem), value.optInt("total"),
            value.optInt("page", 1), value.optBoolean("has_more"))

        fun parseTaskHistoryDetail(value: JSONObject) = TaskHistoryDetail(
            parseDailyItem(value.getJSONObject("item")), parseBoardEvents(value, "events"),
            value.optInt("page", 1), value.optBoolean("has_more"))

        private fun parseSalesHistoryItem(value: JSONObject): DailyWorkItem {
            val orderNo = value.optString("customer_order_no")
            val customer = value.optString("customer_name")
            return DailyWorkItem(value.optString("group_id"), value.optInt("version"), ROLE_SALES,
                listOf(orderNo, customer).filter(String::isNotBlank).joinToString(" · "),
                reason = if (value.optBoolean("review_required")) "客户身份待核对" else if (value.optBoolean("source_present"))
                    "当前订单仍在业务快照" else "来源订单已归档",
                status = value.optString("status"), followUpDate = value.optString("next_follow_up_date"),
                assigneeId = value.optString("assignee_id"),
                assigneeName = value.optString("assignee_name").ifBlank { value.optString("assignee_id") },
                customer = customer)
        }

        fun parseSalesHistory(value: JSONObject) = TaskHistoryPage(
            jsonRows(value, "items").map(::parseSalesHistoryItem), value.optInt("total"),
            value.optInt("page", 1), value.optBoolean("has_more"))

        fun parseSalesHistoryDetail(value: JSONObject) = TaskHistoryDetail(
            parseSalesHistoryItem(value.getJSONObject("item")), jsonRows(value, "events").map { event ->
                val type = event.optString("event_type").ifBlank { "followup" }
                val title = when (type) {
                    "start" -> "开始处理"; "reset_to_todo" -> "退回待处理"; "note" -> "备注"; else -> "销售跟进"
                }
                DailyWorkEvent(event.optString("id"), event.optString("group_id"), type, title,
                    listOf(event.optString("note"), event.optString("waiting_for"))
                        .filter(String::isNotBlank).joinToString(" · "), displayTime(event.optString("created_at")))
            }, value.optInt("page", 1), value.optBoolean("has_more"))

        fun parseDashboardCompletions(value: JSONObject) = DashboardCompletions(jsonRows(value, "items").map {
            DashboardCompletion(it.optString("task_id"), it.optString("actor_id"), it.optString("event_type"),
                it.optString("note"), displayTime(it.optString("created_at")), it.optString("role"),
                it.optString("title"), it.optString("order_no"), it.optString("source"))
        }, value.optInt("total"), value.optInt("page", 1), value.optBoolean("has_more"))

        fun parseAdminDashboard(value: JSONObject): AdminDashboard {
            val company = value.optJSONObject("company") ?: JSONObject()
            val purchase = value.optJSONObject("purchase_snapshot") ?: JSONObject()
            return AdminDashboard(company.optInt("customer_orders"), company.optInt("unfinished_customer_orders"),
                company.optInt("ready_to_ship"), company.optInt("needs_review"),
                displayTime(value.optString("source_updated_at")),
                displayTime(value.optString("purchase_source_updated_at")),
                displayTime(value.optString("refreshed_at")), company.optString("history_coverage"),
                jsonRows(value, "roles").map { role ->
                    val summary = role.optJSONObject("summary") ?: JSONObject()
                    AdminDashboardRole(role.optString("role"), summary.optInt("total"), summary.optInt("todo"),
                        summary.optInt("in_progress"), summary.optInt("waiting"), summary.optInt("overdue"),
                        summary.optInt("unassigned"), jsonRows(role, "people").map { person ->
                            AdminDashboardPerson(person.optString("user_id"), person.optString("display_name"),
                                person.optInt("todo"), person.optInt("in_progress"), person.optInt("waiting"),
                                person.optInt("overdue"), person.optInt("total"), person.optInt("completed_today"),
                                displayTime(person.optString("last_action_at")))
                        }, summary.optInt("business_completed_today"))
                }, purchase.optString("status", "unavailable"), purchase.optInt("row_count"))
        }

        fun parseDailyWork(value: JSONObject): DailyWorkSnapshot {
            val summary = value.optJSONObject("summary") ?: JSONObject()
            val items = value.optJSONArray("items") ?: JSONArray()
            val completedItems = value.optJSONArray("completed_items") ?: JSONArray()
            val todayEvents = value.optJSONArray("today_events") ?: JSONArray()
            val staffLoad = value.optJSONArray("staff_load") ?: JSONArray()
            return DailyWorkSnapshot(
                businessDate = value.optString("business_date"), dataVersion = value.optString("data_version"),
                sourceUpdatedAt = displayTime(value.optString("source_updated_at")),
                taskVersion = value.optString("task_version"), stale = value.optBoolean("stale"),
                roles = stringList(value.optJSONArray("roles")),
                summary = DailyWorkSummary(summary.optInt("todo"), summary.optInt("in_progress"), summary.optInt("waiting"),
                    summary.optInt("today_actions", summary.optInt("actions_completed_today")),
                    summary.optInt("business_completed", summary.optInt("business_completed_today")),
                    summary.optInt("unassigned"), summary.optInt("overdue"), summary.optInt("packed_waiting")),
                items = (0 until items.length()).mapNotNull { index -> items.optJSONObject(index)?.let(::parseDailyItem) },
                completedItems = (0 until completedItems.length()).mapNotNull { index ->
                    completedItems.optJSONObject(index)?.let(::parseDailyItem)
                },
                todayEvents = (0 until todayEvents.length()).mapNotNull { index ->
                    todayEvents.optJSONObject(index)?.let { event ->
                        DailyWorkEvent(event.optString("id"), event.optString("task_id"), event.optString("event_type"),
                            event.optString("title"), event.optString("note"), displayTime(event.optString("created_at")))
                    }
                },
                assignmentEnabled = value.optBoolean("assignment_enabled").takeIf { value.has("assignment_enabled") },
                taskDataVersion = value.optString("task_data_version"),
                taskSourceUpdatedAt = displayTime(value.optString("task_source_updated_at")),
                tasksCurrent = value.optBoolean("tasks_current").takeIf { value.has("tasks_current") },
                staffLoad = (0 until staffLoad.length()).mapNotNull { index -> staffLoad.optJSONObject(index)?.let { row ->
                    StaffLoad(row.optString("user_id"), row.optString("display_name"), stringList(row.optJSONArray("roles")),
                        row.optBoolean("on_duty"), row.optInt("packing_limit", 3), row.optInt("todo"),
                        row.optInt("in_progress"), row.optInt("waiting"), row.optInt("packed"), row.optInt("total"))
                } }, printStateVersion = value.optString("print_state_version")
            )
        }

        fun parseCustomerOwner(value: JSONObject) = CustomerOwner(
            value.optString("profile_id"), value.optString("customer_name"), value.optString("owner_id"),
            value.optString("owner_name"), value.optString("deputy_id"), value.optString("deputy_name"),
            value.optString("deputy_until"), value.optString("status", "unassigned"), value.optString("source"),
            value.optInt("version"), stringList(value.optJSONArray("review_reasons")),
            value.optInt("active_task_count"), value.optInt("active_followup_count"),
            value.optBoolean("can_set_owner"), value.optBoolean("can_set_deputy"),
            jsonRows(value, "identity_conflicts").map { conflict ->
                "订单 ${conflict.optString("customer_order_no")}：" + stringList(conflict.optJSONArray("source_names"))
                    .joinToString("／") { it.ifBlank { "名称缺失" } } +
                    conflict.optString("previous_customer_name").takeIf(String::isNotBlank)?.let { "；原关联 $it" }.orEmpty()
            })

        fun parseCustomerOrderGroup(value: JSONObject): CustomerOrderGroup {
            val preview = value.optJSONArray("missing_preview") ?: JSONArray()
            return CustomerOrderGroup(value.optString("id"), value.optString("customer_order_no"),
                value.optString("customer"), value.optString("assignee_id"), value.optString("assignee_name"),
                value.optInt("follow_up_version"), value.optString("next_follow_up_date"),
                value.optString("due_date_from"), value.optString("due_date_to"),
                value.optInt("order_count"), value.optInt("unshipped_count"), value.optInt("ready_count"),
                value.optInt("packed_count"), value.optInt("procurement_count"), value.optInt("arrival_count"),
                value.optInt("review_count"), value.optInt("missing_count"), value.optInt("missing_product_count"),
                value.optInt("my_packing_count"), value.optInt("purchase_task_count"),
                value.optBoolean("ready_to_ship"), value.optBoolean("customer_review"),
                value.optBoolean("owner_review"), value.optBoolean("urgent"), value.optBoolean("lines_changed"),
                value.optString("status"),
                (0 until preview.length()).mapNotNull { preview.optJSONObject(it)?.let { item ->
                    DailyMissingItem(item.optString("sku"), item.optString("product_id"),
                        item.optString("shortage"), item.optString("pending"), item.optString("order_no"))
                } }, value.optString("profile_id"), value.optString("fixed_owner_id"),
                value.optString("deputy_id"), value.optString("deputy_name"), value.optString("deputy_until"),
                value.optInt("customer_owner_version"))
        }

        fun parseCustomerOrderPage(value: JSONObject): CustomerOrderPage {
            val rows = value.optJSONArray("items") ?: JSONArray()
            return CustomerOrderPage((0 until rows.length()).mapNotNull { rows.optJSONObject(it)?.let(::parseCustomerOrderGroup) },
                value.optInt("total"), value.optInt("page", 1), value.optBoolean("has_more"),
                value.optString("data_version"), value.optString("task_version"), value.optBoolean("tasks_current", true),
                value.optJSONObject("counts")?.let { counts -> listOf("shortage", "packing", "ready_to_ship", "review")
                    .associateWith { counts.optInt(it) } } ?: emptyMap())
        }

        fun parseCustomerOrderDetail(value: JSONObject): CustomerOrderDetail {
            val raw = value.optJSONObject("group") ?: error("客户整单明细缺失")
            val lines = raw.optJSONArray("lines") ?: JSONArray()
            val missing = raw.optJSONArray("missing_items") ?: JSONArray()
            val purchases = raw.optJSONArray("purchase_tasks") ?: JSONArray()
            val followups = raw.optJSONArray("followups") ?: JSONArray()
            return CustomerOrderDetail(parseCustomerOrderGroup(raw),
                (0 until lines.length()).mapNotNull { lines.optJSONObject(it)?.let { line ->
                    CustomerOrderLine(line.optString("order_no"), line.optString("status"), line.optBoolean("packed"),
                        line.optString("print_state"), line.optString("due_date"),
                        stringList(line.optJSONArray("tasks")), stringList(line.optJSONArray("skus")),
                        line.optString("fulfillment_label"), line.optString("packing_basis"))
                } },
                (0 until missing.length()).mapNotNull { missing.optJSONObject(it)?.let { item ->
                    DailyMissingItem(item.optString("sku"), item.optString("product_id"),
                        item.optString("shortage"), item.optString("pending"), item.optString("order_no"))
                } },
                (0 until purchases.length()).mapNotNull { purchases.optJSONObject(it)?.let { item ->
                    DailyWorkItem(item.optString("id"), 0, ROLE_PURCHASE, item.optString("title"),
                        status = item.optString("status"), sku = item.optString("sku"),
                        assigneeId = item.optString("assignee_id"), assigneeName = item.optString("assignee_name"))
                } },
                (0 until followups.length()).mapNotNull { followups.optJSONObject(it)?.let { item ->
                    CustomerOrderFollowup(item.optString("note"), item.optString("waiting_for"),
                        item.optString("next_follow_up_date"), displayTime(item.optString("created_at")))
                } }, value.optBoolean("tasks_current", true))
        }

        fun parseDailyWorkSettings(value: JSONObject) = DailyWorkSettings(
            value.optBoolean("assignment_enabled"), value.optInt("packing_limit_default", 3))

        fun parseDailyWorkVersion(value: JSONObject) = DailyWorkVersion(
            value.optString("business_date"), value.optString("data_version"), value.optString("task_version"),
            value.optBoolean("tasks_current").takeIf { value.has("tasks_current") },
            displayTime(value.optString("source_updated_at")), value.optString("print_state_version"))

        fun parseAssignmentRule(value: JSONObject) = AssignmentRule(
            value.optString("id"), value.optString("role"), value.optString("match_type"),
            value.optString("match_value"), value.optString("staff_id"), value.optString("display_name"),
            value.optInt("priority", 100), value.optBoolean("active", true))

        fun parseDailyItem(value: JSONObject): DailyWorkItem {
            val assignee = value.optJSONObject("assignee")
            val payload = value.optJSONObject("payload") ?: JSONObject()
            fun text(key: String): String = value.optString(key).trim().takeUnless { it == "null" }.orEmpty()
            val blocked = when (val raw = if (value.has("blocked_by")) value.opt("blocked_by") else payload.opt("blocked_by")) {
                is JSONArray -> stringList(raw).joinToString("、")
                is JSONObject -> raw.optString("display_name").ifBlank { raw.optString("title") }.ifBlank { raw.toString() }
                null, JSONObject.NULL -> ""
                else -> raw.toString()
            }
            val orders = when (val raw = value.opt("order_nos").takeUnless { it == null || it == JSONObject.NULL }
                ?: payload.opt("order_nos")) {
                is JSONArray -> stringList(raw)
                is String -> raw.split(',', '、').map(String::trim).filter(String::isNotBlank)
                else -> emptyList()
            }.ifEmpty { text("order_no").takeIf(String::isNotBlank)?.let { listOf(it) }.orEmpty() }
            val priority = when (val raw = value.opt("priority")) {
                is Number -> raw.toInt()
                is String -> raw.toIntOrNull() ?: when (raw.lowercase()) { "urgent" -> 0; "overdue" -> 3; "high" -> 10; else -> 50 }
                else -> 0
            }
            val missing = payload.optJSONArray("missing_items") ?: value.optJSONArray("missing_items") ?: JSONArray()
            return DailyWorkItem(value.optString("id"), value.optInt("version"), text("role"), text("title"),
                text("reason"), text("next_action"), text("status").ifBlank { "todo" }, priority,
                text("due_date"), text("follow_up_date").ifBlank { text("next_follow_up_date") },
                assignee?.optString("id").orEmpty().ifBlank { text("assignee_id") },
                assignee?.optString("display_name").orEmpty().ifBlank { text("assignee_name") },
                orders, text("sku"), text("customer"), text("quantity").ifBlank {
                    payload.optString("quantity").ifBlank { payload.optString("shortage") }
                }, text("location"), text("logistics_company").ifBlank { text("source_category") },
                text("print_state"), text("packing_state"), blocked, value.optBoolean("urgent"))
                .copy(humanCompletedAt = displayTime(text("human_completed_at")),
                    businessCompletedAt = displayTime(text("business_completed_at")), profileId = text("profile_id"),
                    readOnly = if (value.has("read_only")) value.optBoolean("read_only") else payload.optBoolean("read_only"),
                    taskType = text("task_type").ifBlank { payload.optString("task_type") },
                    missingItems = (0 until missing.length()).mapNotNull { index -> missing.optJSONObject(index)?.let { row ->
                        DailyMissingItem(row.optString("sku"), row.optString("product_id"), row.optString("shortage"),
                            row.optString("pending"))
                    } },
                    linkedPurchaseBusinessKey = text("linked_purchase_business_key").ifBlank {
                        payload.optString("linked_purchase_business_key")
                    }, customerOrderImpact = value.optInt("customer_order_impact"),
                    nearCompleteCustomerOrders = value.optInt("near_complete_customer_orders"))
        }

        private fun staffPayload(draft: StaffDraft, includeIdentity: Boolean) = JSONObject().apply {
            if (includeIdentity) {
                put("username", draft.username.trim())
                put("password", draft.password)
            }
            put("display_name", draft.displayName.trim()).put("roles", JSONArray(draft.roles.sorted()))
                .put("active", draft.active).put("packing_limit", draft.packingLimit.coerceAtLeast(0))
                .put("is_admin", draft.isAdmin)
        }

        private fun stringList(value: JSONArray?): List<String> = if (value == null) emptyList() else
            (0 until value.length()).mapNotNull { value.optString(it).trim().takeIf(String::isNotBlank) }

        fun parseTask(value: JSONObject): UpdateTask {
            val result = value.optJSONObject("result") ?: JSONObject()
            val counts = result.optJSONObject("counts") ?: JSONObject()
            val progress = value.optJSONObject("progress") ?: result.optJSONObject("progress") ?: JSONObject()
            val status = value.optString("status")
            val published = result.optBoolean("published")
            val fallback = when (status) {
                "queued" -> "等待更新"
                "running" -> "正在更新，离开页面后仍会继续"
                "succeeded" -> if (value.optString("mode") == "preview") "计算完成，尚未写入物流信息" else "更新完成"
                "partial" -> "部分物流未更新，请查看结果"
                "unknown" -> "部分物流结果待核对，不会自动重复提交"
                else -> if (published) "查询数据已更新，物流更新未完成" else "更新未完成，保留上一份可用数据"
            }
            val attempts = result.optJSONArray("attempts") ?: JSONArray()
            val blocked = result.optJSONArray("blocked_orders") ?: JSONArray()
            return UpdateTask(value.getString("id"), status, result.optString("message").ifBlank { fallback },
                stage = value.optString("stage"), source = result.optString("source"),
                completed = progress.optInt("completed"), total = progress.optInt("total"),
                published = published, dataVersion = result.optString("data_version").ifBlank { value.optString("data_version") },
                publishedAt = displayTime(result.optString("published_at").ifBlank { value.optString("published_at") }),
                orders = counts.optInt("orders"), inventory = counts.optInt("inventory"),
                success = result.optInt("success", result.optInt("succeeded")), failed = result.optInt("failed"),
                unknown = result.optInt("unknown"), skipped = result.optInt("skipped", result.optInt("unchanged")),
                attempts = (0 until attempts.length()).mapNotNull { i -> attempts.optJSONObject(i)?.let {
                    FreightAttempt(it.optString("id"), it.optString("tradeid"), it.optString("status"),
                        it.optString("freight_company"), it.optString("freight_number"), it.optString("message"))
                } }, queryStatus = result.optString("query_status"), logisticsStatus = result.optString("logistics_status"),
                blockedOrders = (0 until blocked.length()).mapNotNull { i -> blocked.optJSONObject(i)?.let {
                    listOf(it.optString("order_no"), it.optString("reason")).filter(String::isNotBlank).joinToString(" · ")
                } })
        }

        fun printQuery(state: WorkbenchState): JSONObject = JSONObject().put("q", state.query).apply {
            if (state.destination == Destination.Inventory && state.queryMode == "sku_exact") put("q", "")
            if (state.destination == Destination.Inventory) state.inventoryFilters.parameters().forEach { (key, value) -> put(key, value) }
            else {
                put("count_basis", "lines")
                put("mode", if (state.queryMode == "note") "summary" else state.queryMode).put("scope", state.scope).put("logistics_company", state.logistics)
                state.orderFilters.parameters().forEach { (key, value) -> put(key, value) }
            }
        }

        fun parseItem(item: JSONObject, orders: Boolean): WorkItem {
            val inv = if (orders) item.optJSONObject("inventory") else item
            fun optionalString(key: String): String = item.optString(key).trim()
                .takeUnless { it.equals("null", ignoreCase = true) }.orEmpty()
            fun modelOf(source: JSONObject?): String = source?.optString("model")?.trim()?.takeUnless { it == "null" }.orEmpty()
            val model = modelOf(item).ifBlank { if (orders && inv?.optBoolean("found") == true) modelOf(inv) else "" }
            val logistics = optionalString("logistics_company").ifBlank { optionalString("freightname") }
            val lifecycle = optionalString("lifecycle_state")
            val process = optionalString("process_state")
            val lifecyclePendingReview = item.optBoolean("lifecycle_pending_review")
            val requiresSettlement = !lifecyclePendingReview &&
                (lifecycle == SHIPPED_UNPOSTED || process == "500" || logistics == SETTLEMENT_LOGISTICS)
            return WorkItem(id = item.getString("id"), sku = item.optString("sku"), name = item.optString("product_name"),
                spec = if (orders) item.optString("specification") else listOf(item.optString("standard_no"), item.optString("spec"), item.optString("model"), item.optString("material")).filter(String::isNotBlank).joinToString("  "),
                orderNo = item.optString("order_no"), buyer = item.optString("buyer"), quantity = item.optString("quantity"),
                logistics = logistics, location = item.optString("location"),
                available = quantity(inv, "available_stock", orders), stock = quantity(inv, "stock_quantity", orders),
                note = item.optString("system_note"), model = model,
                warehouseId = item.optString("warehouse_id"), warehouseName = item.optString("warehouse_name"),
                productId = item.optString("product_id"), unitId = item.optString("base_unit"),
                profileId = item.optString("profile_id"), skuId = item.optString("sku_id"),
                calculationStatus = item.optString("calculation_status"),
                printable = item.optBoolean("printable", true) && !requiresSettlement && !lifecyclePendingReview,
                printBlockReason = optionalString("print_block_reason").ifBlank {
                    when {
                        lifecyclePendingReview -> "订单状态待核对，暂停包装打印"
                        requiresSettlement -> "订单已出库，等待ERP结存"
                        else -> ""
                    }
                }, material = item.optString("material"),
                unitName = item.optString("unit_name").ifBlank { item.optString("base_unit_name") },
                orderDate = item.optString("order_date"), deliveryDate = item.optString("delivery_date"),
                printState = item.optString("print_state", "unprinted"), printStateUpdatedAt = displayTime(item.optString("print_state_updated_at")),
                printContentChanged = item.optBoolean("print_content_changed"),
                lifecycleState = lifecycle, processState = process,
                lifecyclePendingReview = lifecyclePendingReview,
                calculationIssues = (item.optJSONArray("calculation_issues") ?: item.optJSONArray("issues"))?.let { issues ->
                    (0 until issues.length()).mapNotNull { i -> when (val issue = issues.opt(i)) {
                        is JSONObject -> issue.optString("message").ifBlank { issue.optString("reason") }.ifBlank { issue.optString("code") }
                        is String -> issue
                        else -> null
                    } }.filter(String::isNotBlank)
                }.orEmpty(), printWarnings = stringList(item.optJSONArray("print_warnings")),
                summaryPerson = optionalString("summary_person"))
        }

        fun displayTime(raw: String): String = runCatching {
            java.time.OffsetDateTime.parse(raw).atZoneSameInstant(java.time.ZoneId.systemDefault())
                .format(java.time.format.DateTimeFormatter.ofPattern("MM-dd HH:mm"))
        }.getOrDefault(raw)
        fun quantity(data: JSONObject?, field: String, requireFound: Boolean = false): String? {
            if (data == null || (requireFound && !data.optBoolean("found"))) return null
            val raw = if (data.isNull(field)) null else data.optString(field).trim().takeIf { it.isNotEmpty() && it != "null" }
            return raw?.let { runCatching { java.math.BigDecimal(it).stripTrailingZeros().toPlainString() }.getOrDefault(it) }
        }
        private fun encodePath(value: String) = URLEncoder.encode(value, "UTF-8").replace("+", "%20")
    }
}
