package com.ul.workbench.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import com.ul.workbench.data.*
import com.ul.workbench.ui.screens.InventoryCodeField
import com.ul.workbench.ui.screens.inventoryCodeKeyboardOptions
import com.ul.workbench.ui.theme.WorkbenchTheme
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

/** Manual-code events only, with fake callbacks; no server, HID decoder or printer connection. */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@LooperMode(LooperMode.Mode.PAUSED)
@Config(sdk = [35], qualifiers = "w390dp-h844dp-mdpi")
@OptIn(ExperimentalTestApi::class)
class InventoryCodeInputTest {
    @get:Rule val compose = createComposeRule()

    @Test fun codeIsFirstAndSixthCharacterQueriesWithoutSearchButton() {
        val codes = mutableListOf<String>()
        var combinedQueries = 0
        var editing = false
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Inventory), actions().copy(
            searchInventoryCode = { codes += it }, searchInventory = { _, _ -> combinedQueries++ }, editingChanged = { editing = it })) }
        openManual()
        compose.onAllNodes(hasSetTextAction()).onFirst().assert(hasText("商品编号")).assertIsFocused()
        assertTrue(editing)
        codeField().performTextInput("l")
        codeField().assertTextContains("L")
        codeField().performTextInput("s")
        codeField().assertTextContains("LS")
        codeField().performTextInput("000")
        assertEquals(emptyList<String>(), codes)
        codeField().performTextInput("1")
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
        compose.waitForIdle()
        assertEquals(listOf("LS0001"), codes)
        assertEquals(0, combinedQueries)
        assertEquals(false, editing)
        compose.onNode(hasText("订单") and hasClickAction()).assertIsDisplayed()
    }

    @Test fun validPasteUsesDedicatedCodeActionDespiteOtherFilters() {
        var code: String? = null
        var combinedQueries = 0
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Inventory, query = "A01", queried = true,
            inventoryFilters = InventoryFilters(name = "不匹配的名称", spec = "M99", model = "A2", material = "铜")),
            actions().copy(searchInventoryCode = { code = it }, searchInventory = { _, _ -> combinedQueries++ })) }
        openManual()
        codeField().performTextReplacement("ld1234")
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
        assertEquals("LD1234", code)
        assertEquals(0, combinedQueries)
    }

    @Test fun malformedAndIncompleteCodesDoNotSubmitOrGetSilentlyTruncated() {
        val codes = mutableListOf<String>()
        compose.setContent { WorkbenchTheme { InventoryCodeField(onComplete = { codes += it }) } }
        listOf("L", "LS", "LS123", "L0001", "LS00O1", "LS00011", "LS0001X", "ＳＳ0001", "LS0001 ").forEach { value ->
            codeField().performTextReplacement(value)
            codeField().performImeAction()
            codeField().assertTextContains(value)
        }
        assertEquals(emptyList<String>(), codes)
        compose.onNodeWithText("编号需为 2 位英文字母和 4 位数字").assertIsDisplayed()
    }

    @Test fun sameFrameRepeatedPasteAndImeOnlySubmitOnce() {
        val codes = mutableListOf<String>()
        compose.setContent { WorkbenchTheme { InventoryCodeField(onComplete = { codes += it }) } }
        val setText = codeField().fetchSemanticsNode().config[SemanticsActions.SetText].action!!
        compose.runOnIdle {
            setText(AnnotatedString("ls0001"))
            setText(AnnotatedString("ls0001"))
        }
        codeField().performImeAction()
        assertEquals(listOf("LS0001"), codes)
    }

    @Test fun backspaceAllowsPrefixCorrectionBeforeCompletion() {
        var code: String? = null
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Inventory), actions().copy(searchInventoryCode = { code = it })) }
        openManual()
        codeField().performTextInput("ls")
        codeField().performKeyInput { pressKey(Key.Backspace) }
        codeField().assertTextContains("L")
        codeField().performTextInput("d0001")
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
        assertEquals("LD0001", code)
    }

    @Test fun keyboardRequestsLettersThenNumbersAndLettersAfterPrefixDeletion() {
        val letters = inventoryCodeKeyboardOptions(TextFieldValue("L", TextRange(1)))
        assertEquals(KeyboardType.Ascii, letters.keyboardType)
        assertEquals(KeyboardCapitalization.Characters, letters.capitalization)
        assertEquals(false, letters.autoCorrectEnabled)
        val numbers = inventoryCodeKeyboardOptions(TextFieldValue("LS", TextRange(2)))
        assertEquals(KeyboardType.Number, numbers.keyboardType)
        assertEquals(KeyboardType.Number, inventoryCodeKeyboardOptions(TextFieldValue("LS00", TextRange(4))).keyboardType)
        assertEquals(KeyboardType.Ascii, inventoryCodeKeyboardOptions(TextFieldValue("L", TextRange(1))).keyboardType)
        assertEquals(KeyboardType.Ascii, inventoryCodeKeyboardOptions(TextFieldValue("LS00", TextRange(0, 2))).keyboardType)
    }

    @Test fun reopeningExactResultDoesNotRequeryOrRetainCodeAsKeywordFilter() {
        var automatic = 0
        var combined: Pair<String, InventoryFilters>? = null
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Inventory, queried = true,
            query = "LS0001", queryMode = "sku_exact", inventoryFilters = InventoryFilters(sku = "LS0001")),
            actions().copy(searchInventoryCode = { automatic++ }, searchInventory = { keyword, filters -> combined = keyword to filters })) }
        compose.onNodeWithText("LS0001").assertIsDisplayed()
        openManual()
        codeField().assert(SemanticsMatcher.expectValue(SemanticsProperties.EditableText, AnnotatedString("")))
        assertEquals(0, automatic)
        compose.onNode(hasSetTextAction() and hasText("名称")).performScrollTo().performTextReplacement("六角螺栓")
        compose.onNode(hasSetTextAction() and hasText("名称")).performImeAction()
        assertEquals("" to InventoryFilters(name = "六角螺栓"), combined)
        assertEquals(0, automatic)
    }

    @Test @Config(sdk = [35], qualifiers = "w320dp-h568dp-mdpi")
    fun codeInputIsReachableInNarrowKeyboardSizedViewport() {
        var code: String? = null
        compose.setContent { Box(Modifier.height(330.dp)) {
            WorkbenchApp(WorkbenchState(destination = Destination.Inventory), actions().copy(searchInventoryCode = { code = it }))
        } }
        openManual()
        codeField().performScrollTo().performTextInput("ls0")
        codeField().assertIsDisplayed()
        saveScreenshot("inventory-code-320-keyboard-space-fixture.png")
        codeField().performTextInput("001")
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
        assertEquals("LS0001", code)
    }

    @Test @Config(sdk = [35], qualifiers = "w320dp-h568dp-mdpi")
    fun largeTextKeepsCodeAboveProductFilters() {
        compose.setContent { CompositionLocalProvider(LocalDensity provides Density(1f, 1.5f)) {
            WorkbenchApp(WorkbenchState(destination = Destination.Inventory), actions())
        } }
        openManual()
        codeField().performScrollTo().performTextInput("ls")
        codeField().assertIsDisplayed()
        saveScreenshot("inventory-code-320-large-text-fixture.png")
        val codeTop = codeField().getUnclippedBoundsInRoot().top
        val nameTop = compose.onNode(hasSetTextAction() and hasText("名称")).getUnclippedBoundsInRoot().top
        assertTrue(codeTop < nameTop)
    }

    @Test fun screenshotMainCodeField390() {
        compose.setContent { WorkbenchApp(WorkbenchState(destination = Destination.Inventory), actions()) }
        openManual()
        codeField().performTextInput("ls")
        saveScreenshot("inventory-code-390-fixture.png")
    }

    private fun openManual() {
        compose.onNodeWithText("手动输入").performScrollTo().performClick()
        compose.waitForIdle()
    }
    private fun codeField() = compose.onNode(hasSetTextAction() and hasText("商品编号"))
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
            bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
            view.draw(Canvas(bitmap))
        }
        val cwd = File(requireNotNull(System.getProperty("user.dir")))
        val app = if (cwd.name == "app") cwd else File(cwd, "app")
        val file = File(File(app, "build/outputs/screenshots").apply { mkdirs() }, name)
        file.outputStream().use { check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)) }
        bitmap.recycle()
        check(file.length() > 0)
    }
}
