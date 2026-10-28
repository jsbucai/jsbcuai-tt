package com.ul.workbench

import android.app.Application
import androidx.lifecycle.ViewModelStore
import androidx.test.core.app.ApplicationProvider
import com.ul.workbench.data.*
import com.ul.workbench.device.PrintOutcome
import com.ul.workbench.device.PrinterClient
import com.ul.workbench.device.PrinterState
import com.ul.workbench.ui.screens.canSubmitDailyAction
import com.ul.workbench.ui.screens.dailyActionChoices
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestCoroutineScheduler
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.net.ConnectException

@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
class DailyWorkFeatureTest {
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

    @Test fun dailyWorkParserAcceptsBackendSummaryAliasesAndNumericPriority() {
        val snapshot = HttpWorkbenchRepository.parseDailyWork(JSONObject("""{
            "business_date":"2026-10-04","data_version":"data-1","task_version":"task-2","stale":false,"assignment_enabled":false,
            "task_data_version":"data-0","task_source_updated_at":"2026-10-04T08:00:00+08:00","tasks_current":false,
            "roles":["sales","stock_packing"],
            "summary":{"todo":3,"in_progress":2,"waiting":1,"actions_completed_today":4,
                "business_completed_today":5,"packed_waiting":6,"unassigned":1,"overdue":2},
            "items":[{"id":"task-1","version":7,"role":"stock_packing","title":"包装订单 A#1",
                "status":"todo","priority":9,"order_nos":["A#1"],"assignee":{"id":"u1","display_name":"小王"},
                "payload":{"read_only":true,"task_type":"arrival_shortage_waiting",
                    "missing_items":[{"sku":"FB1157","product_id":"p1","shortage":"4","pending":"2"}],
                    "linked_purchase_business_key":"purchase:FB1157"}}],
            "completed_items":[{"id":"done-1","version":2,"role":"stock_packing","title":"订单已发货",
                "status":"business_completed","business_completed_at":"2026-10-04T09:00:00+08:00"}],
            "today_events":[{"id":"e1","task_id":"task-1","event_type":"wait","title":"跟进订单","note":"等待回复",
                "created_at":"2026-10-04T08:30:00+08:00"}],
            "staff_load":[{"user_id":"u1","display_name":"小王","roles":["stock_packing"],"on_duty":true,
                "packing_limit":0,"todo":1,"in_progress":2,"waiting":3,"packed":1,"total":7}]
        }"""))
        assertEquals(4, snapshot.summary.todayActions)
        assertEquals(5, snapshot.summary.businessCompleted)
        assertEquals(6, snapshot.summary.packedWaiting)
        assertEquals(9, snapshot.items.single().priority)
        assertEquals("小王", snapshot.items.single().assigneeName)
        assertEquals(listOf("A#1"), snapshot.items.single().orderNos)
        assertTrue(snapshot.items.single().readOnly)
        assertEquals("arrival_shortage_waiting", snapshot.items.single().taskType)
        assertEquals(DailyMissingItem("FB1157", "p1", "4", "2"), snapshot.items.single().missingItems.single())
        assertEquals("purchase:FB1157", snapshot.items.single().linkedPurchaseBusinessKey)
        assertEquals(false, snapshot.assignmentEnabled)
        assertEquals(false, snapshot.tasksCurrent)
        assertEquals("data-0", snapshot.taskDataVersion)
        assertEquals("wait", snapshot.todayEvents.single().eventType)
        assertEquals("done-1", snapshot.completedItems.single().id)
        assertEquals(0, snapshot.staffLoad.single().packingLimit)
        assertEquals(0, HttpWorkbenchRepository.parseUser(JSONObject(
            """{"id":"u","username":"u","display_name":"U","packing_limit":0}""")).packingLimit)
        assertEquals(DailyWorkVersion("2026-10-04", "data-1", "12", false, "10-04 08:00"),
            HttpWorkbenchRepository.parseDailyWorkVersion(JSONObject(
                """{"business_date":"2026-10-04","data_version":"data-1","task_version":12,
                    "tasks_current":false,"source_updated_at":"2026-10-04T08:00:00+08:00"}""")))
    }

    @Test fun dailyActionFormRequiresFollowUpEvidenceAndRespectsPackingPrintState() {
        val sales = DailyWorkItem("sales-1", 1, ROLE_SALES, "跟进交期", status = "in_progress")
        assertEquals(listOf("progress", "wait", "resolve"), dailyActionChoices(sales, isAdmin = false))
        assertTrue(!canSubmitDailyAction("wait", "", "2026-10-05"))
        assertTrue(!canSubmitDailyAction("wait", "已联系采购", ""))
        assertTrue(canSubmitDailyAction("wait", "已联系采购", "2026-10-05"))
        assertTrue(canSubmitDailyAction("start", "", ""))

        val uncertain = DailyWorkItem("packing-1", 1, ROLE_STOCK_PACKING, "包装订单", status = "in_progress",
            printState = "uncertain")
        assertEquals(listOf("progress", "wait"), dailyActionChoices(uncertain, isAdmin = false))
        assertTrue(dailyActionChoices(uncertain.copy(status = "packed"), isAdmin = false).isEmpty())
        assertEquals(listOf("reopen"), dailyActionChoices(uncertain.copy(status = "packed"), isAdmin = true))
        assertTrue(dailyActionChoices(uncertain.copy(readOnly = true), isAdmin = true).isEmpty())
    }

    @Test fun administratorStartsWithMySalesBoard() = runTest(StandardTestDispatcher(scheduler)) {
        val admin = CurrentUser("admin", "admin", "管理员", emptyList(), isAdmin = true)
        val repository = DailyRepository(failSubmissions = false, currentUser = admin)
        model(repository)
        advanceUntilIdle()
        assertTrue(repository.requestedRoles.isNotEmpty())
        assertTrue(repository.requestedRoles.all { it == ROLE_SALES })
        assertTrue(repository.requestedViews.all { it == "mine" })
    }

    @Test fun staleTaskProjectionAndStaffRoleBothBlockGlobalWrites() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = DailyRepository(failSubmissions = false, tasksCurrent = false)
        val model = model(repository)
        advanceUntilIdle()
        model.submitDailyAction("task-1", "start", "", "", 3)
        model.openDailyOrder("A#1")
        model.updateData()
        advanceUntilIdle()
        assertTrue(repository.submissionKeys.isEmpty())
        assertEquals(0, repository.updateStarts)
        assertEquals(Destination.Tasks, model.state.value.destination)
        assertTrue(model.state.value.dailyWork.actionError.orEmpty().contains("任务安排未跟上"))
    }

    @Test fun taskPageCanResetUncertainPrintWithoutOrderQueryWorkspace() = runTest(StandardTestDispatcher(scheduler)) {
        val task = DailyWorkItem("packing-1", 2, ROLE_STOCK_PACKING, "包装订单 A#1", status = "in_progress",
            orderNos = listOf("A#1"), printState = "uncertain", profileId = "profile-1")
        val repository = DailyRepository(failSubmissions = false, dailyItem = task)
        val model = model(repository)
        advanceUntilIdle()
        assertTrue(model.state.value.items.isEmpty())
        model.resetDailyOrderPrintState("A#1", "profile-1")
        advanceUntilIdle()
        assertEquals(listOf("A#1" to "profile-1"), repository.printResets)
    }

    @Test fun modernPeriodicPollUsesLightweightVersionAndOnlyReloadsWhenItChanges() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = DailyRepository(failSubmissions = false)
        val model = model(repository)
        advanceUntilIdle()
        model.dailyWorkVisible(true)
        runCurrent()
        try {
        val fullAfterOpening = repository.fullDailyRequests
        val versionsAfterOpening = repository.versionRequests

        advanceTimeBy(30_000)
        runCurrent()
        assertEquals(versionsAfterOpening + 1, repository.versionRequests)
        assertEquals(fullAfterOpening, repository.fullDailyRequests)

        repository.versionResponse = repository.versionResponse.copy(taskVersion = "task-2")
        advanceTimeBy(30_000)
        runCurrent()
        assertEquals(versionsAfterOpening + 2, repository.versionRequests)
        assertEquals(fullAfterOpening + 1, repository.fullDailyRequests)

        advanceTimeBy(30_000)
        runCurrent()
        assertEquals(versionsAfterOpening + 3, repository.versionRequests)
        assertEquals(fullAfterOpening + 1, repository.fullDailyRequests)
        } finally { model.dailyWorkVisible(false) }
    }

    @Test fun failedTaskActionIsPersistedAndRetriedWithTheSameIdempotencyKey() =
        runTest(StandardTestDispatcher(scheduler)) {
            val failing = DailyRepository(failSubmissions = true)
            val first = model(failing)
            advanceUntilIdle()
            assertEquals(Destination.Tasks, first.state.value.destination)
            assertEquals("task-1", first.state.value.dailyWork.board.items.single().id)

            first.submitDailyAction("task-1", "progress", "已联系采购", "2026-10-05", 3)
            advanceUntilIdle()
            assertEquals(1, first.state.value.dailyWork.pendingActions.size)
            assertTrue(first.state.value.dailyWork.actionError.orEmpty().contains("已安全保存在手机"))
            val originalKey = failing.submissionKeys.single()
            stores.first().clear()

            val recovered = DailyRepository(failSubmissions = false)
            val second = model(recovered)
            advanceUntilIdle()
            assertEquals(listOf(originalKey), recovered.submissionKeys)
            assertTrue(second.state.value.dailyWork.pendingActions.isEmpty())
        }

    @Test fun aSlowPendingSubmissionDoesNotBlockTheFirstReadableTaskPage() = runTest(StandardTestDispatcher(scheduler)) {
        val origin = "https://ul.yingyinghome.com:8443"
        val queued = JSONArray(listOf(JSONObject().put("idempotency_key", "slow-key")
            .put("origin", origin).put("user_id", "user-1").put("task_id", "task-1")
            .put("action", "progress").put("note", "已联系采购").put("version", 3)))
        app.getSharedPreferences("workbench-settings-v1", 0).edit().putString("server", origin)
            .putString("pending-daily-actions-v1", queued.toString()).commit()
        val repository = DailyRepository(failSubmissions = false).apply { submissionGate = CompletableDeferred() }
        val model = model(repository)
        runCurrent()
        assertEquals("task-1", model.state.value.dailyWork.board.items.single().id)
        assertTrue(!model.state.value.dailyWork.loading && !model.state.value.dailyWork.refreshing)
        assertEquals(listOf("slow-key"), repository.submissionKeys)
        assertEquals(1, model.state.value.dailyWork.pendingActions.size)
        repository.submissionGate!!.complete(Unit)
        advanceUntilIdle()
        assertTrue(model.state.value.dailyWork.pendingActions.isEmpty())
        assertEquals(listOf("slow-key"), repository.submissionKeys)
    }

    @Test fun terminalClientErrorsStayForReviewSurviveRestartAndAreNotRetried() =
        runTest(StandardTestDispatcher(scheduler)) {
            listOf(400, 403, 404, 409).forEach { status ->
                app.getSharedPreferences("workbench-settings-v1", 0).edit().clear().commit()
                val rejected = DailyRepository(failSubmissions = false).apply {
                    submissionFailure = ServiceException("terminal_$status", "拒绝 $status", status)
                }
                val first = model(rejected)
                advanceUntilIdle()
                first.submitDailyAction("task-1", "progress", "已处理", "2026-10-05", 3)
                advanceUntilIdle()
                val review = first.state.value.dailyWork.pendingActions.single()
                assertTrue(review.requiresReview)
                assertEquals(1, rejected.submissionKeys.size)
                stores.last().clear()

                val restored = DailyRepository(failSubmissions = false)
                val second = model(restored)
                advanceUntilIdle()
                assertTrue(second.state.value.dailyWork.pendingActions.single().requiresReview)
                assertTrue(restored.submissionKeys.isEmpty())
                second.retryDailyActions()
                advanceUntilIdle()
                assertTrue(restored.submissionKeys.isEmpty())
                second.discardDailyActionReview(review.idempotencyKey)
                assertTrue(second.state.value.dailyWork.pendingActions.isEmpty())
                stores.last().clear()
            }
        }

    @Test fun terminalReviewDoesNotBlockTheNextQueuedAction() = runTest(StandardTestDispatcher(scheduler)) {
        val origin = "https://ul.yingyinghome.com:8443"
        val queued = JSONArray(listOf("task-1", "task-2").mapIndexed { index, task -> JSONObject()
            .put("idempotency_key", "key-$index").put("origin", origin).put("user_id", "user-1")
            .put("task_id", task).put("action", "progress").put("note", "处理 $task")
            .put("next_follow_up_date", "2026-10-05").put("version", 3).put("created_at", "2026-10-04T08:00:00+08:00")
            .put("last_error", "").put("requires_review", false) })
        app.getSharedPreferences("workbench-settings-v1", 0).edit().putString("server", origin)
            .putString("pending-daily-actions-v1", queued.toString()).commit()
        val repository = DailyRepository(failSubmissions = false).apply {
            failuresByTask["task-1"] = ServiceException("task_condition_resolved", "事项已解除", 409)
        }
        val model = model(repository)
        advanceUntilIdle()
        assertEquals(listOf("task-1", "task-2"), repository.submissionTaskIds)
        val pending = model.state.value.dailyWork.pendingActions.single()
        assertEquals("task-1", pending.taskId)
        assertTrue(pending.requiresReview)
    }

    @Test fun expiredSessionKeepsActionRetryableWithTheSameKey() = runTest(StandardTestDispatcher(scheduler)) {
        val repository = DailyRepository(failSubmissions = false).apply {
            submissionFailure = ServiceException("authentication_required", "请重新登录", 401)
        }
        val model = model(repository)
        advanceUntilIdle()
        model.submitDailyAction("task-1", "progress", "已处理", "2026-10-05", 3)
        advanceUntilIdle()
        val queued = model.state.value.dailyWork.pendingActions.single()
        assertTrue(!queued.requiresReview)
        val key = queued.idempotencyKey
        repository.submissionFailure = null
        model.retryDailyActions()
        advanceUntilIdle()
        assertEquals(listOf(key, key), repository.submissionKeys)
        assertTrue(model.state.value.dailyWork.pendingActions.isEmpty())
    }

    private fun model(repository: DailyRepository): WorkbenchViewModel {
        val value = WorkbenchViewModel(app, repository, NoopPrinter())
        stores += ViewModelStore().also { it.put("daily-${stores.size}", value) }
        return value
    }

    private class DailyRepository(private val failSubmissions: Boolean, private val currentUser: CurrentUser =
        CurrentUser("user-1", "xiaowang", "小王", listOf(ROLE_SALES), false, true, 3),
        private val tasksCurrent: Boolean? = null, dailyItem: DailyWorkItem? = null) : WorkbenchRepository {
        val submissionKeys = mutableListOf<String>()
        val submissionTaskIds = mutableListOf<String>()
        val failuresByTask = mutableMapOf<String, Exception>()
        val requestedRoles = mutableListOf<String>()
        val requestedViews = mutableListOf<String>()
        var updateStarts = 0
        var fullDailyRequests = 0
        var versionRequests = 0
        // This fixture models the modern server. Legacy servers without a print token intentionally
        // refresh two-stage boards on each poll, covered separately by TaskBoardFeatureTest.
        var versionResponse = DailyWorkVersion("2026-10-04", "data-1", "task-1", tasksCurrent,
            printStateVersion = "print-1")
        val printResets = mutableListOf<Pair<String, String>>()
        var submissionFailure: Exception? = if (failSubmissions) ConnectException("offline") else null
        var submissionGate: CompletableDeferred<Unit>? = null
        private val item = dailyItem ?: DailyWorkItem("task-1", 3, ROLE_SALES, "跟进订单 A#1", status = "todo")
        override suspend fun health() = ServiceHealth("data-1", "10-04 08:00", 1, 1, apiUpdates = true)
        override suspend fun me() = currentUser
        override suspend fun taskBoard(parameters: Map<String, String>): TaskBoardPage {
            fullDailyRequests++
            requestedRoles += parameters.getValue("role")
            requestedViews += parameters.getValue("view")
            return TaskBoardPage(items = listOf(TaskBoardItem(item.id, "sales", item.role, item.title, version = item.version, canAct = true)),
                total = 1, dataVersion = versionResponse.dataVersion, taskVersion = versionResponse.taskVersion,
                tasksCurrent = tasksCurrent != false, sourceUpdatedAt = "10-04 08:00",
                businessDate = versionResponse.businessDate, summary = DailyWorkSummary(todo = 1),
                printStateVersion = versionResponse.printStateVersion)
        }
        override suspend fun dailyWork(role: String, view: String): DailyWorkSnapshot {
            fullDailyRequests++
            requestedRoles += role
            requestedViews += view
            return DailyWorkSnapshot(versionResponse.businessDate, versionResponse.dataVersion,
                "10-04 08:00", versionResponse.taskVersion, false,
                currentUser.roles.ifEmpty { DAILY_WORK_ROLES }, DailyWorkSummary(todo = 1), listOf(item),
                assignmentEnabled = false, tasksCurrent = tasksCurrent, printStateVersion = versionResponse.printStateVersion)
        }
        override suspend fun dailyWorkVersion(): DailyWorkVersion {
            versionRequests++
            return versionResponse
        }
        override suspend fun dailyWorkSettings() = DailyWorkSettings(false)
        override suspend fun submitDailyAction(taskId: String, action: String, note: String,
                                               nextFollowUpDate: String, idempotencyKey: String, version: Int) {
            submissionKeys += idempotencyKey
            submissionTaskIds += taskId
            submissionGate?.await()
            (failuresByTask[taskId] ?: submissionFailure)?.let { throw it }
        }
        override suspend fun startUpdate(id: String, write: Boolean): UpdateTask {
            updateStarts++
            return UpdateTask(id, "succeeded", "done")
        }
        override suspend fun resetOrderPrintState(orderNo: String, profileId: String, deviceId: String) {
            printResets += orderNo to profileId
        }
        override suspend fun query(destination: Destination, q: String, mode: String, scope: String,
                                   logistics: String, page: Int, inventoryFilters: InventoryFilters,
                                   expectedDataVersion: String) = QueryResult(emptyList(), 0, 1, null, null, null, "", "data-1", "")
        override suspend fun prepare(state: WorkbenchState, all: Boolean, maximum: Int?) = PreparedPrint("unused", 0)
        override suspend fun claim(id: String, actionKey: String) = ClaimedPrint("unused", emptyList())
        override suspend fun report(id: String, token: String, status: String, message: String, bytes: Int) = Unit
    }

    private class NoopPrinter : PrinterClient {
        override val state: StateFlow<PrinterState> = MutableStateFlow(PrinterState())
        override fun scan() = Unit
        override fun connect(address: String) = Unit
        override fun disconnect() = Unit
        override fun close() = Unit
        override fun cancelSending() = Unit
        override fun acknowledgeUnknownAttempt() = true
        override suspend fun sendPages(pages: List<ByteArray>, shouldCancel: () -> Boolean) =
            PrintOutcome(PrintOutcome.FAILED, 0, "unused")
    }
}
