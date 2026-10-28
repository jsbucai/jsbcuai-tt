package com.ul.workbench

import android.app.Application
import androidx.lifecycle.ViewModelStore
import androidx.test.core.app.ApplicationProvider
import com.ul.workbench.data.*
import com.ul.workbench.device.*
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
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
class TwoStageTaskBoardTest {
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
    private fun model(repository: StageRepository) = WorkbenchViewModel(app, repository, QuietPrinter()).also {
        stores += ViewModelStore().also { store -> store.put("stage", it) }
    }

    @Test fun parserReadsStageCountsNotesAndPrintFactsWithoutChangingLegacyStatus() {
        val parsed = HttpWorkbenchRepository.parseTaskBoard(JSONObject("""{
          "items":[{"id":"phase:in_progress:product-1","role":"stock_packing","status":"waiting",
            "work_state":"in_progress","has_note":true,"latest_note":"等待回复",
            "print_state":"uncertain","work_label":"打印待核对"}],
          "summary":{"work_state_counts":{"todo":8,"in_progress":3}},"print_state_version":"print-1"
        }"""))
        assertEquals(mapOf("todo" to 8, "in_progress" to 3), parsed.workStateCounts)
        assertEquals("print-1", parsed.printStateVersion)
        assertEquals("print-1", HttpWorkbenchRepository.parseDailyWorkVersion(JSONObject("""{"print_state_version":"print-1"}""")).printStateVersion)
        assertEquals("print-1", HttpWorkbenchRepository.parseDailyWork(JSONObject("""{"print_state_version":"print-1"}""")).printStateVersion)
        assertEquals("waiting", parsed.items.single().status)
        assertEquals("in_progress", parsed.items.single().workState)
        assertEquals("打印待核对", parsed.items.single().workLabel)
        assertTrue(parsed.items.single().hasNote)
        val detail = HttpWorkbenchRepository.parseTaskBoardDetail(JSONObject("""{
          "item":{"id":"group-1"},"notes":[{"note":"以前跟进","created_at":"2026-10-05T08:00:00+08:00",
            "actor_id":"sales-1","event_type":"note"}]
        }"""))
        assertEquals("以前跟进", detail.notes.single().note)
        assertFalse(HttpWorkbenchRepository.parseTaskBoardItem(JSONObject("""{"id":"legacy"}""")).hasNote)
        val history = HttpWorkbenchRepository.parseSalesHistoryDetail(JSONObject("""{
          "item":{"group_id":"group-1"},"events":[{"id":"started","event_type":"start"},
            {"id":"reset","event_type":"reset_to_todo"},{"id":"note","event_type":"note","note":"说明"},
            {"id":"legacy","note":"旧跟进"}]
        }"""))
        assertEquals(listOf("开始处理", "退回待处理", "备注", "销售跟进"), history.events.map { it.title })
    }

    @Test fun twoStageRolesDefaultToTodoAndRememberTheirSeparateTabs() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = StageRepository()
        val model = model(repository); advanceUntilIdle()
        assertEquals("todo", model.state.value.dailyWork.boardSection)
        assertEquals("todo", repository.pages.last()["work_state"])
        model.selectTaskBoardSection("in_progress"); advanceUntilIdle()
        model.selectDailyRole(ROLE_STOCK_PACKING); advanceUntilIdle()
        assertEquals("todo", model.state.value.dailyWork.boardSection)
        model.selectDailyRole(ROLE_SALES); advanceUntilIdle()
        assertEquals("in_progress", model.state.value.dailyWork.boardSection)
        model.selectDailyRole(ROLE_PURCHASE); advanceUntilIdle()
        assertEquals("active", model.state.value.dailyWork.boardSection)
        assertFalse(repository.pages.last().containsKey("work_state"))
        model.setTaskBoardFilters("all", "role", "2026-10-01", "2026-10-31", "buyer-1"); advanceUntilIdle()
        model.selectDailyRole(ROLE_ARRIVAL_PACKING); advanceUntilIdle()
        assertEquals("", repository.pages.last()["assignee_id"])
        assertEquals("", repository.pages.last()["due_to"])
        model.selectDailyRole(ROLE_PURCHASE); advanceUntilIdle()
        model.selectTaskBoardSection("processed"); advanceUntilIdle()
        assertEquals("processed", repository.historyScopes.last())
        model.selectDailyRole(ROLE_ARRIVAL_PACKING); advanceUntilIdle()
        assertEquals("todo", model.state.value.dailyWork.boardSection)
        assertEquals("todo", repository.pages.last()["work_state"])
    }

    @Test fun noteIsAnIndependentActionAndDoesNotMoveTodoTask() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = StageRepository()
        val model = model(repository); advanceUntilIdle()
        model.saveTaskBoardSalesNote("group-1", " 已联系客户 "); advanceUntilIdle()
        val request = repository.actions.single()
        assertEquals("note", request.getString("action"))
        assertEquals("data-1", request.getString("expected_data_version"))
        assertEquals("task-1", request.getString("expected_task_version"))
        assertEquals("todo", model.state.value.dailyWork.board.items.single().workState)
        assertEquals("已联系客户", model.state.value.dailyWork.board.items.single().latestNote)
        assertEquals(1, model.state.value.dailyWork.salesActionRevision)
    }

    @Test fun startWaitsForConfirmationAndUndoUsesConfirmedVersion() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = StageRepository().apply { actionGate = CompletableDeferred() }
        val model = model(repository); advanceUntilIdle()
        model.startTaskBoardSales("group-1"); runCurrent()
        model.startTaskBoardSales("group-1"); runCurrent()
        assertEquals(1, repository.actions.size)
        assertEquals("todo", model.state.value.dailyWork.board.items.single().workState)
        repository.actionGate!!.complete(Unit); runCurrent()
        assertTrue(model.state.value.dailyWork.board.items.isEmpty())
        assertEquals("group-1", model.state.value.dailyWork.undoSalesStartGroupId)
        assertFalse(repository.hasNote)
        model.undoTaskBoardSalesStart(); runCurrent()
        val undo = repository.actions.last()
        assertEquals("reset_to_todo", undo.getString("action"))
        assertEquals(2, undo.getInt("version"))
        assertEquals("task-2", undo.getString("expected_task_version"))
        assertNotEquals(repository.actions.first().getString("idempotency_key"), undo.getString("idempotency_key"))
        assertEquals("todo", model.state.value.dailyWork.board.items.single().workState)
        assertEquals("", model.state.value.dailyWork.undoSalesStartGroupId)
    }

    @Test fun offlineNoteSurvivesRestartAndReusesExactFrozenScope() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = StageRepository().apply { failure = ConnectException("offline") }
        val first = model(repository); advanceUntilIdle()
        first.saveTaskBoardSalesNote("group-1", "请保留这条说明"); advanceUntilIdle()
        val pending = first.state.value.dailyWork.pendingActions.single()
        assertFalse(pending.requiresReview)
        assertEquals("todo", first.state.value.dailyWork.board.items.single().workState)
        stores.last().clear()
        val restored = StageRepository()
        val second = model(restored); advanceUntilIdle()
        assertEquals(pending.frozenRequest, restored.actions.single().toString())
        assertTrue(second.state.value.dailyWork.pendingActions.isEmpty())
        assertEquals("请保留这条说明", second.state.value.dailyWork.board.items.single().latestNote)
    }

    @Test fun conflictKeepsInputAndNeverRefreshesItsVersionForRetry() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = StageRepository().apply { failure = ServiceException("task_version_changed", "版本已变", 409) }
        val model = model(repository); advanceUntilIdle()
        model.saveTaskBoardSalesNote("group-1", "冲突前输入"); advanceUntilIdle()
        val pending = model.state.value.dailyWork.pendingActions.single()
        assertTrue(pending.requiresReview)
        assertEquals("冲突前输入", pending.note)
        repository.failure = null
        model.retryDailyActions(); advanceUntilIdle()
        assertEquals(1, repository.actions.size)
        assertEquals(pending.frozenRequest, model.state.value.dailyWork.pendingActions.single().frozenRequest)
    }

    @Test fun detailSelectionAndExportKeepThePhaseScope() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = StageRepository()
        val model = model(repository); advanceUntilIdle()
        model.selectDailyRole(ROLE_STOCK_PACKING); advanceUntilIdle()
        model.selectAllTaskBoard(); advanceUntilIdle()
        assertEquals("todo", repository.selections.single().getString("work_state"))
        model.clearTaskBoardSelection(); advanceUntilIdle()
        model.openTaskBoardItem("phase:todo:product-1"); advanceUntilIdle()
        assertEquals("todo", repository.detailParameters.last()["work_state"])
        model.exportTaskBoardImages(); advanceUntilIdle()
        assertEquals("todo", repository.exports.single().getString("work_state"))
        assertEquals("phase:todo:product-1", repository.exports.single().getJSONArray("ids").getString(0))
    }

    @Test fun readonlyCustomerCannotQueueAnAction() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = StageRepository().apply { canAct = false }
        val model = model(repository); advanceUntilIdle()
        model.startTaskBoardSales("group-1"); advanceUntilIdle()
        assertTrue(repository.actions.isEmpty())
        assertTrue(model.state.value.dailyWork.pendingActions.isEmpty())
    }

    private class StageRepository : WorkbenchRepository {
        val pages = mutableListOf<Map<String, String>>()
        val detailParameters = mutableListOf<Map<String, String>>()
        val actions = mutableListOf<JSONObject>()
        val selections = mutableListOf<JSONObject>()
        val exports = mutableListOf<JSONObject>()
        val historyScopes = mutableListOf<String>()
        var stage = "todo"; var version = 1; var taskVersion = 1
        var hasNote = false; var latestNote = ""; var canAct = true
        var failure: Exception? = null
        var actionGate: CompletableDeferred<Unit>? = null
        override suspend fun health() = ServiceHealth("test", "", 1, 1)
        override suspend fun me() = CurrentUser("sales-1", roles = DAILY_WORK_ROLES)
        private fun item(role: String, selected: String) = TaskBoardItem(
            if (role == ROLE_SALES) "group-1" else "phase:$selected:product-1", "packing", role, "订单",
            canAct = canAct, version = version, workState = if (role == ROLE_SALES) stage else selected,
            hasNote = hasNote, latestNote = latestNote)
        override suspend fun taskBoard(parameters: Map<String, String>): TaskBoardPage {
            pages += parameters.toMap()
            val role = parameters.getValue("role")
            val selected = parameters["work_state"] ?: "todo"
            val rows = if (role == ROLE_SALES && stage != selected) emptyList() else listOf(item(role, selected))
            return TaskBoardPage(rows, rows.size, dataVersion = "data-1", taskVersion = "task-$taskVersion",
                workStateCounts = mapOf("todo" to if (stage == "todo") 1 else 0, "in_progress" to if (stage == "in_progress") 1 else 0))
        }
        override suspend fun taskBoardDetail(id: String, parameters: Map<String, String>): TaskBoardDetail {
            detailParameters += parameters.toMap()
            return TaskBoardDetail(item(parameters.getValue("role"), parameters["work_state"] ?: "todo"),
                dataVersion = "data-1", taskVersion = "task-$taskVersion")
        }
        override suspend fun salesGroupAction(id: String, frozenRequest: String): SalesGroupActionResult {
            val body = JSONObject(frozenRequest)
            actions += body
            actionGate?.await()
            failure?.let { throw it }
            assertEquals("group-1", id)
            assertEquals(version, body.getInt("version"))
            when (body.getString("action")) {
                "start" -> stage = "in_progress"
                "reset_to_todo" -> stage = "todo"
                "note" -> { hasNote = true; latestNote = body.getString("note") }
            }
            version++; taskVersion++
            return SalesGroupActionResult(version, "task-$taskVersion", stage, hasNote, latestNote)
        }
        override suspend fun taskHistory(role: String, view: String, scope: String, page: Int): TaskHistoryPage {
            historyScopes += scope; return TaskHistoryPage()
        }
        override suspend fun taskBoardSelection(frozenRequest: String): TaskBoardSelection {
            selections += JSONObject(frozenRequest)
            return TaskBoardSelection("selection", 1, listOf("ORDER#1"), "data-1", "task-$taskVersion", ROLE_STOCK_PACKING, "profile")
        }
        override suspend fun exportTaskBoardSnapshot(frozenRequest: String): BoardExportSnapshot {
            exports += JSONObject(frozenRequest)
            return BoardExportSnapshot("snapshot", "", "data-1", "task-$taskVersion", emptyList())
        }
        override suspend fun query(destination: Destination, q: String, mode: String, scope: String, logistics: String,
            page: Int, inventoryFilters: InventoryFilters, expectedDataVersion: String) =
            QueryResult(emptyList(), 0, 1, null, null, null, "", "data-1", "")
        override suspend fun prepare(state: WorkbenchState, all: Boolean, maximum: Int?) = error("unused")
        override suspend fun claim(id: String, actionKey: String) = error("unused")
        override suspend fun report(id: String, token: String, status: String, message: String, bytes: Int) = Unit
    }
    private class QuietPrinter : PrinterClient {
        override val state: StateFlow<PrinterState> = MutableStateFlow(PrinterState())
        override fun scan() = Unit
        override fun connect(address: String) = Unit
        override fun disconnect() = Unit
        override fun close() = Unit
        override fun cancelSending() = Unit
        override fun acknowledgeUnknownAttempt() = true
        override suspend fun sendPages(pages: List<ByteArray>, shouldCancel: () -> Boolean) = error("unused")
    }
}
