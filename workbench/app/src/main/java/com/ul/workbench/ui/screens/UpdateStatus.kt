package com.ul.workbench.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.ul.workbench.data.*
import com.ul.workbench.ui.theme.WorkbenchColors

private fun stageLabel(stage: String, source: String) = when (stage) {
    "queued" -> "等待更新"
    "reading", "collecting" -> when (source) {
        "products" -> "读取商品"
        "orders" -> "读取未发货清单"
        "order_details" -> "读取订单摘要"
        "inventory" -> "读取库存"
        "inventory_products" -> "读取库存商品"
        "purchase_history" -> "读取进货流水"
        else -> "读取订单与库存"
    }
    "validating" -> "校验数据"
    "calculating" -> "计算分类"
    "publishing", "persisting" -> "发布查询数据"
    "writing", "freight", "sending" -> "更新物流差异"
    "completed", "done" -> "更新结束"
    "interrupted" -> "任务恢复中"
    else -> "更新进度"
}

@Composable
fun UpdateStatus(state: WorkbenchState, actions: WorkbenchActions) {
    val task = state.updateTask
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(if (state.sourceUpdatedAt.isBlank()) "尚未完成首次同步" else "数据更新 ${state.sourceUpdatedAt}",
                    style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                if (state.updateBusy && task != null) Text(stageLabel(task.stage, task.source) +
                    if (task.total > 0) " · ${task.completed}/${task.total}" else "", style = MaterialTheme.typography.bodySmall)
            }
            if (task != null && state.dailyWork.me?.isAdmin != false) TextButton(onClick = actions.showUpdateDetails, enabled = !state.printBusy,
                modifier = Modifier.heightIn(min = 48.dp), contentPadding = PaddingValues(horizontal = 6.dp)) {
                Text(if (task.unknown > 0) "待核对 ${task.unknown}" else "更新结果")
            }
        }
        if (state.updateBusy) LinearProgressIndicator(Modifier.fillMaxWidth(), color = WorkbenchColors.Red, trackColor = WorkbenchColors.Neutral)
        if (state.updateMessage.isNotBlank()) Text(state.updateMessage, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        if (task?.published == true && !state.updateBusy) Text("查询已更新 · 物流成功 ${task.success} / 失败 ${task.failed} / 待核对 ${task.unknown}",
            style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
fun UpdateResultDialog(state: WorkbenchState, actions: WorkbenchActions) {
    if (state.dailyWork.me?.isAdmin == false) return
    val task = state.updateTask
    var selected by remember(task?.id) { mutableStateOf<FreightAttempt?>(null) }
    var resolution by remember(selected?.id) { mutableStateOf("") }
    val note = when (resolution) {
        "applied" -> "已在管家婆核对，物流公司及单号均与目标一致"
        "not_applied" -> "已在管家婆核对，本次物流更新未生效"
        else -> ""
    }
    LaunchedEffect(task?.attempts) {
        if (selected != null && task?.attempts?.none { it.id == selected?.id && it.status == "unknown" } == true) selected = null
    }
    key(selected?.id) {
    AlertDialog(onDismissRequest = actions.dismissUpdateDetails, title = { Text(if (selected == null) "更新结果" else "核对物流结果") },
        text = {
            if (selected != null) {
                val attempt = selected!!
                Column(Modifier.heightIn(max = 360.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text("${attempt.orderNo}\n目标公司：${attempt.company}\n目标单号：${attempt.number}")
                    Text("请先在管家婆核对实际结果，再保存结论。未生效的记录会留到下次更新处理。", style = MaterialTheme.typography.bodySmall)
                    Column {
                        listOf("applied" to "已核对，与目标一致", "not_applied" to "已核对，未生效").forEach { (key, label) ->
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                RadioButton(selected = resolution == key, onClick = { resolution = key }, enabled = !state.resolvingFreight)
                                TextButton(onClick = { resolution = key }, enabled = !state.resolvingFreight) { Text(label) }
                            }
                        }
                    }
                    if (note.isNotBlank()) Text("核对记录：$note", style = MaterialTheme.typography.bodySmall)
                    if (!state.error.isNullOrBlank()) Text(state.error, color = WorkbenchColors.Error)
                }
            } else {
            LazyColumn(Modifier.heightIn(max = 360.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                if (task == null) {
                    item { Text("暂无更新记录") }
                } else {
                    item { Text(if (task.published) "查询数据已发布 · ${task.orders} 条订单 / ${task.inventory} 条库存\n${task.publishedAt}" else "本次尚未发布新的查询数据，继续使用上一份缓存") }
                    item { Text("物流：成功 ${task.success} · 失败 ${task.failed} · 待核对 ${task.unknown} · 未变化 ${task.skipped}") }
                    if (task.logisticsStatus == "unavailable") item { Text("物流写入暂未启用，查询数据可用。", color = WorkbenchColors.Muted) }
                    item { Text(state.updateMessage.ifBlank { task.message }, style = MaterialTheme.typography.bodySmall) }
                    items(task.blockedOrders) { Text(it, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error) }
                    items(task.attempts, key = { it.id }) { attempt ->
                        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            Text(attempt.orderNo, style = MaterialTheme.typography.titleSmall)
                            Text("${attempt.company} · ${attempt.number}", style = MaterialTheme.typography.bodySmall)
                            Text(when (attempt.status) {
                                "succeeded", "applied" -> "更新成功"
                                "failed", "not_applied" -> "未更新"
                                "unknown" -> "结果待核对"
                                "sending" -> "正在提交"
                                else -> attempt.status
                            } + if (attempt.message.isNotBlank()) " · ${attempt.message}" else "", style = MaterialTheme.typography.bodySmall)
                            if (attempt.status == "unknown") TextButton(onClick = { selected = attempt }, enabled = !state.updateBusy && !state.resolvingFreight) { Text("核对此单") }
                            HorizontalDivider(color = WorkbenchColors.Line)
                        }
                    }
                }
            }
            }
        }, confirmButton = {
            if (selected != null) TextButton(onClick = { actions.resolveFreight(selected!!.id, resolution, note) },
                enabled = resolution.isNotBlank() && note.isNotBlank() && !state.resolvingFreight) { Text(if (state.resolvingFreight) "正在保存…" else "保存核对结论") }
            else TextButton(onClick = actions.dismissUpdateDetails) { Text("关闭") }
        }, dismissButton = {
            if (selected != null) TextButton(onClick = { selected = null }, enabled = !state.resolvingFreight) { Text("返回") }
        })
    }
}
