package com.ul.workbench.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import com.ul.workbench.data.*
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import org.robolectric.annotation.LooperMode

/** Queue and ERP callbacks are in-memory fakes: these tests never submit business data. */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@LooperMode(LooperMode.Mode.PAUSED)
@Config(sdk = [35], qualifiers = "w390dp-h844dp-mdpi")
class AdjustmentUiTest {
    @get:Rule val compose = createComposeRule()

    @Test fun committedDraftClosesEditorBeforeCollectedStateArrives() {
        val app = androidx.test.core.app.ApplicationProvider.getApplicationContext<android.app.Application>()
        val prefs = app.getSharedPreferences("adjustment-ui-save-ack", 0)
        prefs.edit().clear().commit()
        val outbox = AdjustmentOutbox(prefs,
            kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Main),
            { error("Saving a local draft must not contact ERP") }, {})
        outbox.useProfileBucket("27056512834386351", LAN_ADJUSTMENT_SERVER)
        val shown = mutableStateOf(outbox.state)
        val open = mutableStateOf(true)
        val product = WorkItem("ls1619", "LS1619", "六角头螺栓（细牙）GB/T5786",
            spec = "M20-1.5*40", model = "8.8黑", profileId = "27056512834386351",
            warehouseId = "29871293290508390", productId = "27056672654724171",
            skuId = "27056672654724312", unitId = "1", unitName = "")
        compose.setContent {
            if (open.value) com.ul.workbench.ui.screens.AdjustmentEditor(product, shown.value,
                actions().copy(addAdjustment = { item, type, quantity, note -> outbox.add(item, type, quantity, note) }),
                onDismiss = { open.value = false })
        }
        field("报损数量").performScrollTo().performTextReplacement("1")
        compose.onNodeWithText("加入清单").performScrollTo().performClick()
        assertEquals("1", outbox.state.drafts.single().line.quantity)
        // StateFlow/lifecycle collection can deliver after the editor's own state.
        compose.runOnIdle { shown.value = outbox.state }
        assertEquals(false, open.value)
        val restored = AdjustmentOutbox(prefs,
            kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Main),
            { error("No ERP request") }, {})
        restored.useProfileBucket("27056512834386351", LAN_ADJUSTMENT_SERVER)
        assertEquals("1", restored.state.drafts.single().line.quantity)
    }

    @Test fun unavailableApiKeepsDraftAndExplainsWhyUploadCannotStart() {
        var uploads = 0
        val draft = AdjustmentDraft(AdjustmentType.Loss, line())
        val state = WorkbenchState(destination = Destination.Devices, adjustmentUpload = false,
            adjustments = AdjustmentState(drafts = listOf(draft)))
        compose.setContent { WorkbenchApp(state, actions().copy(uploadAdjustments = { uploads++ })) }
        compose.onNodeWithText("局域网上传暂不可用，草稿继续保存在手机。").assertIsDisplayed()
        compose.onNodeWithText("上传报损清单（1）").performScrollTo().performClick()
        compose.onNodeWithText("请连接公司 Wi-Fi，确认原电脑 192.168.1.5 的 5191 服务已启动后重试。本次不会提交 ERP，清单继续保存在手机。").assertIsDisplayed()
        compose.onNodeWithText("确认上传").assertDoesNotExist()
        compose.onNodeWithText("知道了").performClick()
        assertEquals(0, uploads)
        assertEquals(listOf(draft), state.adjustments.drafts)
    }

    @Test fun localAdjustmentSavesDecimalAndReturnsWithoutUploading() {
        val state = mutableStateOf(inventory())
        var editing = false
        var uploaded = false
        var saved: AdjustmentDraft? = null
        compose.setContent { WorkbenchApp(state.value, actions().copy(
            editingChanged = { editing = it }, uploadAdjustments = { uploaded = true },
            addAdjustment = { item, type, quantity, note ->
                saved = AdjustmentDraft(type, line().copy(sku = item.sku, quantity = quantity, note = note))
                state.value = state.value.copy(adjustments = AdjustmentState(drafts = listOf(saved!!), message = "已加入报溢清单，到设备页统一上传"))
                AdjustmentSaveResult(true)
            })) }
        openEditor()
        assertEquals(true, editing)
        compose.onAllNodes(hasText("订单") and hasClickAction()).assertCountEquals(0)
        compose.onNodeWithText("报溢", useUnmergedTree = true).performScrollTo().performClick()
        field("报溢数量").performScrollTo().performTextReplacement("1.2500")
        field("备注（可选）").performScrollTo().performTextReplacement("盘点多出")
        saveScreenshot("adjustment-editor-390-fixture.png")
        field("备注（可选）").performImeAction()
        assertEquals(AdjustmentDraft(AdjustmentType.Overflow, line().copy(quantity = "1.25", note = "盘点多出")), saved)
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
        compose.onNode(hasText("订单") and hasClickAction()).assertIsDisplayed()
        assertEquals(false, editing)
        assertEquals(false, uploaded)
    }

    @Test fun invalidQuantityAndStorageFailureNeverCloseEditor() {
        val state = mutableStateOf(inventory())
        var attempts = 0
        compose.setContent { WorkbenchApp(state.value, actions().copy(addAdjustment = { _, _, _, _ ->
            attempts++
            state.value = state.value.copy(adjustments = AdjustmentState(storageError = true, message = "清单保存失败，请检查手机存储"))
            AdjustmentSaveResult(false, "清单保存失败，请检查手机存储")
        })) }
        openEditor()
        listOf("0", "-1", "1.1234567").forEach { value ->
            field("报损数量").performScrollTo().performTextReplacement(value)
            compose.onNodeWithText("加入清单").performScrollTo().assertIsNotEnabled()
            field("报损数量").performImeAction()
        }
        assertEquals(0, attempts)
        field("报损数量").performScrollTo().performTextReplacement("2")
        field("报损数量").performImeAction()
        assertEquals(1, attempts)
        compose.onNodeWithText("清单保存失败，请检查手机存储").assertIsDisplayed()
        field("报损数量").assertTextContains("2")
        compose.onNodeWithText("加入清单").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("取消").performScrollTo().performClick()
        compose.onNode(hasText("订单") and hasClickAction()).assertIsDisplayed()
    }

    @Test fun noteIsLimitedTo200AndRepeatedValidationFailureCanBeRetried() {
        val state = mutableStateOf(inventory())
        var attempts = 0
        var noteLength = 0
        compose.setContent { WorkbenchApp(state.value, actions().copy(addAdjustment = { _, _, _, note ->
            attempts++
            noteLength = note.length
            state.value = state.value.copy(adjustments = AdjustmentState(message = "合并后的备注不能超过 200 字，请精简备注"))
            AdjustmentSaveResult(false, "合并后的备注不能超过 200 字，请精简备注")
        })) }
        openEditor()
        field("报损数量").performScrollTo().performTextReplacement("1")
        field("备注（可选）").performScrollTo().performTextReplacement("测".repeat(220))
        field("备注（可选）").performImeAction()
        assertEquals(200, noteLength)
        compose.onNodeWithText("加入清单").performScrollTo().assertIsEnabled().performClick()
        assertEquals(2, attempts)
        compose.onNodeWithText("加入清单").assertIsEnabled()
    }

    @Test fun uploadRequiresReviewedConfirmationAndPausesScanning() {
        var uploads = 0
        var editing = false
        compose.setContent { WorkbenchApp(deviceQueue(), actions().copy(uploadAdjustments = { uploads++ }, editingChanged = { editing = it })) }
        compose.onNodeWithText("上传报损清单（1）").performScrollTo().performClick()
        compose.waitForIdle()
        assertEquals(0, uploads)
        assertEquals(true, editing)
        compose.onNode(hasText("数量 2.5") and hasAnyAncestor(isDialog())).assertIsDisplayed()
        compose.onNodeWithText("取消").performClick()
        compose.waitForIdle()
        assertEquals(0, uploads)
        assertEquals(false, editing)
        compose.onNodeWithText("上传报损清单（1）").performScrollTo().performClick()
        compose.onNodeWithText("确认上传").performClick()
        assertEquals(1, uploads)
        compose.onAllNodes(isDialog()).assertCountEquals(0)
    }

    @Test fun changedDraftRequiresFreshReviewBeforeUpload() {
        val state = mutableStateOf(deviceQueue())
        var uploads = 0
        compose.setContent { WorkbenchApp(state.value, actions().copy(uploadAdjustments = { uploads++ })) }
        compose.onNodeWithText("上传报损清单（1）").performScrollTo().performClick()
        compose.runOnIdle { state.value = state.value.copy(adjustments = state.value.adjustments.copy(
            drafts = listOf(AdjustmentDraft(AdjustmentType.Loss, line().copy(quantity = "9")))) ) }
        compose.onNodeWithText("确认上传").assertIsNotEnabled()
        compose.onNodeWithText("清单已更新，请返回核对后重新上传。").assertIsDisplayed()
        assertEquals(0, uploads)
    }

    @Test fun fastSecondSaveDoesNotAddQuantityTwice() {
        val state = mutableStateOf(inventory())
        var saves = 0
        compose.setContent { WorkbenchApp(state.value, actions().copy(addAdjustment = { _, type, quantity, _ ->
            saves++
            state.value = state.value.copy(adjustments = AdjustmentState(drafts = listOf(AdjustmentDraft(type, line().copy(quantity = quantity)))))
            AdjustmentSaveResult(true)
        })) }
        openEditor()
        field("报损数量").performScrollTo().performTextReplacement("2")
        val click = compose.onNodeWithText("加入清单").performScrollTo().fetchSemanticsNode().config[SemanticsActions.OnClick].action!!
        compose.runOnIdle { click(); click() }
        assertEquals(1, saves)
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
    }

    @Test fun draftDetailsCanBeRemovedAndStatusRefreshed() {
        var removed: Pair<AdjustmentType, String>? = null
        var refreshes = 0
        compose.setContent { WorkbenchApp(deviceQueue(), actions().copy(
            removeAdjustment = { type, sku -> removed = type to sku }, refreshAdjustments = { refreshes++ })) }
        compose.onNodeWithText("查看报损明细").performScrollTo().performClick()
        compose.onNodeWithText("数量 2.5").assertIsDisplayed()
        compose.onNodeWithText("移除").performClick()
        assertEquals(AdjustmentType.Loss to "TEST01", removed)
        compose.onNodeWithContentDescription("刷新上传状态").performScrollTo().performClick()
        assertEquals(1, refreshes)
        saveScreenshot("adjustment-queue-390-fixture.png")
    }

    @Test fun activeBatchBlocksOnlyItsOwnDraftTypeAndUnknownCannotRetry() {
        val queue = AdjustmentState(drafts = listOf(AdjustmentDraft(AdjustmentType.Loss, line()), AdjustmentDraft(AdjustmentType.Overflow, line())),
            batches = listOf(batch("running"), batch("unknown").copy(id = "unknown", type = AdjustmentType.Overflow, retryable = true)))
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Devices, adjustments = queue), actions()) }
        compose.onNodeWithText("上传报损清单（1）").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("上传报溢清单（1）").performScrollTo().assertIsEnabled()
        compose.onNode(hasScrollAction()).performScrollToNode(hasText("请先在 ERP 核对上传结果。"))
        compose.onNodeWithText("请先在 ERP 核对上传结果。").assertIsDisplayed()
        compose.onAllNodesWithText("重试报溢上传").assertCountEquals(0)
        saveScreenshot("adjustment-queue-unknown-390-fixture.png")
    }

    @Test fun unknownNeedsDocumentReviewBeforeItCanBeClosedWithoutUploading() {
        var confirmations = 0
        var uploads = 0
        var retries = 0
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Devices,
            adjustments = AdjustmentState(batches = listOf(batch("unknown").copy(documentNo = "2026-09-10-12-08-17-546a085c")))), actions().copy(
                confirmAdjustment = { confirmations++ }, uploadAdjustments = { uploads++ }, retryAdjustment = { retries++ })) }
        compose.onNodeWithText("已核对，确认入账").performScrollTo().performClick()
        compose.onNodeWithText("确认这份清单已入账？").assertIsDisplayed()
        compose.onNode(hasText("数量 2.5") and hasAnyAncestor(isDialog())).assertIsDisplayed()
        assertEquals(0, confirmations)
        compose.onNodeWithText("取消").performClick()
        assertEquals(0, confirmations)
        compose.onNodeWithText("已核对，确认入账").performScrollTo().performClick()
        compose.onNodeWithText("确认已入账").performClick()
        assertEquals(1, confirmations)
        assertEquals(0, uploads)
        assertEquals(0, retries)
    }

    @Test fun retryableFailureNeedsExplicitConfirmation() {
        var retried: String? = null
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Devices,
            adjustments = AdjustmentState(batches = listOf(batch("failed").copy(retryable = true)))),
            actions().copy(retryAdjustment = { retried = it })) }
        compose.onNodeWithText("重试报损上传").performScrollTo().performClick()
        assertEquals(null, retried)
        compose.onNodeWithText("确认上传").performClick()
        assertEquals("batch-test", retried)
    }

    @Test fun storageErrorBlocksUpload() {
        compose.setContent { WorkbenchApp(deviceQueue().copy(adjustments = deviceQueue().adjustments.copy(storageError = true,
            message = "本地清单读取失败")), actions()) }
        compose.onNodeWithText("上传报损清单（1）").performScrollTo().assertIsNotEnabled()
    }

    @Test fun onlyRejectedBatchCanReturnToLocalDrafts() {
        var restored: String? = null
        val state = mutableStateOf(WorkbenchState(destination = Destination.Devices,
            adjustments = AdjustmentState(batches = listOf(batch("rejected")))))
        compose.setContent { WorkbenchApp(state.value, actions().copy(restoreAdjustment = { restored = it })) }
        compose.onNodeWithText("退回待上传清单").performScrollTo().performClick()
        assertEquals("batch-test", restored)
        compose.onAllNodes(isDialog()).assertCountEquals(0)
        compose.runOnIdle { state.value = state.value.copy(adjustments = AdjustmentState(batches = listOf(batch("unknown")))) }
        compose.onNode(hasScrollAction()).performScrollToNode(hasText("清单 batch-te"))
        compose.onNodeWithText("清单 batch-te").assertIsDisplayed()
        compose.onAllNodesWithText("退回待上传清单").assertCountEquals(0)
    }

    @Test @Config(sdk = [35], qualifiers = "w320dp-h568dp-mdpi")
    fun narrowEditorRemainsUsableInKeyboardSizedViewport() {
        var saved: String? = null
        compose.setContent { Box(Modifier.height(330.dp)) {
            WorkbenchApp(inventory(), actions().copy(addAdjustment = { _, _, quantity, _ ->
                saved = quantity; AdjustmentSaveResult(true) }))
        } }
        openEditor()
        field("报损数量").performScrollTo().performTextReplacement("0.5")
        field("备注（可选）").performScrollTo().performTextReplacement("盘点")
        compose.onNodeWithText("先保存到手机，设备页统一上传。").performScrollTo()
        compose.onNodeWithText("加入清单").performScrollTo().assertIsDisplayed().assertIsEnabled()
        saveScreenshot("adjustment-editor-320-keyboard-space-fixture.png")
        field("备注（可选）").performImeAction()
        assertEquals("0.5", saved)
    }

    @Test @Config(sdk = [35], qualifiers = "w320dp-h568dp-mdpi")
    fun largeTextEditorAndQueueKeepActionsReachable() {
        val state = mutableStateOf(inventory())
        compose.setContent {
            CompositionLocalProvider(LocalDensity provides Density(1f, 1.5f)) {
                WorkbenchApp(state.value, actions())
            }
        }
        openEditor()
        field("报损数量").performScrollTo().performTextReplacement("2.5")
        compose.onNodeWithText("先保存到手机，设备页统一上传。").performScrollTo()
        compose.onNodeWithText("加入清单").assertIsDisplayed()
        saveScreenshot("adjustment-editor-320-large-text-fixture.png")
        compose.onNode(hasScrollAction()).performScrollToNode(hasText("取消"))
        compose.onNodeWithText("取消").performClick()
        compose.runOnIdle { state.value = deviceQueue() }
        compose.onNode(hasScrollAction()).performScrollToNode(hasText("上传报损清单（1）"))
        compose.onNodeWithText("上传报损清单（1）").assertIsDisplayed()
        saveScreenshot("adjustment-queue-320-large-text-fixture.png")
        compose.onNodeWithText("上传报损清单（1）").performClick()
        compose.onNodeWithText("确认上传").assertIsDisplayed()
    }

    private fun openEditor() {
        compose.onNode(hasScrollAction()).performScrollToNode(hasContentDescription("库存详情与调整"))
        compose.onNodeWithContentDescription("库存详情与调整").performClick()
        compose.onNodeWithText("库存调整").assertIsDisplayed()
        compose.waitForIdle()
    }

    private fun field(label: String) = compose.onNode(hasSetTextAction() and hasText(label))
    private fun line() = AdjustmentLine("TEST01", "测试六角螺栓", "GB/T 5782  M12 × 50", "8.8级发黑", "2.5")
    private fun batch(status: String) = AdjustmentBatch("batch-test", "fixture-client", AdjustmentType.Loss, listOf(line()), status,
        message = if (status == "unknown") "连接中断，暂未确认 ERP 结果" else "测试上传状态")
    private fun deviceQueue() = WorkbenchState(destination = Destination.Devices,
        adjustments = AdjustmentState(drafts = listOf(AdjustmentDraft(AdjustmentType.Loss, line()))))
    private fun inventory() = WorkbenchState(destination = Destination.Inventory, query = "TEST01", queried = true, total = 1,
        stock = "30", available = "25", items = listOf(WorkItem("item-1", "TEST01", "测试六角螺栓",
            spec = "GB/T 5782  M12 × 50", model = "8.8级发黑", location = "A01", stock = "30", available = "25")))

    private fun actions() = WorkbenchActions(navigate = {}, search = { _, _ -> }, changeScope = { _, _ -> },
        toggleSelection = {}, selectAll = {}, loadMore = {}, printSelected = {}, printAll = {}, cancelPrint = {},
        requestSplit = {}, dismissSplit = {}, editingChanged = {}, scanPrinters = {}, connectPrinter = {},
        disconnectPrinter = {}, saveServer = {}, checkServer = {}, clearMessage = {})

    private fun saveScreenshot(name: String) {
        compose.waitForIdle()
        lateinit var bitmap: Bitmap
        compose.runOnIdle {
            val activity = ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED).first()
            val view = activity.window.decorView
            check(view.width > 0 && view.height > 0)
            bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
            view.draw(Canvas(bitmap))
        }
        val cwd = File(requireNotNull(System.getProperty("user.dir")))
        val app = if (cwd.name == "app") cwd else File(cwd, "app")
        val file = File(File(app, "build/outputs/screenshots").apply { mkdirs() }, name)
        file.outputStream().use { output -> check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)) }
        bitmap.recycle()
        check(file.length() > 0)
    }
}
