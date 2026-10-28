package com.ul.workbench

import android.app.Application
import androidx.lifecycle.ViewModelStore
import androidx.test.core.app.ApplicationProvider
import com.ul.workbench.data.ClaimedPrint
import com.ul.workbench.data.Destination
import com.ul.workbench.data.InventoryFilters
import com.ul.workbench.data.InventoryHistory
import com.ul.workbench.data.InventoryMovement
import com.ul.workbench.data.PreparedPrint
import com.ul.workbench.data.PrintJobState
import com.ul.workbench.data.QueryResult
import com.ul.workbench.data.ServiceHealth
import com.ul.workbench.data.ServiceException
import com.ul.workbench.data.SETTLEMENT_LOGISTICS
import com.ul.workbench.data.SHIPPED_UNPOSTED
import com.ul.workbench.data.WorkItem
import com.ul.workbench.data.WorkbenchRepository
import com.ul.workbench.data.WorkbenchState
import com.ul.workbench.data.UpdateTask
import com.ul.workbench.data.FreightAttempt
import com.ul.workbench.data.AdjustmentType
import com.ul.workbench.device.PrintAttempt
import com.ul.workbench.device.PrintOutcome
import com.ul.workbench.device.PrinterClient
import com.ul.workbench.device.PrinterDevice
import com.ul.workbench.device.PrinterState
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestCoroutineScheduler
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withContext
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.json.JSONObject
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.LooperMode

/** Full ViewModel actions with in-memory repositories and printers; no URL or Android BLE is opened. */
@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
@LooperMode(LooperMode.Mode.PAUSED)
class WorkbenchFlowTest {
    private lateinit var app: Application
    private lateinit var scheduler: TestCoroutineScheduler
    private val stores = mutableListOf<ViewModelStore>()

    @Before fun setUp() {
        scheduler = TestCoroutineScheduler()
        Dispatchers.setMain(StandardTestDispatcher(scheduler))
        app = ApplicationProvider.getApplicationContext()
        app.getSharedPreferences("workbench-settings-v1", 0).edit().clear().commit()
    }

    @After fun tearDown() {
        stores.forEach(ViewModelStore::clear)
        scheduler.runCurrent()
        Dispatchers.resetMain()
    }

    private data class QueryCall(val destination: Destination, val query: String, val mode: String, val scope: String, val logistics: String, val page: Int, val filters: InventoryFilters, val expectedVersion: String)
    private data class PrepareCall(val state: WorkbenchState, val all: Boolean, val maximum: Int?)
    private data class ReportCall(val id: String, val token: String, val status: String, val message: String, val bytes: Int)

    private class FakeRepository : WorkbenchRepository {
        val queries = mutableListOf<QueryCall>()
        val prepares = mutableListOf<PrepareCall>()
        val claims = mutableListOf<Pair<String, String>>()
        val reports = mutableListOf<ReportCall>()
        val cancellations = mutableListOf<Triple<String, String, String>>()
        val recoveries = mutableListOf<Triple<String, String, String>>()
        val frozenPage = "! 0 200 200 800 1\r\nTEXT 0 0 1 1 DIN933  M12x40\r\nB QR 1 1 M 2 U 6\r\nMA,MOCK 001\r\nENDQR\r\nPRINT\r\n".toByteArray()
        var queryAction: suspend (QueryCall) -> QueryResult = { call -> page(call.query) }
        val historyCalls = mutableListOf<Triple<Int, Int, Int>>()
        var historyAction: suspend (Int, Int, Int) -> InventoryHistory = { _, _, _ ->
            InventoryHistory("inv-1", "reconciled", "10", "10", "", "", "", 1, false, emptyList())
        }
        var prepareAction: suspend (PrepareCall) -> PreparedPrint = { call -> PreparedPrint("mock-job-${prepares.size}", if (call.all) call.state.total else call.state.selectedIds.size) }
        var claimAction: suspend (String) -> ClaimedPrint = { id -> ClaimedPrint("token-$id", listOf(frozenPage.copyOf())) }
        var cancelFailure: Exception? = null
        var recoveryFailure: Exception? = null
        var printJobStatus = "prepared"
        var loseNextPrepareResponse = false
        var replayFailure: Exception? = null
        val preparedByKey = mutableMapOf<String, PreparedPrint>()
        val prepareReplays = mutableListOf<String>()
        var healthFailure: Exception? = null
        var healthValue = ServiceHealth("v1", "09-09 18:00", 2, 3)
        var latestTask: UpdateTask? = null
        val updates = mutableListOf<String>()
        val resolutions = mutableListOf<Triple<String, String, String>>()
        val printResets = mutableListOf<String>()
        var startAction: suspend (String) -> UpdateTask = { UpdateTask(it, "succeeded", "已完成") }
        var statusAction: suspend (String) -> UpdateTask = { latestTask ?: UpdateTask(it, "succeeded", "已完成") }
        override suspend fun latestUpdate() = latestTask
        override suspend fun startUpdate(id: String, write: Boolean): UpdateTask { updates += id; return startAction(id) }
        override suspend fun updateStatus(id: String) = statusAction(id)
        override suspend fun resolveFreight(id: String, resolution: String, note: String) { resolutions += Triple(id, resolution, note) }
        override suspend fun resetOrderPrintState(orderNo: String, profileId: String, deviceId: String) { printResets += orderNo }
        override suspend fun health(): ServiceHealth {
            healthFailure?.let { throw it }
            return healthValue
        }
        override suspend fun query(destination: Destination, q: String, mode: String, scope: String, logistics: String, page: Int, inventoryFilters: InventoryFilters, expectedDataVersion: String): QueryResult {
            val call = QueryCall(destination, q, mode, scope, logistics, page, inventoryFilters, expectedDataVersion)
            queries += call
            return queryAction(call)
        }
        override suspend fun inventoryHistory(recordId: String, days: Int, limit: Int, offset: Int): InventoryHistory {
            historyCalls += Triple(days, limit, offset)
            return historyAction(days, limit, offset)
        }
        override suspend fun prepare(state: WorkbenchState, all: Boolean, maximum: Int?): PreparedPrint {
            val call = PrepareCall(state, all, maximum)
            prepares += call
            return prepareAction(call)
        }
        override fun freezePrepareRequest(state: WorkbenchState, all: Boolean, maximum: Int?, prepareKey: String) =
            JSONObject().put("prepare_key", prepareKey).toString()
        override suspend fun prepare(state: WorkbenchState, all: Boolean, maximum: Int?, prepareKey: String): PreparedPrint {
            val prepared = prepare(state, all, maximum)
            preparedByKey[prepareKey] = prepared
            if (loseNextPrepareResponse) {
                loseNextPrepareResponse = false
                throw java.net.ConnectException("prepare response lost after commit")
            }
            return prepared
        }
        override suspend fun replayPrepare(frozenRequest: String): PreparedPrint {
            val key = JSONObject(frozenRequest).getString("prepare_key")
            prepareReplays += key
            replayFailure?.let { throw it }
            return preparedByKey[key] ?: throw java.net.ConnectException("prepare result unavailable")
        }
        override suspend fun claim(id: String, actionKey: String): ClaimedPrint {
            claims += id to actionKey
            return claimAction(id)
        }
        override suspend fun printJob(id: String) = PrintJobState(id, printJobStatus, bytesSent = if (printJobStatus in setOf("cancelled", "failed")) 0 else null)
        override suspend fun cancelPreparedPrint(id: String, actionKey: String, deviceId: String, reason: String) {
            cancellations += Triple(id, actionKey, reason)
            cancelFailure?.let { throw it }
            printJobStatus = "cancelled"
        }
        override suspend fun recoverClaimedPrint(id: String, actionKey: String, deviceId: String, reason: String) {
            recoveries += Triple(id, actionKey, reason)
            recoveryFailure?.let { throw it }
            if (printJobStatus != "claimed") throw ServiceException("print_not_claimed", "尚未领取", 409)
            printJobStatus = "unknown"
        }
        override suspend fun report(id: String, token: String, status: String, message: String, bytes: Int) {
            reports += ReportCall(id, token, status, message, bytes)
        }
    }

    private class FakePrinter(initialAttempt: PrintAttempt? = null) : PrinterClient {
        private val mutable = MutableStateFlow(PrinterState(
            status = "MOCK A300E 已连接", connected = true,
            connectedDevice = PrinterDevice("00:11:22:33:44:55", "MOCK A300E"), lastAttempt = initialAttempt,
        ))
        override val state: StateFlow<PrinterState> = mutable
        val sends = mutableListOf<List<ByteArray>>()
        var cancels = 0
        var acknowledgements = 0
        var acknowledgementAllowed = true
        var closed = false
        var outcomeStatus = PrintOutcome.SUBMITTED
        var beforeSend: suspend () -> Unit = {}
        override fun scan() = error("This workflow test must not scan a device")
        override fun connect(address: String) = error("This workflow test must not connect a device")
        fun setConnected(value: Boolean) { mutable.update { it.copy(connected = value) } }
        override fun disconnect() = setConnected(false)
        override fun close() { closed = true }
        override fun cancelSending() { cancels++ }
        override fun acknowledgeUnknownAttempt(): Boolean {
            acknowledgements++
            if (!acknowledgementAllowed) return false
            mutable.update { it.copy(lastAttempt = it.lastAttempt?.copy(status = "acknowledged")) }
            return true
        }
        override suspend fun sendPages(pages: List<ByteArray>, shouldCancel: () -> Boolean): PrintOutcome {
            beforeSend()
            if (shouldCancel()) return PrintOutcome(PrintOutcome.FAILED, 0, "MOCK 已取消，未发送")
            sends += pages.map(ByteArray::copyOf)
            val total = pages.sumOf { it.size }
            val bytes = if (outcomeStatus == PrintOutcome.UNKNOWN) minOf(total, 60) else total
            val message = if (outcomeStatus == PrintOutcome.UNKNOWN) "MOCK 可能已发送部分标签，请核对；不会自动补打。" else "MOCK 已发送标签数据，请以实际出纸为准。"
            mutable.update { it.copy(lastAttempt = PrintAttempt("mock-attempt-${sends.size}", 1234, outcomeStatus, bytes, total, message)) }
            return PrintOutcome(outcomeStatus, bytes, message)
        }
    }

    private fun model(repo: FakeRepository = FakeRepository(), printer: FakePrinter = FakePrinter()): WorkbenchViewModel {
        val viewModel = WorkbenchViewModel(app, repo, printer)
        // Production now opens Daily Work first. These legacy workflow tests exercise
        // order querying and printing explicitly, so select that workspace in the fixture.
        viewModel.navigate(Destination.Orders)
        stores += ViewModelStore().also { it.put("mock-workbench", viewModel) }
        return viewModel
    }

    private fun TestScope.ready(model: WorkbenchViewModel, code: String = "QA0001") {
        runCurrent()
        assertTrue(model.state.value.printerConnected)
        model.scanned(code)
        advanceUntilIdle()
        assertFalse(model.state.value.loading)
    }

    @Test fun orderDrawingLinkRequiresOneExactInventoryIdentity() = runTest(StandardTestDispatcher(scheduler)) {
        val matching = WorkItem("inv-1", "LS0001", "螺栓", profileId = "p1", warehouseId = "08390",
            productId = "product-1", skuId = "sku-1", unitId = "件")
        val otherUnit = matching.copy(id = "inv-2", unitId = "箱")
        val repo = FakeRepository().apply { queryAction = { call ->
            if (call.destination == Destination.Inventory) QueryResult(listOf(matching, otherUnit), 2, call.page,
                null, null, null, "", "data-1", "") else page(call.query)
        } }
        val model = model(repo); advanceUntilIdle()
        model.openOrderProductDetail(WorkItem("order-1", "LS0001", "螺栓", profileId = "p1",
            warehouseId = "08390", productId = "product-1", skuId = "sku-1", unitId = "件"))
        advanceUntilIdle()
        assertEquals("inv-1", model.state.value.productDetail.item?.id)
        assertEquals("LS0001", repo.queries.last().filters.sku)
        assertEquals(listOf(Triple(90, 30, 0)), repo.historyCalls)
        val saved = model.addAdjustment(model.state.value.productDetail.item!!, AdjustmentType.Loss, "1", "盘点")
        assertTrue(saved.saved)
        assertEquals("1", model.state.value.adjustments.drafts.single().line.quantity)
        assertTrue(model.state.value.adjustments.batches.isEmpty())
        model.closeProductDetail()
        val stale = model.addAdjustment(matching, AdjustmentType.Loss, "1", "")
        assertFalse(stale.saved)
        assertTrue(stale.message.contains("页面已变化"))
        assertEquals("1", model.state.value.adjustments.drafts.single().line.quantity)
        model.openOrderProductDetail(WorkItem("order-2", "LS0001", "螺栓")); advanceUntilIdle()
        assertNull(model.state.value.productDetail.item)
        assertTrue(model.state.value.productDetail.error.orEmpty().contains("缺少完整"))
    }

    @Test fun inventoryHistoryPaginatesAndChangesWindowWithoutDroppingOldDataOnFailure() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { historyAction = { days, _, offset ->
            if (days == 180) throw java.net.ConnectException("offline")
            val movement = InventoryMovement("event-$offset", "", "2026-10-05", "D-$offset", "入库", "供应商",
                "1", "in", "10", "reconciled")
            InventoryHistory("inv-1", "reconciled", "10", "10", "", "", "", 1, offset == 0,
                listOf(movement), total = 2, hasMore = offset == 0)
        } }
        val model = model(repo); advanceUntilIdle()
        model.navigate(Destination.Inventory); advanceUntilIdle()
        model.openProductDetail(WorkItem("inv-1", "LS0001", "螺栓", productId = "product-1")); advanceUntilIdle()
        assertEquals(1, model.state.value.productDetail.history?.movements?.size)
        model.loadMoreInventoryHistory(); advanceUntilIdle()
        assertEquals(2, model.state.value.productDetail.history?.movements?.size)
        assertEquals(listOf(Triple(90, 30, 0), Triple(90, 30, 1)), repo.historyCalls)
        model.setInventoryHistoryDays(180); advanceUntilIdle()
        assertEquals(90, model.state.value.productDetail.historyDays)
        assertEquals(2, model.state.value.productDetail.history?.movements?.size)
    }

    @Test fun unavailableFirstServerShowsConnectionSetupAndRecovers() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { healthFailure = java.net.UnknownHostException("unavailable") }
        val model = model(repo)
        advanceUntilIdle()
        assertTrue(model.state.value.connectionRequired)
        assertFalse(model.state.value.loginRequired)
        assertFalse(model.acceptsScan)
        assertTrue(model.state.value.serverStatus.contains("服务地址暂不可用"))
        repo.healthFailure = null
        model.checkServer()
        advanceUntilIdle()
        assertFalse(model.state.value.connectionRequired)
        assertTrue(model.state.value.serverOnline)
    }

    @Test fun temporaryServerFailureKeepsPreviouslyLoadedWorkspace() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository()
        val model = model(repo)
        ready(model)
        val items = model.state.value.items
        repo.healthFailure = java.net.ConnectException("offline")
        model.checkServer()
        advanceUntilIdle()
        assertFalse(model.state.value.connectionRequired)
        assertFalse(model.state.value.serverOnline)
        assertEquals(items, model.state.value.items)
    }

    @Test fun successfulQuerySelectsOnlyTheLoadedPageAndPreservesZeroAndMissingStock() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository()
        repo.queryAction = { call ->
            when {
                call.query == "EMPTY1" -> page(call.query).copy(available = null, stock = null)
                call.page == 2 -> page(call.query, listOf(item("third", call.query)), total = 3, pageNumber = 2)
                else -> page(call.query, total = 3).copy(demand = "17", available = "0", stock = "125")
            }
        }
        val model = model(repo)
        ready(model)
        assertEquals(setOf("QA0001-a", "QA0001-b"), model.state.value.selectedIds)
        assertEquals("17", model.state.value.demand)
        assertEquals("0", model.state.value.available)
        assertEquals("125", model.state.value.stock)
        assertTrue(model.state.value.hasMore)
        model.loadMore()
        advanceUntilIdle()
        assertEquals(3, model.state.value.items.size)
        assertEquals(setOf("QA0001-a", "QA0001-b"), model.state.value.selectedIds)
        assertFalse(model.state.value.hasMore)
        model.scanned("EMPTY1")
        advanceUntilIdle()
        assertNull(model.state.value.available)
        assertNull(model.state.value.stock)
        assertEquals(setOf("EMPTY1-a", "EMPTY1-b"), model.state.value.selectedIds)
    }

    @Test fun cancellingWhilePreparingNeverClaimsOrCallsThePrinter() = runTest(StandardTestDispatcher(scheduler)) {
        val gate = CompletableDeferred<Unit>()
        val repo = FakeRepository().apply { prepareAction = { gate.await(); PreparedPrint("mock-delayed", 2) } }
        val printer = FakePrinter()
        val model = model(repo, printer)
        ready(model)
        model.printSelected(null)
        runCurrent()
        assertTrue(model.state.value.printBusy)
        assertFalse(model.acceptsScan)
        model.cancelPrint()
        gate.complete(Unit)
        advanceUntilIdle()
        assertEquals(1, repo.prepares.size)
        assertTrue(repo.claims.isEmpty())
        assertTrue(printer.sends.isEmpty())
        assertTrue(repo.reports.isEmpty())
        assertEquals("mock-delayed", repo.cancellations.single().first)
        assertNull(app.getSharedPreferences("workbench-settings-v1", 0).getString("pending-job", null))
        assertFalse(model.state.value.printBusy)
        assertTrue(model.acceptsScan)
        assertTrue(model.state.value.receipt!!.contains("已取消"))
    }

    @Test fun claimFailureCancelsPreparedReservationAndNeverSends() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { claimAction = { throw java.net.ConnectException("claim response lost") } }
        val printer = FakePrinter()
        val model = model(repo, printer)
        ready(model)
        model.printSelected(null)
        advanceUntilIdle()
        assertEquals(1, repo.claims.size)
        assertEquals(1, repo.cancellations.size)
        assertTrue(printer.sends.isEmpty())
        assertFalse(model.state.value.uncertainPrint)
        assertNull(app.getSharedPreferences("workbench-settings-v1", 0).getString("pending-job", null))
    }

    @Test fun lostPrepareResponseReplaysTheSameFrozenRequestWithoutCreatingAnotherJob() =
        runTest(StandardTestDispatcher(scheduler)) {
            val repo = FakeRepository().apply { loseNextPrepareResponse = true }
            val printer = FakePrinter()
            val model = model(repo, printer)
            ready(model)
            model.printSelected(null)
            advanceUntilIdle()
            assertEquals(1, repo.prepares.size)
            assertEquals(1, repo.preparedByKey.size)
            assertEquals(repo.preparedByKey.keys.single(), repo.prepareReplays.single())
            assertEquals(1, printer.sends.size)
            assertNull(app.getSharedPreferences("workbench-settings-v1", 0).getString("pending-job", null))
            assertNull(app.getSharedPreferences("workbench-settings-v1", 0).getString("pending-prepare-request", null))
        }

    @Test fun lostClaimResponseRecoversServerReservationAsUnknownAndNeverSends() =
        runTest(StandardTestDispatcher(scheduler)) {
            val repo = FakeRepository()
            repo.claimAction = {
                repo.printJobStatus = "claimed"
                throw java.net.ConnectException("claim response lost after commit")
            }
            repo.cancelFailure = ServiceException("print_already_claimed", "已经领取", 409)
            val printer = FakePrinter()
            val model = model(repo, printer)
            ready(model)
            model.printSelected(null)
            advanceUntilIdle()
            assertEquals(1, repo.cancellations.size)
            assertEquals(1, repo.recoveries.size)
            assertEquals("unknown", repo.printJobStatus)
            assertTrue(printer.sends.isEmpty())
            assertTrue(model.state.value.uncertainPrint)
            assertNotNull(app.getSharedPreferences("workbench-settings-v1", 0).getString("pending-job", null))
        }

    @Test fun unresolvedPreparedCancellationStaysPersistedAndBlocksReprint() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply {
            claimAction = { throw java.net.ConnectException("claim response lost") }
            cancelFailure = java.net.ConnectException("cancel response lost")
        }
        val printer = FakePrinter()
        val model = model(repo, printer)
        ready(model)
        model.printSelected(null)
        advanceUntilIdle()
        assertTrue(model.state.value.uncertainPrint)
        assertNotNull(app.getSharedPreferences("workbench-settings-v1", 0).getString("pending-job", null))
        val prepares = repo.prepares.size
        model.clearMessage()
        model.printSelected(null)
        advanceUntilIdle()
        assertEquals(prepares, repo.prepares.size)
        assertTrue(printer.sends.isEmpty())
    }

    @Test fun restartCancelsPersistedPreparedJobWithTheOriginalKey() = runTest(StandardTestDispatcher(scheduler)) {
        val key = "prepared-cancel-key-123456"
        app.getSharedPreferences("workbench-settings-v1", 0).edit()
            .putString("pending-job", "interrupted-job")
            .putString("pending-cancel-key", key)
            .putString("pending-print-state", "prepared").commit()
        val repo = FakeRepository()
        val model = model(repo)
        advanceUntilIdle()
        assertEquals("interrupted-job", repo.cancellations.single().first)
        assertEquals(key, repo.cancellations.single().second)
        assertNull(app.getSharedPreferences("workbench-settings-v1", 0).getString("pending-job", null))
        assertFalse(model.state.value.uncertainPrint)
    }

    @Test fun restartReplaysPersistedPrepareRequestToFindAndCancelTheUnknownJobId() =
        runTest(StandardTestDispatcher(scheduler)) {
            val prepareKey = "prepare-response-lost-1234"
            val cancelKey = "cancel-after-replay-key-1234"
            val frozen = JSONObject().put("prepare_key", prepareKey).toString()
            app.getSharedPreferences("workbench-settings-v1", 0).edit()
                .putString("pending-prepare-key", prepareKey)
                .putString("pending-prepare-request", frozen)
                .putString("pending-cancel-key", cancelKey)
                .putString("pending-print-state", "preparing").commit()
            val repo = FakeRepository().apply {
                preparedByKey[prepareKey] = PreparedPrint("recovered-prepared-job", 2)
            }
            val model = model(repo)
            advanceUntilIdle()
            assertEquals(prepareKey, repo.prepareReplays.single())
            assertEquals("recovered-prepared-job", repo.cancellations.single().first)
            assertEquals(cancelKey, repo.cancellations.single().second)
            assertNull(app.getSharedPreferences("workbench-settings-v1", 0).getString("pending-prepare-request", null))
            assertFalse(model.state.value.uncertainPrint)
        }

    @Test fun restartClearsPreparingRecordWhenServerProvesNoJobWasCreated() =
        runTest(StandardTestDispatcher(scheduler)) {
            val prepareKey = "prepare-never-created-1234"
            val frozen = JSONObject().put("prepare_key", prepareKey).toString()
            val prefs = app.getSharedPreferences("workbench-settings-v1", 0)
            prefs.edit().putString("pending-prepare-key", prepareKey)
                .putString("pending-prepare-request", frozen)
                .putString("pending-cancel-key", "cancel-never-created-1234")
                .putString("pending-print-state", "preparing").commit()
            val repo = FakeRepository().apply {
                replayFailure = ServiceException("data_version_conflict", "业务数据已更新", 409)
            }
            val model = model(repo)
            advanceUntilIdle()
            assertNull(prefs.getString("pending-prepare-request", null))
            assertFalse(model.state.value.uncertainPrint)
            assertTrue(model.state.value.receipt.orEmpty().contains("占用已解除"))
        }

    @Test fun restartWithClaimTokenConservativelyReportsUnknown() = runTest(StandardTestDispatcher(scheduler)) {
        val prefs = app.getSharedPreferences("workbench-settings-v1", 0)
        prefs.edit().putString("pending-job", "claimed-job")
            .putString("pending-cancel-key", "claimed-cancel-key-123456")
            .putString("pending-print-state", "claimed")
            .putString("pending-claim-token", "claim-token").commit()
        val repo = FakeRepository().apply { printJobStatus = "claimed" }
        val model = model(repo)
        advanceUntilIdle()
        assertEquals("unknown", repo.reports.single().status)
        assertEquals(0, repo.reports.single().bytes)
        assertEquals("claimed-job", prefs.getString("pending-job", null))
        assertTrue(model.state.value.uncertainPrint)
    }

    @Test fun restartWithoutClaimTokenUsesOriginalKeyToRecoverAsUnknown() = runTest(StandardTestDispatcher(scheduler)) {
        val key = "claim-response-lost-key-1234"
        val prefs = app.getSharedPreferences("workbench-settings-v1", 0)
        prefs.edit().putString("pending-job", "claimed-job")
            .putString("pending-cancel-key", key)
            .putString("pending-print-state", "prepared").commit()
        val repo = FakeRepository().apply {
            printJobStatus = "claimed"
            cancelFailure = ServiceException("print_already_claimed", "已经领取", 409)
        }
        val model = model(repo)
        advanceUntilIdle()
        assertEquals("claimed-job", repo.recoveries.single().first)
        assertEquals(key, repo.recoveries.single().second)
        assertEquals("claimed-job", prefs.getString("pending-job", null))
        assertTrue(model.state.value.uncertainPrint)
    }

    @Test fun cancellingDuringClaimReportsNoBytesAndDoesNotStartSending() = runTest(StandardTestDispatcher(scheduler)) {
        val gate = CompletableDeferred<Unit>()
        val repo = FakeRepository().apply { claimAction = { id -> gate.await(); ClaimedPrint("token-$id", listOf(frozenPage)) } }
        val printer = FakePrinter()
        val model = model(repo, printer)
        ready(model)
        model.printSelected(null)
        runCurrent()
        assertEquals(1, repo.claims.size)
        model.cancelPrint()
        gate.complete(Unit)
        advanceUntilIdle()
        assertTrue(printer.sends.isEmpty())
        assertEquals("cancelled", repo.reports.single().status)
        assertEquals(0, repo.reports.single().bytes)
        assertFalse(model.state.value.printBusy)
    }

    @Test fun cancellationAfterClaimButBeforeTransportChecksStillSendsNoBytes() = runTest(StandardTestDispatcher(scheduler)) {
        val gate = CompletableDeferred<Unit>()
        val repo = FakeRepository()
        val printer = FakePrinter().apply { beforeSend = { gate.await() } }
        val model = model(repo, printer)
        ready(model)
        model.printSelected(null)
        runCurrent()
        assertEquals(1, repo.claims.size)
        model.cancelPrint()
        gate.complete(Unit)
        advanceUntilIdle()
        assertTrue(printer.sends.isEmpty())
        assertEquals(PrintOutcome.FAILED, repo.reports.single().status)
        assertEquals(0, repo.reports.single().bytes)
        assertFalse(model.state.value.printBusy)
        assertFalse(model.state.value.uncertainPrint)
    }

    @Test fun successfulPrintIsSingleSubmissionClearsSelectionAndAllowsTheNextScan() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository()
        val printer = FakePrinter()
        val model = model(repo, printer)
        ready(model)
        model.printSelected(null)
        model.printSelected(null)
        model.scanned("IGNORE")
        advanceUntilIdle()
        assertEquals(1, repo.prepares.size)
        assertEquals(1, repo.claims.size)
        assertEquals(1, printer.sends.size)
        assertArrayEquals(repo.frozenPage, printer.sends.single().single())
        assertEquals(PrintOutcome.SUBMITTED, repo.reports.single().status)
        assertEquals(repo.frozenPage.size, repo.reports.single().bytes)
        assertTrue(model.state.value.selectedIds.isEmpty())
        assertFalse(model.state.value.printBusy)
        assertTrue(model.acceptsScan)
        assertTrue(model.state.value.receipt!!.contains("可以继续扫码"))
        assertEquals(listOf("QA0001"), repo.queries.map { it.query })
        model.scanned("QA0002")
        advanceUntilIdle()
        assertEquals(listOf("QA0001", "QA0002"), repo.queries.map { it.query })
        assertEquals("QA0002", model.state.value.query)
        assertNull(model.state.value.receipt)
        assertEquals(setOf("QA0002-a", "QA0002-b"), model.state.value.selectedIds)
    }

    @Test fun unknownResultSurvivesQueryAndFiltersAndCannotBeReprintedUntilAcknowledged() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository()
        val printer = FakePrinter().apply { outcomeStatus = PrintOutcome.UNKNOWN }
        val model = model(repo, printer)
        ready(model)
        model.printSelected(null)
        advanceUntilIdle()
        assertTrue(model.state.value.uncertainPrint)
        val unknownMessage = model.state.value.error
        assertNotNull(unknownMessage)
        assertEquals(PrintOutcome.UNKNOWN, repo.reports.single().status)
        model.scanned("QA0002")
        advanceUntilIdle()
        assertEquals(unknownMessage, model.state.value.error)
        model.changeScope("stock", "DQ库存件")
        advanceUntilIdle()
        assertEquals(unknownMessage, model.state.value.error)
        model.printSelected(null)
        advanceUntilIdle()
        assertEquals(1, printer.sends.size)
        assertTrue(model.state.value.uncertainPrint)
        printer.acknowledgementAllowed = false
        model.clearMessage()
        runCurrent()
        assertTrue(model.state.value.uncertainPrint)
        assertNotNull(model.state.value.error)
        printer.acknowledgementAllowed = true
        model.clearMessage()
        runCurrent()
        assertFalse(model.state.value.uncertainPrint)
        assertNull(model.state.value.error)
        assertNull(app.getSharedPreferences("workbench-settings-v1", 0).getString("pending-job", null))
        printer.outcomeStatus = PrintOutcome.SUBMITTED
        model.printSelected(null)
        advanceUntilIdle()
        assertEquals(2, printer.sends.size)
        assertFalse(model.state.value.uncertainPrint)
    }

    @Test fun deviceRecoveryWithoutViewModelPendingRecordStillRequiresAnAcknowledgement() = runTest(StandardTestDispatcher(scheduler)) {
        val old = PrintAttempt("interrupted", 1234, PrintOutcome.UNKNOWN, 180, 600, "MOCK 上次出纸情况未知")
        val printer = FakePrinter(initialAttempt = old)
        val repo = FakeRepository()
        val model = model(repo, printer)
        ready(model)
        assertTrue(model.state.value.uncertainPrint)
        assertEquals(old.message, model.state.value.error)
        model.printSelected(null)
        advanceUntilIdle()
        assertTrue(repo.prepares.isEmpty())
        assertTrue(printer.sends.isEmpty())
        model.clearMessage()
        runCurrent()
        assertFalse(model.state.value.uncertainPrint)
        assertEquals("acknowledged", printer.state.value.lastAttempt!!.status)
    }

    @Test fun pboxWithNoResultsDoesNotTurnALaterSelectedPrintIntoPrintAll() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository()
        val printer = FakePrinter()
        val model = model(repo, printer)
        runCurrent()
        model.scanned("PBOX01")
        assertFalse(model.state.value.splitRequested)
        model.changeScope("stock", "DQ库存件")
        model.scanned("QA0001")
        advanceUntilIdle()
        model.toggleSelection("QA0001-b")
        model.printSelected(null)
        advanceUntilIdle()
        val prepared = repo.prepares.single()
        assertFalse(prepared.all)
        assertEquals(setOf("QA0001-a"), prepared.state.selectedIds)
        assertEquals("stock", prepared.state.scope)
        assertEquals("DQ库存件", prepared.state.logistics)
        assertNull(prepared.maximum)
    }

    @Test fun pallPrintsOnlyTheCurrentSelectionAndRepeatedScansCannotResendClearedRows() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { queryAction = { call -> page(call.query, total = 5) } }
        val printer = FakePrinter()
        val model = model(repo, printer)
        ready(model)
        model.toggleSelection("QA0001-b")
        model.scanned("PALL01")
        model.scanned("PALL01")
        model.scanned("PBOX01")
        advanceUntilIdle()

        val prepared = repo.prepares.single()
        assertFalse(prepared.all)
        assertEquals(5, prepared.state.total)
        assertEquals(2, prepared.state.items.size)
        assertEquals(setOf("QA0001-a"), prepared.state.selectedIds)
        assertNull(prepared.maximum)
        assertFalse(model.state.value.splitRequested)
        assertEquals(1, printer.sends.size)
        assertTrue(model.state.value.selectedIds.isEmpty())

        model.scanned("PALL01")
        advanceUntilIdle()
        assertEquals(1, repo.prepares.size)
        assertEquals(1, printer.sends.size)
        assertTrue(model.state.value.error!!.contains("选择要打印"))
        assertEquals(listOf("QA0001"), repo.queries.map { it.query })
    }

    @Test fun bothInventoryPrintCodesPrintTheSelectionDirectlyWithoutOpeningSplit() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { queryAction = { call -> page(call.query, total = 5) } }
        val printer = FakePrinter()
        val model = model(repo, printer)
        runCurrent()
        model.navigate(Destination.Inventory)
        val filters = InventoryFilters(name = "六角", spec = "M12", model = "8.8")
        for (code in listOf("PALL01", "PBOX01")) {
            model.searchInventory("INV001", filters)
            advanceUntilIdle()
            assertTrue(model.state.value.selectedIds.isEmpty())
            model.toggleSelection("INV001-a")
            model.scanned(code)
            advanceUntilIdle()

            val prepared = repo.prepares.last()
            assertEquals(Destination.Inventory, prepared.state.destination)
            assertEquals(filters, prepared.state.inventoryFilters)
            assertFalse(prepared.all)
            assertEquals(setOf("INV001-a"), prepared.state.selectedIds)
            assertNull(prepared.maximum)
            assertFalse(model.state.value.splitRequested)
            assertTrue(model.state.value.selectedIds.isEmpty())

            model.scanned(code)
            advanceUntilIdle()
            assertEquals(repo.prepares.size, printer.sends.size)
            assertTrue(model.state.value.error!!.contains("选择要打印"))
        }
        assertEquals(2, repo.prepares.size)
        assertEquals(2, printer.sends.size)
        assertEquals(listOf("INV001", "INV001"), repo.queries.map { it.query })
    }

    @Test fun inventoryPrintCodesKeepEditingDisconnectedAndUnknownPrintGuards() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository()
        val printer = FakePrinter()
        val model = model(repo, printer)
        runCurrent()
        model.navigate(Destination.Inventory)
        ready(model, "INV001")
        model.toggleSelection("INV001-a")
        model.editingChanged(true)
        model.scanned("PALL01")
        model.scanned("PBOX01")
        advanceUntilIdle()
        assertTrue(repo.prepares.isEmpty())
        assertFalse(model.state.value.splitRequested)

        model.editingChanged(false)
        printer.disconnect()
        runCurrent()
        for (code in listOf("PALL01", "PBOX01")) {
            model.scanned(code)
            advanceUntilIdle()
            assertTrue(repo.prepares.isEmpty())
            assertTrue(model.state.value.error!!.contains("连接 A300E"))
            assertFalse(model.state.value.splitRequested)
        }

        printer.setConnected(true)
        printer.outcomeStatus = PrintOutcome.UNKNOWN
        runCurrent()
        model.scanned("PBOX01")
        advanceUntilIdle()
        assertTrue(model.state.value.uncertainPrint)
        for (code in listOf("PALL01", "PBOX01")) {
            model.scanned(code)
            advanceUntilIdle()
            assertEquals(1, repo.prepares.size)
            assertEquals(1, printer.sends.size)
            assertTrue(model.state.value.error!!.contains("核对上次打印结果"))
            assertFalse(model.state.value.splitRequested)
        }
    }

    @Test fun manualEditingAndSplitDialogBlockScanActionsAndDismissRestoresScanning() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository()
        val model = model(repo)
        ready(model)
        model.editingChanged(true)
        model.scanned("QA0002")
        model.scanned("PALL01")
        model.scanned("PBOX01")
        advanceUntilIdle()
        assertEquals(1, repo.queries.size)
        assertTrue(repo.prepares.isEmpty())
        assertFalse(model.state.value.splitRequested)
        assertFalse(model.acceptsScan)
        model.editingChanged(false)
        model.scanned("PBOX01")
        assertTrue(model.state.value.splitRequested)
        assertFalse(model.acceptsScan)
        model.scanned("QA0003")
        model.scanned("PALL01")
        advanceUntilIdle()
        assertEquals(1, repo.queries.size)
        assertTrue(repo.prepares.isEmpty())
        model.dismissSplit()
        assertTrue(model.acceptsScan)
        model.scanned("QA0004")
        advanceUntilIdle()
        assertEquals("QA0004", repo.queries.last().query)
        model.navigate(Destination.Devices)
        assertFalse(model.acceptsScan)
        model.scanned("QA0005")
        model.scanned("PALL01")
        model.scanned("PBOX01")
        advanceUntilIdle()
        assertEquals(2, repo.queries.size)
        assertTrue(repo.prepares.isEmpty())
        assertFalse(model.state.value.splitRequested)
    }

    @Test fun pboxKeepsTheCurrentSelectionThroughDisconnectedAndInvalidQuantityPrechecks() = runTest(StandardTestDispatcher(scheduler)) {
        // PBOX mirrors the split button: one selected row, not all five query matches.
        val repo = FakeRepository().apply { queryAction = { call -> page(call.query, total = 5) } }
        val printer = FakePrinter()
        val model = model(repo, printer)
        ready(model)
        model.toggleSelection("QA0001-b")
        model.scanned("PBOX01")
        assertTrue(model.state.value.splitRequested)
        printer.disconnect()
        runCurrent()
        model.printSelected(5)
        advanceUntilIdle()
        assertTrue(model.state.value.splitRequested)
        assertFalse(model.state.value.printBusy)
        assertTrue(model.state.value.error!!.contains("连接 A300E"))
        assertTrue(repo.prepares.isEmpty())
        assertTrue(printer.sends.isEmpty())

        printer.setConnected(true)
        runCurrent()
        model.printSelected(0)
        advanceUntilIdle()
        assertTrue(model.state.value.splitRequested)
        assertTrue(repo.prepares.isEmpty())

        model.printSelected(5)
        advanceUntilIdle()
        val split = repo.prepares.single()
        assertFalse(split.all)
        assertEquals(5, split.state.total)
        assertEquals(setOf("QA0001-a"), split.state.selectedIds)
        assertEquals(5, split.maximum)
        assertEquals(1, printer.sends.size)
        assertFalse(model.state.value.splitRequested)
        assertFalse(model.state.value.printBusy)

        // A later ordinary print still uses the current selection without a split maximum.
        model.toggleSelection("QA0001-a")
        model.printSelected(null)
        advanceUntilIdle()
        assertEquals(2, repo.prepares.size)
        assertFalse(repo.prepares.last().all)
        assertEquals(setOf("QA0001-a"), repo.prepares.last().state.selectedIds)
        assertNull(repo.prepares.last().maximum)
    }

    @Test fun failedNewQueryCannotUsePreviousTotalsOrVersionForPallOrPbox() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply {
            queryAction = { call ->
                if (call.query == "FAIL01") throw ServiceException("mock_query_failed", "MOCK 查询失败")
                page(call.query, total = 5)
            }
        }
        val printer = FakePrinter()
        val model = model(repo, printer)
        ready(model)
        assertEquals(5, model.state.value.total)
        assertEquals("v1", model.state.value.dataVersion)
        assertTrue(model.state.value.hasMore)

        model.scanned("FAIL01")
        assertTrue(model.state.value.loading)
        assertEquals(0, model.state.value.total)
        assertEquals("", model.state.value.dataVersion)
        assertFalse(model.state.value.hasMore)
        advanceUntilIdle()
        assertFalse(model.state.value.loading)
        assertTrue(model.state.value.items.isEmpty())
        assertEquals("MOCK 查询失败", model.state.value.error)

        model.scanned("PALL01")
        advanceUntilIdle()
        assertTrue(repo.prepares.isEmpty())
        assertTrue(printer.sends.isEmpty())
        model.scanned("PBOX01")
        advanceUntilIdle()
        assertFalse(model.state.value.splitRequested)
        assertTrue(repo.prepares.isEmpty())
        assertTrue(repo.claims.isEmpty())
        assertTrue(printer.sends.isEmpty())
        assertEquals(0, model.state.value.total)
        assertEquals("", model.state.value.dataVersion)
    }

    @Test fun lateNonCancellableQueryCannotOverwriteTheNewerScannedOrder() = runTest(StandardTestDispatcher(scheduler)) {
        val oldResponse = CompletableDeferred<Unit>()
        val repo = FakeRepository().apply {
            queryAction = { call ->
                if (call.query == "QA0001") withContext(NonCancellable) { oldResponse.await() }
                page(call.query)
            }
        }
        val model = model(repo)
        runCurrent()
        model.scanned("QA0001")
        runCurrent()
        assertTrue(model.state.value.loading)
        model.scanned("PBOX01")
        assertFalse(model.state.value.splitRequested)
        model.scanned("QA0002")
        runCurrent()
        assertEquals("QA0002", model.state.value.items.first().sku)
        oldResponse.complete(Unit)
        advanceUntilIdle()
        assertEquals("QA0002", model.state.value.query)
        assertEquals("QA0002", model.state.value.items.first().sku)
        assertEquals(setOf("QA0002-a", "QA0002-b"), model.state.value.selectedIds)
        assertFalse(model.state.value.loading)
    }

    @Test fun orderAndInventoryWorkspacesRetainIndependentResultsFiltersAndSelections() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository()
        val model = model(repo)
        ready(model)
        model.changeScope("stock", "LS库存件")
        advanceUntilIdle()
        model.toggleSelection("QA0001-b")
        val orders = model.state.value
        model.navigate(Destination.Inventory)
        assertFalse(model.state.value.queried)
        val filters = InventoryFilters(name = "六角", spec = "M12", model = "8.8")
        model.searchInventory("", filters)
        advanceUntilIdle()
        model.toggleSelection("-a")
        val inventory = model.state.value
        val calls = repo.queries.size
        model.navigate(Destination.Orders)
        assertEquals(orders.items, model.state.value.items)
        assertEquals(orders.selectedIds, model.state.value.selectedIds)
        assertEquals(orders.queryRevision, model.state.value.queryRevision)
        assertEquals("QA0001", model.state.value.query)
        assertEquals("LS库存件", model.state.value.logistics)
        model.navigate(Destination.Orders)
        assertEquals(orders.items, model.state.value.items)
        model.navigate(Destination.Devices)
        model.saveServer(model.state.value.serverUrl)
        advanceUntilIdle()
        model.navigate(Destination.Inventory)
        assertEquals(filters, model.state.value.inventoryFilters)
        assertEquals(inventory.items, model.state.value.items)
        assertEquals(inventory.selectedIds, model.state.value.selectedIds)
        assertEquals(calls, repo.queries.size)
    }

    @Test fun inventoryConditionsSurvivePaginationAndPrintAllButANewScanStartsFresh() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { queryAction = { call ->
            page("INV001", listOf(item("row-${call.page}", "INV001")), total = 2, pageNumber = call.page)
        } }
        val model = model(repo)
        runCurrent()
        model.navigate(Destination.Inventory)
        val filters = InventoryFilters(name = " 六角 ", spec = " M12 ", model = " 8.8 ", material = "钢")
        model.searchInventory(" 货位A ", filters)
        advanceUntilIdle()
        model.loadMore()
        advanceUntilIdle()
        assertEquals(listOf(1, 2), repo.queries.map { it.page })
        assertTrue(repo.queries.all { it.query == "货位A" && it.filters == filters.trimmed() })
        model.printAll(null)
        advanceUntilIdle()
        assertEquals(filters.trimmed(), repo.prepares.single().state.inventoryFilters)
        model.scanned("INV002")
        advanceUntilIdle()
        assertEquals(InventoryFilters(), repo.queries.last().filters)
        assertEquals("INV002", repo.queries.last().query)
    }

    @Test fun inventoryAutoSelectsOnlyOneActualResultAndPaginationKeepsManualSelection() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { queryAction = { call ->
            when {
                call.query == "SINGLE" -> page(call.query, listOf(item("only", call.query)), total = 1)
                call.query == "PARTIAL" -> page(call.query, listOf(item("one-of-many", call.query)), total = 2)
                call.query == "INCONSISTENT" -> page(call.query, total = 1)
                call.page == 2 -> page(call.query, listOf(item("third", call.query)), total = 3, pageNumber = 2)
                else -> page(call.query, total = 3)
            }
        } }
        val model = model(repo)
        runCurrent()
        model.navigate(Destination.Inventory)
        model.searchInventory("MULTI", InventoryFilters())
        advanceUntilIdle()
        assertTrue(model.state.value.selectedIds.isEmpty())
        model.scanned("PALL01")
        advanceUntilIdle()
        assertTrue(repo.prepares.isEmpty())
        model.toggleSelection("MULTI-a")
        model.loadMore()
        advanceUntilIdle()
        assertEquals(setOf("MULTI-a"), model.state.value.selectedIds)
        assertEquals(3, model.state.value.items.size)
        for (query in listOf("PARTIAL", "INCONSISTENT")) {
            model.searchInventory(query, InventoryFilters())
            advanceUntilIdle()
            assertTrue(model.state.value.selectedIds.isEmpty())
        }
        model.searchInventory("SINGLE", InventoryFilters())
        advanceUntilIdle()
        assertEquals(setOf("only"), model.state.value.selectedIds)
        model.scanned("PALL01")
        advanceUntilIdle()
        assertEquals(setOf("only"), repo.prepares.single().state.selectedIds)
    }

    @Test fun explicitStopIsRespectedAndDoesNotCancelTheNextAcceptedPrint() = runTest(StandardTestDispatcher(scheduler)) {
        val gate = CompletableDeferred<Unit>()
        val repo = FakeRepository().apply { prepareAction = { gate.await(); PreparedPrint("stopped-first", 2) } }
        val printer = FakePrinter()
        val model = model(repo, printer)
        ready(model)
        model.scanned("PALL01")
        runCurrent()
        model.cancelPrint()
        gate.complete(Unit)
        advanceUntilIdle()
        assertTrue(printer.sends.isEmpty())
        assertTrue(model.state.value.receipt!!.contains("已取消"))
        repo.prepareAction = { PreparedPrint("fresh-request", 2) }
        model.scanned("PALL01")
        advanceUntilIdle()
        assertEquals(1, printer.sends.size)
        assertTrue(model.state.value.receipt!!.contains("已发送"))
    }

    @Test fun dedicatedInventoryCodeAndStandardScanUseExactSkuButOtherScansKeepLegacyQuery() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository()
        val model = model(repo)
        runCurrent()
        model.navigate(Destination.Inventory)
        model.searchInventory("旧货位", InventoryFilters(name = "六角", spec = "M12"))
        advanceUntilIdle()
        model.searchInventoryCode(" ls0001 ")
        advanceUntilIdle()
        assertEquals("LS0001", model.state.value.query)
        assertEquals("sku_exact", repo.queries.last().mode)
        assertEquals(InventoryFilters(sku = "LS0001"), repo.queries.last().filters)
        val calls = repo.queries.size
        model.searchInventoryCode("LS001")
        advanceUntilIdle()
        assertEquals(calls, repo.queries.size)
        assertEquals("LS0001", model.state.value.query)
        model.scanned("LD0002")
        advanceUntilIdle()
        assertEquals("sku_exact", repo.queries.last().mode)
        assertEquals(InventoryFilters(sku = "LD0002"), repo.queries.last().filters)
        model.scanned("INV001")
        advanceUntilIdle()
        assertEquals("sku", repo.queries.last().mode)
        assertEquals("INV001", repo.queries.last().query)
        assertEquals(InventoryFilters(), repo.queries.last().filters)
        model.navigate(Destination.Orders)
        model.scanned("LS0001")
        advanceUntilIdle()
        assertEquals("sku", repo.queries.last().mode)
        assertEquals(InventoryFilters(), repo.queries.last().filters)
    }

    @Test fun clearOnlyResetsActiveQueryAndCannotAcknowledgeAnUnknownPrint() = runTest(StandardTestDispatcher(scheduler)) {
        val printer = FakePrinter().apply { outcomeStatus = PrintOutcome.UNKNOWN }
        val model = model(printer = printer)
        ready(model)
        model.printSelected(null)
        advanceUntilIdle()
        val warning = model.state.value.error
        model.navigate(Destination.Inventory)
        model.searchInventory("", InventoryFilters(name = "螺栓"))
        advanceUntilIdle()
        model.clearQuery()
        assertFalse(model.state.value.queried)
        assertTrue(model.state.value.items.isEmpty())
        assertEquals(InventoryFilters(), model.state.value.inventoryFilters)
        assertTrue(model.state.value.uncertainPrint)
        assertEquals(warning, model.state.value.error)
        model.navigate(Destination.Orders)
        assertEquals("QA0001", model.state.value.query)
        assertEquals(2, model.state.value.items.size)
        assertTrue(model.state.value.uncertainPrint)
    }

    @Test fun changingServerInvalidatesAllRetainedWorkspaces() = runTest(StandardTestDispatcher(scheduler)) {
        val model = model()
        ready(model)
        model.navigate(Destination.Inventory)
        model.searchInventory("", InventoryFilters(spec = "M12"))
        advanceUntilIdle()
        model.navigate(Destination.Devices)
        model.saveServer("https://new.example.org")
        advanceUntilIdle()
        for (page in listOf(Destination.Orders, Destination.Inventory)) {
            model.navigate(page)
            assertFalse(model.state.value.queried)
            assertTrue(model.state.value.items.isEmpty())
            assertEquals("", model.state.value.dataVersion)
        }
    }

    @Test fun switchingDuringAQueryResumesOnReturnAndIgnoresTheOldResponse() = runTest(StandardTestDispatcher(scheduler)) {
        val oldResponse = CompletableDeferred<Unit>()
        var orderCalls = 0
        val repo = FakeRepository().apply { queryAction = { call ->
            if (call.destination == Destination.Orders && ++orderCalls == 1) {
                withContext(NonCancellable) { oldResponse.await() }
                page("OLD001")
            } else page(call.query)
        } }
        val model = model(repo)
        runCurrent()
        model.search("QA0001", "sku")
        runCurrent()
        model.navigate(Destination.Inventory)
        model.searchInventory("INV001", InventoryFilters())
        runCurrent()
        assertEquals("INV001", model.state.value.items.first().sku)
        model.navigate(Destination.Orders)
        runCurrent()
        assertEquals("QA0001", model.state.value.items.first().sku)
        oldResponse.complete(Unit)
        advanceUntilIdle()
        assertEquals("QA0001", model.state.value.items.first().sku)
        assertEquals(2, orderCalls)
    }

    @Test fun paginationConflictReloadsFirstPageWithoutReusingSelections() = runTest(StandardTestDispatcher(scheduler)) {
        var revision = "v1"
        val repo = FakeRepository().apply { queryAction = { call ->
            if (call.page == 2) {
                assertEquals("v1", call.expectedVersion)
                revision = "v2"
                throw ServiceException("data_version_changed", "数据已更新")
            }
            page(call.query, total = 3).copy(dataVersion = revision)
        } }
        val model = model(repo)
        ready(model)
        assertFalse(model.state.value.selectedIds.isEmpty())
        model.loadMore()
        advanceUntilIdle()
        assertEquals(listOf(1, 2, 1), repo.queries.map { it.page })
        assertEquals("v2", model.state.value.dataVersion)
        assertTrue(model.state.value.selectedIds.isEmpty())
        assertNull(model.state.value.error)
    }

    @Test fun lanAdjustmentHealthIsIndependentOfPublicApiCapability() = runTest(StandardTestDispatcher(scheduler)) {
        val api = FakeRepository().apply { healthValue = healthValue.copy(apiUpdates = true, adjustmentUpload = false) }
        val lan = FakeRepository().apply { healthValue = healthValue.copy(adjustmentUpload = true) }
        val model = WorkbenchViewModel(app, api, FakePrinter(), lan)
        ViewModelStore().also { it.put("lan-model", model); stores += it }
        advanceUntilIdle()
        assertTrue(model.state.value.adjustmentUpload)
        assertTrue(model.state.value.serverOnline)
        assertEquals("局域网上传电脑已连接", model.state.value.adjustmentServerStatus)
        assertEquals("http://192.168.1.5:5191", WorkbenchViewModel.ADJUSTMENT_SERVER)
    }

    @Test fun unconnectedAdjustmentApiPreservesEditableDraftOnUploadAttempt() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { healthValue = healthValue.copy(apiUpdates = true, adjustmentUpload = false) }
        val model = model(repo)
        advanceUntilIdle()
        model.navigate(Destination.Inventory)
        assertTrue(model.addAdjustment(item("draft", "QA0001"), AdjustmentType.Loss, "2", "盘点").saved)
        assertEquals(1, model.state.value.adjustments.drafts.size)
        model.uploadAdjustments(AdjustmentType.Loss)
        advanceUntilIdle()
        assertEquals(1, model.state.value.adjustments.drafts.size)
        assertTrue(model.state.value.adjustments.batches.isEmpty())
        assertTrue(model.state.value.error!!.contains("局域网上传电脑暂不可用"))
    }

    @Test fun incompletePrintRecordsRemainVisibleButCannotBeSelectedOrPrinted() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { queryAction = { call -> page(call.query, listOf(
            item("good", call.query), item("blocked", call.query).copy(printable = false, calculationIssues = listOf("摘要未完整")))) } }
        val model = model(repo)
        ready(model)
        assertEquals(2, model.state.value.items.size)
        assertEquals(setOf("good"), model.state.value.selectedIds)
        model.toggleSelection("blocked")
        assertEquals(setOf("good"), model.state.value.selectedIds)
        model.selectAll(); model.selectAll()
        assertEquals(setOf("good"), model.state.value.selectedIds)
        model.printAll(null)
        advanceUntilIdle()
        assertTrue(repo.prepares.isEmpty())
        assertTrue(model.state.value.error!!.contains("打印字段"))
    }

    @Test fun shippedUnpostedOrderStaysVisibleButCannotEnterSelectionOrPrint() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { queryAction = { call -> page(call.query, listOf(
            item("good", call.query),
            item("settlement", call.query).copy(logistics = SETTLEMENT_LOGISTICS,
                lifecycleState = SHIPPED_UNPOSTED, processState = "500", printable = true,
                printBlockReason = "订单已出库，等待ERP结存"))) } }
        val model = model(repo)
        ready(model)
        assertEquals(2, model.state.value.items.size)
        assertEquals(setOf("good"), model.state.value.selectedIds)
        model.toggleSelection("settlement")
        assertEquals(setOf("good"), model.state.value.selectedIds)
        model.printAll(null)
        advanceUntilIdle()
        assertTrue(repo.prepares.isEmpty())
        assertTrue(model.state.value.error!!.contains("无需打印"))
    }

    @Test fun applyingUnchangedOrderFiltersAndScopeRefreshesThePublishedVersion() = runTest(StandardTestDispatcher(scheduler)) {
        var version = "v1"
        val repo = FakeRepository().apply { queryAction = { call -> page(call.query).copy(dataVersion = version) } }
        val model = model(repo)
        ready(model)
        val count = repo.queries.size
        version = "v2"
        model.changeOrderFilters(model.state.value.orderFilters)
        advanceUntilIdle()
        assertEquals(count + 1, repo.queries.size)
        assertEquals("v2", model.state.value.dataVersion)
        version = "v3"
        model.changeScope(model.state.value.scope, model.state.value.logistics)
        advanceUntilIdle()
        assertEquals(count + 2, repo.queries.size)
        assertEquals("v3", model.state.value.dataVersion)
    }

    @Test fun lifecycleConflictBeforeClaimSendsNoBytesAndClearsTheStaleSelection() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { claimAction = {
            throw ServiceException("order_lifecycle_conflict", "订单已完成发货或需结存，请刷新订单")
        } }
        val printer = FakePrinter()
        val model = model(repo, printer)
        ready(model)
        assertFalse(model.state.value.selectedIds.isEmpty())
        model.printSelected(null)
        advanceUntilIdle()
        assertEquals(1, repo.prepares.size)
        assertTrue(printer.sends.isEmpty())
        assertTrue(model.state.value.selectedIds.isEmpty())
        assertFalse(model.state.value.printBusy)
        assertEquals("未发送，订单状态已变化", model.state.value.printMessage)
        assertTrue(model.state.value.error!!.contains("需结存"))
        assertNull(app.getSharedPreferences("workbench-settings-v1", 0).getString("pending-job", null))
    }

    @Test fun shippedUnpostedPrintedOrderCannotBeResetToUnprinted() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply { queryAction = { call -> page(call.query, listOf(
            item("settlement", call.query).copy(orderNo = "ORDER#500", logistics = SETTLEMENT_LOGISTICS,
                lifecycleState = SHIPPED_UNPOSTED, processState = "500", printable = false, printState = "printed"))) } }
        val model = model(repo)
        ready(model)
        model.resetOrderPrintState("ORDER#500")
        advanceUntilIdle()
        assertTrue(repo.printResets.isEmpty())
        assertTrue(model.state.value.error!!.contains("无需重置"))
    }

    @Test fun dataVersionConflictRefreshesTheSameQueryWithoutRestoringSelection() = runTest(StandardTestDispatcher(scheduler)) {
        var version = "v1"
        val repo = FakeRepository().apply {
            queryAction = { call -> page(call.query).copy(dataVersion = version) }
            claimAction = {
                version = "v2"
                throw ServiceException("data_version_conflict", "业务数据已更新，请重新查询")
            }
        }
        val model = model(repo, FakePrinter())
        ready(model)
        val queries = repo.queries.size
        model.printSelected(null)
        advanceUntilIdle()
        assertEquals(queries + 1, repo.queries.size)
        assertEquals("v2", model.state.value.dataVersion)
        assertTrue(model.state.value.selectedIds.isEmpty())
        assertTrue(model.state.value.error!!.contains("已刷新当前列表"))
    }

    @Test fun singleUpdateRefreshesCurrentConditionsAndClearsOldSelections() = runTest(StandardTestDispatcher(scheduler)) {
        val gate = CompletableDeferred<Unit>()
        var revision = "v1"
        val repo = FakeRepository().apply {
            healthValue = healthValue.copy(apiUpdates = true, logisticsWrite = true)
            queryAction = { call -> page(call.query).copy(dataVersion = revision) }
            startAction = { id ->
                gate.await(); revision = "v2"
                UpdateTask(id, "partial", "物流部分失败", published = true, dataVersion = "v2", publishedAt = "09-26 10:00", failed = 1)
            }
        }
        val model = model(repo)
        ready(model)
        model.changeScope("stock", "LS库存件")
        advanceUntilIdle()
        model.updateData(); model.updateData()
        runCurrent()
        assertEquals(1, repo.updates.size)
        gate.complete(Unit)
        advanceUntilIdle()
        assertEquals("v2", model.state.value.dataVersion)
        assertEquals("QA0001", repo.queries.last().query)
        assertEquals("LS库存件", repo.queries.last().logistics)
        assertTrue(model.state.value.selectedIds.isEmpty())
        assertEquals(1, model.state.value.updateTask!!.failed)
        assertFalse(model.state.value.updateBusy)
    }

    @Test fun lostSubmissionResponseReusesTheSameTaskIdentityOnRetry() = runTest(StandardTestDispatcher(scheduler)) {
        val repo = FakeRepository().apply {
            healthValue = healthValue.copy(apiUpdates = true)
            startAction = { id -> if (updates.size == 1) throw java.net.SocketTimeoutException()
                else UpdateTask(id, "blocked", "缺少资料") }
        }
        val model = model(repo)
        advanceUntilIdle()
        model.updateData(); advanceUntilIdle()
        assertFalse(model.state.value.updateBusy)
        model.updateData(); advanceUntilIdle()
        assertEquals(2, repo.updates.size)
        assertEquals(repo.updates[0], repo.updates[1])
    }

    @Test fun completedBackgroundUpdateInvalidatesInactiveWorkspaceBeforeReturn() = runTest(StandardTestDispatcher(scheduler)) {
        val gate = CompletableDeferred<Unit>()
        var revision = "v1"
        val repo = FakeRepository().apply {
            healthValue = healthValue.copy(apiUpdates = true)
            queryAction = { call -> page(call.query).copy(dataVersion = revision) }
            startAction = { id -> gate.await(); revision = "v2"; UpdateTask(id, "succeeded", "完成", published = true, dataVersion = "v2") }
        }
        val model = model(repo)
        ready(model)
        model.updateData(); runCurrent()
        model.navigate(Destination.Inventory)
        model.searchInventory("螺丝", InventoryFilters(model = "8.8")); runCurrent()
        model.navigate(Destination.Devices)
        gate.complete(Unit); advanceUntilIdle()
        model.navigate(Destination.Orders); advanceUntilIdle()
        assertEquals("v2", model.state.value.dataVersion)
        assertTrue(model.state.value.selectedIds.isEmpty())
        model.navigate(Destination.Inventory); advanceUntilIdle()
        assertEquals("8.8", model.state.value.inventoryFilters.model)
        assertEquals("v2", model.state.value.dataVersion)
        assertTrue(model.state.value.selectedIds.isEmpty())
    }

    @Test fun unknownFreightRequiresExplicitConclusionAndRefreshesLedger() = runTest(StandardTestDispatcher(scheduler)) {
        val unknown = FreightAttempt("attempt-1", "ORDER#1", "unknown", "待到货", "@09/26#ORDER#1")
        val repo = FakeRepository().apply {
            healthValue = healthValue.copy(apiUpdates = true)
            latestTask = UpdateTask("job-1", "unknown", "待核对", unknown = 1, attempts = listOf(unknown))
            statusAction = { latestTask!!.copy(unknown = 0, attempts = listOf(unknown.copy(status = "applied"))) }
        }
        val model = model(repo)
        advanceUntilIdle()
        model.showUpdateDetails()
        assertFalse(model.acceptsScan)
        model.resolveFreight("attempt-1", "applied", "")
        assertTrue(repo.resolutions.isEmpty())
        model.resolveFreight("attempt-1", "applied", "已核对 ERP 公司和单号")
        advanceUntilIdle()
        assertEquals(Triple("attempt-1", "applied", "已核对 ERP 公司和单号"), repo.resolutions.single())
        assertEquals(0, model.state.value.updateTask!!.unknown)
        assertFalse(model.state.value.resolvingFreight)
    }

    companion object {
        private fun item(id: String, sku: String) = WorkItem(id, sku, "MOCK 商品 $sku", quantity = "12", available = "0", stock = "125")
        private fun page(sku: String, items: List<WorkItem> = listOf(item("$sku-a", sku), item("$sku-b", sku)), total: Int = items.size, pageNumber: Int = 1) =
            QueryResult(items, total, pageNumber, "17", "0", "125", "MOCK 商品 $sku", "v1", "09-09 18:00")
    }
}
