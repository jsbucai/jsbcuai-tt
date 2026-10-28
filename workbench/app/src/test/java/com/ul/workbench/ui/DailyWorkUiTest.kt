package com.ul.workbench.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import androidx.activity.ComponentActivity
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.unit.Density
import com.ul.workbench.data.*
import com.ul.workbench.ui.screens.dailyActionChoices
import com.ul.workbench.ui.screens.historyCoverageLabel
import com.ul.workbench.ui.screens.purchaseSnapshotLabel
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import org.robolectric.annotation.LooperMode
import java.io.File

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@LooperMode(LooperMode.Mode.PAUSED)
@Config(sdk = [35], qualifiers = "w390dp-h844dp-mdpi")
class DailyWorkUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Test fun customerOwnerEditFreezesVersionAndKeepsTextWhileServerChanges() {
        val initial = CustomerOwner("P1", "客户甲", "s1", "销售一", status = "assigned", version = 3,
            canSetOwner = true, canSetDeputy = true)
        var state by mutableStateOf(daily(emptyList()).copy(
            me = CurrentUser("admin", "admin", "管理员", DAILY_WORK_ROLES, isAdmin = true),
            customerOwners = listOf(initial), staff = listOf(
            StaffMember("s1", "sales1", "销售一", listOf(ROLE_SALES), true, 3),
            StaffMember("s2", "sales2", "销售二", listOf(ROLE_SALES), true, 3))))
        var submitted: CustomerOwnerDraft? = null
        compose.setContent {
            WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state),
                actions().copy(saveCustomerOwner = { submitted = it }))
        }
        compose.onNodeWithText("库存件包装 ▾").performClick()
        compose.onNodeWithText("人员管理").performClick()
        compose.onNodeWithText("管理客户销售／代办").performScrollTo().performClick()
        compose.onNodeWithText("设置固定销售").performClick()
        compose.onNode(hasText("销售二") and hasClickAction()).performClick()
        compose.onNodeWithText("变更原因").performTextInput("客户长期交接")
        compose.runOnIdle { state = state.copy(customerOwners = listOf(initial.copy(version = 4))) }
        compose.onNodeWithText("确认客户变更").assertIsNotEnabled()
        compose.onNodeWithText("客户长期交接").assertExists()
        compose.onNodeWithText("已核对，采用当前归属版本").performScrollTo().performClick()
        screenshot("customer-owner-conflict-390.png")
        compose.onNodeWithText("确认客户变更").performClick()
        assertEquals(4, submitted?.version)
        assertEquals("s2", submitted?.personId)
        assertEquals("客户长期交接", submitted?.reason)
        assertFalse(submitted?.deputy ?: true)
    }

    @Test fun identityReviewCannotOpenOwnerOrDeputyMutation() {
        val state = daily(emptyList()).copy(me = CurrentUser("admin", "admin", "管理员", DAILY_WORK_ROLES, isAdmin = true),
            customerOwners = listOf(CustomerOwner("P1", "客户甲", status = "review",
            reviewReasons = listOf("客户名称冲突"))))
        compose.setContent {
            WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state), actions())
        }
        compose.onNodeWithText("库存件包装 ▾").performClick()
        compose.onNodeWithText("人员管理").performClick()
        compose.onNodeWithText("管理客户销售／代办").performScrollTo().performClick()
        compose.onNodeWithText("客户名称冲突").assertIsDisplayed()
        compose.onNodeWithText("设置固定销售").assertIsNotEnabled()
        compose.onNodeWithText("临时代办").assertIsNotEnabled()
    }

    @Test fun dashboardNeverLabelsPartialHistoryOrReviewPurchaseComplete() {
        val dashboard = AdminDashboard(0, 0, 0, 0, "", "10-06 08:00", "", "partial_archive", emptyList(), "review", 90)
        assertTrue(purchaseSnapshotLabel(dashboard).contains("可查看 90 行"))
        assertTrue(purchaseSnapshotLabel(dashboard).contains("10-06 08:00"))
        assertTrue(historyCoverageLabel("partial_archive").contains("尚未完整"))
        assertTrue(historyCoverageLabel("current_snapshot_only").contains("尚未补齐"))
    }

    @Test fun normalBoardLeavesAtLeastThreeQuartersForRealWork() {
        show(daily(items(100)))
        compose.onNodeWithText("本人全部 · 100 卡／800 单").assertIsDisplayed()
        compose.onNodeWithText("商品 0").assertIsDisplayed()
        assertContentRatio()
        screenshot("task-board-390.png")
    }

    @Test fun explicitLineAndOrderCountsKeepUsableBoardSpace() {
        val state = daily(items(100))
        show(state.copy(board = state.board.copy(lineCount = 1600)))
        compose.onNodeWithText("本人全部 · 100 卡／800 单／1600 行").assertIsDisplayed()
        compose.onNodeWithText("商品 0").assertIsDisplayed()
        assertContentRatio()
        screenshot("task-board-line-counts-390.png")
    }

    @Test @Config(qualifiers = "w320dp-h640dp-mdpi")
    fun compactBoardKeepsContentSpaceWithLargeFontOnSmallScreen() {
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 1.3f)) {
                WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = legacyPurchase(daily(items(100)))), actions())
            }
        }
        compose.onNodeWithText("商品 0").assertIsDisplayed()
        assertContentRatio()
        screenshot("task-board-320-large-font.png")
    }

    @Test fun scrollingLoadsAllPagesWithoutMoreButtonAndKeepsUnloadedTotal() {
        var displayed by mutableStateOf(50)
        var pageCalls = 0
        compose.setContent {
            val state = legacyPurchase(daily(items(displayed)).copy(board = page(items(displayed)).copy(total = 121, hasMore = displayed < 121)))
            WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state), actions().copy(loadMoreTaskBoard = {
                pageCalls++; displayed = (displayed + 50).coerceAtMost(121)
            }))
        }
        compose.onNodeWithText("本人全部 · 121 项").assertIsDisplayed()
        compose.onNodeWithTag("task-board-list").performScrollToIndex(49)
        compose.waitUntil { displayed >= 100 }
        compose.onNodeWithTag("task-board-list").performScrollToIndex(99)
        compose.waitUntil { displayed == 121 }
        compose.onNodeWithTag("task-board-list").performScrollToIndex(120)
        compose.onNodeWithText("商品 120").assertIsDisplayed()
        compose.onNodeWithText("加载更多").assertDoesNotExist()
        assertEquals(2, pageCalls)
    }

    @Test fun closingUnchangedFilterPreservesScrollAndShowsAllRoleChoice() {
        var applied: List<String>? = null
        show(legacyPurchase(daily(items(100))), actions().copy(setTaskBoardFilters = { status, view, from, to, owner ->
            applied = listOf(status, view, from, to, owner)
        }))
        compose.onNodeWithTag("task-board-list").performScrollToIndex(60)
        val before = compose.onNodeWithText("商品 60").fetchSemanticsNode().boundsInRoot.top
        compose.onNodeWithText("筛选").performClick()
        compose.onNodeWithText("本岗位全部").assertIsDisplayed()
        compose.onNodeWithText("交期范围（含起止日期）").assertIsDisplayed()
        compose.onNodeWithText("应用筛选").performClick()
        compose.waitForIdle()
        val after = compose.onNodeWithText("商品 60").fetchSemanticsNode().boundsInRoot.top
        assertEquals(before, after, 1f)
        assertEquals(listOf("all", "mine", "", "", ""), applied)
    }

    @Test fun coworkerItemsRemainVisibleButCannotBeSelected() {
        val item = items(1).single().copy(canAct = false, canPrint = false, assigneeName = "另一包装员")
        show(daily(listOf(item)).copy(view = "role"))
        compose.onNodeWithText("本岗位全部 · 1 卡／8 单").assertIsDisplayed()
        compose.onNodeWithText("多选").performClick()
        compose.onNodeWithTag("board-select-${item.id}").assertIsNotEnabled()
        compose.onNodeWithText("全选本页").assertIsNotEnabled()
        compose.onNodeWithText("开始").assertDoesNotExist()
        compose.onNodeWithText("打印").assertIsNotEnabled()
    }

    @Test fun tappingCardBodyInMultiSelectTogglesWithoutOpeningDetail() {
        val item = items(1).single()
        var toggled = 0
        var opened = 0
        show(daily(listOf(item)), actions().copy(
            toggleTaskBoardItem = { toggled++ }, openTaskBoardItem = { opened++ }))
        compose.onNodeWithText("多选").performClick()
        compose.onNodeWithText("商品 0").performClick()
        assertEquals(1, toggled)
        assertEquals(0, opened)
    }

    @Test fun longPressOnCardStartsSelectionWithoutOpeningDetail() {
        val item = items(1).single()
        var toggled = 0
        var opened = 0
        show(daily(listOf(item)), actions().copy(
            toggleTaskBoardItem = { toggled++ }, openTaskBoardItem = { opened++ }))
        compose.onNodeWithText("商品 0").performTouchInput { longClick() }
        assertEquals(1, toggled)
        assertEquals(0, opened)
    }

    @Test fun longPressDragSelectsCardsCrossedByTheFinger() {
        val toggled = mutableListOf<String>()
        show(daily(items(3)), actions().copy(toggleTaskBoardItem = { toggled += it }))
        val list = compose.onNodeWithTag("task-board-list").fetchSemanticsNode().boundsInRoot
        val first = compose.onNodeWithText("商品 0").fetchSemanticsNode().boundsInRoot.center
        val third = compose.onNodeWithText("商品 2").fetchSemanticsNode().boundsInRoot.center
        compose.onNodeWithTag("task-board-list").performTouchInput {
            down(Offset(first.x - list.left, first.y - list.top))
            advanceEventTime(700)
            moveTo(Offset(first.x - list.left, (first.y + third.y) / 2f - list.top))
            moveTo(Offset(third.x - list.left, third.y - list.top))
            up()
        }
        assertEquals(listOf("item-0", "item-1", "item-2"), toggled.distinct())
    }

    @Test fun historySectionsKeepProcessedRecordsAccessible() {
        val task = DailyWorkItem("old-1", 2, ROLE_STOCK_PACKING, "上周包装事项",
            status = "completed", orderNos = listOf("PO-1"), assigneeName = "小王")
        var opened = ""
        var section = ""
        show(daily(items(1)).copy(boardSection = "processed",
            taskHistory = TaskHistoryPage(listOf(task), 1)), actions().copy(
                openTaskHistoryItem = { opened = it }, selectTaskBoardSection = { section = it }))
        compose.onNodeWithText("上周包装事项").performClick()
        assertEquals("old-1", opened)
        compose.onNodeWithText("已归档").performClick()
        assertEquals("archived", section)
    }

    @Test fun adminDashboardShowsDistinctCompanyTotalsAndStaffDrilldown() {
        val person = AdminDashboardPerson("u1", "小王", 2, 1, 1, 0, 4, 3, "10-06 08:00")
        val role = AdminDashboardRole(ROLE_STOCK_PACKING, 5, 2, 1, 1, 0, 1, listOf(person))
        val dashboard = AdminDashboard(12, 7, 2, 1, "10-06 07:30", "", "10-06 08:01",
            "current_snapshot_only", listOf(role))
        var drill: Pair<String, String>? = null
        show(daily(items(1)).copy(me = CurrentUser("admin", "admin", "管理员", DAILY_WORK_ROLES, isAdmin = true),
            adminDashboardOpen = true, adminDashboard = dashboard),
            actions().copy(openDashboardRole = { selectedRole, user -> drill = selectedRole to user }))
        compose.onNodeWithText("客户整单 12 · 未完成 7").assertIsDisplayed()
        compose.onNodeWithText("小王").performScrollTo().performClick()
        assertEquals(ROLE_STOCK_PACKING to "u1", drill)
    }

    @Test fun outOfDateProjectionDisablesBatchActionsAndDoesNotFallBackToOldCards() {
        val item = items(1).single()
        show(daily(listOf(item)).copy(board = page(listOf(item)).copy(tasksCurrent = false),
            snapshot = DailyWorkSnapshot(items = listOf(DailyWorkItem("old", 1, ROLE_STOCK_PACKING, "旧版任务卡")))))
        compose.onNodeWithText("任务尚未跟上业务数据，处理已暂停。").assertIsDisplayed()
        compose.onNodeWithText("旧版任务卡").assertDoesNotExist()
        compose.onNodeWithText("多选").performClick()
        compose.onNodeWithTag("board-select-${item.id}").assertIsNotEnabled()
        compose.onNodeWithText("全选本页").assertIsNotEnabled()
        compose.onNodeWithText("已包装").assertDoesNotExist()
    }

    @Test fun detailDoesNotTruncateAndUnknownPaperCanBeSelectedForConfirmedReprint() {
        val item = items(1).single()
        val lines = (0..120).map { index -> TaskBoardLine("line-$index", "task-$index", 1, "A#$index",
            sku = "LS0017", quantity = "10", unit = "件", assigneeName = "小王", canAct = true, canPrint = true,
            printState = if (index == 0) "uncertain" else "unprinted") }
        show(daily(listOf(item)).copy(boardDetail = TaskBoardDetail(item, lines)))
        compose.onNodeWithText("121 条订单明细").assertIsDisplayed()
        compose.onNodeWithText("多选").performClick()
        compose.onNodeWithText("打印待核对，确认后可重打 · 小王", substring = true).assertIsDisplayed()
        compose.onNodeWithTag("board-line-select-line-0").assertIsEnabled()
        compose.onNodeWithTag("task-board-detail-list").performScrollToIndex(121)
        compose.onNodeWithText("A#120").assertIsDisplayed()
        assertFalse(dailyActionChoices(DailyWorkItem("t", 1, ROLE_STOCK_PACKING, "任务", status = "in_progress",
            printState = "uncertain"), false).contains("packed"))
    }

    @Test fun todayRecordsSeparateFollowupPackingAndErpConfirmation() {
        val board = page(items(1)).copy(todayEvents = listOf(
            DailyWorkEvent("a", "t1", "followup", "已协调客户交期"),
            DailyWorkEvent("b", "t2", "packed", "A#2 已包装"),
            DailyWorkEvent("c", "t3", "business_completed", "ERP 确认 A#3 已发货")))
        show(daily(items(1)).copy(board = board))
        compose.onNodeWithTag("task-board-more").performClick()
        compose.onNodeWithText("今日记录").performClick()
        compose.onNodeWithText("跟进动作 1").assertIsDisplayed()
        compose.onNodeWithText("包装完成 1").assertIsDisplayed()
        compose.onNodeWithText("ERP 确认完成 1").assertIsDisplayed()
    }

    @Test fun combinationOrderShowsEachProductQuantityAndNeverInventsATotal() {
        val item = items(1).single().copy(kind = "combination", quantity = "", unit = "")
        val line = TaskBoardLine("combo", "task", 1, "A#1", sku = "LS0017 / DQ0517", quantity = "30", unit = "件",
            productLines = listOf(TaskBoardProductLine("r1", "LS0017", "10", "件", "每袋5件"),
                TaskBoardProductLine("r2", "DQ0517", "20", "套", "单独贴标")))
        show(daily(listOf(item)).copy(boardDetail = TaskBoardDetail(item, listOf(line))))
        compose.onNodeWithText("LS0017 · 10件").assertIsDisplayed()
        compose.onNodeWithText("DQ0517 · 20套").assertIsDisplayed()
        compose.onNodeWithText("摘要：每袋5件").assertIsDisplayed()
        compose.onNodeWithText("摘要：单独贴标").assertIsDisplayed()
        compose.onNodeWithText("可执行 30", substring = true).assertDoesNotExist()
    }

    @Test fun ordinaryBatchFollowupUsesQuickResultWithoutMandatoryNote() {
        var result: List<String>? = null
        val item = items(1).single().copy(role = ROLE_PURCHASE, kind = "purchase")
        val state = daily(listOf(item)).copy(selectedRole = ROLE_PURCHASE, boardSelectedIds = setOf(item.id))
        show(state, actions().copy(submitTaskBoardBatch = { action, choice, note, waiting, promised, next ->
            result = listOf(action, choice, note, waiting, promised, next)
        }))
        compose.onNodeWithText("记录跟进").performClick()
        compose.onNodeWithText("已安排采购").assertIsDisplayed()
        compose.onNodeWithText("保存记录").assertIsEnabled().performClick()
        assertEquals("followup", result?.get(0))
        assertEquals("已安排采购", result?.get(1))
        assertEquals("", result?.get(2))
        assertFalse(result?.get(5).isNullOrBlank())
    }

    @Test fun adminCanReachStaffAndAttendanceWithoutPackingLimitControl() {
        var attendance: Pair<String, Boolean>? = null
        val state = daily(items(1)).copy(me = CurrentUser("admin", "admin", "管理员", emptyList(), isAdmin = true),
            staff = listOf(StaffMember("u1", "wang", "小王", listOf(ROLE_STOCK_PACKING), true, 3)),
            snapshot = DailyWorkSnapshot(assignmentEnabled = true), attendance = mapOf("u1" to false), attendanceAvailable = true)
        show(state, actions().copy(setStaffAttendance = { id, present -> attendance = id to present }))
        compose.onNodeWithText("库存件包装 ▾").performClick()
        compose.onNodeWithText("人员管理").performClick()
        compose.onNodeWithText("人员与岗位").assertIsDisplayed()
        compose.onNodeWithText("包装派工：按同品归并，无数量上限").assertIsDisplayed()
        compose.onNodeWithText("包装任务上限 3").assertDoesNotExist()
        compose.onAllNodes(isToggleable()).onLast().performClick()
        assertEquals("u1" to true, attendance)
    }

    @Test fun staffCardShowsNameWithoutMistakingLoginAccountForName() {
        val state = daily(emptyList()).copy(
            me = CurrentUser("admin", "admin", "管理员", emptyList(), isAdmin = true),
            staff = listOf(StaffMember("sales-1", "刘峰", "刘锋", listOf(ROLE_SALES), true, 3)))
        show(state)
        compose.onNodeWithText("库存件包装 ▾").performClick()
        compose.onNodeWithText("人员管理").performClick()
        compose.onNodeWithText("刘锋").assertExists()
        compose.onNodeWithText("刘峰").assertDoesNotExist()
    }

    @Test fun staffCannotSeeGlobalErpUpdateEntry() {
        val state = daily(items(1)).copy(selectedRole = ROLE_SALES,
            me = CurrentUser("u1", "wang", "小王", listOf(ROLE_SALES)))
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Orders, apiUpdates = true,
            serverOnline = true, dailyWork = state), actions()) }
        compose.onNodeWithText("更新订单").assertDoesNotExist()
    }

    private fun assertContentRatio() {
        val board = compose.onNodeWithTag("task-board").fetchSemanticsNode().boundsInRoot
        val content = compose.onNodeWithTag("task-board-content").fetchSemanticsNode().boundsInRoot
        assertTrue("Content ${content.height} / board ${board.height} must be at least 75%", content.height / board.height >= .75f)
    }
    private fun screenshot(name: String) {
        val target = File("build/test-screenshots/$name"); target.parentFile?.mkdirs()
        compose.runOnIdle {
            val view = compose.activity.window.decorView
            val bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
            view.draw(Canvas(bitmap))
            target.outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
        }
    }
    private fun show(daily: DailyWorkState, handlers: WorkbenchActions = actions()) {
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = daily), handlers) }
    }
    private fun items(count: Int) = (0 until count).map { TaskBoardItem("item-$it", "packing", ROLE_STOCK_PACKING,
        "商品 $it", sku = "LS$it", quantity = "120", unit = "件", location = "A2", orderCount = 8,
        assigneeId = "u1", assigneeName = "小王", canAct = true, canPrint = true) }
    private fun page(items: List<TaskBoardItem>) = TaskBoardPage(items, items.size, dataVersion = "d1", taskVersion = "t1",
        sourceUpdatedAt = "2026-10-05T08:30:00+08:00", businessDate = "2026-10-05", orderCount = items.sumOf { it.orderCount })
    private fun legacyPurchase(value: DailyWorkState) = value.copy(selectedRole = ROLE_PURCHASE, boardSection = "active",
        me = value.me?.copy(roles = listOf(ROLE_PURCHASE)), board = value.board.copy(items = value.board.items.map { it.copy(role = ROLE_PURCHASE) }))
    private fun daily(items: List<TaskBoardItem>) = DailyWorkState(
        me = CurrentUser("u1", "wang", "小王", listOf(ROLE_STOCK_PACKING)), selectedRole = ROLE_STOCK_PACKING,
        boardSection = "todo", board = page(items))
    private fun actions() = WorkbenchActions(
        navigate = {}, search = { _, _ -> }, changeScope = { _, _ -> }, toggleSelection = {}, selectAll = {}, loadMore = {},
        printSelected = {}, printAll = {}, cancelPrint = {}, requestSplit = {}, dismissSplit = {}, editingChanged = {},
        scanPrinters = {}, connectPrinter = {}, disconnectPrinter = {}, saveServer = {}, checkServer = {}, clearMessage = {}
    )
}
