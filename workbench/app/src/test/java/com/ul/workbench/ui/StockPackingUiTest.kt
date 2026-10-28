package com.ul.workbench.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import android.view.inspector.WindowInspector
import androidx.activity.ComponentActivity
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Density
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import com.ul.workbench.data.*
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.*
import java.io.File

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@LooperMode(LooperMode.Mode.PAUSED)
@Config(sdk = [35], qualifiers = "w390dp-h844dp-mdpi")
class StockPackingUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Test @Config(qualifiers = "w320dp-h640dp-mdpi")
    fun specificationAndModelKeepCompactHeightAtLargeFont() {
        var daily by mutableStateOf(state())
        compose.setContent { CompositionLocalProvider(LocalDensity provides Density(LocalDensity.current.density,1.3f)) {
            WorkbenchApp(WorkbenchState(destination=Destination.Tasks,dailyWork=daily),actions())
        } }
        val oldHeight=compose.onNodeWithTag("board-card-stock-0").fetchSemanticsNode().boundsInRoot.height
        compose.runOnIdle { daily=daily.copy(board=daily.board.copy(items=daily.board.items.map {
            it.copy(specification="M10×25",model="8.8级达克罗",productName="标准内六角圆柱头螺钉超长商品名称")
        })) }
        compose.onNodeWithText("LS0001 · M10×25").assertIsDisplayed()
        compose.onNodeWithText("8.8级达克罗",substring=true).assertIsDisplayed()
        val newHeight=compose.onNodeWithTag("board-card-stock-0").fetchSemanticsNode().boundsInRoot.height
        assertEquals(oldHeight,newHeight,1f)
        screenshot("packing-spec-model-320-large.png")
    }

    @Test fun reprintPreviewNeedsBothChecksAndChangedMaximumNeedsNewPreview() {
        var confirmed: Pair<Boolean, Boolean>? = null; var maximum = ""
        val preview = preview()
        val initial = WorkbenchState(destination = Destination.Tasks, dailyWork = state().copy(packingPrintPreview = preview))
        compose.setContent { WorkbenchApp(initial,
            actions().copy(confirmStockPackingPrint = { printed, unknown -> confirmed = printed to unknown }, previewStockPackingPrint = { maximum = it })) }
        compose.onNodeWithTag("stock-print-confirm").assertIsNotEnabled()
        compose.onAllNodes(isToggleable())[0].performClick()
        compose.onNodeWithTag("stock-print-confirm").assertIsNotEnabled()
        compose.onAllNodes(isToggleable())[1].performClick()
        compose.onNodeWithTag("stock-print-confirm").assertIsEnabled()
        screenshot("stock-print-preview-390.png", dialog = true)
        compose.onNodeWithTag("stock-print-confirm").performClick()
        assertEquals(true to true, confirmed)
        compose.onNodeWithTag("stock-print-maximum").performTextInput("5")
        compose.onNodeWithTag("stock-print-confirm").assertIsNotEnabled()
        compose.onNodeWithText("重新预览数量").performClick()
        assertEquals("5", maximum)
    }

    @Test fun phaseChangeAsksBeforeClearingCrossPageSelectionAndPageNavigationDoesNot() {
        var state by mutableStateOf(state().copy(boardSelectedIds = setOf("stock-0", "stock-50")))
        val calls = mutableListOf<String>()
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state), actions().copy(
            clearTaskBoardSelection = { calls += "clear"; state = state.copy(boardSelectedIds = emptySet()) },
            selectTaskBoardSection = { calls += it }, changeStockPackingPage = { calls += "page-$it" })) }
        compose.onNodeWithTag("stock-page-next").performClick()
        assertEquals(listOf("page-2"), calls)
        compose.onNodeWithText("清空当前选择？").assertDoesNotExist()
        compose.onNodeWithText("处理中 5").performClick()
        compose.onNodeWithText("清空当前选择？").assertIsDisplayed()
        compose.onNodeWithText("保留选择").performClick()
        assertEquals(listOf("page-2"), calls)
        compose.onNodeWithText("处理中 5").performClick()
        compose.onNodeWithText("清空并继续").performClick()
        assertEquals(listOf("page-2", "clear", "in_progress"), calls)
        screenshot("stock-paged-todo-390.png")
    }

    @Test fun customPageSizeHasBoundsAndFilterDraftIsExplicit() {
        var chosen: StockPackingFilters? = null
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state()), actions().copy(
            setStockPackingFilters = { filters, _, _, _, _ -> chosen = filters })) }
        compose.onNodeWithText("筛选").performClick()
        compose.onNodeWithTag("stock-page-size").performScrollTo().performTextReplacement("101")
        compose.onNodeWithText("应用筛选").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithTag("stock-page-size").performTextReplacement("17")
        compose.onNodeWithText("应用筛选").assertIsEnabled()
        screenshot("stock-filter-custom-page-390.png", dialog = true)
        compose.onNodeWithText("应用筛选").performClick()
        assertEquals(17, chosen!!.pageSize)
    }

    @Test fun reservedDetailsShowCurrentPrinterSeparateFromPreviousPrintRecord() {
        val item = state().board.items.single()
        val line = TaskBoardLine("line-1", "task-1", 1, "ORDER-1", printState = "reserved", canPrint = false,
            printBlockedBy = "订单正在打印", currentPrintActorName = "包装二", latestPrintActorName = "包装一", latestPrintAt = "10-08 09:00")
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Tasks, dailyWork = state().copy(boardDetail = TaskBoardDetail(item, listOf(line)))), actions()) }
        compose.onNodeWithText("正在打印：包装二；请等待该打印完成").assertIsDisplayed()
        compose.onNodeWithText("最近打印：包装二 · 10-08 09:00").assertDoesNotExist()
        screenshot("stock-reserved-detail-390.png")
    }

    private fun state() = DailyWorkState(me = CurrentUser("u1", "worker", "包装一", listOf(ROLE_STOCK_PACKING)),
        selectedRole = ROLE_STOCK_PACKING, boardSection = "todo", board = TaskBoardPage(
            listOf(TaskBoardItem("stock-0", "packing", ROLE_STOCK_PACKING, "M6 内六角圆柱头螺钉", sku = "LS0001", quantity = "100", unit = "支",
                location = "B6-3", canPrint = true, printState = "unprinted", orderCount = 3)), total = 121, orderCount = 180,
            dataVersion = "d1", taskVersion = "t1", printStateVersion = "p1", workStateCounts = mapOf("todo" to 121, "in_progress" to 5)))
    private fun preview(): PackingPrintPreview {
        val orders = listOf(PackingPrintOrder("p", "ORDER-1", "printed", "r1", "f1", "包装一", "10-08 09:00", 2),
            PackingPrintOrder("p", "ORDER-2", "uncertain", "r2", "f2", labelCount = 1))
        return PackingPrintPreview("preview-1", "a".repeat(64), 2, 3, 1, 1, orders, listOf(PackingPrintSegment(1, orders.map { it.orderNo }, 2, 3)))
    }
    private fun actions() = WorkbenchActions(navigate = {}, search = { _, _ -> }, changeScope = { _, _ -> }, toggleSelection = {}, selectAll = {}, loadMore = {},
        printSelected = {}, printAll = {}, cancelPrint = {}, requestSplit = {}, dismissSplit = {}, editingChanged = {}, scanPrinters = {}, connectPrinter = {},
        disconnectPrinter = {}, saveServer = {}, checkServer = {}, clearMessage = {})
    private fun screenshot(name: String, dialog: Boolean = false) {
        compose.runOnIdle {
            val main = compose.activity.window.decorView
            val view = if (dialog) WindowInspector.getGlobalWindowViews().last { it !== main && it.width > 0 && it.height > 0 } else main
            val bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888); view.draw(Canvas(bitmap))
            val target = File("build/test-screenshots/$name"); target.parentFile?.mkdirs()
            target.outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
        }
    }
}
