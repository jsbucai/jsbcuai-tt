package com.ul.workbench

import android.app.Application
import androidx.lifecycle.ViewModelStore
import androidx.test.core.app.ApplicationProvider
import com.ul.workbench.data.*
import com.ul.workbench.device.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.test.*
import org.json.JSONObject
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.net.ConnectException

@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
class StockPackingFeatureTest {
    private lateinit var app: Application
    private lateinit var scheduler: TestCoroutineScheduler
    private val stores = mutableListOf<ViewModelStore>()
    @Before fun setup() {
        scheduler = TestCoroutineScheduler(); Dispatchers.setMain(StandardTestDispatcher(scheduler))
        app = ApplicationProvider.getApplicationContext()
        app.getSharedPreferences("workbench-settings-v1", 0).edit().clear().commit()
    }
    @After fun teardown() { stores.forEach(ViewModelStore::clear); scheduler.runCurrent(); Dispatchers.resetMain() }
    private fun model(repo: StockRepository, printer: StockPrinter = StockPrinter()) = WorkbenchViewModel(app, repo, printer).also {
        stores += ViewModelStore().also { store -> store.put("stock", it) }
    }

    @Test fun arrivalUsesExplicitPagesFrozenScopesAndIndependentFilters() = runTest(StandardTestDispatcher(scheduler)) {
        val repo=StockRepository(role=ROLE_ARRIVAL_PACKING);val model=model(repo);advanceUntilIdle()
        assertEquals("todo",repo.pages.last()["work_state"])
        model.loadMoreTaskBoard();advanceUntilIdle();assertEquals(1,repo.pages.size)
        val filter=StockPackingFilters(pageSize=20,customerKey="customer-id",orderNo="0804",supplier="supplier-id",receiptFrom="2026-10-01",sort="receipt_date")
        model.setStockPackingFilters(filter,"mine","","","");advanceUntilIdle()
        model.toggleTaskBoardItem("card-0");advanceUntilIdle()
        model.changeStockPackingPage(2);advanceUntilIdle()
        model.toggleTaskBoardItem("card-20");advanceUntilIdle()
        assertEquals(listOf("ORDER-0","ORDER-20"),model.state.value.dailyWork.boardSelection!!.orderNos)
        assertEquals("supplier-id",repo.pages.last()["supplier"])
        assertEquals("customer-id",repo.pages.last()["customer_key"])
        assertEquals("contains",repo.pages.last()["order_no_match"])
        assertEquals("2026-10-01",repo.pages.last()["receipt_from"])
        model.clearTaskBoardSelection();model.selectTaskBoardSection("exception");advanceUntilIdle()
        assertEquals("exception",repo.pages.last()["work_state"])
        assertEquals(20,model.state.value.dailyWork.board.items.size)
        stores.last().clear();val restored=model(StockRepository(role=ROLE_ARRIVAL_PACKING));advanceUntilIdle()
        assertEquals(filter,restored.state.value.dailyWork.stockFilters)
        stores.last().clear();val stock=model(StockRepository());advanceUntilIdle()
        assertEquals(StockPackingFilters(),stock.state.value.dailyWork.stockFilters)
    }

    @Test fun cachedPackingPageIsVisibleWhileVersionRequestIsStillWaiting() = runTest(StandardTestDispatcher(scheduler)) {
        val repo=StockRepository(role=ROLE_ARRIVAL_PACKING);val model=model(repo);advanceUntilIdle()
        model.changeStockPackingPage(2);advanceUntilIdle()
        val calls=repo.pages.size
        model.navigate(Destination.Inventory);advanceUntilIdle()
        val gate=CompletableDeferred<Unit>();repo.versionGate=gate
        model.navigate(Destination.Tasks);runCurrent()
        assertEquals("card-50",model.state.value.dailyWork.board.items.first().id)
        assertFalse(model.state.value.dailyWork.loading)
        assertFalse(model.state.value.dailyWork.boardVerified)
        assertEquals(calls,repo.pages.size)
        gate.complete(Unit);advanceUntilIdle()
        assertTrue(model.state.value.dailyWork.boardVerified)
        assertEquals(calls,repo.pages.size)
    }

    @Test fun stockPagesReplaceRowsKeepCrossPageSelectionAndIgnoreInfiniteLoading() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(); val model = model(repo); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle(); assertEquals(1, repo.pages.size)
        model.toggleTaskBoardItem("card-0"); advanceUntilIdle()
        model.changeStockPackingPage(2); advanceUntilIdle()
        assertEquals(50, model.state.value.dailyWork.board.items.size)
        assertEquals("card-50", model.state.value.dailyWork.board.items.first().id)
        model.toggleTaskBoardItem("card-50"); advanceUntilIdle()
        assertEquals(setOf("card-0", "card-50"), model.state.value.dailyWork.boardSelectedIds)
        assertEquals(listOf("ORDER-0", "ORDER-50"), model.state.value.dailyWork.boardSelection!!.orderNos)
        assertEquals("print", repo.selections.last().getString("purpose"))
        model.changeStockPackingPage(99); model.changeStockPackingPage(0); advanceUntilIdle()
        assertEquals(2, model.state.value.dailyWork.stockPage)
        model.changeStockPackingPage(1); advanceUntilIdle()
        assertEquals("card-0", model.state.value.dailyWork.board.items.first().id)
        assertEquals(setOf("card-0", "card-50"), model.state.value.dailyWork.boardSelectedIds)
    }

    @Test fun selectCurrentPageAndSelectAllUseDifferentFrozenScopes() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(); val model = model(repo); advanceUntilIdle()
        model.setStockPackingFilters(StockPackingFilters(pageSize = 10), "mine", "", "", ""); advanceUntilIdle()
        model.selectStockPackingPage(); advanceUntilIdle()
        assertEquals(10, model.state.value.dailyWork.boardSelection!!.objectCount)
        assertEquals(10, repo.selections.single().getJSONArray("ids").length())
        model.clearTaskBoardSelection(); model.selectAllTaskBoard(); advanceUntilIdle()
        assertTrue(repo.selections.last().getBoolean("all"))
        assertEquals(121, model.state.value.dailyWork.boardSelection!!.orderNos.size)
        assertEquals("print", repo.pages.last()["packing_mode"])
        assertEquals("10", repo.pages.last()["page_size"])
    }

    @Test fun dragSelectionIsDebouncedAndOpeningCardCannotDiscardIt() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(); val model = model(repo); advanceUntilIdle()
        (0..25).forEach { model.toggleTaskBoardItem("card-$it"); runCurrent() }
        assertTrue(repo.selections.isEmpty())
        advanceTimeBy(201); runCurrent()
        assertEquals(1, repo.selections.size)
        assertEquals(26, repo.selections.single().getJSONArray("ids").length())
        model.openTaskBoardItem("card-30"); advanceUntilIdle()
        assertNull(model.state.value.dailyWork.boardDetail)
        assertEquals(27, model.state.value.dailyWork.boardSelectedIds.size)
    }

    @Test fun explicitFiltersPersistPerUserAndRejectInvalidPageSize() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(); val model = model(repo); advanceUntilIdle()
        val filter = StockPackingFilters("客户甲", "LS0007", "PO7", "2026-10-01", "2026-10-31", "printed", "location", 20)
        model.setStockPackingFilters(filter, "mine", "2026-10-02", "2026-11-01", ""); advanceUntilIdle()
        model.changeStockPackingPage(2); advanceUntilIdle()
        assertEquals("客户甲", repo.pages.last()["customer"])
        assertEquals("location", repo.pages.last()["sort"])
        model.setStockPackingFilters(filter.copy(pageSize = 101), "mine", "", "", ""); advanceUntilIdle()
        assertEquals(filter, model.state.value.dailyWork.stockFilters)
        stores.last().clear(); val restored = model(StockRepository()); advanceUntilIdle()
        assertEquals(filter, restored.state.value.dailyWork.stockFilters)
        assertEquals(2, restored.state.value.dailyWork.stockPage)
        stores.last().clear(); val other = model(StockRepository(user = "u2")); advanceUntilIdle()
        assertEquals(StockPackingFilters(), other.state.value.dailyWork.stockFilters)
        assertEquals(1, other.state.value.dailyWork.stockPage)
    }

    @Test fun refreshAfterTasksFinishClampsSavedPageWithoutAppendingOldRows() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(); val model = model(repo); advanceUntilIdle()
        model.changeStockPackingPage(3); advanceUntilIdle()
        repo.count = 12
        model.refreshDailyWork(); advanceUntilIdle()
        assertEquals(1, model.state.value.dailyWork.stockPage)
        assertEquals(12, model.state.value.dailyWork.board.items.size)
        assertEquals("p1", repo.pages.last()["print_state_version"])
    }

    @Test fun stalePagePreservesSelectionAndStopsPrinting() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(); val model = model(repo); advanceUntilIdle()
        model.toggleTaskBoardItem("card-0"); advanceUntilIdle(); repo.printVersion = "p2"
        model.changeStockPackingPage(2); advanceUntilIdle()
        assertEquals(setOf("card-0"), model.state.value.dailyWork.boardSelectedIds)
        assertEquals(1, model.state.value.dailyWork.stockPage)
        assertFalse(model.state.value.dailyWork.board.tasksCurrent)
        model.previewStockPackingPrint(""); advanceUntilIdle()
        assertTrue(repo.previews.isEmpty()); assertTrue(repo.prepares.isEmpty())
    }

    @Test fun oldPageResponseCannotPublishAfterChangingFilters() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(); val model = model(repo); advanceUntilIdle()
        repo.pageGate = CompletableDeferred()
        model.changeStockPackingPage(2); runCurrent()
        model.setStockPackingFilters(StockPackingFilters(customer = "新客户", pageSize = 10), "mine", "", "", ""); runCurrent()
        repo.pageGate!!.complete(Unit); advanceUntilIdle()
        assertEquals(1, model.state.value.dailyWork.stockPage)
        assertEquals(10, model.state.value.dailyWork.board.items.size)
        assertEquals("新客户", model.state.value.dailyWork.stockFilters.customer)
        assertFalse(model.state.value.dailyWork.boardPageBusy)
    }

    @Test fun previewNeverPreparesAndReprintsRequireEachExplicitConfirmation() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(count = 2).apply { printStates = listOf("printed", "uncertain") }
        val printer = StockPrinter(); val model = model(repo, printer); advanceUntilIdle()
        model.selectAllTaskBoard(); advanceUntilIdle(); model.previewStockPackingPrint(""); advanceUntilIdle()
        assertNotNull(model.state.value.dailyWork.packingPrintPreview)
        assertTrue(repo.prepares.isEmpty()); assertEquals(0, printer.sends)
        model.confirmStockPackingPrint(false, false); model.confirmStockPackingPrint(true, false); advanceUntilIdle()
        assertTrue(repo.prepares.isEmpty()); assertEquals(0, printer.sends)
        model.confirmStockPackingPrint(true, true); advanceUntilIdle()
        assertEquals(0, repo.oldPrepareCalls)
        assertEquals(1, repo.prepares.size)
        val body = repo.prepares.single()
        assertEquals("preview-1", body.getString("preview_id")); assertEquals("a".repeat(64), body.getString("preview_hash"))
        assertTrue(body.getBoolean("confirm_reprint")); assertTrue(body.getBoolean("confirm_uncertain"))
        assertFalse(body.has("order_nos")); assertFalse(body.has("expected_data_version"))
        assertEquals(1, printer.sends)
    }

    @Test fun maxQuantityChangeMustObtainANewPreviewAndFrozenRetryKeepsExactBody() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(count = 1).apply { firstPrepareFails = true }; val model = model(repo); advanceUntilIdle()
        model.selectAllTaskBoard(); advanceUntilIdle(); model.previewStockPackingPrint("10"); advanceUntilIdle()
        model.previewStockPackingPrint("5"); advanceUntilIdle()
        assertEquals(listOf("10", "5"), repo.previews.map { it.getString("max_per_package") })
        model.confirmStockPackingPrint(false, false); advanceUntilIdle()
        assertEquals(2, repo.prepares.size)
        assertEquals(repo.prepares[0].toString(), repo.prepares[1].toString())
        assertEquals("preview-2", repo.prepares[0].getString("preview_id"))
        assertEquals(0, repo.oldPrepareCalls)
    }

    @Test fun serverSegmentsStopAtUnknownWithoutPreparingLaterOrders() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(count = 250); val printer = StockPrinter(unknownAt = 2)
        val model = model(repo, printer); advanceUntilIdle()
        model.selectAllTaskBoard(); advanceUntilIdle(); model.previewStockPackingPrint(""); advanceUntilIdle()
        model.confirmStockPackingPrint(false, false); advanceUntilIdle()
        assertEquals(listOf(1, 2), repo.prepares.map { it.getInt("segment_index") })
        assertEquals(listOf("submitted", "unknown"), repo.reports.map { it.second })
        assertEquals(2, printer.sends); assertTrue(model.state.value.uncertainPrint)
        assertFalse(model.state.value.dailyWork.boardPrinting)
    }

    @Test fun malformedPreviewCannotPrepareAndNoWholeBatchCapIsImposed() {
        val big = StockRepository(count = 601).preview((0..600).map { "ORDER-$it" })
        assertTrue(big.valid())
        assertFalse(big.copy(orderCount = 600).valid())
        assertFalse(big.copy(segments = big.segments.mapIndexed { index, segment -> if (index == 0) segment.copy(labelCount = 101) else segment }).valid())
        assertFalse(big.copy(orders = big.orders.mapIndexed { index, order -> if (index == 0) order.copy(stateRevision = "") else order }).valid())
    }

    @Test fun coverageUpgradeRetainsOldClaimedTokenAndNeverSendsOrPreparesAgain() = runTest(StandardTestDispatcher(scheduler)) {
        val prefs = app.getSharedPreferences("workbench-settings-v1", 0)
        prefs.edit().putString("pending-job", "old-20-label-job").putString("pending-action", "old-cancel-key-1234567")
            .putString("pending-print-state", "claimed").putString("pending-claim-token", "old-durable-token")
            .putString("pending-print-origin", "https://ul.yingyinghome.com:8443").putString("pending-print-user", "u1").commit()
        val repo = StockRepository().apply { remoteJobStatus = "claimed" }; val printer = StockPrinter()
        val model = model(repo, printer); advanceUntilIdle()
        assertEquals(listOf("old-durable-token" to "unknown"), repo.reports)
        assertEquals("old-durable-token", prefs.getString("pending-claim-token", ""))
        assertEquals("old-20-label-job", prefs.getString("pending-job", ""))
        assertEquals(0, printer.sends); assertTrue(repo.prepares.isEmpty()); assertEquals(0, repo.oldPrepareCalls)
        model.selectAllTaskBoard(); advanceUntilIdle(); model.previewStockPackingPrint(""); advanceUntilIdle()
        assertTrue(model.state.value.dailyWork.packingPrintRecoveryRequired)
        assertTrue(repo.previews.isEmpty())
        model.acknowledgeStockPackingPrint(); advanceUntilIdle()
        assertFalse(model.state.value.uncertainPrint)
        assertEquals(0, printer.sends)
        assertNull(prefs.getString("pending-job", null))
    }

    @Test fun revokedJobClearsPendingJournalWithoutAnySendOrResultReplay() = runTest(StandardTestDispatcher(scheduler)) {
        val prefs = app.getSharedPreferences("workbench-settings-v1", 0)
        prefs.edit().putString("pending-job", "corrected-job").putString("pending-action", "old-key-1234567890123")
            .putString("pending-print-state", "claimed").putString("pending-claim-token", "revoked-token").commit()
        val repo = StockRepository().apply { remoteJobStatus = "revoked"; remoteBytes = 300 }
        val printer = StockPrinter(); val model = model(repo, printer); advanceUntilIdle()
        assertNull(prefs.getString("pending-job", null))
        assertNull(prefs.getString("pending-claim-token", null))
        assertFalse(model.state.value.uncertainPrint)
        assertEquals(0, printer.sends); assertTrue(repo.reports.isEmpty()); assertTrue(repo.prepares.isEmpty())
    }

    @Test fun cancelledResultWithoutKnownZeroBytesKeepsReviewAcrossUpgrade() = runTest(StandardTestDispatcher(scheduler)) {
        val prefs = app.getSharedPreferences("workbench-settings-v1", 0)
        prefs.edit().putString("pending-job", "cancelled-partial-job").putString("pending-action", "old-cancel-key-1234567")
            .putString("pending-print-state", "claimed").putString("pending-claim-token", "old-token").commit()
        val repo = StockRepository().apply { remoteJobStatus = "cancelled"; remoteBytes = 8 }
        val printer = StockPrinter(); val model = model(repo, printer); advanceUntilIdle()
        assertTrue(model.state.value.uncertainPrint)
        assertEquals("review", prefs.getString("pending-print-state", ""))
        assertEquals("cancelled-partial-job", prefs.getString("pending-job", ""))
        assertTrue(repo.reports.isEmpty()); assertEquals(0, printer.sends)
    }

    @Test fun rejectedResetRetainsReasonButNextExplicitConfirmationUsesNewRevision() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(count = 1).apply { resetException = ServiceException("order_print_conflict", "已被另一人修改", 409) }
        val model = model(repo); advanceUntilIdle(); model.openTaskBoardItem("card-0"); advanceUntilIdle()
        repo.revision = "rev-new"
        model.resetStockPackingPrint("task-0", "纸张损坏"); advanceUntilIdle()
        assertTrue(model.state.value.dailyWork.message.orEmpty().contains("原请求未执行"))
        assertEquals("rev-new", model.state.value.dailyWork.boardDetail!!.lines.single().stateRevision)
        repo.resetException = null
        model.resetStockPackingPrint("task-0", "纸张损坏"); advanceUntilIdle()
        assertEquals("rev-0", repo.resets[0].getString("expected_state_revision"))
        assertEquals("rev-new", repo.resets[1].getString("expected_state_revision"))
        assertNotEquals(repo.resets[0].getString("idempotency_key"), repo.resets[1].getString("idempotency_key"))
        assertEquals("纸张损坏", repo.resets[1].getString("reason"))
    }

    @Test fun resetRetryUsesDurableReasonAndRevisionInsteadOfCurrentVersion() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = StockRepository(count = 1).apply { resetFails = true; printStates = listOf("printed") }
        val model = model(repo); advanceUntilIdle(); model.openTaskBoardItem("card-0"); advanceUntilIdle()
        model.resetStockPackingPrint("task-0", "纸张损坏重新包装"); advanceUntilIdle()
        assertEquals(1, repo.resets.size)
        repo.resetFails = false
        model.resetStockPackingPrint("task-0", "纸张损坏重新包装"); advanceUntilIdle()
        assertEquals(repo.resets[0].toString(), repo.resets[1].toString())
        assertEquals("rev-0", repo.resets[1].getString("expected_state_revision"))
        assertEquals("label-0", repo.resets[1].getString("expected_label_fingerprint"))
        assertTrue(model.state.value.dailyWork.message.orEmpty().contains("原打印记录保留"))
    }

    private class StockRepository(var count: Int = 121, val user: String = "u1",val role: String=ROLE_STOCK_PACKING) : WorkbenchRepository {
        val pages = mutableListOf<Map<String, String>>(); val selections = mutableListOf<JSONObject>()
        val previews = mutableListOf<JSONObject>(); val prepares = mutableListOf<JSONObject>()
        val reports = mutableListOf<Pair<String, String>>(); val resets = mutableListOf<JSONObject>()
        var printVersion = "p1"; var printStates = emptyList<String>(); var oldPrepareCalls = 0
        var pageGate: CompletableDeferred<Unit>? = null; var firstPrepareFails = false; var resetFails = false
        var versionGate: CompletableDeferred<Unit>? = null
        var remoteJobStatus = "submitted"; var remoteBytes: Int? = null; var revision = "rev-0"
        var resetException: Exception? = null; private var selectedOrders = emptyList<String>()
        override suspend fun health() = ServiceHealth("d1", "2026-10-08 07:30", count, 1)
        override suspend fun me() = CurrentUser(user, user, "包装一", listOf(role))
        override suspend fun dailyWorkVersion(): DailyWorkVersion {
            versionGate?.await()
            return DailyWorkVersion("2026-10-08","d1","t1",true,"2026-10-08 07:30",printVersion)
        }
        override suspend fun taskBoard(parameters: Map<String, String>): TaskBoardPage {
            pages += parameters; val page = parameters.getValue("page").toInt(); val size = parameters.getValue("page_size").toInt()
            if (page == 2) pageGate?.await()
            return TaskBoardPage((0 until count).drop((page - 1) * size).take(size).map { item(it) }, count, page, page * size < count,
                "d1", "t1", "2026-10-08 07:30", true, "2026-10-08", printStateVersion = printVersion, orderCount = count)
        }
        private fun item(i: Int) = TaskBoardItem("card-$i", "packing", role, "商品 $i", profileId = "profile",
            orderCount = 1, canAct = false, canPrint = true, printState = printStates.getOrElse(i) { "unprinted" })
        override suspend fun taskBoardDetail(id: String, parameters: Map<String, String>) = TaskBoardDetail(item(0),
            listOf(TaskBoardLine("line-0", "task-0", 1, "ORDER-0", printState = "printed", canPrint = true,
                stateRevision = revision, labelFingerprint = "label-0")), dataVersion = "d1", taskVersion = "t1", printStateVersion = printVersion)
        override suspend fun taskBoardSelection(frozenRequest: String): TaskBoardSelection {
            val json = JSONObject(frozenRequest); selections += json
            val ids = json.optJSONArray("ids")
            selectedOrders = if (json.optBoolean("all")) (0 until count).map { "ORDER-$it" }
                else if (ids != null) (0 until ids.length()).map { "ORDER-" + ids.getString(it).substringAfter("card-") }
                else listOf("ORDER-0")
            return TaskBoardSelection("selection-${selections.size}", selectedOrders.size, selectedOrders, "d1", "t1", role, "profile", printStateVersion = printVersion)
        }
        fun preview(orderNos: List<String>): PackingPrintPreview {
            val rows = orderNos.map { no -> val i = no.substringAfter("ORDER-").toInt(); PackingPrintOrder("profile", no,
                printStates.getOrElse(i) { "unprinted" }, "rev-$i", "label-$i", labelCount = 1) }
            return PackingPrintPreview("preview-${previews.size}", "a".repeat(64), rows.size, rows.size,
                rows.count { it.printState == "printed" }, rows.count { it.printState == "uncertain" }, rows,
                rows.chunked(100).mapIndexed { i, segment -> PackingPrintSegment(i + 1, segment.map { it.orderNo }, segment.size, segment.size) })
        }
        override suspend fun packingPrintPreview(frozenRequest: String): PackingPrintPreview { previews += JSONObject(frozenRequest); return preview(selectedOrders) }
        override suspend fun prepare(state: WorkbenchState, all: Boolean, maximum: Int?): PreparedPrint { oldPrepareCalls++; error("New stock printing must never use legacy prepare") }
        override suspend fun replayPrepare(frozenRequest: String): PreparedPrint {
            prepares += JSONObject(frozenRequest)
            if (firstPrepareFails && prepares.size == 1) throw ConnectException("lost response")
            remoteJobStatus = "prepared"; return PreparedPrint("job-${prepares.size}", 1)
        }
        override suspend fun claim(id: String, actionKey: String) = ClaimedPrint("token", listOf(byteArrayOf(1)))
        override suspend fun report(id: String, token: String, status: String, message: String, bytes: Int) { reports += token to status; remoteJobStatus = status }
        override suspend fun printJob(id: String) = PrintJobState(id, remoteJobStatus, remoteBytes)
        override suspend fun resetPackingPrint(orderNo: String, frozenRequest: String) { resets += JSONObject(frozenRequest); resetException?.let { throw it }; if (resetFails) throw ConnectException("offline") }
        override suspend fun query(destination: Destination, q: String, mode: String, scope: String, logistics: String,
            page: Int, inventoryFilters: InventoryFilters, expectedDataVersion: String) = QueryResult(emptyList(), 0, 1, null, null, null, "", "d1", "")
    }
    private class StockPrinter(val unknownAt: Int = 0) : PrinterClient {
        var sends = 0
        override val state: StateFlow<PrinterState> = MutableStateFlow(PrinterState(connected = true))
        override fun scan() = Unit; override fun connect(address: String) = Unit; override fun disconnect() = Unit
        override fun close() = Unit; override fun cancelSending() = Unit; override fun acknowledgeUnknownAttempt() = true
        override suspend fun sendPages(pages: List<ByteArray>, shouldCancel: () -> Boolean): PrintOutcome { sends++; return PrintOutcome(if (sends == unknownAt) "unknown" else "submitted", 1, "test") }
    }
}
