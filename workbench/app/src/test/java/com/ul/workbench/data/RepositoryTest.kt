package com.ul.workbench.data

import org.json.JSONObject
import kotlinx.coroutines.runBlocking
import java.net.InetAddress
import java.net.ServerSocket
import java.net.URLDecoder
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class RepositoryTest {
    @Test fun summaryWarningsAndPersonnelDoNotReplaceActualLineIdentity() {
        val row = JSONObject("""{"id":"row1","sku":"LM2562","quantity":48,"product_name":"螺母",
            "summary_person":"刘兆杰","printable":true,"print_warnings":["交期待确认"]}""")
        val item = HttpWorkbenchRepository.parseItem(row, true)
        assertEquals("LM2562", item.sku); assertEquals("48", item.quantity)
        assertEquals("刘兆杰", item.summaryPerson)
        assertEquals(listOf("交期待确认"), item.printWarnings); assertTrue(item.printable)
    }

    @Test fun originalAttachmentDownloadUsesScopedApiAndExactBinaryBytes(): Unit = runBlocking {
        val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        val executor = Executors.newSingleThreadExecutor()
        val original = byteArrayOf(0, 1, 2, 13, 10, -1)
        try {
            val received = executor.submit<String> {
                server.accept().use { socket ->
                    val header = java.io.ByteArrayOutputStream(); val input = socket.getInputStream()
                    while (true) {
                        val byte = input.read(); check(byte >= 0); header.write(byte)
                        if (header.toString("UTF-8").endsWith("\r\n\r\n")) break
                    }
                    socket.getOutputStream().apply {
                        write("HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\nContent-Length: ${original.size}\r\nConnection: close\r\n\r\n".toByteArray())
                        write(original); flush()
                    }
                    header.toString("UTF-8")
                }
            }
            val file = HttpWorkbenchRepository { "http://127.0.0.1:${server.localPort}" }.downloadSalesAttachment("session-1", "file-1")
            assertEquals("application/pdf", file.mimeType); assertArrayEquals(original, file.bytes)
            val header = received.get(5, TimeUnit.SECONDS)
            assertTrue(header.startsWith("GET /api/v2/sales-assistant/sessions/session-1/attachments/file-1 "))
            assertFalse(header.substringBefore("\r\n").contains("?"))
        } finally { server.close(); executor.shutdownNow() }
    }

    @Test fun originalAttachmentNeverFollowsRedirectAndRejectsChangedOriginBeforeIo(): Unit = runBlocking {
        var reads = 0
        val changing = HttpWorkbenchRepository { if (reads++ == 0) "http://127.0.0.1:1" else "http://127.0.0.1:2" }
        try { changing.downloadSalesAttachment("s1", "f1"); fail("Changed origin must abort original download") }
        catch (expected: IllegalStateException) { assertTrue(expected.message.orEmpty().contains("服务已变化")) }
        val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        val executor = Executors.newSingleThreadExecutor()
        try {
            val reply = executor.submit<Unit> {
                server.accept().use { socket ->
                    val input = socket.getInputStream(); val header = java.io.ByteArrayOutputStream()
                    while (true) {
                        val byte = input.read(); check(byte >= 0); header.write(byte)
                        if (header.toString("UTF-8").endsWith("\r\n\r\n")) break
                    }
                    socket.getOutputStream().apply {
                        write("HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/external-file\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".toByteArray()); flush()
                    }
                }
            }
            try { HttpWorkbenchRepository { "http://127.0.0.1:${server.localPort}" }.downloadSalesAttachment("s1", "f1"); fail("Redirect must not be followed") }
            catch (expected: ServiceException) { assertEquals(302, expected.status) }
            reply.get(5, TimeUnit.SECONDS)
            Unit
        } finally { server.close(); executor.shutdownNow() }
    }

    @Test fun attachmentMultipartCarriesFrozenVersionKeyAndExactBytesWithoutHeaderInjection() = runBlocking {
        val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        val executor = Executors.newSingleThreadExecutor()
        try {
            val received = executor.submit<Pair<String, ByteArray>> {
                server.accept().use { socket ->
                    val input = socket.getInputStream()
                    val header = java.io.ByteArrayOutputStream()
                    while (true) {
                        val byte = input.read(); check(byte >= 0); header.write(byte)
                        val bytes = header.toByteArray()
                        if (bytes.size >= 4 && bytes.takeLast(4) == listOf(13.toByte(), 10.toByte(), 13.toByte(), 10.toByte())) break
                    }
                    val headers = header.toString("UTF-8")
                    val length = headers.lineSequence().first { it.startsWith("Content-Length:", true) }.substringAfter(':').trim().toInt()
                    val body = ByteArray(length); var offset = 0
                    while (offset < length) { val count = input.read(body, offset, length - offset); check(count > 0); offset += count }
                    val response = """{"ok":true,"session":{"id":"session-1","version":8}}""".toByteArray()
                    socket.getOutputStream().apply {
                        write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${response.size}\r\nConnection: close\r\n\r\n".toByteArray())
                        write(response); flush()
                    }
                    headers to body
                }
            }
            val payload = byteArrayOf(0, 1, 2, 13, 10, -1)
            val result = HttpWorkbenchRepository { "http://127.0.0.1:${server.localPort}" }.uploadSalesAttachment(
                "session-1", 7, "same-action-key", "订单\r\n\".pdf", "application/pdf", payload)
            assertEquals(8, result.getJSONObject("session").getInt("version"))
            val (header, body) = received.get(5, TimeUnit.SECONDS)
            assertTrue(header.startsWith("POST /api/v2/sales-assistant/sessions/session-1/attachments "))
            assertTrue(header.contains("multipart/form-data; boundary="))
            val text = body.toString(Charsets.UTF_8)
            assertTrue(text.contains("name=\"version\"\r\n\r\n7\r\n"))
            assertTrue(text.contains("name=\"idempotency_key\"\r\n\r\nsame-action-key\r\n"))
            assertTrue(text.contains("filename=\"订单___.pdf\""))
            val marker = "Content-Type: application/pdf\r\n\r\n".toByteArray()
            val markerIndex = (0..body.size - marker.size).first { index -> body.copyOfRange(index, index + marker.size).contentEquals(marker) }
            val payloadStart = markerIndex + marker.size
            assertArrayEquals(payload, body.copyOfRange(payloadStart, payloadStart + payload.size))
        } finally { server.close(); executor.shutdownNow() }
    }

    @Test fun helperRequestCannotLeaveAssistantApiScope() = runBlocking {
        val repository = HttpWorkbenchRepository { "http://127.0.0.1:1" }
        for (path in listOf("/orders", "/assistant/../../orders", "/sales-assistant/../orders")) {
            try { repository.assistantRequest(path); fail("API scope must be checked before networking") }
            catch (expected: IllegalArgumentException) { assertEquals("助手请求范围无效", expected.message) }
        }
    }

    @Test fun assistantDispatchFreezesOriginBeforeIoAndRejectsAChangedServer() = runBlocking {
        var reads = 0
        val repository = HttpWorkbenchRepository { if (reads++ == 0) "http://127.0.0.1:1" else "http://127.0.0.1:2" }
        try { repository.assistantRequest("/assistant/purchase/plan"); fail("Changed origin must abort before opening HTTP") }
        catch (expected: IllegalStateException) { assertTrue(expected.message.orEmpty().contains("服务已变化")) }
        reads = 0
        try { repository.uploadSalesAttachment("session-1", 1, "frozen-key", "订单.pdf", "application/pdf", byteArrayOf(1)); fail("Changed origin must abort upload") }
        catch (expected: IllegalStateException) { assertTrue(expected.message.orEmpty().contains("服务已变化")) }
    }

    @Test fun boardAndDetailModelDecodingRunsAwayFromTheCallingUiThread() = runBlocking {
        val caller = Thread.currentThread()
        var boardThread: Thread? = null
        val boardJson = object : JSONObject("""{"items":[],"print_state_version":"print-1"}""") {
            override fun optJSONArray(name: String?): org.json.JSONArray? {
                boardThread = Thread.currentThread()
                return super.optJSONArray(name)
            }
        }
        val board = HttpWorkbenchRepository.decodeTaskBoard(boardJson)
        assertEquals("print-1", board.printStateVersion)
        assertNotNull(boardThread)
        assertNotEquals(caller, boardThread)
        var detailThread: Thread? = null
        val detailJson = object : JSONObject("""{"item":{"id":"group-1"},"lines":[]}""") {
            override fun getJSONObject(name: String): JSONObject {
                detailThread = Thread.currentThread()
                return super.getJSONObject(name)
            }
        }
        assertEquals("group-1", HttpWorkbenchRepository.decodeTaskBoardDetail(detailJson).item.id)
        assertNotNull(detailThread)
        assertNotEquals(caller, detailThread)
    }
    @Test fun customerOwnershipParsesServerScopeReviewAndPermissions() {
        val owner = HttpWorkbenchRepository.parseCustomerOwner(JSONObject("""{
          "profile_id":"P1","customer_name":"客户甲","owner_id":"sales-1","owner_name":"销售一",
          "deputy_id":"sales-2","deputy_name":"销售二","deputy_until":"2026-10-20",
          "status":"review","source":"existing_assignment","version":7,
          "review_reasons":["原负责人不一致"],"active_task_count":74,"active_followup_count":6,
          "can_set_owner":true,"can_set_deputy":false
        }"""))
        assertEquals("P1", owner.profileId)
        assertEquals("review", owner.status)
        assertEquals(7, owner.version)
        assertEquals("销售二", owner.deputyName)
        assertEquals(listOf("原负责人不一致"), owner.reviewReasons)
        assertTrue(owner.canSetOwner)
        assertFalse(owner.canSetDeputy)
        val legacy = HttpWorkbenchRepository.parseCustomerOwner(JSONObject("""{"profile_id":"P1","customer_name":"客户甲"}"""))
        assertFalse(legacy.canSetOwner)
    }

    @Test fun taskExecutorAndFixedCustomerSalesRemainDifferentFields() {
        val item = HttpWorkbenchRepository.parseTaskBoardItem(JSONObject("""{
          "id":"pack-1","role":"stock_packing","assignee_id":"packer","assignee_name":"包装员",
          "customer_sales_owners":[{"customer":"客户甲","owner_id":"sales-1","owner_name":"销售一","review_required":false}]
        }"""))
        assertEquals("包装员", item.assigneeName)
        assertEquals("销售一", item.customerSalesOwners.single().ownerName)
    }
    @Test fun selectedOrderPrintUsesDistinctWholeOrderNumbersAndStableDeviceId() = runBlocking {
        val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        val executor = Executors.newSingleThreadExecutor()
        try {
            val received = executor.submit<String> {
                server.accept().use { socket ->
                    val reader = socket.getInputStream().bufferedReader()
                    reader.readLine()
                    var length = 0
                    while (true) {
                        val line = reader.readLine()
                        if (line.isNullOrEmpty()) break
                        if (line.startsWith("Content-Length:", true)) length = line.substringAfter(':').trim().toInt()
                    }
                    val chars = CharArray(length)
                    var read = 0
                    while (read < length) read += reader.read(chars, read, length - read)
                    val response = """{"ok":true,"job_id":"job","labels":2}""".toByteArray()
                    socket.getOutputStream().apply {
                        write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${response.size}\r\nConnection: close\r\n\r\n".toByteArray())
                        write(response); flush()
                    }
                    String(chars)
                }
            }
            val state = WorkbenchState(destination = Destination.Orders, dataVersion = "v1", deviceId = "device-fixed",
                items = listOf(WorkItem("line-1", "DQ0517", "图纸", orderNo = "AD001#1"),
                    WorkItem("line-2", "DQ0518", "图纸2", orderNo = "AD001#1")), selectedIds = setOf("line-1"))
            HttpWorkbenchRepository { "http://127.0.0.1:${server.localPort}" }.prepare(state, false, null)
            val json = JSONObject(received.get(5, TimeUnit.SECONDS))
            assertEquals("device-fixed", json.getString("device_id"))
            assertEquals(1, json.getJSONArray("order_nos").length())
            assertEquals("AD001#1", json.getJSONArray("order_nos").getString(0))
            assertFalse(json.has("item_ids"))
        } finally { server.close(); executor.shutdownNow() }
    }

    @Test fun orderFiltersKeepAllStateAndDateSortWithAnEmptyKeyword() = runBlocking {
        val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        val executor = Executors.newSingleThreadExecutor()
        try {
            val received = executor.submit<String> {
                server.accept().use { socket ->
                    val reader = socket.getInputStream().bufferedReader()
                    val requestLine = reader.readLine()
                    while (!reader.readLine().isNullOrEmpty()) { }
                    val body = """{"ok":true,"items":[],"total":0,"data_version":"v1","customer_options":["客户甲","客户乙"],"print_state_counts":{"all":8,"unprinted":3,"reserved":1,"printed":3,"uncertain":1}}""".toByteArray()
                    socket.getOutputStream().apply {
                        write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray())
                        write(body); flush()
                    }
                    requestLine
                }
            }
            val result = HttpWorkbenchRepository { "http://127.0.0.1:${server.localPort}" }.queryOrders(
                "", "summary", "stock", "DQ库存件", 1,
                OrderFilters("all", "2026-09-01", "2026-09-30", "2026-10-01", "2026-10-31", "location", "客户甲"))
            val params = received.get(5, TimeUnit.SECONDS).substringAfter('?').substringBefore(' ').split('&').associate {
                it.substringBefore('=') to URLDecoder.decode(it.substringAfter('='), "UTF-8")
            }
            assertEquals("", params["q"])
            assertEquals("summary", params["mode"])
            assertEquals("all", params["print_state"])
            assertEquals("2026-09-01", params["order_date_from"])
            assertEquals("2026-10-31", params["delivery_date_to"])
            assertEquals("location", params["sort"])
            assertEquals("客户甲", params["customer"])
            assertEquals(3, result.printStateCounts.unprinted)
            assertEquals(1, result.printStateCounts.uncertain)
            assertEquals(listOf("客户甲", "客户乙"), result.customerOptions)
        } finally { server.close(); executor.shutdownNow() }
    }

    @Test fun printAllOrderSnapshotContainsEveryStructuredFilter() {
        val query = HttpWorkbenchRepository.printQuery(WorkbenchState(destination = Destination.Orders,
            queryMode = "summary", scope = "stock", logistics = "DQ库存件",
            orderFilters = OrderFilters("all", "2026-09-01", "2026-09-30", "2026-10-01", "2026-10-31", "delivery_date", "客户甲")))
        assertEquals("summary", query.getString("mode"))
        assertEquals("all", query.getString("print_state"))
        assertEquals("2026-09-01", query.getString("order_date_from"))
        assertEquals("2026-09-30", query.getString("order_date_to"))
        assertEquals("2026-10-01", query.getString("delivery_date_from"))
        assertEquals("2026-10-31", query.getString("delivery_date_to"))
        assertEquals("delivery_date", query.getString("sort"))
        assertEquals("客户甲", query.getString("customer"))
        assertEquals("DQ库存件", query.getString("logistics_company"))
    }

    @Test fun inventoryRequestUsesSeparateEncodedConditions() = runBlocking {
        val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        val executor = Executors.newSingleThreadExecutor()
        try {
            val received = executor.submit<String> {
                server.accept().use { socket ->
                    val reader = socket.getInputStream().bufferedReader()
                    val requestLine = reader.readLine()
                    while (!reader.readLine().isNullOrEmpty()) { }
                    val body = """{"ok":true,"items":[],"total":0,"data_version":"v1"}""".toByteArray()
                    socket.getOutputStream().apply {
                        write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray())
                        write(body); flush()
                    }
                    requestLine
                }
            }
            HttpWorkbenchRepository { "http://127.0.0.1:${server.localPort}" }.query(
                Destination.Inventory, "A&1", "sku", "stock", "LS库存件", 2,
                InventoryFilters("六角", "M12 × 50", "8.8", "钢"), expectedDataVersion = "snapshot-1")
            val request = received.get(5, TimeUnit.SECONDS)
            val params = request.substringAfter('?').substringBefore(' ').split('&').associate {
                it.substringBefore('=') to URLDecoder.decode(it.substringAfter('='), "UTF-8")
            }
            assertEquals("A&1", params["q"])
            assertEquals("六角", params["name"])
            assertEquals("M12 × 50", params["spec"])
            assertEquals("8.8", params["model"])
            assertEquals("钢", params["material"])
            assertEquals("2", params["page"])
            assertEquals("snapshot-1", params["expected_data_version"])
            assertFalse(params.containsKey("logistics_company"))
        } finally { server.close(); executor.shutdownNow() }
    }

    @Test fun printAllRetainsTheFullInventoryFilterInsteadOfWideningTheQuery() {
        val query = HttpWorkbenchRepository.printQuery(WorkbenchState(destination = Destination.Inventory,
            inventoryFilters = InventoryFilters(name = "六角", spec = "M12", model = "8.8")))
        assertEquals("", query.getString("q"))
        assertEquals("六角", query.getString("name"))
        assertEquals("M12", query.getString("spec"))
        assertEquals("8.8", query.getString("model"))
        assertFalse(query.has("material"))
        assertFalse(query.has("scope"))
    }

    @Test fun orderModelUsesItsOwnSourceBeforeInventoryAndNeverReplacesItWithNoteGrade() {
        val row = JSONObject("""{"id":"one","model":"订单型号","system_note":"客户等级","inventory":{"found":true,"model":"库存型号"}}""")
        assertEquals("订单型号", HttpWorkbenchRepository.parseItem(row, true).model)
        row.remove("model")
        assertEquals("库存型号", HttpWorkbenchRepository.parseItem(row, true).model)
        row.getJSONObject("inventory").put("found", false)
        assertEquals("", HttpWorkbenchRepository.parseItem(row, true).model)
    }

    @Test fun absentInventoryDoesNotBecomeZero() {
        assertNull(HttpWorkbenchRepository.quantity(JSONObject("""{"found":false,"stock_quantity":"0"}"""), "stock_quantity", true))
        assertNull(HttpWorkbenchRepository.quantity(JSONObject("""{"found":true,"stock_quantity":null}"""), "stock_quantity", true))
        assertNull(HttpWorkbenchRepository.quantity(JSONObject("""{"found":true,"stock_quantity":""}"""), "stock_quantity", true))
        assertEquals("0", HttpWorkbenchRepository.quantity(JSONObject("""{"found":true,"stock_quantity":"0"}"""), "stock_quantity", true))
        assertEquals("125", HttpWorkbenchRepository.quantity(JSONObject("""{"found":true,"stock_quantity":"125"}"""), "stock_quantity", true))
    }
    @Test fun publishedQueriesAndUnknownLogisticsAreParsedAsSeparateResults() {
        val task = HttpWorkbenchRepository.parseTask(JSONObject("""{
            "id":"job-1","status":"unknown","stage":"writing","progress":{"completed":2,"total":3},
            "result":{"published":true,"data_version":"v2","counts":{"orders":15,"inventory":20},
                "succeeded":1,"failed":0,"unknown":1,"unchanged":12,"logistics_status":"partial",
                "attempts":[{"id":"attempt-1","tradeid":"ORDER#1","status":"unknown","freight_company":"待到货","freight_number":"@09/26#ORDER#1","message":"连接中断"}],
                "blocked_orders":[{"order_no":"ORDER#2","reason":"缺交货日期"}]}}
        """))
        assertTrue(task.published)
        assertEquals("v2", task.dataVersion)
        assertEquals(15, task.orders)
        assertEquals(1, task.success)
        assertEquals(1, task.unknown)
        assertEquals(12, task.skipped)
        assertEquals(2, task.completed)
        assertEquals("@09/26#ORDER#1", task.attempts.single().number)
        assertEquals("ORDER#2 · 缺交货日期", task.blockedOrders.single())
        assertFalse(task.active)
    }

    @Test fun orderIdentityAndPrintingSafetyAreIndependentOfClassification() {
        val item = HttpWorkbenchRepository.parseItem(JSONObject("""{
            "id":"one","warehouse_id":"w1","warehouse_name":"一仓","product_id":"p1","sku_id":"s1",
            "profile_id":"account","base_unit":"件","calculation_status":"blocked","printable":true,
            "calculation_issues":[{"code":"missing_due_date","reason":"缺交货日期"}]}
        """), true)
        assertEquals("w1", item.warehouseId)
        assertEquals("一仓", item.warehouseName)
        assertEquals("件", item.unitId)
        assertEquals(listOf("缺交货日期"), item.calculationIssues)
        assertTrue(item.printable)
        assertTrue(HttpWorkbenchRepository.parseItem(JSONObject("""{"id":"legacy"}"""), true).printable)
    }
    @Test fun shippedUnpostedOrderUsesFreightFallbackAndIsNeverPrintable() {
        val item = HttpWorkbenchRepository.parseItem(JSONObject("""{
            "id":"settlement","order_no":"QJ0805#71","freightname":"已完成订单需结存",
            "lifecycle_state":"shipped_unposted","process_state":500,"printable":true
        }"""), true)
        assertEquals("已完成订单需结存", item.logistics)
        assertEquals("shipped_unposted", item.lifecycleState)
        assertEquals("500", item.processState)
        assertTrue(item.requiresSettlement)
        assertFalse(item.printable)
        assertEquals("订单已出库，等待ERP结存", item.printBlockReason)
    }
    @Test fun lifecycleFieldsRemainCompatibleWithOldAndExplicitlyEmptyResponses() {
        val old = HttpWorkbenchRepository.parseItem(JSONObject("""{
            "id":"old","logistics_company":"已完成订单需结存"
        }"""), true)
        assertTrue(old.requiresSettlement)
        assertFalse(old.printable)

        val active = HttpWorkbenchRepository.parseItem(JSONObject().put("id", "active")
            .put("lifecycle_state", JSONObject.NULL).put("process_state", "")
            .put("logistics_company", JSONObject.NULL).put("freightname", JSONObject.NULL)
            .put("printable", true), true)
        assertEquals("", active.lifecycleState)
        assertEquals("", active.processState)
        assertEquals("", active.logistics)
        assertFalse(active.requiresSettlement)
        assertTrue(active.printable)
    }
    @Test fun lifecyclePendingReviewIsExplicitAndNeverPrintable() {
        val pending = HttpWorkbenchRepository.parseItem(JSONObject("""{
            "id":"pending","order_no":"QJ0805#71","freightname":"已完成订单需结存",
            "lifecycle_state":"shipped_unposted","process_state":500,
            "lifecycle_pending_review":true,"printable":true
        }"""), true)
        assertTrue(pending.lifecyclePendingReview)
        assertFalse(pending.requiresSettlement)
        assertFalse(pending.printable)
        assertFalse(pending.isPrintEligible(Destination.Orders))
        assertEquals("订单状态待核对，暂停包装打印", pending.printBlockReason)
    }
    @Test fun serverAddressesAreExplicitAndDoNotAcceptCredentialsOrPaths() {
        assertEquals("http://192.168.1.5:5191", ServerAddress.normalize(" http://192.168.1.5:5191/ "))
        assertEquals("https://example.org", ServerAddress.normalize("https://example.org"))
        for (invalid in listOf("javascript:alert(1)", "http://user:pass@192.168.1.5:5191", "http://example.org", "http://192.168.1.5:5191/api", "http://192.168.1.5:99999")) {
            assertTrue(invalid, runCatching { ServerAddress.normalize(invalid) }.isFailure)
        }
    }
}
