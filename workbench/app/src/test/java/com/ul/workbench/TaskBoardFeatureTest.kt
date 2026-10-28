package com.ul.workbench

import android.app.Application
import androidx.lifecycle.ViewModelStore
import androidx.test.core.app.ApplicationProvider
import com.ul.workbench.data.*
import com.ul.workbench.device.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
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
class TaskBoardFeatureTest {
    private lateinit var app: Application
    private lateinit var scheduler: TestCoroutineScheduler
    private val stores = mutableListOf<ViewModelStore>()
    @Before fun setUp() {
        scheduler = TestCoroutineScheduler()
        Dispatchers.setMain(StandardTestDispatcher(scheduler))
        app = ApplicationProvider.getApplicationContext()
        app.getSharedPreferences("workbench-settings-v1", 0).edit().clear().commit()
    }
    @After fun tearDown() { stores.forEach(ViewModelStore::clear); scheduler.runCurrent(); Dispatchers.resetMain() }
    private fun model(repository: BoardRepository, printer: BoardPrinter = BoardPrinter()): WorkbenchViewModel =
        WorkbenchViewModel(app, repository, printer).also { value -> stores += ViewModelStore().also { it.put("board", value) } }

    @Test fun fullListLoadsAll121ItemsWithVersionedPagesAndNoLegacyFallback() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository()
        val model = model(repository)
        advanceUntilIdle()
        assertEquals(50, model.state.value.dailyWork.board.items.size)
        model.loadMoreTaskBoard(); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle()
        assertEquals(121, model.state.value.dailyWork.board.items.size)
        assertFalse(model.state.value.dailyWork.board.hasMore)
        assertEquals("data-1", repository.pages[1]["data_version"])
        assertEquals("task-1", repository.pages[1]["task_version"])
        assertEquals(0, repository.legacyRequests)
        model.selectDailyView("role"); advanceUntilIdle()
        assertEquals("role", repository.pages.last()["view"])
        repository.failBoard = true
        model.searchTaskBoard("LS"); advanceUntilIdle()
        assertTrue(model.state.value.dailyWork.board.items.isEmpty())
        assertTrue(model.state.value.dailyWork.error.orEmpty().isNotBlank())
        assertEquals(0, repository.legacyRequests)
    }

    @Test fun changingVersionDuringPaginationStartsAgainWithoutMixingPages() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository()
        val model = model(repository); advanceUntilIdle()
        repository.changedVersion = true
        model.loadMoreTaskBoard(); advanceUntilIdle()
        assertEquals(50, model.state.value.dailyWork.board.items.size)
        assertEquals("data-2", model.state.value.dailyWork.board.dataVersion)
        assertEquals(1, model.state.value.dailyWork.board.page)
    }

    @Test fun changingColumnsRestoresEachPreviouslyLoadedPageDepth() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_SALES)
        val model = model(repository); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle()
        assertEquals(100, model.state.value.dailyWork.board.items.size)
        model.selectTaskBoardSection("in_progress"); advanceUntilIdle()
        assertEquals(50, model.state.value.dailyWork.board.items.size)
        model.loadMoreTaskBoard(); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle()
        assertEquals(121, model.state.value.dailyWork.board.items.size)
        model.selectTaskBoardSection("todo"); advanceUntilIdle()
        assertEquals(100, model.state.value.dailyWork.board.items.size)
        assertEquals(2, model.state.value.dailyWork.board.page)
        assertEquals("todo", repository.pages.last()["work_state"])
        assertEquals("data-1", repository.pages.last()["data_version"])
        assertEquals("task-1", repository.pages.last()["task_version"])
        model.selectTaskBoardSection("in_progress"); advanceUntilIdle()
        assertEquals(121, model.state.value.dailyWork.board.items.size)
        assertEquals(3, model.state.value.dailyWork.board.page)
    }

    @Test fun unchangedModernColumnsAndReturnReuseAllLoadedPagesAfterOneVersionProbe() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_SALES).apply { printStateVersion = "print-1" }
        val model = model(repository); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle()
        model.selectTaskBoardSection("in_progress"); advanceUntilIdle()
        val reads = repository.pages.size
        model.selectTaskBoardSection("todo"); advanceUntilIdle()
        assertEquals(reads, repository.pages.size)
        assertEquals(121, model.state.value.dailyWork.board.items.size)
        assertEquals(3, model.state.value.dailyWork.board.page)
        model.navigate(Destination.Inventory); advanceUntilIdle()
        model.navigate(Destination.Tasks); advanceUntilIdle()
        assertEquals(reads, repository.pages.size)
        assertEquals(121, model.state.value.dailyWork.board.items.size)
        assertEquals(2, repository.versionRequests)
        // A manual refresh still fetches fresh pages even when a version endpoint is unchanged.
        model.refreshDailyWork(); advanceUntilIdle()
        assertEquals(reads + 3, repository.pages.size)
    }

    @Test fun changedPrintTokenOnReturnInvalidatesTheCachedScopeAndRestoresFullDepth() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_SALES).apply { printStateVersion = "print-1" }
        val model = model(repository); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle()
        model.navigate(Destination.Inventory); advanceUntilIdle()
        repository.printStateVersion = "print-2"
        repository.sameVersionCardSuffix = "-new-scope"
        val reads = repository.pages.size
        model.navigate(Destination.Tasks); advanceUntilIdle()
        assertEquals(reads + 2, repository.pages.size)
        assertEquals(100, model.state.value.dailyWork.board.items.size)
        assertTrue(model.state.value.dailyWork.board.items.all { it.id.endsWith("-new-scope") })
        assertEquals("print-2", model.state.value.dailyWork.snapshot.printStateVersion)
    }

    @Test fun restoringColumnRejectsVersionDriftBeforePublishingAnyRows() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_SALES)
        val model = model(repository); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle()
        model.selectTaskBoardSection("in_progress"); advanceUntilIdle()
        repository.driftFromPage = 2
        model.selectTaskBoardSection("todo"); advanceUntilIdle()
        assertEquals(100, model.state.value.dailyWork.board.items.size)
        assertFalse(model.state.value.dailyWork.boardVerified)
        assertTrue(model.state.value.dailyWork.error.orEmpty().contains("任务已变化"))
        repository.driftFromPage = 0
        model.refreshDailyWork(); advanceUntilIdle()
        assertEquals(100, model.state.value.dailyWork.board.items.size)
    }

    @Test fun restoringColumnRejectsPrintTokenDriftBeforePublishingAnyRows() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_SALES).apply { printStateVersion = "print-1" }
        val model = model(repository); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle()
        assertEquals("print-1", repository.pages.last()["print_state_version"])
        model.selectTaskBoardSection("in_progress"); advanceUntilIdle()
        repository.printStateVersion = "print-2"
        repository.printDriftFromPage = 2
        model.selectTaskBoardSection("todo"); advanceUntilIdle()
        assertEquals(100, model.state.value.dailyWork.board.items.size)
        assertFalse(model.state.value.dailyWork.boardVerified)
        assertTrue(model.state.value.dailyWork.error.orEmpty().contains("打印状态已变化"))
        repository.printDriftFromPage = 0
        model.refreshDailyWork(); advanceUntilIdle()
        assertEquals(100, model.state.value.dailyWork.board.items.size)
        assertEquals("print-2", model.state.value.dailyWork.snapshot.printStateVersion)
    }

    @Test fun selectionAndExportCarryTheCurrentPrintToken() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_STOCK_PACKING).apply { printStateVersion = "print-1" }
        val model = model(repository); advanceUntilIdle()
        model.selectAllTaskBoard(); advanceUntilIdle()
        assertEquals("print-1", repository.selections.single().getString("expected_print_state_version"))
        assertEquals("print-1", model.state.value.dailyWork.boardSelection?.printStateVersion)
        model.clearTaskBoardSelection(); advanceUntilIdle()
        model.openTaskBoardItem("item-0"); advanceUntilIdle()
        assertEquals("print-1", model.state.value.dailyWork.boardDetail?.printStateVersion)
        model.exportTaskBoardImages(); advanceUntilIdle()
        assertEquals("print-1", repository.exports.single().getString("expected_print_state_version"))
        assertEquals("print-1", model.state.value.dailyWork.boardExport?.printStateVersion)
    }

    @Test fun refreshKeepsLoadedPagesAndDefersNewVersionUntilSelectionEnds() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository()
        val model = model(repository); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle()
        assertEquals(100, model.state.value.dailyWork.board.items.size)
        model.refreshDailyWork(); advanceUntilIdle()
        assertEquals(100, model.state.value.dailyWork.board.items.size)
        model.toggleTaskBoardItem("item-60")
        repository.changedVersion = true
        model.refreshDailyWork(); advanceUntilIdle()
        assertEquals(100, model.state.value.dailyWork.board.items.size)
        assertEquals(setOf("item-60"), model.state.value.dailyWork.boardSelectedIds)
        model.clearTaskBoardSelection(); advanceUntilIdle()
        assertEquals(100, model.state.value.dailyWork.board.items.size)
        assertEquals("data-2", model.state.value.dailyWork.board.dataVersion)
        assertTrue(model.state.value.dailyWork.boardSelectedIds.isEmpty())
    }

    @Test fun selectionStartedDuringRefreshKeepsItsOriginalPageUntilReleased() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository()
        val model = model(repository); advanceUntilIdle()
        repository.boardGate = CompletableDeferred()
        model.refreshDailyWork(); runCurrent()
        model.toggleTaskBoardItem("item-0")
        repository.changedVersion = true
        repository.boardGate?.complete(Unit)
        advanceUntilIdle()
        assertEquals("data-1", model.state.value.dailyWork.board.dataVersion)
        assertEquals(setOf("item-0"), model.state.value.dailyWork.boardSelectedIds)
        model.clearTaskBoardSelection(); advanceUntilIdle()
        assertEquals("data-2", model.state.value.dailyWork.board.dataVersion)
    }

    @Test fun sameVersionRefreshReplacesChangedCardIdsAndPrintLabelsAcrossLoadedPages() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_SALES)
        val model = model(repository); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle()
        repository.sameVersionCardSuffix = "-new-members"
        repository.currentPrintState = "reserved"
        model.refreshDailyWork(); advanceUntilIdle()
        val refreshed = model.state.value.dailyWork.board
        assertEquals("data-1", refreshed.dataVersion)
        assertEquals("task-1", refreshed.taskVersion)
        assertEquals(100, refreshed.items.size)
        assertTrue(refreshed.items.all { it.id.endsWith("-new-members") && it.printState == "reserved" })
        repository.currentPrintState = "uncertain"
        model.refreshDailyWork(); advanceUntilIdle()
        assertTrue(model.state.value.dailyWork.board.items.all { it.printState == "uncertain" })
    }

    @Test fun selectionFreezesSameVersionMembershipChangesUntilReleased() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_STOCK_PACKING)
        val model = model(repository); advanceUntilIdle()
        repository.boardGate = CompletableDeferred()
        model.refreshDailyWork(); runCurrent()
        model.toggleTaskBoardItem("item-0")
        repository.sameVersionCardSuffix = "-new-members"
        repository.currentPrintState = "reserved"
        repository.boardGate!!.complete(Unit); advanceUntilIdle()
        assertEquals("item-0", model.state.value.dailyWork.board.items.first().id)
        assertEquals("", model.state.value.dailyWork.board.items.first().printState)
        assertEquals(setOf("item-0"), model.state.value.dailyWork.boardSelectedIds)
        model.refreshDailyWork(); advanceUntilIdle()
        assertEquals("item-0", model.state.value.dailyWork.board.items.first().id)
        model.clearTaskBoardSelection(); advanceUntilIdle()
        assertEquals("item-0-new-members", model.state.value.dailyWork.board.items.first().id)
        assertEquals("reserved", model.state.value.dailyWork.board.items.first().printState)
        assertTrue(model.state.value.dailyWork.boardSelectedIds.isEmpty())
    }

    @Test fun twoColumnPollingRefreshesOtherPhonePrintChangesWithoutVersionBumps() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_STOCK_PACKING)
        val model = model(repository); advanceUntilIdle()
        model.dailyWorkVisible(true); runCurrent()
        repository.sameVersionCardSuffix = "-other-phone"
        repository.currentPrintState = "printed"
        advanceTimeBy(30_000); runCurrent()
        assertEquals("task-1", model.state.value.dailyWork.board.taskVersion)
        assertEquals("item-0-other-phone", model.state.value.dailyWork.board.items.first().id)
        assertEquals("printed", model.state.value.dailyWork.board.items.first().printState)
        model.dailyWorkVisible(false); advanceUntilIdle()
    }

    @Test fun tokenAwarePollingAvoidsUnchangedReadsAndDetectsChangedPrintFacts() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_STOCK_PACKING).apply { printStateVersion = "print-1" }
        val model = model(repository); advanceUntilIdle()
        model.dailyWorkVisible(true); runCurrent()
        val previousReads = repository.pages.size
        advanceTimeBy(30_000); runCurrent()
        assertEquals(previousReads, repository.pages.size)
        repository.printStateVersion = "print-2"
        repository.sameVersionCardSuffix = "-new-print-facts"
        advanceTimeBy(30_000); runCurrent()
        assertEquals(previousReads + 1, repository.pages.size)
        assertEquals("print-2", model.state.value.dailyWork.snapshot.printStateVersion)
        assertEquals("item-0-new-print-facts", model.state.value.dailyWork.board.items.first().id)
        model.dailyWorkVisible(false); advanceUntilIdle()
    }

    @Test fun printingKeepsItsFrozenOrderRangeDuringSameVersionMembershipChange() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_ARRIVAL_PACKING, count = 2)
        val printer = BoardPrinter().apply { sendGate = CompletableDeferred() }
        val model = model(repository, printer); advanceUntilIdle()
        model.selectAllTaskBoard(); advanceUntilIdle()
        val frozen = model.state.value.dailyWork.boardSelection!!
        model.printTaskBoardSelection(null); runCurrent()
        model.confirmStockPackingPrint(false, false); runCurrent()
        assertTrue(model.state.value.dailyWork.boardPrinting)
        repository.sameVersionCardSuffix = "-changed-during-print"
        repository.currentPrintState = "printed"
        model.refreshDailyWork(); runCurrent()
        assertEquals("item-0", model.state.value.dailyWork.board.items.first().id)
        assertEquals(frozen, model.state.value.dailyWork.boardSelection)
        printer.sendGate!!.complete(Unit); advanceUntilIdle()
        assertEquals(listOf(2), repository.printedSizes)
        assertEquals(1, repository.selections.size)
        assertEquals("item-0-changed-during-print", model.state.value.dailyWork.board.items.first().id)
        assertFalse(model.state.value.dailyWork.boardPrinting)
    }

    @Test fun selectAllFreezesCompleteFilterNotOnlyTheFirst50() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository()
        val model = model(repository); advanceUntilIdle()
        model.setTaskBoardFilters("waiting", "mine", "2026-10-01", "2026-10-31", "u1"); advanceUntilIdle()
        model.searchTaskBoard("FB1157"); advanceUntilIdle()
        model.selectAllTaskBoard(); advanceUntilIdle()
        val request = repository.selections.single()
        assertTrue(request.getBoolean("all"))
        assertEquals("waiting", request.getString("status"))
        assertEquals("2026-10-31", request.getString("due_to"))
        assertEquals("u1", request.getString("assignee_id"))
        assertEquals("FB1157", request.getString("keyword"))
        assertEquals(121, model.state.value.dailyWork.boardSelection?.objectCount)
        assertTrue(model.state.value.dailyWork.boardSelectionAll)
    }

    @Test fun imageExportReadsVersionedSnapshotWithoutChangingSelectionOrPrinting() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository()
        val model = model(repository); advanceUntilIdle()
        model.loadMoreTaskBoard(); advanceUntilIdle()
        model.toggleTaskBoardItem("item-60")
        model.exportTaskBoardImages(); advanceUntilIdle()
        assertEquals(listOf("item-60"), repository.exports.single().getJSONArray("ids").let { array ->
            (0 until array.length()).map(array::getString)
        })
        assertEquals("data-1", repository.exports.single().getString("expected_data_version"))
        assertEquals(1, model.state.value.dailyWork.boardExport?.items?.size)
        assertEquals(setOf("item-60"), model.state.value.dailyWork.boardSelectedIds)
        assertTrue(repository.printedSizes.isEmpty())
    }

    @Test fun offlineBatchPersistsBeforeSelectionAndRetriesExactBodyAfterRestart() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository()
        val first = model(repository); advanceUntilIdle()
        first.toggleTaskBoardItem("item-0")
        repository.failSelection = true
        first.submitTaskBoardBatch("followup", "已催到货", "", "采购", "2026-10-06", "2026-10-07")
        advanceUntilIdle()
        val stored = first.state.value.dailyWork.pendingActions.single()
        assertTrue(JSONObject(stored.frozenRequest).has("selection_request"))
        assertFalse(stored.requiresReview)
        stores.last().clear()
        val restored = BoardRepository().apply { failBatch = true }
        val second = model(restored); advanceUntilIdle()
        val frozen = JSONObject(second.state.value.dailyWork.pendingActions.single().frozenRequest)
        assertEquals(50, second.state.value.dailyWork.board.items.size)
        assertNull(second.state.value.dailyWork.boardSelection)
        assertEquals(ROLE_PURCHASE, second.state.value.dailyWork.selectedRole)
        assertEquals("selection-1", frozen.getString("selection_id"))
        assertFalse(frozen.has("selection_request"))
        assertEquals(stored.idempotencyKey, frozen.getString("idempotency_key"))
        restored.failBatch = false
        second.retryDailyActions(); advanceUntilIdle()
        assertTrue(second.state.value.dailyWork.pendingActions.isEmpty())
        assertEquals(50, second.state.value.dailyWork.board.items.size)
        assertNull(second.state.value.dailyWork.boardSelection)
        assertEquals(1, restored.selections.size)
        assertEquals(restored.batches[0].toString(), restored.batches[1].toString())
    }

    @Test fun detailKeepsAllRowsAndSelectsOnlyExplicitTaskIds() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository()
        val model = model(repository); advanceUntilIdle()
        model.openTaskBoardItem("item-0"); advanceUntilIdle()
        assertEquals(121, model.state.value.dailyWork.boardDetail?.lines?.size)
        model.toggleTaskBoardLine("task-120")
        model.submitTaskBoardBatch("start", "", "", "", "", ""); advanceUntilIdle()
        assertEquals("task-120", repository.selections.single().getJSONArray("task_ids").getString(0))
        assertFalse(model.state.value.dailyWork.boardSelectionAll)
    }

    @Test fun restoredBatchDoesNotSelectOrdersInAnotherRole() = runTest(StandardTestDispatcher(scheduler)) {
        val previousRepository = BoardRepository(role = ROLE_ARRIVAL_PACKING).apply { failSelection = true }
        val previous = model(previousRepository); advanceUntilIdle()
        previous.toggleTaskBoardItem("item-0")
        previous.submitTaskBoardBatch("start", "", "", "", "", ""); advanceUntilIdle()
        val pending = previous.state.value.dailyWork.pendingActions.single()
        stores.last().clear()
        val currentRepository = BoardRepository(role = ROLE_PURCHASE)
        val current = model(currentRepository); advanceUntilIdle()
        val request = currentRepository.selections.single()
        assertEquals(ROLE_ARRIVAL_PACKING, request.getString("role"))
        assertEquals("item-0", request.getJSONArray("ids").getString(0))
        assertEquals(pending.idempotencyKey, currentRepository.batches.single().getString("idempotency_key"))
        assertEquals(ROLE_PURCHASE, current.state.value.dailyWork.selectedRole)
        assertEquals(50, current.state.value.dailyWork.board.items.size)
        assertNull(current.state.value.dailyWork.boardSelection)
        assertTrue(current.state.value.dailyWork.boardSelectedIds.isEmpty())
        assertTrue(current.state.value.dailyWork.pendingActions.isEmpty())
    }

    @Test fun printingSegments100CompleteOrdersAndStopsAtUnknown() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_ARRIVAL_PACKING, count = 250)
        val printer = BoardPrinter(unknownAt = 2)
        val model = model(repository, printer); advanceUntilIdle()
        model.selectAllTaskBoard(); advanceUntilIdle()
        model.printTaskBoardSelection(null); advanceUntilIdle()
        model.confirmStockPackingPrint(false, false); advanceUntilIdle()
        assertEquals(listOf(100, 100), repository.printedSizes)
        assertEquals(2, printer.sends)
        assertTrue(model.state.value.uncertainPrint)
        assertEquals(listOf("submitted", "unknown"), repository.reports)
        assertFalse(model.state.value.dailyWork.boardPrinting)
    }

    @Test fun labelCapacityRejectionShrinksOnlyUncreatedSegments() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_ARRIVAL_PACKING, count = 121).apply { labelCapacity = 50 }
        val printer = BoardPrinter()
        val model = model(repository, printer); advanceUntilIdle()
        model.selectAllTaskBoard(); advanceUntilIdle()
        model.printTaskBoardSelection(null); advanceUntilIdle()
        model.confirmStockPackingPrint(false, false); advanceUntilIdle()
        assertEquals(listOf(50, 50, 21), repository.printedSizes)
        assertEquals(3, printer.sends)
        assertFalse(model.state.value.uncertainPrint)
    }

    @Test fun cancellationAfterOneSentSegmentDoesNotPrepareTheNextSegment() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(role = ROLE_ARRIVAL_PACKING, count = 250)
        val printer = BoardPrinter()
        val model = model(repository, printer); advanceUntilIdle()
        printer.afterSend = model::cancelPrint
        model.selectAllTaskBoard(); advanceUntilIdle()
        model.printTaskBoardSelection(null); advanceUntilIdle()
        model.confirmStockPackingPrint(false, false); advanceUntilIdle()
        assertEquals(listOf(100), repository.printedSizes)
        assertEquals(1, printer.sends)
        assertFalse(model.state.value.uncertainPrint)
    }

    @Test fun pendingBatchesStayIsolatedFromOtherUsersAndServers() = runTest(StandardTestDispatcher(scheduler)) {
        val firstRepository = BoardRepository().apply { failBatch = true }
        val first = model(firstRepository); advanceUntilIdle()
        first.toggleTaskBoardItem("item-0")
        first.submitTaskBoardBatch("followup", "已催到货", "", "", "", ""); advanceUntilIdle()
        assertEquals(1, first.state.value.dailyWork.pendingActions.size)
        stores.last().clear()
        val otherUser = BoardRepository(userId = "u2")
        val second = model(otherUser); advanceUntilIdle()
        assertTrue(otherUser.batches.isEmpty())
        assertTrue(second.state.value.dailyWork.pendingActions.isEmpty())
        stores.last().clear()
        app.getSharedPreferences("workbench-settings-v1", 0).edit().putString("server", "https://other.example.com").commit()
        val otherServer = BoardRepository()
        model(otherServer); advanceUntilIdle()
        assertTrue(otherServer.batches.isEmpty())
        stores.last().clear()
        app.getSharedPreferences("workbench-settings-v1", 0).edit().putString("server", "https://ul.yingyinghome.com:8443").commit()
        val original = BoardRepository()
        val fourth = model(original); advanceUntilIdle()
        assertEquals(1, original.batches.size)
        assertTrue(fourth.state.value.dailyWork.pendingActions.isEmpty())
    }

    @Test fun customerOwnerConflictKeepsFrozenCustomerScopeAndDoesNotRetryWithNewVersion() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(admin = true)
        repository.ownerFailure = ServiceException("task_version_changed", "客户归属已变化", 409)
        val model = model(repository); advanceUntilIdle()
        model.saveCustomerOwner(CustomerOwnerDraft("profile-A", "客户甲", 3, "sales-2", "确认固定销售"))
        advanceUntilIdle()
        val request = repository.ownerRequests.single().first
        assertEquals("customer", request.getString("scope"))
        assertEquals("profile-A", request.getString("profile_id"))
        assertEquals("客户甲", request.getString("customer"))
        assertEquals(3, request.getInt("version"))
        assertFalse(request.has("deputy_id"))
        val retained = model.state.value.dailyWork.pendingActions.single()
        assertTrue(retained.requiresReview)
        assertEquals(request.toString(), retained.frozenRequest)
        model.retryDailyActions(); advanceUntilIdle()
        assertEquals(1, repository.ownerRequests.size)
        assertEquals(0, model.state.value.dailyWork.customerOwnersSaveRevision)
    }

    @Test fun customerDeputyIsSeparateFromOwnerAndOfflineRetryReusesExactRequest() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(admin = true)
        repository.ownerFailure = ConnectException("offline")
        val model = model(repository); advanceUntilIdle()
        model.saveCustomerOwner(CustomerOwnerDraft("profile-A", "客户甲", 4, "sales-3", "请假代办", true, "2026-10-20"))
        advanceUntilIdle()
        val request = repository.ownerRequests.single()
        assertTrue(request.second)
        assertFalse(request.first.has("owner_id"))
        assertEquals("sales-3", request.first.getString("deputy_id"))
        assertEquals("2026-10-20", request.first.getString("deputy_until"))
        assertEquals(1, model.state.value.dailyWork.pendingActions.size)
        repository.ownerFailure = null
        model.retryDailyActions(); advanceUntilIdle()
        assertEquals(request.first.toString(), repository.ownerRequests.last().first.toString())
        assertTrue(model.state.value.dailyWork.pendingActions.isEmpty())
        assertEquals(1, model.state.value.dailyWork.customerOwnersSaveRevision)
    }

    @Test fun salesRuleCannotBeWrittenThroughOldPhoneSettingsPath() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = BoardRepository(admin = true)
        val model = model(repository); advanceUntilIdle()
        model.saveAssignmentRule(ROLE_SALES, "客户甲", "sales-2"); advanceUntilIdle()
        assertEquals(0, repository.ruleWrites)
        model.saveAssignmentRule(ROLE_PURCHASE, "LS001", "buyer"); advanceUntilIdle()
        assertEquals(1, repository.ruleWrites)
    }

    private class BoardRepository(val role: String = ROLE_PURCHASE, val count: Int = 121, val userId: String = "u1",
        val admin: Boolean = false) : WorkbenchRepository {
        val pages = mutableListOf<Map<String, String>>()
        val selections = mutableListOf<JSONObject>()
        val batches = mutableListOf<JSONObject>()
        val printedSizes = mutableListOf<Int>()
        val reports = mutableListOf<String>()
        val exports = mutableListOf<JSONObject>()
        val ownerRequests = mutableListOf<Pair<JSONObject, Boolean>>()
        var ownerFailure: Exception? = null
        var ruleWrites = 0
        override suspend fun customerOwners() = listOf(CustomerOwner("profile-A", "客户甲", "sales-2", "销售二", version = 4))
        override suspend fun saveCustomerOwner(frozenRequest: String, deputy: Boolean) {
            ownerRequests += JSONObject(frozenRequest) to deputy
            ownerFailure?.let { throw it }
        }
        override suspend fun saveAssignmentRule(role: String, matchValue: String, staffId: String): List<AssignmentRule> {
            ruleWrites++
            return emptyList()
        }
        var legacyRequests = 0
        var labelCapacity = 100
        var failBoard = false; var failSelection = false; var failBatch = false; var changedVersion = false
        var driftFromPage = 0
        var sameVersionCardSuffix = ""
        var currentPrintState = ""
        var printStateVersion = ""
        var printDriftFromPage = 0
        var versionRequests = 0
        var boardGate: CompletableDeferred<Unit>? = null
        override suspend fun health() = ServiceHealth("v1", "10-05 08:00", count, 1)
        override suspend fun me() = CurrentUser(userId, userId, "员工", listOf(role), isAdmin = admin)
        override suspend fun dailyWorkVersion(): DailyWorkVersion {
            versionRequests++
            return DailyWorkVersion("2026-10-05", if (changedVersion) "data-2" else "data-1", "task-1", true,
                printStateVersion = printStateVersion)
        }
        override suspend fun dailyWork(role: String, view: String): DailyWorkSnapshot { legacyRequests++; error("No legacy requests") }
        override suspend fun taskBoard(parameters: Map<String, String>): TaskBoardPage {
            pages += parameters.toMap()
            boardGate?.await()
            if (failBoard) throw ConnectException("offline")
            val page = parameters.getValue("page").toInt()
            val rows = (0 until count).map { TaskBoardItem("item-$it$sameVersionCardSuffix", if (role == ROLE_PURCHASE) "purchase" else "packing", role,
                "FB$it", quantity = "10", orderCount = 1, canAct = true, version = 1, printState = currentPrintState, canPrint = true) }
            return TaskBoardPage(rows.drop((page - 1) * 50).take(50), count, page, page * 50 < count,
                if (changedVersion || driftFromPage > 0 && page >= driftFromPage) "data-2" else "data-1",
                "task-1", "10-05 08:00", true, "2026-10-05",
                printStateVersion = if (printDriftFromPage > 0 && page >= printDriftFromPage) "$printStateVersion-drift" else printStateVersion)
        }
        override suspend fun taskBoardDetail(id: String, parameters: Map<String, String>) = TaskBoardDetail(
            TaskBoardItem(id, "packing", role, "FB1157", canAct = true),
            (0 until count).map { TaskBoardLine("task-$it", "task-$it", 1, "PO#$it", canAct = true, canPrint = true) },
            dataVersion = "data-1", taskVersion = "task-1", printStateVersion = printStateVersion)
        override suspend fun taskBoardSelection(frozenRequest: String): TaskBoardSelection {
            if (failSelection) throw ConnectException("offline")
            selections += JSONObject(frozenRequest)
            return TaskBoardSelection("selection-1", count, (0 until count).map { "PO#$it" }, "data-1", "task-1", role, "profile",
                printStateVersion = printStateVersion)
        }
        override suspend fun exportTaskBoardSnapshot(frozenRequest: String): BoardExportSnapshot {
            val request = JSONObject(frozenRequest)
            exports += request
            val ids = request.getJSONArray("ids")
            return BoardExportSnapshot("snapshot-1", "10-06 08:00", "data-1", "task-1",
                (0 until ids.length()).map { index -> BoardExportCard(
                    TaskBoardItem(ids.getString(index), "purchase", role, "FB1157", canAct = true),
                    emptyList(), listOf(BoardExportOrder("PO#1", listOf(BoardExportLine("PO#1", "客户", "LS0001",
                        "商品", "1", "件", "A01", ""))))
                ) }, printStateVersion = printStateVersion)
        }
        override suspend fun taskBoardBatch(frozenRequest: String) {
            batches += JSONObject(frozenRequest)
            if (failBatch) throw ConnectException("offline")
        }
        override suspend fun query(destination: Destination, q: String, mode: String, scope: String, logistics: String,
            page: Int, inventoryFilters: InventoryFilters, expectedDataVersion: String) =
            QueryResult(emptyList(), 0, 1, null, null, null, "", "data-1", "")
        override suspend fun packingPrintPreview(frozenRequest: String): PackingPrintPreview {
            val rows = (0 until count).map { PackingPrintOrder("profile", "PO#$it", "unprinted", "r$it", "f$it", labelCount = 1) }
            return PackingPrintPreview("preview-1", "a".repeat(64), count, count, 0, 0, rows,
                rows.chunked(minOf(100, labelCapacity)).mapIndexed { i, segment -> PackingPrintSegment(i + 1, segment.map { it.orderNo }, segment.size, segment.size) })
        }
        override suspend fun replayPrepare(frozenRequest: String): PreparedPrint {
            val index = JSONObject(frozenRequest).getInt("segment_index")
            val size = minOf(minOf(100, labelCapacity), count - (index - 1) * minOf(100, labelCapacity))
            printedSizes += size
            return PreparedPrint("print-${printedSizes.size}", size)
        }
        override suspend fun prepare(state: WorkbenchState, all: Boolean, maximum: Int?): PreparedPrint {
            if (state.items.size > labelCapacity) throw ServiceException("invalid_request", "拆箱后超过100张，请缩小范围分批打印", 400)
            printedSizes += state.items.size
            return PreparedPrint("print-${printedSizes.size}", state.items.size)
        }
        override suspend fun claim(id: String, actionKey: String) = ClaimedPrint("token", listOf(byteArrayOf(1)))
        override suspend fun report(id: String, token: String, status: String, message: String, bytes: Int) { reports += status }
    }
    private class BoardPrinter(val unknownAt: Int = 0) : PrinterClient {
        var sends = 0
        var sendGate: CompletableDeferred<Unit>? = null
        var afterSend: () -> Unit = {}
        override val state: StateFlow<PrinterState> = MutableStateFlow(PrinterState(connected = true))
        override fun scan() = Unit; override fun connect(address: String) = Unit; override fun disconnect() = Unit
        override fun close() = Unit; override fun cancelSending() = Unit
        override fun acknowledgeUnknownAttempt() = true
        override suspend fun sendPages(pages: List<ByteArray>, shouldCancel: () -> Boolean): PrintOutcome {
            sends++
            sendGate?.await()
            afterSend()
            return PrintOutcome(if (sends == unknownAt) "unknown" else "submitted", 1, "test")
        }
    }
}
