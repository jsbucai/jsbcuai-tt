package com.ul.workbench.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import android.view.inspector.WindowInspector
import androidx.activity.ComponentActivity
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.unit.Density
import com.ul.workbench.data.*
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
class TwoStageTaskBoardUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Test fun salesHasTwoColumnsAndCardStartWaitsForConfirmation() {
        val item = sales("s1").copy(hasNote = true, latestNote = "已反馈客户，等待备货", urgent = true)
        var state by mutableStateOf(boardState(ROLE_SALES, listOf(item)))
        var started = ""
        var opened = ""
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state),
            actions().copy(startTaskBoardSales = { started = it }, openTaskBoardItem = { opened = it })) }
        compose.onNodeWithText("待处理 12").assertIsDisplayed()
        compose.onNodeWithText("处理中 3").assertIsDisplayed()
        compose.onNodeWithText("已处理").assertDoesNotExist()
        compose.onNodeWithText("历史").assertDoesNotExist()
        compose.onNodeWithText("有备注 ").assertIsDisplayed()
        compose.onNodeWithText("客户订单 s1 · 客户 s1").performClick()
        assertEquals("s1", opened)
        compose.onNodeWithTag("board-start-s1").performClick()
        assertEquals("s1", started)
        compose.onNodeWithTag("board-start-s1").assertExists()
        screenshot("two-stage-sales-todo-390.png")
        compose.runOnIdle { state = state.copy(boardSelectedIds = setOf("s1")) }
        compose.onNodeWithTag("board-start-s1").assertDoesNotExist()
        compose.onNodeWithText("记录跟进").assertDoesNotExist()
        compose.onNodeWithText("分享所选订单图片").assertIsDisplayed()
    }

    @Test fun salesDetailNoteKeepsInputOnConflictAndClosesOnlyAfterSaved() {
        val item = sales("s1").copy(hasNote = true, latestNote = "之前的说明")
        var state by mutableStateOf(boardState(ROLE_SALES, listOf(item)).copy(
            boardDetail = TaskBoardDetail(item, notes = listOf(TaskBoardNote("之前的说明", "10-06 09:00", "u1", "note")))))
        var submitted: Pair<String, String>? = null
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state),
            actions().copy(saveTaskBoardSalesNote = { id, note -> submitted = id to note })) }
        compose.onNodeWithText("最新备注：之前的说明").assertIsDisplayed()
        compose.onNodeWithText("备注记录").assertIsDisplayed()
        compose.onNodeWithText("备注").performClick()
        compose.onNodeWithTag("sales-note-input").performTextInput("客户确认明天发货")
        compose.onNodeWithText("等待对象（选填）").assertDoesNotExist()
        compose.onNodeWithText("下次跟进：", substring = true).assertDoesNotExist()
        compose.onNodeWithText("保存备注").performClick()
        assertEquals("s1" to "客户确认明天发货", submitted)
        compose.runOnIdle { state = state.copy(actionErrorId = "s1", actionError = "订单已更新，请核对后重试") }
        compose.onNodeWithText("订单已更新，请核对后重试").assertIsDisplayed()
        compose.onNodeWithText("客户确认明天发货").assertExists()
        screenshot("two-stage-sales-note-conflict-390.png")
        compose.runOnIdle { state = state.copy(actionError = null, salesActionSavedId = "s1", salesActionRevision = 1) }
        compose.onNodeWithTag("sales-note-input").assertDoesNotExist()
        assertEquals("todo", state.boardSection)
    }

    @Test fun salesProcessingOffersUndoAndDetailResetWithoutStart() {
        val item = sales("s1").copy(workState = "in_progress")
        var state by mutableStateOf(boardState(ROLE_SALES, listOf(item)).copy(boardSection = "in_progress",
            undoSalesStartGroupId = "s1", undoSalesStartUntil = Long.MAX_VALUE))
        var undone = 0
        var reset = ""
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state),
            actions().copy(undoTaskBoardSalesStart = { undone++ }, resetTaskBoardSales = { reset = it })) }
        compose.onNodeWithTag("board-start-s1").assertDoesNotExist()
        val startedBounds = compose.onNodeWithText("已开始处理").fetchSemanticsNode().boundsInRoot
        val undoBounds = compose.onNodeWithText("撤销").fetchSemanticsNode().boundsInRoot
        assertTrue("Undo action must have its own horizontal space", startedBounds.right <= undoBounds.left)
        compose.onNodeWithText("撤销").performClick()
        assertEquals(1, undone)
        screenshot("two-stage-sales-processing-390.png")
        compose.runOnIdle { state = state.copy(boardDetail = TaskBoardDetail(item)) }
        compose.onNodeWithTag("task-board-detail-more").performClick()
        compose.onNodeWithText("退回待处理").performClick()
        assertEquals("s1", reset)
    }

    @Test fun detailRecordsClosesDetailBeforeSwitchingAndReturnsToPreviousColumn() {
        val item = sales("s1").copy(workState = "in_progress")
        var state by mutableStateOf(boardState(ROLE_SALES, listOf(item)).copy(boardSection = "in_progress",
            boardSectionsByRole = mapOf(ROLE_SALES to "in_progress"), boardDetail = TaskBoardDetail(item)))
        val calls = mutableListOf<String>()
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state),
            actions().copy(closeTaskBoardDetail = { calls += "close"; state = state.copy(boardDetail = null) },
                selectTaskBoardSection = { section -> calls += section; state = state.copy(boardSection = section) })) }
        compose.onNodeWithTag("task-board-detail-more").performClick()
        compose.onNodeWithText("过往记录").performClick()
        assertEquals(listOf("close", "processed"), calls)
        compose.onNodeWithText("← 返回").performClick()
        assertEquals("in_progress", calls.last())
    }

    @Test fun offlineStartRemainsInTodoAndShowsPendingUntilConfirmed() {
        val item = sales("s1")
        val pending = PendingDailyAction("request1", "server", "u1", "s1", "sales_group_action", "", "", 1, "now")
        val state = boardState(ROLE_SALES, listOf(item)).copy(pendingActions = listOf(pending))
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state), actions()) }
        compose.onNodeWithText("待提交").assertIsDisplayed()
        compose.onNodeWithTag("board-start-s1").assertIsNotEnabled()
        compose.onNodeWithText("客户订单 s1 · 客户 s1").assertIsDisplayed()
        assertEquals("todo", state.boardSection)
    }

    @Test fun stockSelectionOnlyOffersPrintAndImagesAndShowsPrintReview() {
        val item = stock("p1").copy(workState = "in_progress", printState = "uncertain", workLabel = "打印待核对", canAct = false)
        val second = stock("p2").copy(workState = "in_progress", printState = "printed", workLabel = "待出库")
        val state = boardState(ROLE_STOCK_PACKING, listOf(item, second)).copy(boardSection = "in_progress", boardSelectedIds = setOf("p2"))
        var printCalls = 0
        var exportCalls = 0
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state),
            actions().copy(previewStockPackingPrint = { printCalls++ }, exportTaskBoardImages = { exportCalls++ })) }
        compose.onNodeWithText("打印待核对", substring = true).assertIsDisplayed()
        compose.onNodeWithText("待出库", substring = true).assertIsDisplayed()
        compose.onNodeWithText("开始").assertDoesNotExist()
        compose.onNodeWithText("已包装").assertDoesNotExist()
        compose.onNodeWithTag("board-select-p1").assertIsEnabled()
        compose.onNodeWithText("打印").performClick()
        compose.onNodeWithText("分享所选订单图片").performClick()
        assertEquals(1, printCalls)
        assertEquals(1, exportCalls)
        screenshot("two-stage-stock-processing-390.png")
    }

    @Test @Config(qualifiers = "w320dp-h640dp-mdpi")
    fun stockTodoKeepsTwoColumnsAndLargeFontUsable() {
        compose.setContent { CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density, 1.3f)) {
            WorkbenchApp(WorkbenchState(destination = Destination.Tasks,
                dailyWork = boardState(ROLE_STOCK_PACKING, (1..12).map { stock("p$it") })), actions())
        } }
        compose.onNodeWithText("待处理 12").assertIsDisplayed()
        compose.onNodeWithText("处理中 3").assertIsDisplayed()
        compose.onNodeWithText("商品 p1").assertIsDisplayed()
        compose.onNodeWithTag("stock-page-prev").assertIsDisplayed()
        compose.onNodeWithTag("stock-page-next").assertIsDisplayed()
        val boardBounds = compose.onNodeWithTag("task-board").fetchSemanticsNode().boundsInRoot
        val contentBounds = compose.onNodeWithTag("task-board-content").fetchSemanticsNode().boundsInRoot
        assertTrue("Small stock screen must keep at least 65% for cards plus its explicit pager", contentBounds.height / boardBounds.height >= .65f)
        screenshot("two-stage-stock-todo-320-large-font.png")
    }

    @Test fun columnsAndDetailReturnRetainTheirOwnScrollPositions() {
        val todo = (0..99).map { stock("todo-$it") }
        val processing = (0..99).map { stock("doing-$it").copy(workState = "in_progress") }
        var state by mutableStateOf(boardState(ROLE_STOCK_PACKING, todo))
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state),
            actions().copy(selectTaskBoardSection = { section -> state = state.copy(boardSection = section,
                board = state.board.copy(items = if (section == "todo") todo else processing)) },
                openTaskBoardItem = { id -> state = state.copy(boardDetail = TaskBoardDetail(state.board.items.first { it.id == id })) },
                closeTaskBoardDetail = { state = state.copy(boardDetail = null) })) }
        compose.onNodeWithTag("task-board-list").performScrollToIndex(60)
        val before = compose.onNodeWithText("商品 todo-60").fetchSemanticsNode().boundsInRoot.top
        compose.onNodeWithText("商品 todo-60").performClick()
        compose.onNodeWithText("← 返回").performClick()
        assertEquals(before, compose.onNodeWithText("商品 todo-60").fetchSemanticsNode().boundsInRoot.top, 1f)
        compose.onNodeWithText("处理中 3").performClick()
        compose.onNodeWithTag("task-board-list").performScrollToIndex(20)
        val processingBefore = compose.onNodeWithText("商品 doing-20").fetchSemanticsNode().boundsInRoot.top
        compose.onNodeWithText("待处理 12").performClick()
        assertEquals(before, compose.onNodeWithText("商品 todo-60").fetchSemanticsNode().boundsInRoot.top, 1f)
        compose.onNodeWithText("处理中 3").performClick()
        assertEquals(processingBefore, compose.onNodeWithText("商品 doing-20").fetchSemanticsNode().boundsInRoot.top, 1f)
    }

    @Test fun oldRecordsAreBehindMenuAndPurchaseArrivalKeepOriginalControls() {
        var section = ""
        var state by mutableStateOf(boardState(ROLE_STOCK_PACKING, listOf(stock("p1"))))
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state),
            actions().copy(selectTaskBoardSection = { section = it })) }
        compose.onNodeWithTag("task-board-more").performClick()
        compose.onNodeWithText("过往记录").performClick()
        assertEquals("processed", section)
        compose.runOnIdle { state = boardState(ROLE_ARRIVAL_PACKING, listOf(stock("p1").copy(role = ROLE_ARRIVAL_PACKING)))
            .copy(boardSection = "active", boardSelectedIds = setOf("p1")) }
        compose.onNodeWithText("异常 0").assertIsDisplayed()
        compose.onNodeWithText("已处理").assertDoesNotExist()
        compose.onNodeWithText("历史").assertDoesNotExist()
        compose.onNodeWithText("开始").assertDoesNotExist()
        compose.onNodeWithText("已包装").assertDoesNotExist()
        compose.runOnIdle { state = boardState(ROLE_PURCHASE, listOf(stock("p1").copy(role = ROLE_PURCHASE)))
            .copy(boardSection = "active", boardSelectedIds = setOf("p1")) }
        compose.onNodeWithText("记录跟进").performClick()
        compose.onNodeWithText("已安排采购").assertIsDisplayed()
        compose.onNodeWithText("等待对象（选填）").assertIsDisplayed()
    }

    private fun sales(id: String) = TaskBoardItem(id, "sales", ROLE_SALES, "客户 $id", customer = "客户 $id",
        customerOrderNo = "客户订单 $id", dueDate = "2026-10-08", totalCount = 6, readyCount = 4,
        missingCount = 2, assigneeId = "u1", assigneeName = "销售一", canAct = true, workState = "todo")
    private fun stock(id: String) = TaskBoardItem(id, "packing", ROLE_STOCK_PACKING, "商品 $id",
        sku = "LS-$id", quantity = "120", unit = "件", location = "B6-3", orderCount = 8,
        assigneeId = "u1", assigneeName = "包装一", canAct = true, canPrint = true, workState = "todo", workLabel = "待处理")
    private fun boardState(role: String, items: List<TaskBoardItem>) = DailyWorkState(
        me = CurrentUser("u1", "worker", "员工", listOf(role)), selectedRole = role, boardSection = "todo",
        board = TaskBoardPage(items, items.size, dataVersion = "d1", taskVersion = "t1",
            sourceUpdatedAt = "2026-10-06T07:30:00+08:00", workStateCounts = mapOf("todo" to 12, "in_progress" to 3)))
    private fun actions() = WorkbenchActions(
        navigate = {}, search = { _, _ -> }, changeScope = { _, _ -> }, toggleSelection = {}, selectAll = {}, loadMore = {},
        printSelected = {}, printAll = {}, cancelPrint = {}, requestSplit = {}, dismissSplit = {}, editingChanged = {},
        scanPrinters = {}, connectPrinter = {}, disconnectPrinter = {}, saveServer = {}, checkServer = {}, clearMessage = {})
    private fun screenshot(name: String) {
        val target = File("build/test-screenshots/$name"); target.parentFile?.mkdirs()
        compose.runOnIdle {
            val activityView = compose.activity.window.decorView
            // PixelCopy does not receive a frame under Robolectric. Draw the
            // actual dialog decor view so the artifact still shows the input.
            val view = if (name.contains("note-conflict")) WindowInspector.getGlobalWindowViews()
                .last { it !== activityView && it.width > 0 && it.height > 0 }
                else activityView
            val bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
            view.draw(Canvas(bitmap))
            target.outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
        }
    }
}
