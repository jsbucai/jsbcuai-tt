package com.ul.workbench.ui

import androidx.activity.ComponentActivity
import androidx.compose.runtime.*
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import com.ul.workbench.data.*
import com.ul.workbench.ui.screens.PurchaseUnfinishedScreen
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import org.robolectric.annotation.LooperMode

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@LooperMode(LooperMode.Mode.PAUSED)
@Config(sdk = [35], qualifiers = "w390dp-h844dp-mdpi")
class PurchaseUnfinishedUiTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    @Test fun internalTrialShowsSupplierAndLinesWhileReplyAndShareStayDisabled() {
        val meta = PurchaseSnapshotMeta(status = "review", publicationStage = "internal_trial",
            snapshotId = "trial-1", capturedAt = "2026-10-06T00:00:00Z", accountScopeVerified = true,
            rowCount = 1, supplierCount = 1)
        val supplier = PurchaseSupplier("S01", "供应商甲", 1, 1)
        val line = PurchaseUnfinishedLine("line-1", 1, "S01", "供应商甲", "PO1",
            "FB0001", "螺栓", "主仓", "2026-10-01", "", "10", "2", "8", "件", ambiguous = false)
        var state by mutableStateOf(PurchaseUnfinishedState(open = true, meta = meta, suppliers = listOf(supplier)))
        var editCalls = 0
        val actions = actions().copy(openPurchaseSupplier = {
            state = state.copy(selectedSupplier = it,
                page = PurchaseUnfinishedPage(meta, listOf(line), total = 1))
        }, editPurchasePromise = { editCalls++ })
        compose.setContent { PurchaseUnfinishedScreen(state, actions) }

        compose.onNodeWithText("采购未交／供应商 · 内部试用").assertIsDisplayed()
        compose.onNodeWithText("供应商甲").performClick()
        compose.onNodeWithText("供应商甲 · 内部试用").assertIsDisplayed()
        compose.onNodeWithText("FB0001 · 螺栓").assertIsDisplayed()
        compose.onNodeWithText("记录／核对回复交期").assertIsNotEnabled()
        compose.onNodeWithText("内部试用期间不可催货分享").assertIsNotEnabled()
        assertEquals(0, editCalls)
    }

    private fun actions() = WorkbenchActions(
        navigate = {}, search = { _, _ -> }, changeScope = { _, _ -> }, toggleSelection = {}, selectAll = {}, loadMore = {},
        printSelected = {}, printAll = {}, cancelPrint = {}, requestSplit = {}, dismissSplit = {}, editingChanged = {},
        scanPrinters = {}, connectPrinter = {}, disconnectPrinter = {}, saveServer = {}, checkServer = {}, clearMessage = {}
    )
}
