package com.ul.workbench.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.ul.workbench.data.*
import com.ul.workbench.ui.components.StateNotice
import com.ul.workbench.ui.components.WorkbenchCard
import com.ul.workbench.ui.theme.WorkbenchColors
import java.time.LocalDate

internal fun customerOwnerStatus(owner: CustomerOwner): String = when (owner.status) {
    "review" -> "归属待核对"
    "assigned" -> "固定销售：${owner.ownerName.ifBlank { "人员资料待核对" }}"
    else -> "固定销售：待指定"
}

@Composable
internal fun CustomerOwnershipDialog(state: DailyWorkState, actions: WorkbenchActions,
    initialCustomer: String = "", initialProfile: String = "", onDismiss: () -> Unit) {
    var search by rememberSaveable { mutableStateOf(initialCustomer) }
    var editingKey by rememberSaveable { mutableStateOf<String?>(null) }
    var editingDeputy by rememberSaveable { mutableStateOf(false) }
    val owners = state.customerOwners.filter { (initialProfile.isBlank() || it.profileId == initialProfile) &&
        (search.isBlank() || it.customer.contains(search.trim(), ignoreCase = true)) }
    if (editingKey == null) Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxWidth(.94f).fillMaxHeight(.88f), shape = MaterialTheme.shapes.large) {
        Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("客户固定销售", style = MaterialTheme.typography.titleLarge)
            Text("同一客户共用一名固定销售；请假使用临时代办。历史处理人保持原记录。",
                style = MaterialTheme.typography.bodySmall)
            OutlinedTextField(search, { search = it }, Modifier.fillMaxWidth(), singleLine = true,
                label = { Text("搜索客户") })
            if (state.customerOwnersLoading) LinearProgressIndicator(Modifier.fillMaxWidth())
            state.customerOwnersError?.let { StateNotice(it, error = true) }
            if (!state.customerOwnersLoading && owners.isEmpty()) Text("没有匹配客户。请先更新订单，客户名称必须来自订单资料。")
            LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(owners, key = CustomerOwner::key) { owner ->
                    WorkbenchCard {
                        Text(owner.customer, style = MaterialTheme.typography.titleSmall)
                        Text(customerOwnerStatus(owner), color = if (owner.status == "review") WorkbenchColors.Error else WorkbenchColors.Muted)
                        if (owner.deputyId.isNotBlank()) Text("临时代办：${owner.deputyName.ifBlank { "人员待核对" }} · 至 ${owner.deputyUntil}",
                            style = MaterialTheme.typography.bodySmall)
                        if (state.customerOwners.map { it.profileId }.distinct().size > 1) Text("账套 ${owner.profileId}", style = MaterialTheme.typography.labelSmall)
                        Text("当前事项 ${owner.activeTaskCount} · 跟进订单 ${owner.activeFollowupCount}", style = MaterialTheme.typography.bodySmall)
                        owner.reviewReasons.forEach { Text(it, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error) }
                        owner.identityConflicts.forEach { Text(it, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error) }
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            TextButton(onClick = { editingKey = owner.key; editingDeputy = false }, enabled = owner.canSetOwner) { Text("设置固定销售") }
                            TextButton(onClick = { editingKey = owner.key; editingDeputy = true },
                                enabled = owner.canSetDeputy) { Text("临时代办") }
                        }
                    }
                }
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                TextButton(onClick = actions.loadCustomerOwners, enabled = !state.customerOwnersLoading) { Text("刷新归属") }
                TextButton(onClick = onDismiss) { Text("关闭") }
            }
        }
        }
    }
    editingKey?.let { key -> state.customerOwners.firstOrNull { it.key == key }?.let { owner ->
        CustomerOwnerEditor(owner, editingDeputy, state, actions, onDismiss = { editingKey = null })
    } }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun CustomerOwnerEditor(owner: CustomerOwner, deputy: Boolean, state: DailyWorkState,
    actions: WorkbenchActions, onDismiss: () -> Unit) {
    var selectedId by rememberSaveable(owner.key, deputy) { mutableStateOf(if (deputy) owner.deputyId else owner.ownerId) }
    var until by rememberSaveable(owner.key, deputy) { mutableStateOf(owner.deputyUntil) }
    var reason by rememberSaveable(owner.key, deputy) { mutableStateOf("") }
    var expectedVersion by rememberSaveable(owner.key, deputy) { mutableStateOf(owner.version) }
    val openedRevision = remember { state.customerOwnersSaveRevision }
    val candidates = state.staff.filter { it.active && ROLE_SALES in it.roles && (!deputy || it.id != owner.ownerId) }
    val pending = state.pendingActions.firstOrNull { it.taskId == "customer-owner:${owner.key}" }
    val validDate = !deputy || selectedId.isBlank() || runCatching { LocalDate.parse(until) }.isSuccess
    LaunchedEffect(state.customerOwnersSaveRevision) {
        if (state.customerOwnersSaveRevision > openedRevision && state.customerOwnersSavedKey == "customer-owner:${owner.key}") onDismiss()
    }
    Dialog(onDismissRequest = { if (!state.customerOwnersSaving) onDismiss() },
        properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxWidth(.94f).fillMaxHeight(.88f), shape = MaterialTheme.shapes.large) {
            Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(if (deputy) "设置临时代办" else "设置客户固定销售", style = MaterialTheme.typography.titleLarge)
            Column(Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(owner.customer, style = MaterialTheme.typography.titleSmall)
                Text(if (deputy) "代办到期后结束，不改变固定销售和历史处理人。" else
                    "影响该客户全部当前销售事项及后续新订单，保留历史处理记录。",
                    style = MaterialTheme.typography.bodySmall)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    FilterChip(selected = selectedId.isBlank(), onClick = { selectedId = "" },
                        label = { Text(if (deputy) "取消代办" else "待指定") }, enabled = !state.customerOwnersSaving)
                    candidates.forEach { person -> FilterChip(selected = person.id == selectedId,
                        onClick = { selectedId = person.id }, label = { Text(person.displayName) },
                        enabled = !state.customerOwnersSaving) }
                }
                if (deputy && selectedId.isNotBlank()) OutlinedTextField(until, { until = it },
                    label = { Text("代办截止日期 YYYY-MM-DD") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(reason, { reason = it }, label = { Text("变更原因") }, modifier = Modifier.fillMaxWidth(), enabled = !state.customerOwnersSaving)
                if (owner.version != expectedVersion) {
                    StateNotice("客户归属已变化。当前固定销售：${owner.ownerName.ifBlank { "待指定" }}。填写内容已保留。", error = true)
                    TextButton(onClick = { expectedVersion = owner.version }, enabled = pending == null) { Text("已核对，采用当前归属版本") }
                }
                state.customerOwnersError?.let { StateNotice(it, error = true) }
                if (state.customerOwnersError != null || owner.version != expectedVersion) TextButton(onClick = actions.loadCustomerOwners,
                    enabled = !state.customerOwnersLoading) { Text("重新读取当前归属（保留填写内容）") }
                if (pending != null) {
                    StateNotice(if (pending.requiresReview) "这次变更未生效，内容已保留待核对。" else "变更已保存在手机，等待提交结果。", error = pending.requiresReview)
                    if (pending.requiresReview) TextButton(onClick = { actions.discardDailyActionReview(pending.idempotencyKey) }) { Text("撤回未提交变更（保留填写内容）") }
                }
            }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(onClick = onDismiss, enabled = !state.customerOwnersSaving) { Text("返回") }
                    TextButton(onClick = { actions.saveCustomerOwner(CustomerOwnerDraft(
            owner.profileId, owner.customer, expectedVersion, selectedId, reason.trim(), deputy,
            if (selectedId.isBlank()) "" else until.trim())) },
            enabled = !state.customerOwnersSaving && pending == null && reason.isNotBlank() && validDate && owner.version == expectedVersion &&
                (if (deputy) owner.canSetDeputy else owner.canSetOwner)) {
            Text(if (state.customerOwnersSaving) "保存中…" else "确认客户变更")
                    }
                }
            }
        }
    }
}
