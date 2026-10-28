package com.ul.workbench.ui.screens

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.ul.workbench.data.*
import com.ul.workbench.ui.components.*
import com.ul.workbench.ui.theme.WorkbenchColors
import com.ul.workbench.ui.theme.WorkbenchTokens
import com.ul.workbench.ui.AssistantActions
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZoneOffset

private val shanghai = ZoneId.of("Asia/Shanghai")

@Composable
fun DailyWorkScreen(state: DailyWorkState, actions: WorkbenchActions, modifier: Modifier = Modifier,
                    visible: Boolean = true, assistantActions: AssistantActions = AssistantActions()) {
    var staffOpen by rememberSaveable { mutableStateOf(false) }
    var selectedTaskId by rememberSaveable { mutableStateOf<String?>(null) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val currentActions by rememberUpdatedState(actions)
    val boardVisible = visible && !staffOpen && !state.purchaseUnfinished.open && state.customerOrderDetail == null &&
        state.boardDetail == null && state.taskHistoryDetail == null && selectedTaskId == null
    DisposableEffect(lifecycle, boardVisible) {
        fun publishVisibility() = currentActions.dailyWorkVisible(boardVisible && lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))
        val observer = LifecycleEventObserver { _, _ -> publishVisibility() }
        lifecycle.addObserver(observer)
        publishVisibility()
        onDispose { lifecycle.removeObserver(observer); currentActions.dailyWorkVisible(false) }
    }
    LaunchedEffect(state.actionSucceededId) {
        if (state.actionSucceededId == selectedTaskId) selectedTaskId = null
    }
    if (state.purchaseUnfinished.open) {
        PurchaseUnfinishedScreen(state.purchaseUnfinished, actions, modifier)
        return
    }
    if (staffOpen && state.me?.isAdmin == true) {
        StaffAdminScreen(state, actions, modifier, onClose = { staffOpen = false })
    } else if (state.customerOrderDetail != null) {
        CustomerOrderDetailView(state, actions, onTask = { selectedTaskId = it }, modifier = modifier.fillMaxSize())
    } else {
        TaskBoardScreen(state, actions, modifier,
            onStaff = { staffOpen = true; actions.loadStaff() },
            onTask = { selectedTaskId = it; if (state.me?.isAdmin == true) actions.loadStaff() },
            assistantActions = assistantActions, visible = boardVisible)
    }
    val selectedTask = state.snapshot.items.firstOrNull { it.id == selectedTaskId }
        ?: state.boardDetail?.let { detail -> detail.lines.firstOrNull { it.taskId == selectedTaskId }?.let { line ->
            DailyWorkItem(line.taskId, line.version, detail.item.role, "${line.orderNo} · ${line.sku}",
                reason = line.blockedBy, status = line.status, dueDate = line.dueDate,
                assigneeId = line.assigneeId, assigneeName = line.assigneeName, orderNos = listOf(line.orderNo),
                sku = line.sku, customer = line.customer, quantity = line.quantity, location = line.location,
                printState = line.printState, profileId = detail.item.profileId,
                readOnly = !line.canAct && state.me?.isAdmin != true)
        } }
    selectedTask?.let { task ->
        val pending = state.pendingActions.firstOrNull { it.taskId == task.id }
        DailyTaskActionDialog(task, state.actionBusyId == task.id,
            pending = pending != null, pendingReview = pending?.requiresReview == true,
            state.actionError.takeIf { state.actionErrorId == task.id },
            actionsEnabled = state.board.tasksCurrent && state.snapshot.tasksCurrent != false,
            isAdmin = state.me?.isAdmin == true, staff = state.staff,
            attendance = state.attendance, attendanceAvailable = state.attendanceAvailable,
            onDismiss = { if (state.actionBusyId != task.id) selectedTaskId = null },
            onSubmit = { action, note, nextDate -> actions.submitDailyAction(task.id, action, note, nextDate, task.version) },
            onTransfer = { assignee, reason -> actions.transferDailyTask(task.id, assignee, reason, task.version) },
            onUrgent = { urgent, reason -> actions.setDailyTaskUrgent(task.id, urgent, reason, task.version) },
            onResetPrint = actions.resetDailyOrderPrintState, onOrder = actions.openDailyOrder)
    }
}

@Composable
private fun CustomerOrderCard(group: CustomerOrderGroup, role: String, onOpen: () -> Unit) {
    WorkbenchCard(Modifier.clickable(onClick = onOpen), tinted = group.urgent || group.linesChanged || group.customerReview || group.reviewCount > 0) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(group.customer.ifBlank { "客户待核对" }, modifier = Modifier.weight(1f),
                style = MaterialTheme.typography.labelLarge, color = WorkbenchColors.SecondaryInk,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
            if (group.urgent) SmallTag("急单", selected = true)
            if (group.linesChanged) SmallTag("订单行已变化", selected = true)
            if (group.dueDateFrom.isNotBlank()) Text("交期 ${group.dueDateFrom}",
                style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
        }
        Text(group.customerOrderNo, style = MaterialTheme.typography.titleMedium)
        Text("齐货 ${group.readyCount}/${group.unshippedCount} 行 · 已包装 ${group.packedCount} 行",
            style = MaterialTheme.typography.bodyMedium)
        val warning = when {
            group.customerReview -> "同一订单号的客户信息不一致，需核对归组"
            group.reviewCount > 0 -> "${group.reviewCount} 行资料或计算待核对"
            group.missingCount > 0 -> "还差 ${group.missingCount} 行／${group.missingProductCount} 种商品，请推进补齐"
            group.readyToShip -> "全部齐货并包装完成，可安排整单发货"
            else -> "已齐行可先包装，等待整单完成"
        }
        Text(warning, style = MaterialTheme.typography.bodySmall,
            color = if (group.missingCount > 0 || group.reviewCount > 0) WorkbenchColors.Error else WorkbenchColors.SecondaryInk)
        group.missingPreview.take(2).forEach { missing ->
            Text("${missing.sku.ifBlank { missing.productId }}：待到货 ${missing.pending} · 需采购 ${missing.shortage}",
                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        val responsibility = when (role) {
            ROLE_SALES -> "销售：${group.assigneeName.ifBlank { "待安排" }}"
            ROLE_PURCHASE -> "关联采购事项 ${group.purchaseTaskCount} 项"
            ROLE_STOCK_PACKING, ROLE_ARRIVAL_PACKING -> "我负责 ${group.myPackingCount} 行"
            else -> "销售：${group.assigneeName.ifBlank { "待安排" }} · 我负责包装 ${group.myPackingCount} 行"
        }
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(responsibility, Modifier.weight(1f), style = MaterialTheme.typography.labelSmall,
                color = WorkbenchColors.Muted)
            TextButton(onClick = onOpen) { Text("查看整单") }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun CustomerOrderDetailView(state: DailyWorkState, actions: WorkbenchActions,
                                    onTask: (String) -> Unit, modifier: Modifier = Modifier) {
    val detail = state.customerOrderDetail ?: return
    val group = detail.group
    var followupOpen by rememberSaveable(group.id) { mutableStateOf(false) }
    var transferOpen by rememberSaveable(group.id) { mutableStateOf(false) }
    val lines = remember(detail.lines) { detail.lines.sortedWith(compareBy<CustomerOrderLine> {
        when (it.status) { "procurement", "arrival" -> 0; "review" -> 1; "ready" -> 2; else -> 3 }
    }.thenBy { it.orderNo }) }
    androidx.compose.foundation.lazy.LazyColumn(modifier = modifier,
        contentPadding = PaddingValues(start = 18.dp, end = 18.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(onClick = actions.closeCustomerOrder) { Text("← 返回客户整单") }
                Text(group.customer.ifBlank { "客户待核对" }, style = MaterialTheme.typography.titleLarge)
                Text(group.customerOrderNo, style = MaterialTheme.typography.titleMedium)
                Text("固定销售：${group.assigneeName.ifBlank { "待指定" }}", style = MaterialTheme.typography.bodyMedium)
                if (group.deputyId.isNotBlank()) Text("临时代办：${group.deputyName.ifBlank { state.staff.firstOrNull { it.id == group.deputyId }?.displayName ?: "人员资料待核对" }} · 至 ${group.deputyUntil}",
                    style = MaterialTheme.typography.bodySmall)
                Text("齐货 ${group.readyCount}/${group.unshippedCount} 行 · 已包装 ${group.packedCount} 行 · 缺项 ${group.missingCount} 行 · 待核对 ${group.reviewCount} 行",
                    style = MaterialTheme.typography.bodyMedium)
                if (!detail.tasksCurrent || group.customerReview || group.ownerReview) StateNotice(
                    if (!detail.tasksCurrent) "任务汇总尚未跟上订单数据，暂停整单发货结论。"
                    else if (group.customerReview) "客户名称缺失或冲突，请先核对归组。"
                    else "客户销售归属待核对，请管理员在客户固定销售中确认。", error = true)
                if (group.readyToShip && detail.tasksCurrent) StateNotice("已齐货且完成包装，可安排整单发货。")
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (state.selectedRole == ROLE_SALES || state.me?.isAdmin == true)
                        Button(onClick = { followupOpen = true }, enabled = detail.tasksCurrent && !state.customerOrderActionBusy) {
                            Text("记录整单跟进")
                        }
                    if (state.me?.isAdmin == true)
                        OutlinedButton(onClick = { actions.loadStaff(); transferOpen = true }, enabled = detail.tasksCurrent) { Text("客户固定销售／代办") }
                }
                if (detail.missingItems.isNotEmpty()) Text("缺货与待到货", style = MaterialTheme.typography.titleMedium)
                detail.missingItems.forEach { missing ->
                    Text("${missing.orderNo} · ${missing.sku.ifBlank { missing.productId }} · 待到货 ${missing.pending} · 需采购 ${missing.shortage}",
                        style = MaterialTheme.typography.bodySmall)
                }
                if (detail.purchaseTasks.isNotEmpty()) Text("关联采购事项 ${detail.purchaseTasks.size} 项", style = MaterialTheme.typography.labelLarge)
                detail.followups.forEach { followup ->
                    Text("${followup.createdAt.take(10)} ${followup.note} · 下次 ${followup.nextFollowUpDate}",
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                }
                Text("订单行 ${lines.size}", style = MaterialTheme.typography.titleMedium)
            }
        }
        items(lines.size, key = { lines[it].orderNo }) { index ->
            val line = lines[index]
            WorkbenchCard {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text(line.orderNo, modifier = Modifier.weight(1f), style = MaterialTheme.typography.labelLarge)
                    SmallTag(customerLineLabel(line.status), selected = line.status in setOf("review", "procurement"))
                }
                Text("${line.skus.joinToString("、")} · ${line.fulfillmentLabel.ifBlank { if (line.packed) "已包装" else "未确认包装" }} · ${printStateLabel(line.printState)}",
                    style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    line.taskIds.firstOrNull { id -> state.snapshot.items.any { it.id == id } }?.let { taskId ->
                        TextButton(onClick = { onTask(taskId) }) { Text("处理任务") }
                    }
                    TextButton(onClick = { actions.openDailyOrder(line.orderNo) }) { Text("查看订单") }
                }
            }
        }
    }
    if (followupOpen) CustomerOrderFollowupDialog(group, state.customerOrderActionBusy,
        onDismiss = { followupOpen = false }, onSubmit = { note, waiting, date ->
            actions.recordCustomerOrderFollowup(group.id, note, waiting, date, group.followUpVersion)
            followupOpen = false
        })
    if (transferOpen) CustomerOwnershipDialog(state, actions, group.customer, group.profileId,
        onDismiss = { transferOpen = false })
}

private fun customerLineLabel(status: String) = when (status) {
    "ready" -> "实物已齐"; "arrival" -> "待到货"; "procurement" -> "需采购"
    "review" -> "待核对"; "shipped_unposted" -> "已出库待结存"; else -> status
}

@Composable
private fun CustomerOrderFollowupDialog(group: CustomerOrderGroup, busy: Boolean,
    onDismiss: () -> Unit, onSubmit: (String, String, String) -> Unit) {
    var note by rememberSaveable(group.id) { mutableStateOf("") }
    var waiting by rememberSaveable(group.id) { mutableStateOf("") }
    var nextDate by rememberSaveable(group.id) { mutableStateOf(LocalDate.now(shanghai).plusDays(1).toString()) }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("整单跟进 · ${group.customerOrderNo}") },
        text = { Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedTextField(note, { note = it }, label = { Text("今天处理了什么") }, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(waiting, { waiting = it }, label = { Text("等待谁协同（可选）") }, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(nextDate, { nextDate = it }, label = { Text("下次跟进日期 YYYY-MM-DD") },
                modifier = Modifier.fillMaxWidth(), singleLine = true)
        } }, confirmButton = { TextButton(onClick = { onSubmit(note.trim(), waiting.trim(), nextDate.trim()) },
            enabled = !busy && note.isNotBlank()) { Text("保存跟进") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } })
}

@Composable
private fun SummaryStrip(summary: DailyWorkSummary) {
    Surface(shape = WorkbenchTokens.CardShape, color = WorkbenchColors.Paper, border = BorderStroke(1.dp, WorkbenchColors.Line)) {
        Row(Modifier.fillMaxWidth().padding(vertical = 12.dp)) {
            SummaryValue("今日待办", summary.todo, Modifier.weight(1f), true)
            Column(Modifier.weight(1f).padding(horizontal = 7.dp)) {
                Text("处理中", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted, maxLines = 1)
                Text(summary.inProgress.toString(), style = MaterialTheme.typography.titleLarge, color = WorkbenchColors.Ink)
                if (summary.packedWaiting > 0) Text("待发 ${summary.packedWaiting}", style = MaterialTheme.typography.labelSmall,
                    color = WorkbenchColors.Muted)
            }
            SummaryValue("待协同", summary.waiting, Modifier.weight(1f))
            Column(Modifier.weight(1.2f).padding(horizontal = 6.dp)) {
                Text("今日完成", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                Text("${summary.todayActions}", style = MaterialTheme.typography.titleLarge, color = WorkbenchColors.Ink)
                Text("业务 ${summary.businessCompleted}", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
            }
        }
    }
}

@Composable
private fun SummaryValue(label: String, value: Int, modifier: Modifier, primary: Boolean = false) {
    Column(modifier.padding(horizontal = 7.dp)) {
        Text(label, style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted, maxLines = 1)
        Text(value.toString(), style = MaterialTheme.typography.titleLarge,
            color = if (primary) WorkbenchColors.Red else WorkbenchColors.Ink)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun DailyTaskCard(item: DailyWorkItem, pending: Boolean, actionsEnabled: Boolean,
                          onClick: () -> Unit, onOrder: (String) -> Unit) {
    val highPriority = item.urgent || item.priority in 1..10
    WorkbenchCard(Modifier.clickable(onClick = onClick), tinted = highPriority) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            SmallTag(dailyRoleLabel(item.role), selected = item.urgent)
            SmallTag(statusLabel(item.status), selected = item.status in setOf("todo", "review"))
            if (item.printState.isNotBlank())
                SmallTag(printStateLabel(item.printState), selected = item.printState == "uncertain")
            if (item.readOnly) SmallTag("只读协同", selected = true)
            if (pending) SmallTag("待提交", selected = true)
            Spacer(Modifier.weight(1f))
            if (item.urgent) Text("急", color = WorkbenchColors.Red, style = MaterialTheme.typography.labelLarge)
            if (item.dueDate.isNotBlank()) Text(item.dueDate, style = MaterialTheme.typography.labelMedium,
                color = if (highPriority) WorkbenchColors.Error else WorkbenchColors.Muted)
        }
        Text(item.title, style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
        if (item.customer.isNotBlank() || item.orderNos.isNotEmpty() || item.sku.isNotBlank()) {
            Text(listOf(item.customer, item.orderNos.joinToString("、"), item.sku).filter(String::isNotBlank).joinToString(" · "),
                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted, maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
        if (item.reason.isNotBlank()) Text("原因：${item.reason}", style = MaterialTheme.typography.bodyMedium, maxLines = 2,
            overflow = TextOverflow.Ellipsis)
        if (item.role == ROLE_PURCHASE && item.customerOrderImpact > 0) Text(
            "影响 ${item.customerOrderImpact} 张客户整单，其中 ${item.nearCompleteCustomerOrders} 张只差此商品",
            style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.SecondaryInk)
        if (item.nextAction.isNotBlank()) Text("下一步：${item.nextAction}", style = MaterialTheme.typography.bodyMedium,
            color = WorkbenchColors.SecondaryInk, maxLines = 2, overflow = TextOverflow.Ellipsis)
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            val ownerText = if (item.readOnly) "岗位协同提醒（无需领取）" else item.assigneeName.ifBlank { "尚未安排负责人" }
            Text(ownerText, Modifier.weight(1f), style = MaterialTheme.typography.labelMedium,
                color = if (!item.readOnly && item.assigneeName.isBlank()) WorkbenchColors.Error else WorkbenchColors.Muted)
            if (item.readOnly) {
                TextButton(onClick = onClick) { Text("查看等待原因") }
            } else if (item.role in setOf(ROLE_STOCK_PACKING, ROLE_ARRIVAL_PACKING) && item.orderNos.isNotEmpty()) {
                TextButton(onClick = { onOrder(item.orderNos.first()) }, enabled = actionsEnabled) {
                    Text(if (actionsEnabled) "去包装" else "任务待更新")
                }
            } else TextButton(onClick = onClick, enabled = actionsEnabled) { Text(if (actionsEnabled) "记录处理" else "任务待更新") }
        }
    }
}

private fun statusLabel(status: String) = when (status) {
    "todo" -> "待处理"
    "in_progress" -> "处理中"
    "waiting" -> "待协同"
    "resolved" -> "已提交解决"
    "packed" -> "已包装待发货"
    "business_completed" -> "业务已完成"
    "condition_resolved" -> "条件已解除"
    "review" -> "待核对"
    else -> status
}

private fun printStateLabel(status: String) = when (status) {
    "reserved", "printing" -> "打印中"
    "uncertain" -> "打印待核对"
    "printed" -> "已打印"
    "unprinted" -> "未打印"
    else -> status
}

@OptIn(ExperimentalLayoutApi::class, ExperimentalMaterial3Api::class)
@Composable
private fun DailyTaskActionDialog(item: DailyWorkItem, busy: Boolean, pending: Boolean, pendingReview: Boolean,
                                  actionError: String?, onDismiss: () -> Unit,
                                  actionsEnabled: Boolean, isAdmin: Boolean, staff: List<StaffMember>, attendance: Map<String, Boolean>,
                                  attendanceAvailable: Boolean, onSubmit: (String, String, String) -> Unit,
                                  onTransfer: (String, String) -> Unit, onUrgent: (Boolean, String) -> Unit,
                                  onResetPrint: (String, String) -> Unit, onOrder: (String) -> Unit) {
    val packing = item.role in setOf(ROLE_STOCK_PACKING, ROLE_ARRIVAL_PACKING)
    val printBlocked = packing && item.printState in setOf("reserved", "printing", "uncertain")
    val choices = dailyActionChoices(item, isAdmin)
    var action by rememberSaveable(item.id) { mutableStateOf(choices.firstOrNull().orEmpty()) }
    var note by rememberSaveable(item.id) { mutableStateOf("") }
    var nextDate by rememberSaveable(item.id) { mutableStateOf(LocalDate.now(shanghai).plusDays(1).toString()) }
    var dateOpen by remember { mutableStateOf(false) }
    var transferOpen by rememberSaveable(item.id) { mutableStateOf(false) }
    var transferTo by rememberSaveable(item.id) { mutableStateOf("") }
    var transferReason by rememberSaveable(item.id) { mutableStateOf("") }
    var urgentOpen by rememberSaveable(item.id) { mutableStateOf(false) }
    var urgentReason by rememberSaveable(item.id) { mutableStateOf("") }
    val transferCandidates = staff.filter { person -> person.active && item.role in person.roles && person.id != item.assigneeId &&
        (!attendanceAvailable || attendance[person.id] == true) }
    val needsDate = action in setOf("progress", "wait")
    val needsNote = action in setOf("progress", "wait", "resolve")
    val canSubmit = canSubmitDailyAction(action, note, nextDate)

    AlertDialog(onDismissRequest = onDismiss, title = { Text(item.title) }, text = {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(listOf(dailyRoleLabel(item.role), statusLabel(item.status), item.assigneeName).filter(String::isNotBlank).joinToString(" · "),
                style = MaterialTheme.typography.labelMedium, color = WorkbenchColors.Muted)
            if (item.reason.isNotBlank()) LabelValue("为什么要处理", item.reason)
            if (item.nextAction.isNotBlank()) LabelValue("建议下一步", item.nextAction)
            if (item.orderNos.isNotEmpty()) LabelValue("关联订单", item.orderNos.joinToString("、"))
            if (item.sku.isNotBlank()) LabelValue("商品", listOf(item.sku, item.quantity, item.location).filter(String::isNotBlank).joinToString(" · "))
            if (item.printState.isNotBlank()) LabelValue("打印状态", printStateLabel(item.printState))
            if (item.blockedBy.isNotBlank()) LabelValue("正在等待", item.blockedBy)
            if (item.missingItems.isNotEmpty()) LabelValue("等待采购补齐", item.missingItems.joinToString("；") { missing ->
                listOf(missing.sku.ifBlank { missing.productId }, missing.shortage.takeIf(String::isNotBlank)?.let { "缺 $it" },
                    missing.pending.takeIf(String::isNotBlank)?.let { "在途 $it" }).filterNotNull().filter(String::isNotBlank).joinToString(" · ")
            })
            if (item.linkedPurchaseBusinessKey.isNotBlank()) LabelValue("关联采购事项", "采购岗位已收到同订单的补齐事项")
            if (item.readOnly) StateNotice(
                "等待采购补齐。这是一项只读协同提醒，不占待包装额度，也不能从这里开始包装。", error = false)
            if (printBlocked) StateNotice(
                if (item.printState == "uncertain") "打印结果待核对，确认实际出纸前不能标记已包装。"
                else "该订单正在打印，暂时不能标记已包装。", error = true)
            if (!item.readOnly && item.printState == "uncertain" && item.orderNos.isNotEmpty()) QuietAction(
                "已核对，恢复为未打印", { onResetPrint(item.orderNos.first(), item.profileId) }, Modifier.fillMaxWidth(),
                enabled = actionsEnabled && item.profileId.isNotBlank() && !busy && !pending)
            if (item.printState == "uncertain" && item.profileId.isBlank()) Text("任务缺少账套信息，暂时不能恢复打印状态。",
                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
            if (pending) StateNotice(if (pendingReview) "这条手机记录被服务器拒绝，需要核对后从任务页移除。"
                else "这条处理记录已保存在手机，网络恢复后会自动提交。", error = true)
            if (!actionsEnabled) StateNotice("当前任务版本落后于订单数据，只能查看，刷新完成后才能操作。", error = true)
            if (!item.readOnly && packing && item.orderNos.isNotEmpty()) QuietAction("查看订单／去包装", { onOrder(item.orderNos.first()) }, Modifier.fillMaxWidth(),
                enabled = actionsEnabled && !busy && !pending)
            if (isAdmin && !item.readOnly) {
                QuietAction(if (transferOpen) "收起转交" else "转交任务", { transferOpen = !transferOpen }, Modifier.fillMaxWidth(),
                    enabled = actionsEnabled && !busy && !pending)
                if (transferOpen) {
                    Text("选择同岗位的今日在岗人员", style = MaterialTheme.typography.labelLarge)
                    if (transferCandidates.isEmpty()) Text("没有可转交的在岗人员，请先到人员管理设置今日在岗。",
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
                    else FlowRow(horizontalArrangement = Arrangement.spacedBy(7.dp)) {
                        transferCandidates.forEach { person -> FilterChip(selected = transferTo == person.id,
                            onClick = { transferTo = person.id }, label = { Text(person.displayName) }) }
                    }
                    OutlinedTextField(transferReason, { transferReason = it }, Modifier.fillMaxWidth(),
                        label = { Text("转交原因（必填）") }, minLines = 2, enabled = !busy)
                    PrimaryAction("确认转交", { onTransfer(transferTo, transferReason) }, Modifier.fillMaxWidth(),
                        enabled = transferTo.isNotBlank() && transferReason.isNotBlank() && actionsEnabled && !busy)
                }
                QuietAction(if (urgentOpen) "收起急单设置" else if (item.urgent) "取消急单" else "标记急单",
                    { urgentOpen = !urgentOpen }, Modifier.fillMaxWidth(), enabled = actionsEnabled && !busy && !pending)
                if (urgentOpen) {
                    OutlinedTextField(urgentReason, { urgentReason = it }, Modifier.fillMaxWidth(),
                        label = { Text(if (item.urgent) "取消急单原因（必填）" else "标记急单原因（必填）") },
                        minLines = 2, enabled = !busy)
                    PrimaryAction(if (item.urgent) "确认取消急单" else "确认标记急单",
                        { onUrgent(!item.urgent, urgentReason) }, Modifier.fillMaxWidth(),
                        enabled = urgentReason.isNotBlank() && actionsEnabled && !busy)
                }
            }
            if (item.readOnly) {
                // Broadcast cards only explain the dependency. They never expose
                // a local action form or imply that an employee must claim them.
            } else if (choices.isEmpty()) Text("这项工作已经由业务数据确认完成。", color = WorkbenchColors.Muted)
            else {
                Text("记录本次处理", style = MaterialTheme.typography.titleSmall)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    choices.forEach { value ->
                        val selected = action == value
                        val interaction = remember(value) { MutableInteractionSource() }
                        Surface(shape = WorkbenchTokens.ControlShape,
                            border = BorderStroke(if (selected) 2.dp else 1.dp,
                                if (selected) WorkbenchColors.Red else WorkbenchColors.Line),
                            color = if (selected) WorkbenchColors.Tinted else WorkbenchColors.Paper,
                            modifier = Modifier.clickable(interactionSource = interaction, indication = null) { action = value }) {
                            Text(actionLabel(value), modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                                color = if (selected) WorkbenchColors.Red else WorkbenchColors.Ink)
                        }
                    }
                }
                if (action !in setOf("start", "reopen") || !packing) {
                    OutlinedTextField(note, { note = it }, Modifier.fillMaxWidth(), label = {
                        Text(if (needsNote) "处理结果（必填）" else "处理说明（选填）")
                    }, minLines = 2, enabled = !busy)
                }
                if (needsDate) QuietAction("下次跟进：$nextDate", { dateOpen = true }, Modifier.fillMaxWidth(), enabled = !busy,
                    icon = WorkbenchIcons.Calendar)
                actionError?.let { StateNotice(it, error = true) }
            }
        }
    }, confirmButton = {
        if (choices.isNotEmpty()) TextButton(onClick = { onSubmit(action, note, if (needsDate) nextDate else "") },
            enabled = canSubmit && actionsEnabled && !busy && !pending) {
            Text(if (busy) "正在提交…" else if (pending) "等待提交" else "提交记录")
        }
    }, dismissButton = { TextButton(onClick = onDismiss, enabled = !busy) { Text("关闭") } })

    if (dateOpen) {
        val initial = runCatching { LocalDate.parse(nextDate).atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli() }.getOrNull()
        val picker = rememberDatePickerState(initialSelectedDateMillis = initial)
        DatePickerDialog(onDismissRequest = { dateOpen = false }, confirmButton = {
            TextButton(onClick = {
                picker.selectedDateMillis?.let { nextDate = Instant.ofEpochMilli(it).atZone(ZoneOffset.UTC).toLocalDate().toString() }
                dateOpen = false
            }) { Text("确定") }
        }, dismissButton = { TextButton(onClick = { dateOpen = false }) { Text("取消") } }) { DatePicker(picker) }
    }
}

internal fun dailyActionChoices(item: DailyWorkItem, isAdmin: Boolean): List<String> {
    if (item.readOnly) return emptyList()
    val packing = item.role in setOf(ROLE_STOCK_PACKING, ROLE_ARRIVAL_PACKING)
    val printBlocked = packing && item.printState in setOf("reserved", "printing", "uncertain")
    return (when (item.status) {
        "todo", "review" -> listOf("start") + if (packing) listOf("packed") else emptyList()
        "in_progress" -> listOf("progress", "wait") + if (packing) listOf("packed") else listOf("resolve")
        "waiting" -> listOf("progress") + if (packing) listOf("packed") else listOf("resolve")
        "resolved", "packed", "condition_resolved" -> if (isAdmin) listOf("reopen") else emptyList()
        else -> emptyList()
    }).filterNot { it == "packed" && printBlocked }
}

internal fun canSubmitDailyAction(action: String, note: String, nextDate: String): Boolean {
    val needsDate = action in setOf("progress", "wait")
    val needsNote = action in setOf("progress", "wait", "resolve")
    return action.isNotBlank() && (!needsNote || note.isNotBlank()) && (!needsDate || nextDate.isNotBlank())
}

private fun actionLabel(action: String) = when (action) {
    "start" -> "开始处理"
    "progress" -> "继续跟进"
    "wait" -> "等待协同"
    "resolve" -> "提交解决"
    "packed" -> "已包装待发货"
    "reopen" -> "重新打开"
    else -> action
}

private fun pendingActionLabel(action: String) = when (action) {
    "transfer" -> "转交"
    "urgent_on" -> "标记急单"
    "urgent_off" -> "取消急单"
    else -> actionLabel(action)
}

@Composable
private fun TodayCompletedDialog(snapshot: DailyWorkSnapshot, onOrder: (String) -> Unit, onDismiss: () -> Unit) {
    val businessCompleted = snapshot.completedItems.filter { it.businessCompletedAt.isNotBlank() || it.status == "business_completed" }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("今日工作记录") }, text = {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("已完成跟进动作 ${snapshot.summary.todayActions}", style = MaterialTheme.typography.titleSmall)
            if (snapshot.todayEvents.isEmpty()) {
                Text("服务端返回了完成数量，暂无可显示的动作明细。", style = MaterialTheme.typography.bodySmall,
                    color = WorkbenchColors.Muted)
            } else snapshot.todayEvents.forEach { event ->
                Surface(shape = WorkbenchTokens.ControlShape, color = WorkbenchColors.Neutral) {
                    Column(Modifier.fillMaxWidth().padding(10.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(actionLabel(event.eventType), style = MaterialTheme.typography.labelLarge,
                                color = WorkbenchColors.Red)
                            Spacer(Modifier.weight(1f))
                            Text(event.createdAt, style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                        }
                        Text(event.title, style = MaterialTheme.typography.bodyMedium)
                        if (event.note.isNotBlank()) Text(event.note, style = MaterialTheme.typography.bodySmall,
                            color = WorkbenchColors.SecondaryInk)
                    }
                }
            }

            HorizontalDivider(color = WorkbenchColors.Line)
            Text("业务已完成 ${snapshot.summary.businessCompleted}", style = MaterialTheme.typography.titleSmall)
            if (businessCompleted.isEmpty()) {
                Text("尚无由 ERP 业务状态确认的完成明细。", style = MaterialTheme.typography.bodySmall,
                    color = WorkbenchColors.Muted)
            } else businessCompleted.forEach { item ->
                Surface(shape = WorkbenchTokens.ControlShape, border = BorderStroke(1.dp, WorkbenchColors.Line),
                    color = WorkbenchColors.Paper) {
                    Column(Modifier.fillMaxWidth().padding(10.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(dailyRoleLabel(item.role), style = MaterialTheme.typography.labelMedium,
                                color = WorkbenchColors.Muted)
                            Spacer(Modifier.weight(1f))
                            Text(item.businessCompletedAt, style = MaterialTheme.typography.labelSmall,
                                color = WorkbenchColors.Muted)
                        }
                        Text(item.title, style = MaterialTheme.typography.bodyMedium)
                        if (item.orderNos.isNotEmpty()) TextButton(onClick = { onOrder(item.orderNos.first()) },
                            contentPadding = PaddingValues(0.dp)) { Text("查看订单 ${item.orderNos.first()}") }
                    }
                }
            }
        }
    }, confirmButton = { TextButton(onClick = onDismiss) { Text("关闭") } })
}

@Composable
private fun StaffAdminScreen(state: DailyWorkState, actions: WorkbenchActions, modifier: Modifier, onClose: () -> Unit) {
    var editing by remember { mutableStateOf<StaffMember?>(null) }
    var creating by remember { mutableStateOf(false) }
    var resetting by remember { mutableStateOf<StaffMember?>(null) }
    var ruleOpen by rememberSaveable { mutableStateOf(false) }
    var customerOwnersOpen by rememberSaveable { mutableStateOf(false) }
    Column(modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 18.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onClose) { Icon(WorkbenchIcons.Close, "关闭人员管理") }
            Column(Modifier.weight(1f)) {
                Text("人员与岗位", style = MaterialTheme.typography.headlineMedium.copy(fontSize = 23.sp))
                Text("账号启用与今日在岗分别管理", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            }
            TextButton(onClick = { creating = true }) { Text("新增") }
        }
        if (state.staffLoading) LinearProgressIndicator(Modifier.fillMaxWidth(), color = WorkbenchColors.Red)
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 18.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            WorkbenchCard(tinted = state.snapshot.assignmentEnabled == false) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("自动派工", style = MaterialTheme.typography.titleMedium)
                        Text(when (state.snapshot.assignmentEnabled) {
                            true -> "销售按客户固定归属；包装等岗位按在岗和负荷分配。"
                            false -> "预览中；任务会生成，但保持未分配。"
                            null -> "正在读取自动派工设置…"
                        }, style = MaterialTheme.typography.bodySmall,
                            color = WorkbenchColors.Muted)
                    }
                    Switch(checked = state.snapshot.assignmentEnabled == true,
                        onCheckedChange = actions.setAssignmentEnabled,
                        enabled = !state.settingsSaving && state.snapshot.assignmentEnabled != null)
                }
            }
            WorkbenchCard {
                Text("客户固定销售", style = MaterialTheme.typography.titleMedium)
                Text("统一管理每个客户的固定销售，临时代办单独设置。", style = MaterialTheme.typography.bodySmall)
                TextButton(onClick = { actions.loadCustomerOwners(); customerOwnersOpen = true }) { Text("管理客户销售／代办") }
            }
            WorkbenchCard {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("采购负责人规则", style = MaterialTheme.typography.titleMedium)
                        Text("采购按商品编号固定负责人；已有采购事项保留原负责人。",
                            style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    }
                    TextButton(onClick = { ruleOpen = true }, enabled = !state.rulesSaving) { Text("新增") }
                }
                if (state.rulesLoading) LinearProgressIndicator(Modifier.fillMaxWidth(), color = WorkbenchColors.Red)
                state.rulesError?.let { StateNotice(it, error = true) }
                if (!state.rulesLoading && state.assignmentRules.none { it.role == ROLE_PURCHASE }) Text("尚未设置采购规则，按采购人员当前负荷分配。",
                    style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                state.assignmentRules.filter { it.role == ROLE_PURCHASE }.forEach { rule ->
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("${dailyRoleLabel(rule.role)} · ${if (rule.matchType == "customer") "客户" else "商品"} ${rule.matchValue}",
                                style = MaterialTheme.typography.bodyMedium)
                            Text("负责人：${rule.staffName.ifBlank { state.staff.firstOrNull { it.id == rule.staffId }?.displayName ?: "未知员工" }}",
                                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                        }
                        TextButton(onClick = { actions.deleteAssignmentRule(rule.id) }, enabled = !state.rulesSaving) { Text("删除") }
                    }
                }
            }
            state.staffError?.let { StateNotice(it, error = true) }
            state.attendanceError?.let { StateNotice(it, error = true) }
            if (state.staff.isEmpty() && !state.staffLoading) EmptyWorkspace("还没有员工账号", "新增账号并选择岗位后，系统才能自动安排个人任务。", WorkbenchIcons.People)
            state.staff.forEach { person ->
                val load = state.snapshot.staffLoad.firstOrNull { it.userId == person.id }
                WorkbenchCard {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text(person.displayName, style = MaterialTheme.typography.titleMedium)
                            Text((if (person.isAdmin) "管理员 · " else "") +
                                if (person.active) "账号启用" else "账号停用",
                                style = MaterialTheme.typography.bodySmall, color = if (person.active) WorkbenchColors.Muted else WorkbenchColors.Error)
                        }
                        TextButton(onClick = { editing = person }) { Text("编辑") }
                    }
                    Text(person.roles.joinToString("、", transform = ::dailyRoleLabel).ifBlank {
                        if (person.isAdmin) "管理员不参与岗位派工" else "尚未分配岗位"
                    },
                        style = MaterialTheme.typography.bodyMedium)
                    if (load != null) Text("当前负荷：待办 ${load.todo} · 处理中 ${load.inProgress} · 待协同 ${load.waiting} · 合计 ${load.total}",
                        style = MaterialTheme.typography.bodySmall,
                        color = if (load.total > 0) WorkbenchColors.SecondaryInk else WorkbenchColors.Muted)
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("包装派工：按同品归并，无数量上限", Modifier.weight(1f), style = MaterialTheme.typography.bodySmall,
                            color = WorkbenchColors.Muted)
                        if (state.attendanceAvailable) {
                            Text("今日在岗", style = MaterialTheme.typography.labelMedium)
                            Switch(checked = state.attendance[person.id] == true,
                                onCheckedChange = { actions.setStaffAttendance(person.id, it) },
                                enabled = state.attendanceSavingId == null && person.active)
                        } else Text("今日在岗待服务支持", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                    }
                    TextButton(onClick = { resetting = person }) { Text("重设临时密码") }
                }
            }
            Spacer(Modifier.height(18.dp))
        }
    }
    if (creating) StaffEditorDialog(null, state, onDismiss = { if (!state.staffSaving) creating = false }, onSave = actions.createStaff)
    editing?.let { person -> StaffEditorDialog(person, state, onDismiss = { if (!state.staffSaving) editing = null },
        onSave = { actions.updateStaff(person.id, it) }) }
    resetting?.let { person -> PasswordResetDialog(person, state, onDismiss = { if (!state.staffSaving) resetting = null },
        onSave = { actions.resetStaffPassword(person.id, it) }) }
    if (ruleOpen) AssignmentRuleDialog(state, onDismiss = { if (!state.rulesSaving) ruleOpen = false },
        onSave = actions.saveAssignmentRule)
    if (customerOwnersOpen) CustomerOwnershipDialog(state, actions, onDismiss = { customerOwnersOpen = false })
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun AssignmentRuleDialog(state: DailyWorkState, onDismiss: () -> Unit,
                                 onSave: (String, String, String) -> Unit) {
    val role = ROLE_PURCHASE
    var matchValue by rememberSaveable { mutableStateOf("") }
    var staffId by rememberSaveable { mutableStateOf("") }
    val candidates = state.staff.filter { it.active && role in it.roles }
    val openedRevision = remember { state.rulesSaveRevision }
    LaunchedEffect(state.rulesSaveRevision) { if (state.rulesSaveRevision > openedRevision) onDismiss() }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("新增采购负责人规则") }, text = {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedTextField(matchValue, { matchValue = it }, Modifier.fillMaxWidth(), singleLine = true,
                label = { Text("商品编号 SKU（完整匹配）") },
                enabled = !state.rulesSaving)
            Text("负责人", style = MaterialTheme.typography.labelLarge)
            if (candidates.isEmpty()) Text("没有已启用且具备此岗位的员工，请先编辑人员岗位。",
                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
            else FlowRow(horizontalArrangement = Arrangement.spacedBy(7.dp)) {
                candidates.forEach { person -> FilterChip(selected = staffId == person.id,
                    onClick = { staffId = person.id }, label = { Text(person.displayName) }) }
            }
            state.rulesError?.let { StateNotice(it, error = true) }
        }
    }, confirmButton = { TextButton(onClick = { onSave(role, matchValue.trim(), staffId) },
        enabled = matchValue.isNotBlank() && staffId.isNotBlank() && !state.rulesSaving) {
        Text(if (state.rulesSaving) "保存中…" else "保存规则")
    } }, dismissButton = { TextButton(onClick = onDismiss, enabled = !state.rulesSaving) { Text("取消") } })
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun StaffEditorDialog(person: StaffMember?, state: DailyWorkState, onDismiss: () -> Unit, onSave: (StaffDraft) -> Unit) {
    var username by rememberSaveable(person?.id) { mutableStateOf(person?.username.orEmpty()) }
    var displayName by rememberSaveable(person?.id) { mutableStateOf(person?.displayName.orEmpty()) }
    var password by rememberSaveable(person?.id) { mutableStateOf("") }
    var roles by rememberSaveable(person?.id) { mutableStateOf(person?.roles?.toSet() ?: emptySet()) }
    var active by rememberSaveable(person?.id) { mutableStateOf(person?.active ?: true) }
    val openedRevision = remember { state.staffSaveRevision }
    LaunchedEffect(state.staffSaveRevision) { if (state.staffSaveRevision > openedRevision) onDismiss() }
    val isAdmin = person?.isAdmin == true
    val valid = username.isNotBlank() && displayName.isNotBlank() && (roles.isNotEmpty() || isAdmin) &&
        (person != null || password.length >= 8)
    AlertDialog(onDismissRequest = onDismiss, title = { Text(if (person == null) "新增员工" else "编辑员工") }, text = {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedTextField(username, { username = it }, Modifier.fillMaxWidth(), label = { Text("登录账号") },
                singleLine = true, enabled = person == null && !state.staffSaving)
            OutlinedTextField(displayName, { displayName = it }, Modifier.fillMaxWidth(), label = { Text("姓名") },
                singleLine = true, enabled = !state.staffSaving)
            if (person == null) OutlinedTextField(password, { password = it }, Modifier.fillMaxWidth(), label = { Text("临时密码（至少8位）") },
                singleLine = true, enabled = !state.staffSaving)
            Text("岗位", style = MaterialTheme.typography.labelLarge)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(7.dp)) {
                DAILY_WORK_ROLES.forEach { role -> FilterChip(selected = role in roles, onClick = {
                    roles = if (role in roles) roles - role else roles + role
                }, label = { Text(dailyRoleLabel(role)) }) }
            }
            Text("包装按同品自动分配，不设数量上限。暂停派工请关闭今日在岗。",
                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            if (person != null) Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("账号启用", Modifier.weight(1f)); Switch(active, { active = it }, enabled = !state.staffSaving)
            }
            state.staffError?.let { StateNotice(it, error = true) }
        }
    }, confirmButton = { TextButton(onClick = {
        onSave(StaffDraft(username.trim(), displayName.trim(), password, roles, active, person?.packingLimit ?: 3, isAdmin))
    }, enabled = valid && !state.staffSaving) { Text(if (state.staffSaving) "保存中…" else "保存") } },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !state.staffSaving) { Text("取消") } })
}

@Composable
private fun PasswordResetDialog(person: StaffMember, state: DailyWorkState, onDismiss: () -> Unit, onSave: (String) -> Unit) {
    var password by rememberSaveable(person.id) { mutableStateOf("") }
    val openedRevision = remember { state.staffSaveRevision }
    LaunchedEffect(state.staffSaveRevision) { if (state.staffSaveRevision > openedRevision) onDismiss() }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("重设 ${person.displayName} 的密码") }, text = {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("员工下次登录时使用这个临时密码。", color = WorkbenchColors.Muted, style = MaterialTheme.typography.bodyMedium)
            OutlinedTextField(password, { password = it }, Modifier.fillMaxWidth(), label = { Text("新临时密码（至少8位）") },
                singleLine = true, enabled = !state.staffSaving)
            state.staffError?.let { StateNotice(it, error = true) }
        }
    }, confirmButton = { TextButton(onClick = { onSave(password) }, enabled = password.length >= 8 && !state.staffSaving) {
        Text(if (state.staffSaving) "保存中…" else "保存密码")
    } }, dismissButton = { TextButton(onClick = onDismiss, enabled = !state.staffSaving) { Text("取消") } })
}
