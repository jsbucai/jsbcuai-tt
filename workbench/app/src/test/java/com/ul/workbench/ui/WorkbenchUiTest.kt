package com.ul.workbench.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import com.ul.workbench.data.*
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import org.robolectric.annotation.LooperMode

/** All callbacks are in-memory fakes. No network, printer connection or device write. */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@LooperMode(LooperMode.Mode.PAUSED)
@Config(sdk = [35], qualifiers = "w390dp-h844dp-mdpi")
class WorkbenchUiTest {
    @get:Rule val compose = createComposeRule()

    @Test fun disconnectedFirstStartOffersConnectionInsteadOfEmptyWorkspace() {
        var connects = 0
        compose.setContent { WorkbenchApp(WorkbenchState(connectionRequired = true,
            serverUrl = "https://ul.yingyinghome.com", serverStatus = "服务地址暂不可用"),
            fakeActions().copy(checkServer = { connects++ })) }
        compose.onNodeWithText("连接数据服务").assertIsDisplayed()
        compose.onNodeWithText("服务地址暂不可用").assertIsDisplayed()
        compose.onNodeWithText("直接扫描商品条码").assertDoesNotExist()
        saveScreenshot("connection-first-start-390-fixture.png")
        compose.onNodeWithText("连接服务").performClick()
        assertEquals(1, connects)
    }

    @Test fun defaultScanHasNoEditableField() {
        compose.setContent { WorkbenchApp(WorkbenchState(), fakeActions()) }
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
        compose.onNodeWithText("直接扫描商品条码").assertIsDisplayed()
    }

    @Test fun orderHeadingOffersTheSharedUpdateActionAndProgress() {
        var updates = 0
        var details = 0
        val state = mutableStateOf(WorkbenchState(apiUpdates = true, serverOnline = true,
            sourceUpdatedAt = "09-26 10:00", updateTask = UpdateTask("job", "partial", "物流部分失败", published = true, failed = 1)))
        compose.setContent { WorkbenchApp(state.value, fakeActions().copy(updateData = { updates++ }, showUpdateDetails = { details++ })) }
        compose.onNodeWithText("更新订单").assertIsDisplayed().performClick()
        assertEquals(1, updates)
        compose.onNodeWithText("更新结果").performClick()
        assertEquals(1, details)
        state.value = state.value.copy(updateBusy = true, updateTask = UpdateTask("job", "running", "正在更新", stage = "reading", completed = 2, total = 5))
        compose.onNodeWithText("正在更新…").assertIsNotEnabled()
        compose.onNodeWithText("读取订单与库存 · 2/5").assertIsDisplayed()
        saveScreenshot("order-update-progress-390-fixture.png")
    }

    @Test fun readingProgressShowsTheParsedSourceInsteadOfCountingEverythingAsOrders() {
        val state = mutableStateOf(WorkbenchState(apiUpdates = true, serverOnline = true, updateBusy = true))
        compose.setContent { WorkbenchApp(state.value, fakeActions()) }
        val labels = listOf("products" to "读取商品", "orders" to "读取未发货清单", "order_details" to "读取订单摘要",
            "inventory" to "读取库存", "inventory_products" to "读取库存商品", "purchase_history" to "读取进货流水")
        labels.forEach { (source, label) ->
            val task = HttpWorkbenchRepository.parseTask(org.json.JSONObject("""{
                "id":"sync","status":"running","stage":"reading",
                "result":{"source":"$source","progress":{"completed":18000,"total":18052}}}
            """))
            assertEquals(source, task.source)
            state.value = state.value.copy(updateTask = task)
            compose.onNodeWithText("$label · 18000/18052").assertIsDisplayed()
        }
    }

    @Test fun freightResolutionRequiresAnExplicitVerifiedConclusion() {
        val resolutions = mutableListOf<Triple<String, String, String>>()
        val state = WorkbenchState(updateDetailsOpen = true, updateTask = UpdateTask("job", "unknown", "待核对", unknown = 1,
            published = true, orders = 2, inventory = 3,
            attempts = listOf(FreightAttempt("a1", "ORDER#1", "unknown", "待到货", "@09/26#ORDER#1"))))
        compose.setContent { WorkbenchApp(state, fakeActions().copy(resolveFreight = { id, result, note -> resolutions += Triple(id, result, note) })) }
        compose.onNodeWithText("核对此单").performScrollTo().performClick()
        compose.onNodeWithText("保存核对结论").assertIsNotEnabled()
        compose.onNodeWithText("已核对，与目标一致").performScrollTo().performClick()
        compose.onNodeWithText("保存核对结论").assertIsEnabled()
        compose.onNodeWithText("保存核对结论").performClick()
        assertEquals(Triple("a1", "applied", "已在管家婆核对，物流公司及单号均与目标一致"), resolutions.single())
    }

    @Test fun manualSearchEmitsIntentAndClosesEditor() {
        var emitted: Pair<String, String>? = null
        val actions = fakeActions().copy(search = { query, mode -> emitted = query to mode })
        compose.setContent { WorkbenchApp(WorkbenchState(), actions) }
        compose.onNodeWithText("手动输入").performClick()
        compose.onNodeWithText("查询").assertDoesNotExist()
        compose.onNode(hasSetTextAction()).performTextReplacement("DH0019")
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
        assertEquals("DH0019" to "sku", emitted)
    }

    @Test fun inventoryManualSearchCombinesEveryFilledConditionAndClosesEditor() {
        var emitted: Pair<String, InventoryFilters>? = null
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Inventory),
            fakeActions().copy(searchInventory = { keyword, filters -> emitted = keyword to filters })) }
        compose.onNodeWithText("手动输入").performClick()
        compose.onNodeWithText("查询").assertIsNotEnabled()
        inventoryField("名称").performTextReplacement(" 六角螺栓 ")
        inventoryField("规格").performTextReplacement(" M12 ")
        inventoryField("等级 / 型号").performTextReplacement(" 8.8 ")
        saveScreenshot("inventory-form-390-fixture.png")
        compose.onNodeWithText("更多条件 · 货位、材质").performClick()
        inventoryField("关键词 / 货位").performScrollTo().performTextReplacement(" A01 ")
        inventoryField("材质").performScrollTo().performTextReplacement(" 碳钢 ")
        inventoryField("材质").performImeAction()
        assertEquals("A01" to InventoryFilters(name = "六角螺栓", spec = "M12", model = "8.8", material = "碳钢"), emitted)
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
        compose.onNode(hasText("订单") and hasClickAction()).assertIsDisplayed()
    }

    @Test fun inventoryManualSearchAcceptsSpecificationWithoutKeyword() {
        var emitted: Pair<String, InventoryFilters>? = null
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Inventory),
            fakeActions().copy(searchInventory = { keyword, filters -> emitted = keyword to filters })) }
        compose.onNodeWithText("手动输入").performClick()
        inventoryField("规格").performTextReplacement("M10×30")
        inventoryField("规格").performImeAction()
        assertEquals("" to InventoryFilters(spec = "M10×30"), emitted)
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
    }

    @Test fun inventoryFilterSummaryAndEditorPreserveSeparateFields() {
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Inventory, queried = true,
            inventoryFilters = InventoryFilters(name = "六角螺栓", spec = "M12", model = "8.8")), fakeActions()) }
        compose.onNodeWithText("六角螺栓 · M12 · 8.8").assertIsDisplayed()
        compose.onNodeWithText("手动输入").performClick()
        inventoryField("名称").assertTextContains("六角螺栓")
        inventoryField("规格").assertTextContains("M12")
        inventoryField("等级 / 型号").assertTextContains("8.8")
        compose.onNodeWithText("更多条件 · 货位、材质").performClick()
        inventoryField("关键词 / 货位").assertTextEquals("关键词 / 货位", "")
    }

    @Test fun orderModelIsVisibleInCardAndDetails() {
        compose.setContent { WorkbenchApp(fixture().copy(items = fixture().items.take(1), total = 1), fakeActions()) }
        orderList().performScrollToNode(hasText("型号 8.8级发黑"))
        compose.onNodeWithText("型号 8.8级发黑").assertIsDisplayed()
        compose.onNodeWithContentDescription("查看订单详情").performScrollTo().performClick()
        compose.onNode(hasScrollAction() and hasAnyAncestor(isDialog())).performScrollToNode(hasText("型号"))
        compose.onNode(hasText("型号") and hasAnyAncestor(isDialog())).assertIsDisplayed()
        compose.onNode(hasText("8.8级发黑") and hasAnyAncestor(isDialog())).assertIsDisplayed()
    }

    @Test fun navigationRestoresOrderScrollAndNewQueryReturnsToTop() {
        val orders = fixture().copy(total = 20, selectedIds = emptySet(), queryRevision = 1,
            items = (1..20).map { number -> fixture().items.first().copy(id = "scroll-$number", buyer = "滚动测试客户 $number") })
        val state = mutableStateOf(orders)
        val actions = fakeActions().copy(navigate = { destination ->
            state.value = if (destination == Destination.Orders) orders else WorkbenchState(destination = destination)
        })
        compose.setContent { WorkbenchApp(state.value, actions) }
        orderList().performScrollToNode(hasText("滚动测试客户 8"))
        val scrollBefore = orderList().fetchSemanticsNode().config[SemanticsProperties.VerticalScrollAxisRange].value()
        assertTrue(scrollBefore > 0f)
        compose.onAllNodes(hasText("库存") and hasClickAction()).onLast().performClick()
        compose.onNode(hasText("订单") and hasClickAction()).performClick()
        compose.onNodeWithText("滚动测试客户 8").assertIsDisplayed()
        val scrollAfter = orderList().fetchSemanticsNode().config[SemanticsProperties.VerticalScrollAxisRange].value()
        assertEquals(scrollBefore, scrollAfter, 0.1f)
        compose.runOnIdle { state.value = state.value.copy(query = "NEXT01", queryRevision = 2) }
        compose.onNodeWithText("NEXT01").assertIsDisplayed()
        assertEquals(0f, orderList().fetchSemanticsNode().config[SemanticsProperties.VerticalScrollAxisRange].value(), 0.1f)
    }

    @Test fun clearingResultsIsAnExplicitAction() {
        var cleared = 0
        var navigated = 0
        compose.setContent { WorkbenchApp(fixture(), fakeActions().copy(clearQuery = { cleared++ }, navigate = { navigated++ })) }
        compose.onNode(hasText("订单") and hasClickAction()).performClick()
        assertEquals(1, navigated)
        assertEquals(0, cleared)
        compose.onNodeWithText("清空").performScrollTo().performClick()
        assertEquals(1, cleared)
    }

    @Test fun emptySelectionCannotPrintAgain() {
        compose.setContent { WorkbenchApp(fixture().copy(selectedIds = emptySet(), receipt = "测试回执：标签已发送。"), fakeActions()) }
        compose.onNodeWithText("选择后打印").assertIsNotEnabled()
        compose.onAllNodes(isDialog()).assertCountEquals(0)
    }

    @Test fun uncertainPrintRequiresExplicitAcknowledgement() {
        var acknowledged = false
        compose.setContent { WorkbenchApp(fixture().copy(uncertainPrint = true, error = "测试：打印结果不确定。"),
            fakeActions().copy(clearMessage = { acknowledged = true })) }
        compose.onNodeWithText("已核对出纸").assertIsDisplayed().performClick()
        compose.onAllNodesWithContentDescription("收起提示").assertCountEquals(0)
        assertEquals(true, acknowledged)
    }

    @Test fun sendingDisablesDetailAndNavigation() {
        compose.setContent { WorkbenchApp(fixture().copy(printBusy = true, printMessage = "测试：正在发送"), fakeActions()) }
        compose.onAllNodesWithContentDescription("查看订单详情").onFirst().assertIsNotEnabled()
        compose.onNodeWithText("设备").assertIsNotEnabled()
        compose.onNodeWithText("手动输入").assertIsNotEnabled()
        compose.onNodeWithText("停止发送").assertIsDisplayed()
    }

    @Test fun splitSubmitDoesNotDismissBeforePrint() {
        var limit: Int? = null
        var dismissed = 0
        val state = mutableStateOf(fixture().copy(splitRequested = true))
        compose.setContent { WorkbenchApp(state.value, fakeActions().copy(
            printSelected = { limit = it; state.value = state.value.copy(splitRequested = false) }, dismissSplit = { dismissed++ }
        )) }
        compose.onNode(hasSetTextAction()).performTextReplacement("20")
        compose.onNode(hasSetTextAction()).performImeAction()
        assertEquals(20, limit)
        assertEquals(0, dismissed)
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
        compose.onAllNodes(isDialog()).assertCountEquals(0)
        compose.onNodeWithText("打印 2 单").assertIsDisplayed()
    }

    @Test fun splitCancelReturnsWithoutPrinting() {
        val state = mutableStateOf(fixture().copy(splitRequested = true))
        var printed = false
        var dismissed = 0
        compose.setContent { WorkbenchApp(state.value, fakeActions().copy(
            printSelected = { printed = true }, dismissSplit = { dismissed++; state.value = state.value.copy(splitRequested = false) }
        )) }
        compose.onNodeWithText("确认拆箱打印").assertIsNotEnabled()
        compose.onNodeWithText("取消").performClick()
        assertEquals(false, printed)
        assertEquals(1, dismissed)
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
    }

    @Test fun splitPreflightErrorIsVisibleAndPreservesQuantity() {
        val state = mutableStateOf(fixture().copy(splitRequested = true))
        compose.setContent { WorkbenchApp(state.value, fakeActions().copy(
            printSelected = { state.value = state.value.copy(error = "测试：请先连接打印机。") }
        )) }
        compose.onNode(hasSetTextAction()).performTextReplacement("20")
        compose.onNode(hasSetTextAction()).performImeAction()
        compose.onNodeWithText("测试：请先连接打印机。").assertIsDisplayed()
        compose.onNode(hasSetTextAction()).assertTextContains("20")
        compose.onNodeWithText("取消").assertIsDisplayed().assertIsEnabled()
        saveScreenshot("split-390-error-fixture.png")
    }

    @Test fun splitUncertainResultRequiresAcknowledgementBeforeRetry() {
        val state = mutableStateOf(fixture().copy(splitRequested = true, uncertainPrint = true, error = "测试：打印结果不确定。"))
        var acknowledged = false
        var printed = false
        compose.setContent { WorkbenchApp(state.value, fakeActions().copy(
            clearMessage = { acknowledged = true; state.value = state.value.copy(uncertainPrint = false, error = null) },
            printSelected = { printed = true }
        )) }
        compose.onNodeWithText("测试：打印结果不确定。").assertIsDisplayed()
        compose.onNodeWithText("已核对出纸").assertIsDisplayed()
        compose.onAllNodesWithContentDescription("收起提示").assertCountEquals(0)
        compose.onNode(hasSetTextAction()).performTextReplacement("20")
        compose.onNodeWithText("确认拆箱打印").assertIsNotEnabled()
        compose.onNode(hasSetTextAction()).performImeAction()
        assertEquals(false, printed)
        compose.onNodeWithText("已核对出纸").performClick()
        assertEquals(true, acknowledged)
        compose.onNodeWithText("确认拆箱打印").assertIsEnabled()
    }

    @Test fun orderCategoryRowEmitsBusinessCategoryAndKeepsFiveStockGroups() {
        val state = mutableStateOf(fixture())
        var selected: Pair<String, String>? = null
        val actions = fakeActions().copy(changeScope = { scope, logistics ->
            selected = scope to logistics
            state.value = state.value.copy(scope = scope, logistics = logistics)
        })
        compose.setContent { WorkbenchApp(state.value, actions) }
        compose.onNodeWithContentDescription("订单分类 异常").performClick()
        assertEquals("all" to "异常", selected)
        compose.onNodeWithContentDescription("订单分类 全部").performClick()
        assertEquals("all" to "", selected)
        compose.onNodeWithContentDescription("订单分类列表").performScrollToIndex(6)
        compose.onNodeWithContentDescription("订单分类 库存").performClick()
        compose.onNodeWithText("库存分类").assertIsDisplayed()
        compose.onNode(hasText("LM库存件") and hasAnyAncestor(isDialog()), useUnmergedTree = true).performClick()
        assertEquals("stock" to "LM库存件", selected)
        compose.onAllNodes(isDialog()).assertCountEquals(0)
    }

    @Test fun companyPickerPausesScanningUntilDismissed() {
        var editing = false
        compose.setContent { WorkbenchApp(fixture(), fakeActions().copy(editingChanged = { editing = it })) }
        compose.onNodeWithContentDescription("订单分类列表").performScrollToIndex(6)
        compose.onNodeWithContentDescription("订单分类 库存").performClick()
        compose.waitForIdle()
        assertEquals(true, editing)
        compose.onNodeWithText("取消").performClick()
        compose.waitForIdle()
        assertEquals(false, editing)
    }

    @Test fun orderFilterDatesUseCalendarPickersInsteadOfTextInput() {
        compose.setContent { WorkbenchApp(fixture(), fakeActions()) }
        compose.onNodeWithContentDescription("打开订单筛选").performClick()
        compose.onNodeWithText("筛选订单").assertIsDisplayed()
        compose.onNodeWithText("YYYY-MM-DD").assertDoesNotExist()
        compose.onNodeWithContentDescription("选择订货日期开始日期").performClick()
        compose.onNodeWithText("订货开始日期").assertIsDisplayed()
        compose.onNodeWithText("确定").assertIsNotEnabled()
    }

    @Test fun orderFilterAppliesCompactChoicesAndPreservesDateRanges() {
        var applied: OrderFilters? = null
        val filters = OrderFilters(orderDateFrom = "2026-09-01", orderDateTo = "2026-09-30",
            deliveryDateFrom = "2026-10-01", deliveryDateTo = "2026-10-31")
        compose.setContent { WorkbenchApp(fixture().copy(orderFilters = filters),
            fakeActions().copy(changeOrderFilters = { applied = it })) }
        compose.onNodeWithContentDescription("打开订单筛选").performClick()
        compose.onNode(hasText("已打印") and hasAnyAncestor(isDialog()), useUnmergedTree = true).performClick()
        compose.onNode(hasText("货位") and hasAnyAncestor(isDialog()), useUnmergedTree = true).performClick()
        compose.onNodeWithText("应用筛选").performClick()
        assertEquals(filters.copy(printState = "printed", sort = "location"), applied)
    }

    @Test @Config(sdk = [35], qualifiers = "w320dp-h568dp-mdpi")
    fun orderFilterKeepsActionsVisibleOnSmallPhones() {
        compose.setContent { WorkbenchApp(fixture(), fakeActions()) }
        compose.onNodeWithContentDescription("打开订单筛选").performClick()
        compose.onNodeWithText("应用筛选").assertIsDisplayed()
        compose.onNodeWithContentDescription("选择交期结束日期").assertExists()
    }

    @Test fun screenshotOrders390() {
        compose.setContent { WorkbenchApp(fixture(), fakeActions()) }
        compose.onNodeWithText("打印 2 单").assertIsDisplayed()
        assertMetricsDisplayed()
        compose.onNodeWithText("界面测试客户一").assertIsDisplayed()
        saveScreenshot("orders-390-fixture.png")
    }

    @Test @Config(sdk = [35], qualifiers = "w320dp-h568dp-mdpi")
    fun screenshotOrders320() {
        compose.setContent { WorkbenchApp(fixture(), fakeActions()) }
        compose.onNodeWithText("打印 2 单").assertIsDisplayed()
        assertMetricsDisplayed()
        saveScreenshot("orders-320-fixture.png")
    }

    @Test @Config(sdk = [35], qualifiers = "w320dp-h568dp-mdpi")
    fun screenshotStockOrders320() {
        compose.setContent { WorkbenchApp(fixture().copy(scope = "stock", logistics = "LM库存件"), fakeActions()) }
        compose.onNodeWithText("打印 2 单").assertIsDisplayed()
        compose.onNodeWithText("库存 LM").assertIsDisplayed()
        assertMetricsDisplayed()
        saveScreenshot("orders-stock-320-fixture.png")
    }

    @Test @Config(sdk = [35], qualifiers = "w320dp-h568dp-mdpi")
    fun screenshotOrdersLargeText() {
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(1f, 1.5f)) { WorkbenchApp(fixture(), fakeActions()) }
        }
        compose.onNodeWithText("打印 2 单").assertIsDisplayed()
        saveScreenshot("orders-320-large-text-fixture.png")
    }

    @Test fun screenshotDevices390() {
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Devices), fakeActions()) }
        saveScreenshot("devices-390-empty.png")
    }

    @Test @Config(sdk = [35], qualifiers = "w320dp-h568dp-mdpi")
    fun inventoryFormWorksInNarrowKeyboardSizedViewport() {
        var emitted: InventoryFilters? = null
        // Restrict the content to the space left by a keyboard; no simulated IME success claim.
        compose.setContent { Box(Modifier.height(330.dp)) {
            WorkbenchApp(WorkbenchState(destination = Destination.Inventory),
                fakeActions().copy(searchInventory = { _, filters -> emitted = filters }))
        } }
        compose.onNodeWithText("手动输入").performScrollTo().performClick()
        inventoryField("名称").performScrollTo().performTextReplacement("六角螺栓")
        inventoryField("规格").performScrollTo().performTextReplacement("M12")
        inventoryField("等级 / 型号").performScrollTo().performTextReplacement("8.8")
        inventoryField("等级 / 型号").assertIsDisplayed()
        saveScreenshot("inventory-form-320-keyboard-space-fixture.png")
        inventoryField("等级 / 型号").performImeAction()
        assertEquals(InventoryFilters(name = "六角螺栓", spec = "M12", model = "8.8"), emitted)
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
    }

    @Test @Config(sdk = [35], qualifiers = "w320dp-h568dp-mdpi")
    fun inventoryFormLargeTextStacksFieldsAndAllowsSubmit() {
        var emitted: InventoryFilters? = null
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(1f, 1.5f)) {
                WorkbenchApp(WorkbenchState(destination = Destination.Inventory),
                    fakeActions().copy(searchInventory = { _, filters -> emitted = filters }))
            }
        }
        compose.onNodeWithText("手动输入").performScrollTo().performClick()
        inventoryField("规格").performScrollTo().performTextReplacement("M12 × 50")
        inventoryField("等级 / 型号").performScrollTo().performTextReplacement("A2-70 清洗")
        val specBounds = inventoryField("规格").getUnclippedBoundsInRoot()
        val modelBounds = inventoryField("等级 / 型号").getUnclippedBoundsInRoot()
        assertTrue(modelBounds.top >= specBounds.bottom)
        saveScreenshot("inventory-form-320-large-text-fixture.png")
        inventoryField("等级 / 型号").performImeAction()
        assertEquals(InventoryFilters(spec = "M12 × 50", model = "A2-70 清洗"), emitted)
    }

    private fun inventoryField(label: String) = compose.onNode(hasSetTextAction() and hasText(label))

    private fun saveScreenshot(name: String) {
        compose.waitForIdle()
        lateinit var bitmap: Bitmap
        compose.runOnIdle {
            val activity = ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED).firstOrNull()
                ?: error("Screenshot requires a resumed test activity")
            val view = activity.window.decorView
            check(view.width > 0 && view.height > 0) { "Screenshot has no measured view" }
            bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
            view.draw(Canvas(bitmap))
        }
        val cwd = File(requireNotNull(System.getProperty("user.dir")) { "Screenshot output requires user.dir" })
        val app = if (cwd.name == "app") cwd else File(cwd, "app")
        val folder = File(app, "build/outputs/screenshots").apply { mkdirs() }
        val file = File(folder, name)
        file.outputStream().use { output -> check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)) }
        bitmap.recycle()
        check(file.length() > 0) { "Screenshot output is empty" }
    }

    /** The three values must be visible before scrolling, not merely present in composition. */
    private fun assertMetricsDisplayed() {
        compose.onNodeWithText("120").assertIsDisplayed()
        compose.onNodeWithText("0").assertIsDisplayed()
        compose.onNodeWithText("300").assertIsDisplayed()
    }

    private fun orderList() = compose.onNode(hasContentDescription("订单结果列表"))

    private fun fixture() = WorkbenchState(
        query = "TEST01", queried = true, total = 2, demand = "120", available = "0", stock = "300",
        productName = "界面测试商品 · 六角螺栓 M12 × 50",
        selectedIds = setOf("fixture-1", "fixture-2"), items = listOf(
            WorkItem(id = "fixture-1", sku = "TEST01", name = "测试六角螺栓", spec = "GB/T 5782  M12 × 50", model = "8.8级发黑", orderNo = "测试订单 001", buyer = "界面测试客户一", quantity = "80", logistics = "LM库存件", stock = "300", available = "0"),
            WorkItem(id = "fixture-2", sku = "TEST01", name = "测试六角螺栓", spec = "GB/T 5782  M12 × 50", model = "8.8级发黑", orderNo = "测试订单 002", buyer = "界面测试客户二", quantity = "40", stock = "300", available = "0")
        )
    )

    private fun fakeActions() = WorkbenchActions(
        navigate = {}, search = { _, _ -> }, searchInventory = { _, _ -> }, clearQuery = {}, changeScope = { _, _ -> }, toggleSelection = {}, selectAll = {}, loadMore = {},
        printSelected = {}, printAll = {}, cancelPrint = {}, requestSplit = {}, dismissSplit = {}, editingChanged = {},
        scanPrinters = {}, connectPrinter = {}, disconnectPrinter = {}, saveServer = {}, checkServer = {}, clearMessage = {}
    )
}
