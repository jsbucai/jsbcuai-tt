package com.ul.workbench.ui

import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import android.content.Intent
import android.provider.OpenableColumns
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.text.input.PasswordVisualTransformation
import com.ul.workbench.data.*
import com.ul.workbench.ui.components.*
import com.ul.workbench.ui.screens.*
import com.ul.workbench.ui.theme.WorkbenchColors
import com.ul.workbench.ui.theme.WorkbenchTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Native UI boundary. Network, scan decoding and printer writes are owned externally. */
@Composable
fun WorkbenchApp(state: WorkbenchState, actions: WorkbenchActions,
                 assistants: AssistantUiState = AssistantUiState(), assistantActions: AssistantActions = AssistantActions()) {
    WorkbenchTheme {
        if (state.loginRequired && !state.printBusy) {
            LoginScreen(state, actions)
            return@WorkbenchTheme
        }
        if (state.connectionRequired && !state.printBusy) {
            ConnectionScreen(state, actions)
            return@WorkbenchTheme
        }
        val pageStateHolder = rememberSaveableStateHolder()
        var manualOpen by rememberSaveable(state.destination) { mutableStateOf(false) }
        var details by remember { mutableStateOf<WorkItem?>(null) }
        var inventoryDetail by remember { mutableStateOf<WorkItem?>(null) }
        var adjustmentItem by remember { mutableStateOf<WorkItem?>(null) }
        var serverEditing by remember { mutableStateOf(false) }
        var companyPicker by rememberSaveable { mutableStateOf(false) }
        val editing = assistants.panel != null || state.dailyWork.purchaseUnfinished.editorOpen || manualOpen || details != null || inventoryDetail != null || adjustmentItem != null || state.splitRequested || serverEditing || companyPicker || state.updateDetailsOpen
        val context = LocalContext.current
        val attachmentScope = rememberCoroutineScope()
        val currentAssistantActions by rememberUpdatedState(assistantActions)
        val currentAssistants by rememberUpdatedState(assistants)
        val currentMainState by rememberUpdatedState(state)
        var pickerContext by remember { mutableStateOf("") }
        fun currentAttachmentContext() = listOf(currentMainState.serverUrl, currentAssistants.sales.session?.profileId.orEmpty(), currentMainState.dailyWork.me?.id.orEmpty(),
            currentAssistants.sales.session?.id.orEmpty()).joinToString("\n")
        val attachments = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
            val expectedContext = pickerContext
            attachmentScope.launch {
                uris.forEach { uri ->
                    if (expectedContext != currentAttachmentContext()) {
                        currentAssistantActions.attachmentError("账号或会话已切换，请在当前会话重新选择文件。")
                        return@launch
                    }
                    try {
                        val file = withContext(Dispatchers.IO) {
                            try { context.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) } catch (_: SecurityException) { }
                            var name = uri.lastPathSegment.orEmpty()
                            var size: Long? = null
                            context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { cursor ->
                                if (cursor.moveToFirst()) {
                                    val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                                    val sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE)
                                    if (nameIndex >= 0 && !cursor.isNull(nameIndex)) name = cursor.getString(nameIndex)
                                    if (sizeIndex >= 0 && !cursor.isNull(sizeIndex)) size = cursor.getLong(sizeIndex)
                                }
                            }
                            AssistantLocalAttachment(uri.toString(), name, context.contentResolver.getType(uri).orEmpty(), size)
                        }
                        if (expectedContext == currentAttachmentContext()) currentAssistantActions.attachSalesFile(file)
                        else currentAssistantActions.attachmentError("账号或会话已切换，请重新选择文件。")
                    } catch (_: Exception) {
                        currentAssistantActions.attachmentError("文件暂时无法读取，文字和草稿已保留，请重新选择。")
                    }
                }
            }
        }
        val keyboard = LocalSoftwareKeyboardController.current
        val focus = LocalFocusManager.current
        LaunchedEffect(editing) { actions.editingChanged(editing) }
        DisposableEffect(Unit) { onDispose { actions.editingChanged(false) } }
        LaunchedEffect(state.destination) { details = null; inventoryDetail = null; actions.closeProductDetail(); adjustmentItem = null; serverEditing = false; companyPicker = false }
        BackHandler(enabled = details == null && !companyPicker && !state.updateDetailsOpen && (editing || state.destination != Destination.Tasks || state.printBusy)) {
            when {
                state.printBusy -> Unit // Keep the sending queue and its stop action on screen.
                state.splitRequested -> { keyboard?.hide(); focus.clearFocus(); actions.dismissSplit() }
                adjustmentItem != null -> { keyboard?.hide(); focus.clearFocus(); adjustmentItem = null }
                inventoryDetail != null -> { inventoryDetail = null; actions.closeProductDetail() }
                manualOpen || serverEditing -> {
                    keyboard?.hide(); focus.clearFocus(); manualOpen = false; serverEditing = false
                }
                state.destination != Destination.Tasks -> actions.navigate(Destination.Tasks)
            }
        }
        Scaffold(
            containerColor = WorkbenchColors.Background,
            contentWindowInsets = WindowInsets.safeDrawing,
            bottomBar = {
                if (!editing) Column {
                    if (state.destination in setOf(Destination.Orders, Destination.Inventory) && (state.items.isNotEmpty() || state.printBusy)) PrintActionBar(state, actions)
                    NavigationBar(containerColor = WorkbenchColors.Paper, tonalElevation = 0.dp) {
                        listOf(Destination.Tasks to ("任务" to WorkbenchIcons.Tasks), Destination.Orders to ("订单" to WorkbenchIcons.Orders),
                            Destination.Inventory to ("库存" to WorkbenchIcons.Inventory), Destination.Devices to ("设备" to WorkbenchIcons.Devices)).forEach { (destination, visual) ->
                            NavigationBarItem(selected = state.destination == destination,
                                enabled = !state.printBusy,
                                onClick = { keyboard?.hide(); focus.clearFocus(); manualOpen = false; details = null; inventoryDetail = null;
                                    actions.closeProductDetail(); adjustmentItem = null; serverEditing = false; companyPicker = false; actions.navigate(destination) },
                                icon = { Icon(visual.second, contentDescription = null, modifier = Modifier.size(23.dp)) }, label = { Text(visual.first) },
                                colors = NavigationBarItemDefaults.colors(selectedIconColor = WorkbenchColors.Red, selectedTextColor = WorkbenchColors.Red, indicatorColor = WorkbenchColors.Tinted, unselectedIconColor = WorkbenchColors.Muted, unselectedTextColor = WorkbenchColors.Muted))
                        }
                    }
                }
            }
        ) { innerPadding ->
            // safeDrawing includes IME; consume it once. No nested imePadding.
            val content = Modifier.padding(innerPadding).consumeWindowInsets(innerPadding)
            if (state.splitRequested) SplitEditor(state, actions, content)
            else if (adjustmentItem != null) AdjustmentEditor(adjustmentItem!!, state.adjustments, actions, content, onDismiss = { adjustmentItem = null }, uploadAvailable = state.adjustmentUpload)
            else if (inventoryDetail != null) InventoryDetailScreen(state.productDetail, actions, content,
                onDismiss = { inventoryDetail = null; actions.closeProductDetail() },
                onAdjust = { state.productDetail.item?.let { adjustmentItem = it } },
                localPendingAdjustments = state.productDetail.item?.let { localPendingAdjustments(state.adjustments, it) }.orEmpty())
            else when (state.destination) {
                Destination.Tasks -> DailyWorkScreen(state.dailyWork, actions, content, visible = !editing, assistantActions = assistantActions)
                Destination.Orders, Destination.Inventory -> pageStateHolder.SaveableStateProvider(state.destination.name) {
                    WorkScreen(state, actions, content, manualOpen,
                        onManual = { manualOpen = it }, onDetails = {
                            if (state.destination == Destination.Inventory) {
                                if (it.productId.isBlank()) adjustmentItem = it else {
                                    inventoryDetail = it
                                    actions.openProductDetail(it)
                                }
                            } else details = it
                        }, onInventoryDetails = { item ->
                            inventoryDetail = item
                            actions.openOrderProductDetail(item)
                        }, onScopeDetails = { companyPicker = true })
                }
                Destination.Devices -> DevicesScreen(state, actions, content, onEditing = { serverEditing = it })
            }
        }
        details?.let { item -> ItemDetails(item, onDismiss = { details = null },
            onResetPrint = if (!item.requiresSettlement && item.printState in setOf("printed", "uncertain"))
                ({ actions.resetOrderPrintState(item.orderNo); details = null }) else null) }
        if (state.updateDetailsOpen) UpdateResultDialog(state, actions)
        if (companyPicker) LogisticsDialog(state.logistics, onSelect = { company ->
            companyPicker = false
            actions.changeScope("stock", company)
        }, onDismiss = { companyPicker = false })
        assistants.panel?.let { panel ->
            BackHandler {
                if (panel == AssistantPanel.Sales && assistants.sales.editor != null && assistants.sales.editorOpen) assistantActions.closeSalesDraftEditor()
                else assistantActions.close()
            }
                Surface(color = WorkbenchColors.Background, modifier = Modifier.fillMaxSize()) {
                    // A full-screen panel shares the Activity's measured bounds
                    // and consumes keyboard/system insets once.
                    val panelModifier = Modifier.safeDrawingPadding()
                    when (panel) {
                        AssistantPanel.Sales -> SalesAssistantScreen(assistants.sales, assistantActions, panelModifier,
                            onPickAttachment = {
                                pickerContext = currentAttachmentContext()
                                attachments.launch(arrayOf("image/*", "application/pdf", "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
                            }, webOperations = assistants.webOperations,
                            webOperationsError = assistants.webOperationsError)
                        AssistantPanel.Purchase -> PurchaseAssistantScreen(assistants.purchase, assistantActions, panelModifier,
                            webOperations = assistants.webOperations, webOperationsError = assistants.webOperationsError)
                    }
                }
        }
    }
}

private fun localPendingAdjustments(state: AdjustmentState, item: WorkItem): List<String> {
    fun matches(line: AdjustmentLine) = when {
        item.productId.isNotBlank() && line.productId.isNotBlank() -> item.productId == line.productId &&
            item.profileId == line.profileId && item.skuId == line.skuId && item.unitId == line.unitId &&
            item.warehouseId.ifBlank { "29871293290508390" } == line.warehouseId
        else -> item.sku.equals(line.sku, ignoreCase = true)
    }
    val drafts = state.drafts.filter { matches(it.line) }.map { "手机${it.type.label}草稿 ${it.line.quantity}" }
    val batches = state.batches.flatMap { batch -> batch.items.filter(::matches).map { line ->
        "${batch.type.label}${batch.statusLabel} ${line.quantity}"
    } }
    return drafts + batches
}

@Composable
private fun ConnectionScreen(state: WorkbenchState, actions: WorkbenchActions) {
    var server by remember(state.serverUrl) { mutableStateOf(state.serverUrl) }
    Surface(color = WorkbenchColors.Background, modifier = Modifier.fillMaxSize()) {
        Column(Modifier.safeDrawingPadding().imePadding().padding(24.dp).fillMaxWidth(),
            verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Text("UL 随行", style = MaterialTheme.typography.headlineMedium)
            Text("连接数据服务", style = MaterialTheme.typography.titleLarge)
            Text("连接成功后继续登录、查询和打印。", color = WorkbenchColors.Muted)
            OutlinedTextField(server, { server = it }, label = { Text("服务地址") },
                modifier = Modifier.fillMaxWidth(), singleLine = true)
            Text(state.serverStatus.ifBlank { "正在连接…" }, color = WorkbenchColors.Muted)
            if (!state.error.isNullOrBlank()) Text(state.error, color = WorkbenchColors.Error)
            PrimaryAction("连接服务", {
                if (server.trim() != state.serverUrl) actions.saveServer(server) else actions.checkServer()
            }, Modifier.fillMaxWidth(), enabled = server.isNotBlank())
        }
    }
}

@Composable
private fun LoginScreen(state: WorkbenchState, actions: WorkbenchActions) {
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var server by remember(state.serverUrl) { mutableStateOf(state.serverUrl) }
    Surface(color = WorkbenchColors.Background, modifier = Modifier.fillMaxSize()) {
        Column(Modifier.safeDrawingPadding().imePadding().padding(24.dp).fillMaxWidth(),
            verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Text("UL 随行", style = MaterialTheme.typography.headlineMedium)
            Text("登录后查看今日任务、查询和包装", color = WorkbenchColors.Muted)
            OutlinedTextField(server, { server = it }, label = { Text("服务地址") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
            if (server != state.serverUrl) TextButton(onClick = { actions.saveServer(server) }) { Text("保存服务地址") }
            OutlinedTextField(username, { username = it }, label = { Text("登录账号（管理员可留空）") }, modifier = Modifier.fillMaxWidth(),
                singleLine = true, enabled = !state.loginBusy)
            OutlinedTextField(password, { password = it }, label = { Text("密码") }, modifier = Modifier.fillMaxWidth(),
                visualTransformation = PasswordVisualTransformation(), singleLine = true, enabled = !state.loginBusy)
            if (!state.error.isNullOrBlank()) Text(state.error, color = WorkbenchColors.Error)
            PrimaryAction(if (state.loginBusy) "正在登录…" else "登录", {
                val user = username; val value = password; password = ""; actions.loginWithUsername(user, value)
            }, Modifier.fillMaxWidth(), enabled = password.isNotBlank() && !state.loginBusy && server == state.serverUrl)
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun PrintActionBar(state: WorkbenchState, actions: WorkbenchActions) {
    val inventory = state.destination == Destination.Inventory
    val selectedRows = state.items.filter { it.id in state.selectedIds }
    val count = if (inventory) selectedRows.size else selectedRows.map { it.orderNo.ifBlank { it.id } }.distinct().size
    val selectable = state.items.filter { it.printable && (inventory || it.printState == "unprinted") }
    Surface(color = WorkbenchColors.Paper, shadowElevation = 3.dp, border = BorderStroke(0.5.dp, WorkbenchColors.Line)) {
        Column(Modifier.fillMaxWidth().padding(horizontal = 18.dp, vertical = 10.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
            if (state.printBusy) {
                Text(state.printMessage.ifBlank { "正在发送标签…" }, style = MaterialTheme.typography.bodyMedium)
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth(), color = WorkbenchColors.Red, trackColor = WorkbenchColors.Neutral)
                QuietAction("停止发送", actions.cancelPrint, Modifier.fillMaxWidth())
            } else {
                FlowRow(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(0.dp)) {
                    Box(Modifier.heightIn(min = 48.dp), contentAlignment = Alignment.CenterStart) {
                        Text("已选 $count ${if (inventory) "项" else "单"}", style = MaterialTheme.typography.labelLarge)
                    }
                    TextButton(onClick = actions.selectAll, modifier = Modifier.heightIn(min = 48.dp), enabled = selectable.isNotEmpty()) {
                        Text(if (selectable.isNotEmpty() && selectable.all { it.id in state.selectedIds }) "取消全选" else "本页全选")
                    }
                    if (state.total > state.items.size && (inventory || state.orderFilters.printState == "unprinted")) TextButton(onClick = { actions.printAll(null) }, modifier = Modifier.heightIn(min = 48.dp), enabled = state.total <= 100 && !state.uncertainPrint && state.items.all(WorkItem::printable)) {
                        Text("全部 ${state.total}")
                    }
                }
                if (state.uncertainPrint) Text("结果不确定，请先核对实际出纸。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
                Row(horizontalArrangement = Arrangement.spacedBy(9.dp)) {
                    if (!inventory) QuietAction("拆箱", actions.requestSplit, Modifier.weight(0.34f), enabled = count > 0 && !state.uncertainPrint, icon = WorkbenchIcons.Split)
                    PrimaryAction(if (count == 0) "选择后打印" else "打印 $count ${if (inventory) "项" else "单"}", { actions.printSelected(null) },
                        Modifier.weight(if (inventory) 1f else 0.66f), enabled = count > 0 && !state.loading && !state.uncertainPrint, icon = WorkbenchIcons.Printer)
                }
            }
        }
    }
}
