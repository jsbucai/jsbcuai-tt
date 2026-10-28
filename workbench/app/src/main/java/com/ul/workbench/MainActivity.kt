package com.ul.workbench

import android.Manifest
import android.annotation.SuppressLint
import android.os.Build
import android.os.Bundle
import android.view.KeyEvent
import android.view.inputmethod.InputMethodManager
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.compose.runtime.getValue
import com.ul.workbench.data.WorkbenchActions
import com.ul.workbench.device.HardwareScanner
import com.ul.workbench.ui.WorkbenchApp

class MainActivity : ComponentActivity() {
    private val model: WorkbenchViewModel by viewModels()
    private val scanner = HardwareScanner()
    private val scannerKeysDown = mutableSetOf<Pair<Int, Int>>()
    private var permissionAction: (() -> Unit)? = null
    private val permissions = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted ->
        if (granted.values.all { it }) permissionAction?.invoke()
        else model.message("连接打印机需要附近设备权限，请在系统设置中允许后重试")
        permissionAction = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(statusBarStyle = SystemBarStyle.light(android.graphics.Color.TRANSPARENT, android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.light(android.graphics.Color.WHITE, android.graphics.Color.BLACK))
        if (Build.VERSION.SDK_INT >= 29) window.isNavigationBarContrastEnforced = false
        val actions = WorkbenchActions(
            navigate = { scanner.reset(); hideKeyboard(); model.navigate(it) },
            search = { value, mode -> scanner.reset(); hideKeyboard(); model.search(value, mode) },
            changeScope = { scope, logistics -> scanner.reset(); model.changeScope(scope, logistics) },
            changeOrderFilters = { filters -> scanner.reset(); hideKeyboard(); model.changeOrderFilters(filters) },
            toggleSelection = model::toggleSelection, selectAll = model::selectAll, loadMore = model::loadMore,
            printSelected = { hideKeyboard(); scanner.reset(); model.printSelected(it) },
            printAll = { hideKeyboard(); scanner.reset(); model.printAll(it) },
            cancelPrint = model::cancelPrint, requestSplit = { scanner.reset(); model.requestSplit() },
            dismissSplit = { hideKeyboard(); scanner.reset(); model.dismissSplit() },
            editingChanged = { scanner.reset(); model.editingChanged(it); if (!it) hideKeyboard() },
            scanPrinters = { withBluetoothPermission { model.printer.scan() } },
            connectPrinter = { address -> withBluetoothPermission { model.printer.connect(address) } },
            disconnectPrinter = { model.printer.disconnect() },
            saveServer = { hideKeyboard(); model.saveServer(it) }, checkServer = model::checkServer,
            clearMessage = model::clearMessage,
            searchInventory = { value, filters -> scanner.reset(); hideKeyboard(); model.searchInventory(value, filters) },
            searchInventoryCode = { value -> scanner.reset(); hideKeyboard(); model.searchInventoryCode(value) },
            clearQuery = { scanner.reset(); hideKeyboard(); model.clearQuery() },
            addAdjustment = { item, type, quantity, note -> scanner.reset(); hideKeyboard(); model.addAdjustment(item, type, quantity, note) },
            removeAdjustment = model::removeAdjustment, uploadAdjustments = model::uploadAdjustments,
            retryAdjustment = model::retryAdjustment, restoreAdjustment = model::restoreAdjustment,
            confirmAdjustment = model::confirmAdjustment, refreshAdjustments = model::refreshAdjustments,
            login = model::login, loginWithUsername = model::login, logout = model::logout, updateData = model::updateData,
            showUpdateDetails = model::showUpdateDetails, dismissUpdateDetails = model::dismissUpdateDetails,
            resolveFreight = model::resolveFreight,
            openProductDetail = model::openProductDetail, openOrderProductDetail = model::openOrderProductDetail,
            closeProductDetail = model::closeProductDetail, setInventoryHistoryDays = model::setInventoryHistoryDays,
            loadMoreInventoryHistory = model::loadMoreInventoryHistory,
            resetOrderPrintState = model::resetOrderPrintState,
            loadBusinessConfirmations = model::loadBusinessConfirmations,
            loadPackingFilterOptions = model::loadPackingFilterOptions,
            dailyWorkVisible = model::dailyWorkVisible, refreshDailyWork = model::refreshDailyWork,
            retryDailyActions = model::retryDailyActions,
            selectDailyRole = model::selectDailyRole, selectDailyView = model::selectDailyView,
            submitDailyAction = model::submitDailyAction, openDailyOrder = model::openDailyOrder,
            loadStaff = model::loadStaff, createStaff = model::createStaff, updateStaff = model::updateStaff,
            resetStaffPassword = model::resetStaffPassword, setStaffAttendance = model::setStaffAttendance,
            setAssignmentEnabled = model::setAssignmentEnabled, transferDailyTask = model::transferDailyTask,
            saveAssignmentRule = model::saveAssignmentRule, deleteAssignmentRule = model::deleteAssignmentRule,
            openPurchaseUnfinished = model::openPurchaseUnfinished, closePurchaseUnfinished = model::closePurchaseUnfinished,
            refreshPurchaseUnfinished = model::refreshPurchaseUnfinished, openPurchaseSupplier = model::openPurchaseSupplier,
            closePurchaseSupplier = model::closePurchaseSupplier, loadMorePurchaseUnfinished = model::loadMorePurchaseUnfinished,
            editPurchasePromise = model::editPurchasePromise, resumePurchasePromise = model::resumePurchasePromise,
            closePurchasePromise = model::closePurchasePromise, discardPurchasePromise = model::discardPurchasePromise,
            updatePurchasePromise = model::updatePurchasePromise, savePurchasePromise = model::savePurchasePromise,
            reviewPurchasePromise = model::reviewPurchasePromise, acceptPurchasePromiseReview = model::acceptPurchasePromiseReview,
            loadCustomerOwners = model::loadCustomerOwners, saveCustomerOwner = model::saveCustomerOwner,
            openDashboardCompletions = model::openDashboardCompletions,
            loadMoreDashboardCompletions = model::loadMoreDashboardCompletions,
            closeDashboardCompletions = model::closeDashboardCompletions,
            setDailyTaskUrgent = model::setDailyTaskUrgent,
            discardDailyActionReview = model::discardDailyActionReview,
            resetDailyOrderPrintState = model::resetDailyOrderPrintState,
            selectCustomerOrderFilter = model::selectCustomerOrderFilter,
            searchCustomerOrders = model::searchCustomerOrders,
            loadMoreCustomerOrders = model::loadMoreCustomerOrders,
            openCustomerOrder = model::openCustomerOrder,
            closeCustomerOrder = model::closeCustomerOrder,
            recordCustomerOrderFollowup = model::recordCustomerOrderFollowup,
            transferCustomerOrder = model::transferCustomerOrder,
            loadMoreTaskBoard = model::loadMoreTaskBoard, openTaskBoardItem = model::openTaskBoardItem,
            closeTaskBoardDetail = model::closeTaskBoardDetail, searchTaskBoard = model::searchTaskBoard,
            selectTaskBoardStatus = model::selectTaskBoardStatus, toggleTaskBoardItem = model::toggleTaskBoardItem,
            toggleTaskBoardLine = model::toggleTaskBoardLine, selectAllTaskBoard = model::selectAllTaskBoard,
            clearTaskBoardSelection = model::clearTaskBoardSelection,
            submitTaskBoardBatch = model::submitTaskBoardBatch, printTaskBoardSelection = model::printTaskBoardSelection,
            startTaskBoardSales = model::startTaskBoardSales, resetTaskBoardSales = model::resetTaskBoardSales,
            saveTaskBoardSalesNote = model::saveTaskBoardSalesNote, undoTaskBoardSalesStart = model::undoTaskBoardSalesStart,
            exportTaskBoardImages = model::exportTaskBoardImages, clearTaskBoardExport = model::clearTaskBoardExport,
            selectTaskBoardSection = model::selectTaskBoardSection, loadMoreTaskHistory = model::loadMoreTaskHistory,
            openTaskHistoryItem = model::openTaskHistoryItem, closeTaskHistoryDetail = model::closeTaskHistoryDetail,
            openAdminDashboard = model::openAdminDashboard, closeAdminDashboard = model::closeAdminDashboard,
            openDashboardRole = model::openDashboardRole,
            setTaskBoardFilters = model::setTaskBoardFilters,
            setStockPackingFilters = model::setStockPackingFilters, changeStockPackingPage = model::changeStockPackingPage,
            selectStockPackingPage = model::selectStockPackingPage, previewStockPackingPrint = model::previewStockPackingPrint,
            confirmStockPackingPrint = model::confirmStockPackingPrint, dismissStockPackingPrint = model::dismissStockPackingPrint,
            acknowledgeStockPackingPrint = model::acknowledgeStockPackingPrint, resetStockPackingPrint = model::resetStockPackingPrint
        )
        setContent {
            val state by model.state.collectAsStateWithLifecycle()
            val assistants by model.assistantState.collectAsStateWithLifecycle()
            WorkbenchApp(state, actions, assistants, model.assistantActions)
        }
    }

    /** Receives HID scanner events at Activity level; no invisible EditText or soft keyboard. */
    @SuppressLint("RestrictedApi") // Public Activity callback; Core's inherited annotation also flags the required super call.
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        val key = event.deviceId to event.keyCode
        // The sixth key changes printBusy synchronously. Its UP and trailing
        // Enter must not reach the new "停止发送" button or a newly opened editor.
        if (event.action == KeyEvent.ACTION_UP && scannerKeysDown.remove(key)) return true
        val isEnter = event.keyCode == KeyEvent.KEYCODE_ENTER || event.keyCode == KeyEvent.KEYCODE_NUMPAD_ENTER
        val char = event.unicodeChar.toChar()
        val isScannerChar = char in 'A'..'Z' || char in 'a'..'z' || char in '0'..'9'
        val modified = event.isCtrlPressed || event.isAltPressed || event.isMetaPressed
        if (!modified && isEnter && scanner.ownsTerminator(event.eventTime)) {
            if (event.action == KeyEvent.ACTION_DOWN) scannerKeysDown.add(key)
            return true
        }
        // Ignore another scanner burst while a print is in flight. Touch and
        // accessibility clicks on the explicit stop action still work normally.
        if (!modified && model.state.value.printBusy && (isEnter || isScannerChar)) {
            scanner.reset()
            if (event.action == KeyEvent.ACTION_DOWN) scannerKeysDown.add(key)
            return true
        }
        if (!model.acceptsScan || model.assistantState.value.panel != null || currentFocus?.onCheckIsTextEditor() == true || modified) {
            scanner.reset()
            return super.dispatchKeyEvent(event)
        }
        if (!isEnter && !isScannerChar) {
            if (event.unicodeChar != 0) scanner.reset()
            return super.dispatchKeyEvent(event)
        }
        if (event.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0) {
            scannerKeysDown.add(key)
            val code = if (isEnter) scanner.enter(event.eventTime) else scanner.feed(char, event.eventTime)
            code?.let { hideKeyboard(); model.scanned(it) }
        }
        return true
    }

    override fun onPause() { scanner.reset(); scannerKeysDown.clear(); super.onPause() }

    private fun hideKeyboard() {
        (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager).hideSoftInputFromWindow(window.decorView.windowToken, 0)
    }

    private fun withBluetoothPermission(action: () -> Unit) {
        val required = if (Build.VERSION.SDK_INT >= 31) arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
            else arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)
        if (required.all { checkSelfPermission(it) == android.content.pm.PackageManager.PERMISSION_GRANTED }) action()
        else { permissionAction = action; permissions.launch(required) }
    }
}
