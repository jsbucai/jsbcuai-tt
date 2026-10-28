package com.ul.workbench.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.ul.workbench.data.WorkbenchActions
import com.ul.workbench.data.WorkbenchState
import com.ul.workbench.ui.components.*
import com.ul.workbench.ui.theme.WorkbenchColors
import com.ul.workbench.ui.theme.WorkbenchTokens

@Composable
fun DevicesScreen(state: WorkbenchState, actions: WorkbenchActions, modifier: Modifier = Modifier, onEditing: (Boolean) -> Unit) {
    var server by rememberSaveable(state.serverUrl) { mutableStateOf(state.serverUrl) }
    var serverFocused by remember { mutableStateOf(false) }
    var adjustmentConfirmationOpen by remember { mutableStateOf(false) }
    val keyboard = LocalSoftwareKeyboardController.current
    val focus = LocalFocusManager.current
    fun saveServer() {
        if (server.isNotBlank()) {
            keyboard?.hide(); focus.clearFocus(); serverFocused = false; actions.saveServer(server.trim())
        }
    }
    DisposableEffect(Unit) { onDispose { onEditing(false) } }
    LaunchedEffect(serverFocused, adjustmentConfirmationOpen) { onEditing(serverFocused || adjustmentConfirmationOpen) }
    LazyColumn(modifier.fillMaxSize(), contentPadding = PaddingValues(WorkbenchTokens.ScreenPadding), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item("heading") { PageHeading("设备", "让连接和打印状态一目了然") }
        if (state.apiUpdates && state.dailyWork.me?.isAdmin != false) item("api-update") {
            WorkbenchCard {
                Text("数据更新", style = MaterialTheme.typography.titleMedium)
                UpdateStatus(state, actions)
                PrimaryAction(if (state.updateBusy) "正在更新…" else "更新订单",
                    actions.updateData, Modifier.fillMaxWidth(), enabled = !state.updateBusy && !state.printBusy && state.serverOnline)
            }
        }
        item("account") {
            WorkbenchCard {
                Text("当前账号", style = MaterialTheme.typography.titleMedium)
                Text(state.dailyWork.me?.displayName.orEmpty().ifBlank { "已登录" },
                    style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                QuietAction("退出登录", actions.logout, Modifier.fillMaxWidth(), enabled = !state.printBusy)
            }
        }
        if (state.uncertainPrint) item("uncertain") {
            StateNotice(state.error ?: state.printMessage.ifBlank { "打印结果不确定，请先检查实际出纸。" }, error = true,
                onDismiss = actions.clearMessage, confirmLabel = "已核对出纸")
        }
        if (!state.error.isNullOrBlank() && !state.uncertainPrint) item("error") { StateNotice(state.error, error = true, onDismiss = actions.clearMessage) }
        if (!state.receipt.isNullOrBlank() && !state.uncertainPrint) item("receipt") { StateNotice(state.receipt, onDismiss = actions.clearMessage) }
        if (state.dailyWork.me?.isAdmin == true) item("business-confirmations") {
            LaunchedEffect(state.serverUrl, state.dailyWork.me?.id) { actions.loadBusinessConfirmations() }
            if (state.businessConfirmations.isNotEmpty() || state.businessConfirmationError != null) WorkbenchCard {
                Text("待确认事项", style = MaterialTheme.typography.titleMedium)
                state.businessConfirmationError?.let { Text(it, color = WorkbenchColors.Error) }
                state.businessConfirmations.forEach { Text(it, style = MaterialTheme.typography.bodySmall) }
                TextButton(onClick = actions.loadBusinessConfirmations) { Text("刷新确认结果") }
            }
        }
        item("adjustment-status") {
            WorkbenchCard(tinted = state.adjustmentUpload) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("局域网报损报溢", style = MaterialTheme.typography.titleMedium)
                        Text("上传电脑：${com.ul.workbench.data.LAN_ADJUSTMENT_SERVER}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    }
                    SmallTag(if (state.adjustmentUpload) "可上传" else "离线")
                }
                Text(state.adjustmentServerStatus, style = MaterialTheme.typography.bodySmall,
                    color = if (state.adjustmentUpload) WorkbenchColors.Muted else WorkbenchColors.Error)
            }
        }
        item("adjustments") { AdjustmentQueueCard(state.adjustments, actions, onModalChanged = { adjustmentConfirmationOpen = it }, uploadAvailable = state.adjustmentUpload) }
        item("printer") {
            WorkbenchCard {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Surface(shape = WorkbenchTokens.ControlShape, color = WorkbenchColors.Neutral) { Icon(WorkbenchIcons.Printer, null, Modifier.padding(13.dp).size(27.dp)) }
                    Column(Modifier.weight(1f)) {
                        Text(state.printerName.ifBlank { "A300E 标签打印机" }, style = MaterialTheme.typography.titleMedium)
                        Text(state.printerStatus, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    }
                }
                if (state.printerConnected) QuietAction("断开连接", actions.disconnectPrinter, Modifier.fillMaxWidth(), enabled = !state.printBusy)
                else PrimaryAction("查找打印机", actions.scanPrinters, Modifier.fillMaxWidth(), enabled = !state.printBusy, icon = WorkbenchIcons.Bluetooth)
                Text("连接后返回订单或库存，即可打印标签。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            }
        }
        if (state.printers.isNotEmpty()) {
            item("list-title") {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("可连接设备", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                    TextButton(onClick = actions.scanPrinters, enabled = !state.printBusy, modifier = Modifier.heightIn(min = 48.dp)) { Text("刷新") }
                }
            }
            items(state.printers, key = { it.address }) { printer ->
                WorkbenchCard {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        Icon(WorkbenchIcons.Bluetooth, null, tint = WorkbenchColors.Muted)
                        Column(Modifier.weight(1f)) {
                            Text(printer.name.ifBlank { "未命名设备" }, style = MaterialTheme.typography.titleMedium)
                            Text(printer.address, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                        }
                        QuietAction("连接", { actions.connectPrinter(printer.address) }, enabled = !state.printBusy)
                    }
                }
            }
        }
        item("server") {
            WorkbenchCard {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("数据服务", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                    SmallTag(if (state.serverOnline) "已连接" else "未连接")
                }
                Text("订单和库存从这里读取。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                OutlinedTextField(value = server, onValueChange = { server = it }, label = { Text("服务器地址") },
                    modifier = Modifier.fillMaxWidth().onFocusChanged { serverFocused = it.isFocused },
                    shape = WorkbenchTokens.ControlShape, minLines = 1, maxLines = 3,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Done), keyboardActions = KeyboardActions(onDone = { saveServer() }))
                Row(horizontalArrangement = Arrangement.spacedBy(9.dp)) {
                    QuietAction("测试连接", { keyboard?.hide(); focus.clearFocus(); serverFocused = false; actions.checkServer() }, Modifier.weight(1f), enabled = !state.printBusy)
                    PrimaryAction("保存地址", { saveServer() }, Modifier.weight(1f), enabled = server.isNotBlank() && !state.printBusy)
                }
                Text(state.serverStatus, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                if (server.trim() != state.serverUrl.trim()) Text("地址修改后请先保存，再测试连接。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            }
        }
        item("print-status") {
            WorkbenchCard {
                Text("打印状态", style = MaterialTheme.typography.titleMedium)
                Text(state.printMessage.ifBlank { "暂无正在发送的标签。" }, style = MaterialTheme.typography.bodyMedium,
                    color = if (state.uncertainPrint) WorkbenchColors.Error else WorkbenchColors.Muted)
                if (state.printBusy) {
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth(), color = WorkbenchColors.Red, trackColor = WorkbenchColors.Neutral)
                    QuietAction("停止发送", actions.cancelPrint, Modifier.fillMaxWidth())
                }
                if (state.uncertainPrint) Text("请先检查实际出纸，再决定下一步；不会自动重发。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
            }
        }
        item("foot") { Text("UL 工作台 · 原生扫码与打印", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted, modifier = Modifier.padding(vertical = 6.dp)) }
    }
}
