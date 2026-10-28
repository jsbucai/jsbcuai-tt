package com.ul.workbench.ui.screens

import androidx.compose.animation.animateContentSize
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.ul.workbench.data.*
import com.ul.workbench.ui.components.*
import com.ul.workbench.ui.theme.WorkbenchColors
import com.ul.workbench.ui.theme.WorkbenchTokens

/** Same-window editor: the app Scaffold owns safeDrawing/IME insets and scan suspension. */
@Composable
fun AdjustmentEditor(item: WorkItem, state: AdjustmentState, actions: WorkbenchActions,
                     modifier: Modifier = Modifier, onDismiss: () -> Unit, uploadAvailable: Boolean = true) {
    var type by rememberSaveable(item.sku) { mutableStateOf(AdjustmentType.Loss) }
    var quantity by rememberSaveable(item.sku) { mutableStateOf("") }
    var note by rememberSaveable(item.sku) { mutableStateOf("") }
    var submitted by remember { mutableStateOf(false) }
    var submitError by remember { mutableStateOf<String?>(null) }
    val parsedQuantity = remember(quantity) { runCatching { adjustmentQuantity(quantity) } }
    val quantityError = if (quantity.isBlank()) null else parsedQuantity.exceptionOrNull()?.message
    val keyboard = LocalSoftwareKeyboardController.current
    val focus = LocalFocusManager.current
    val listState = rememberLazyListState()
    val canSave = parsedQuantity.isSuccess && note.length <= 200 && !state.storageError && !submitted
    fun close() { keyboard?.hide(); focus.clearFocus(); onDismiss() }
    fun save() {
        if (!submitted && canSave) {
            keyboard?.hide()
            focus.clearFocus()
            submitError = null
            submitted = true
            val result = actions.addAdjustment(item, type, parsedQuantity.getOrThrow(), note.trim())
            if (result.saved) {
                // Keep submitted latched through dismissal to reject a second tap.
                close()
            } else {
                submitted = false
                submitError = result.message.ifBlank { "本地清单未能保存，请稍后重试" }
            }
        }
    }
    LaunchedEffect(submitError) { if (submitError != null) listState.scrollToItem(0) }
    LazyColumn(modifier.fillMaxSize(), state = listState, contentPadding = PaddingValues(WorkbenchTokens.ScreenPadding),
        verticalArrangement = Arrangement.spacedBy(14.dp)) {
        item("heading") {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Icon(WorkbenchIcons.Inventory, null, Modifier.size(25.dp), tint = WorkbenchColors.Red)
                Text("库存调整", style = MaterialTheme.typography.headlineMedium, modifier = Modifier.weight(1f))
                TextButton(onClick = { close() }, modifier = Modifier.heightIn(min = 48.dp)) { Text("取消") }
            }
        }
        if (state.storageError || submitError != null) {
            item("message") { StateNotice(submitError ?: state.message ?: "本地清单未能保存，请稍后重试。", error = true) }
        }
        item("product") {
            WorkbenchCard {
                Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
                    Text(item.name.ifBlank { item.sku }, style = MaterialTheme.typography.titleMedium)
                    Text("商品 ${item.sku}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    if (item.spec.isNotBlank()) Text(item.spec, style = MaterialTheme.typography.bodyMedium, color = WorkbenchColors.Muted)
                    if (item.model.isNotBlank() && !item.spec.contains(item.model)) {
                        Text("型号 ${item.model}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    }
                    if (item.note.isNotBlank()) Text("商品备注 ${item.note}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                }
                HorizontalDivider(color = WorkbenchColors.Line)
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    LabelValue("货位", item.location, Modifier.weight(1f))
                    LabelValue("可销售", item.available ?: "—", Modifier.weight(1f))
                    LabelValue("库存", item.stock ?: "—", Modifier.weight(1f))
                }
            }
        }
        item("editor") {
            WorkbenchCard {
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    AdjustmentType.entries.forEach { choice ->
                        FilterChip(selected = type == choice, onClick = { type = choice },
                            modifier = Modifier.weight(1f).heightIn(min = 48.dp), label = { Text(choice.label) },
                            leadingIcon = if (type == choice) ({ Icon(WorkbenchIcons.Check, null, Modifier.size(17.dp)) }) else null,
                            colors = FilterChipDefaults.filterChipColors(selectedContainerColor = WorkbenchColors.Ink,
                                selectedLabelColor = WorkbenchColors.Paper, selectedLeadingIconColor = WorkbenchColors.Paper))
                    }
                }
                OutlinedTextField(value = quantity, onValueChange = { quantity = it.take(24) },
                    modifier = Modifier.fillMaxWidth(), label = { Text("${type.label}数量") }, singleLine = true,
                    isError = quantityError != null, shape = WorkbenchTokens.ControlShape,
                    supportingText = quantityError?.let { message -> ({ Text(message) }) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal, imeAction = ImeAction.Done),
                    keyboardActions = KeyboardActions(onDone = { save() }))
                OutlinedTextField(value = note, onValueChange = { note = it.take(200) }, modifier = Modifier.fillMaxWidth(),
                    label = { Text("备注（可选）") }, minLines = 1, maxLines = 3,
                    shape = WorkbenchTokens.ControlShape, supportingText = { Text("${note.length} / 200") },
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done), keyboardActions = KeyboardActions(onDone = { save() }))
                PrimaryAction("加入清单", { save() }, Modifier.fillMaxWidth(), enabled = canSave)
                if (uploadAvailable) {
                    Text("先保存到手机，设备页统一上传。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    Text("在设备页确认后，由公司局域网电脑上传 ERP。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                } else Text("局域网上传电脑暂未连接，仍可保存手机草稿。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            }
        }
    }
}

private data class AdjustmentConfirmation(val type: AdjustmentType, val lines: List<AdjustmentLine>, val batchId: String? = null)

@Composable
fun AdjustmentQueueCard(state: AdjustmentState, actions: WorkbenchActions, onModalChanged: (Boolean) -> Unit, uploadAvailable: Boolean = true) {
    var confirmation by remember { mutableStateOf<AdjustmentConfirmation?>(null) }
    var verification by remember { mutableStateOf<AdjustmentBatch?>(null) }
    var unavailableNotice by remember { mutableStateOf(false) }
    val keyboard = LocalSoftwareKeyboardController.current
    val focus = LocalFocusManager.current
    LaunchedEffect(confirmation != null || verification != null || unavailableNotice) { onModalChanged(confirmation != null || verification != null || unavailableNotice) }
    DisposableEffect(Unit) { onDispose { onModalChanged(false) } }
    fun review(value: AdjustmentConfirmation) {
        keyboard?.hide()
        focus.clearFocus()
        if (uploadAvailable) confirmation = value else unavailableNotice = true
    }
    WorkbenchCard {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("待上传清单", style = MaterialTheme.typography.titleMedium)
                Text(if (state.count == 0) "库存报损与报溢" else "${state.count} 项待处理", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            }
            IconButton(onClick = actions.refreshAdjustments, enabled = !state.syncing, modifier = Modifier.size(48.dp)) {
                Icon(WorkbenchIcons.Refresh, "刷新上传状态", tint = WorkbenchColors.Muted)
            }
        }
        if (state.syncing) LinearProgressIndicator(modifier = Modifier.fillMaxWidth(), color = WorkbenchColors.Red, trackColor = WorkbenchColors.Neutral)
        if (!uploadAvailable) Text("局域网上传暂不可用，草稿继续保存在手机。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        if (!state.message.isNullOrBlank()) Text(state.message, style = MaterialTheme.typography.bodySmall,
            color = if (state.storageError) WorkbenchColors.Error else WorkbenchColors.Muted)
        if (state.count == 0) Text(if (uploadAvailable) "在库存中点开商品，将报损或报溢加入清单后，在这里统一上传。" else "可先在库存商品详情中加入草稿，连接公司 Wi-Fi 和上传电脑后再上传。",
            style = MaterialTheme.typography.bodyMedium, color = WorkbenchColors.Muted)
        AdjustmentType.entries.forEach { type ->
            val lines = state.drafts.filter { it.type == type }.map { it.line }
            if (lines.isNotEmpty()) key("draft-${type.wire}") {
                var expanded by rememberSaveable { mutableStateOf(false) }
                val active = state.batches.any { it.type == type && it.active }
                Column(Modifier.animateContentSize(tween(160)), verticalArrangement = Arrangement.spacedBy(9.dp)) {
                    HorizontalDivider(color = WorkbenchColors.Line)
                    AdjustmentGroupHeading(type, lines.size, expanded, onToggle = { expanded = !expanded })
                    if (expanded) lines.forEach { line -> key(line.identityKey()) {
                        AdjustmentLineDetail(line, onRemove = { actions.removeAdjustment(type, line.identityKey()) }, enabled = !state.storageError)
                    } }
                    AdjustmentUploadAction("上传${type.label}清单（${lines.size}）", { review(AdjustmentConfirmation(type, lines)) },
                        enabled = !state.storageError && !state.syncing && !active)
                    if (active) Text("上次${type.label}正在处理，确认完成后可继续上传。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                }
            }
        }
        state.batches.forEach { batch -> key(batch.id) {
            var expanded by rememberSaveable { mutableStateOf(false) }
            Column(Modifier.animateContentSize(tween(160)), verticalArrangement = Arrangement.spacedBy(9.dp)) {
                HorizontalDivider(color = WorkbenchColors.Line)
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("${batch.type.label} · ${batch.items.size} 项", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                    SmallTag(batch.statusLabel)
                }
                if (batch.message.isNotBlank()) Text(batch.message, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                if (batch.documentNo.isNotBlank()) Text("单据 ${batch.documentNo}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                else if (batch.status !in setOf("submitting", "queued", "running", "succeeded", "failed", "rejected")) {
                    Text("清单 ${batch.id.take(8)}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                }
                TextButton(onClick = { expanded = !expanded }, modifier = Modifier.heightIn(min = 48.dp), contentPadding = PaddingValues(horizontal = 0.dp)) {
                    Text(if (expanded) "收起上传明细" else "查看上传明细")
                }
                if (expanded) batch.items.forEach { line -> key(line.identityKey()) { AdjustmentLineDetail(line) } }
                if (batch.status == "rejected") {
                    QuietAction("退回待上传清单", { actions.restoreAdjustment(batch.id) }, Modifier.fillMaxWidth(),
                        enabled = !state.storageError && !state.syncing)
                } else if (batch.status == "failed" && batch.retryable) {
                    AdjustmentUploadAction("重试${batch.type.label}上传", { review(AdjustmentConfirmation(batch.type, batch.items, batch.id)) },
                        enabled = !state.storageError && !state.syncing && state.batches.none { it.type == batch.type && it.active })
                } else if (batch.status == "unknown") {
                    Text("请先在 ERP 核对上传结果。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
                    QuietAction(if (batch.confirmationPending) "等待保存核对结果" else "已核对，确认入账", {
                        keyboard?.hide(); focus.clearFocus(); verification = batch
                    }, Modifier.fillMaxWidth(), enabled = !state.storageError && !state.syncing && !batch.confirmationPending)
                } else if (batch.status == "failed") {
                    Text("本清单未上传，请查看上方原因。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
                }
            }
        } }
    }
    if (unavailableNotice) AlertDialog(onDismissRequest = { unavailableNotice = false }, title = { Text("局域网上传暂不可用") },
        text = { Text("请连接公司 Wi-Fi，确认原电脑 192.168.1.5 的 5191 服务已启动后重试。本次不会提交 ERP，清单继续保存在手机。") },
        confirmButton = { TextButton(onClick = { unavailableNotice = false }) { Text("知道了") } })
    confirmation?.let { review ->
        val currentBatch = review.batchId?.let { id -> state.batches.firstOrNull { it.id == id } }
        val unchanged = if (review.batchId == null) state.drafts.filter { it.type == review.type }.map { it.line } == review.lines
            else currentBatch?.items == review.lines && currentBatch.status == "failed" && currentBatch.retryable
        val canUpload = unchanged && !state.storageError && !state.syncing && state.batches.none { it.type == review.type && it.active }
        AlertDialog(onDismissRequest = { confirmation = null },
            title = { Text(if (review.batchId == null) "上传${review.type.label}清单？" else "重新上传${review.type.label}清单？") },
            text = { LazyColumn(Modifier.heightIn(max = 360.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                item { Text(if (unchanged) "请核对以下 ${review.lines.size} 项的数量，确认后上传 ERP。" else "清单已更新，请返回核对后重新上传。",
                    style = MaterialTheme.typography.bodyMedium) }
                items(review.lines, key = { it.identityKey() }) { line -> AdjustmentLineDetail(line) }
            } },
            confirmButton = { TextButton(onClick = {
                if (canUpload) {
                    confirmation = null
                    if (review.batchId == null) actions.uploadAdjustments(review.type) else actions.retryAdjustment(review.batchId)
                }
            }, enabled = canUpload, modifier = Modifier.heightIn(min = 48.dp)) { Text("确认上传") } },
            dismissButton = { TextButton(onClick = { confirmation = null }, modifier = Modifier.heightIn(min = 48.dp)) { Text("取消") } })
    }
    verification?.let { review ->
        val current = state.batches.firstOrNull { it.id == review.id }
        val canConfirm = current?.status == "unknown" && !current.confirmationPending && current.items == review.items && !state.syncing && !state.storageError
        AlertDialog(onDismissRequest = { verification = null }, title = { Text("确认这份清单已入账？") },
            text = { LazyColumn(Modifier.heightIn(max = 360.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                item { Text("请确认 ERP 中的商品和数量与以下 ${review.items.size} 项一致。确认后保存核对记录并收起清单。", style = MaterialTheme.typography.bodyMedium) }
                item { Text(if (review.documentNo.isBlank()) "清单 ${review.id.take(8)}" else "单据 ${review.documentNo}", style = MaterialTheme.typography.bodySmall) }
                items(review.items, key = { it.identityKey() }) { line -> AdjustmentLineDetail(line) }
                item { Text("此操作不会再次上传 ERP。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted) }
            } },
            confirmButton = { TextButton(onClick = {
                if (canConfirm) { verification = null; actions.confirmAdjustment(review.id) }
            }, enabled = canConfirm, modifier = Modifier.heightIn(min = 48.dp)) { Text("确认已入账") } },
            dismissButton = { TextButton(onClick = { verification = null }, modifier = Modifier.heightIn(min = 48.dp)) { Text("取消") } })
    }
}

@Composable
private fun AdjustmentGroupHeading(type: AdjustmentType, count: Int, expanded: Boolean, onToggle: () -> Unit) {
    val detailAction: @Composable () -> Unit = {
        TextButton(onClick = onToggle, modifier = Modifier.heightIn(min = 48.dp), contentPadding = PaddingValues(horizontal = 6.dp)) {
            Text(if (expanded) "收起${type.label}明细" else "查看${type.label}明细")
        }
    }
    if (LocalDensity.current.fontScale > 1.2f) Column(Modifier.fillMaxWidth()) {
        Text("${type.label} · $count 项", style = MaterialTheme.typography.titleMedium)
        detailAction()
    } else Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("${type.label} · $count 项", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
        detailAction()
    }
}

@Composable
private fun AdjustmentLineDetail(line: AdjustmentLine, onRemove: (() -> Unit)? = null, enabled: Boolean = true) {
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(5.dp)) {
        Text(line.name.ifBlank { line.sku }, style = MaterialTheme.typography.bodyMedium)
        Text(line.sku + if (line.spec.isNotBlank()) " · ${line.spec}" else "", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        if (line.model.isNotBlank() && !line.spec.contains(line.model)) Text("型号 ${line.model}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("数量 ${line.quantity}", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            if (onRemove != null) TextButton(onClick = onRemove, enabled = enabled, modifier = Modifier.heightIn(min = 48.dp)) { Text("移除") }
        }
        if (line.note.isNotBlank()) Text(line.note, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
    }
}

/** Upload labels may wrap at large font sizes instead of losing the count to an ellipsis. */
@Composable
private fun AdjustmentUploadAction(label: String, onClick: () -> Unit, enabled: Boolean) {
    Button(onClick = onClick, enabled = enabled, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
        shape = WorkbenchTokens.ControlShape, contentPadding = PaddingValues(horizontal = 14.dp, vertical = 12.dp)) {
        Text(label)
    }
}
