package com.ul.workbench.ui.screens

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
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
import com.ul.workbench.ui.theme.WorkbenchColors
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@Composable
internal fun PurchaseUnfinishedScreen(state: PurchaseUnfinishedState, actions: WorkbenchActions,
                                      modifier: Modifier = Modifier) {
    var keyword by rememberSaveable { mutableStateOf("") }
    BackHandler {
        if (state.editorOpen) actions.closePurchasePromise()
        else if (state.selectedSupplier != null) actions.closePurchaseSupplier()
        else actions.closePurchaseUnfinished()
    }
    Column(modifier.fillMaxSize().padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            TextButton(onClick = if (state.selectedSupplier == null) actions.closePurchaseUnfinished else actions.closePurchaseSupplier,
                enabled = !state.saving) { Text(if (state.selectedSupplier == null) "返回任务" else "返回供应商") }
            TextButton(onClick = actions.refreshPurchaseUnfinished, enabled = !state.loading && !state.loadingMore && !state.saving) { Text("刷新") }
        }
        val meta = if (state.selectedSupplier != null && state.page.meta.snapshotId.isNotBlank()) state.page.meta else state.meta
        Text((state.selectedSupplier?.name ?: "采购未交／供应商") +
            if (meta.internalTrial) " · 内部试用" else "", style = MaterialTheme.typography.titleLarge)
        if (meta.internalTrial) StateNotice("内部试用快照，仅供核对采购未交数据。交期回复和催货分享暂未开放。")
        Text("${if (meta.internalTrial) "试用数据采集时间" else "当前已发布数据"}：${purchaseTime(meta.capturedAt)}",
            style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        if (meta.queryStart.isNotBlank()) Text("导出范围 ${meta.queryStart} 至 ${meta.queryEnd}", style = MaterialTheme.typography.bodySmall)
        if (meta.stale && meta.snapshotId.isNotBlank()) StateNotice("采购数据已过期，当前显示上次成功快照", error = true)
        when (state.meta.collectorStatus) {
            "needs_human" -> StateNotice("采集等待人工验证，请处理网页登录验证码。" +
                if (meta.readable) "当前仍显示上次成功数据。" else "尚无已发布数据。", error = true)
            "failed" -> StateNotice("本轮采集失败，" +
                if (meta.readable) "保留上次成功数据" else "尚无已发布数据", error = true)
            "running" -> Text("采购采集正在运行", color = WorkbenchColors.Muted)
            "succeeded" -> if (!meta.readable) Text("采集试验成功，采购数据待验收发布", color = WorkbenchColors.Muted)
        }
        if (!meta.unitsVerified || !meta.coverageComplete)
            StateNotice("当前仅供内部核对：采购单位和历史覆盖尚未全部核实，正式催货分享暂未开放。")
        state.error?.let { StateNotice(it, error = true) }
        state.message?.let { StateNotice(it) }
        if (state.editor != null && !state.editorOpen && meta.canRecordReply)
            TextButton(onClick = actions.resumePurchasePromise) { Text("继续已保留的交期回复 · ${state.editor.line.orderNo}") }
        else if (state.editor != null && !state.editorOpen && meta.internalTrial)
            Text("已保留的交期回复草稿将在正式验收后才能继续。", color = WorkbenchColors.Muted,
                style = MaterialTheme.typography.bodySmall)
        if (state.loading) LinearProgressIndicator(Modifier.fillMaxWidth())
        if (state.selectedSupplier == null) {
            OutlinedTextField(keyword, { keyword = it }, label = { Text("查找供应商名称或编号") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            val suppliers = state.suppliers.filter { keyword.isBlank() || it.name.contains(keyword.trim(), true) || it.code.contains(keyword.trim(), true) }
            if (!state.loading && suppliers.isEmpty()) Text(if (meta.readable) "没有匹配的供应商" else "尚无已发布采购快照，自动采集正在验收。", color = WorkbenchColors.Muted)
            LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(8.dp), contentPadding = PaddingValues(bottom = 20.dp)) {
                items(suppliers, key = { it.code }) { supplier ->
                    Surface(Modifier.fillMaxWidth().clickable { actions.openPurchaseSupplier(supplier) },
                        color = WorkbenchColors.Paper, shape = MaterialTheme.shapes.medium, border = BorderStroke(0.5.dp, WorkbenchColors.Line)) {
                        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                            Text(supplier.name, style = MaterialTheme.typography.titleMedium)
                            Text("${supplier.code} · ${supplier.orderCount} 张采购单 · ${supplier.rowCount} 行未交", style = MaterialTheme.typography.bodySmall)
                            if (supplier.ambiguousRows > 0) Text("${supplier.ambiguousRows} 行明细身份待核对", color = WorkbenchColors.Error)
                        }
                    }
                }
            }
        } else {
            Text("已加载 ${state.page.items.size} / ${state.page.total} 行", style = MaterialTheme.typography.bodySmall)
            // The current backend always returns draft_only=true/send_allowed=false.
            // Rendering/sharing remains closed until the complete export contract is accepted.
            OutlinedButton(onClick = {}, enabled = false, modifier = Modifier.fillMaxWidth()) {
                Text(if (meta.internalTrial) "内部试用期间不可催货分享" else "生成催货图片并分享（暂未开放）")
            }
            Text(state.reminder.reason.ifBlank { "催货资料尚未通过完整核验" }, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(8.dp), contentPadding = PaddingValues(bottom = 20.dp)) {
                items(state.page.items, key = { "${it.lineNumber}:${it.lineKey}" }) { line ->
                    PurchaseLineCard(line, canEdit = meta.canRecordReply && state.meta.canRecordReply &&
                        !line.ambiguous && !state.loading && !state.saving,
                        onEdit = { actions.editPurchasePromise(line) })
                }
                if (state.page.hasMore) item {
                    TextButton(onClick = actions.loadMorePurchaseUnfinished, enabled = !state.loadingMore && !state.loading,
                        modifier = Modifier.fillMaxWidth()) { Text(if (state.loadingMore) "加载中…" else "加载更多明细") }
                }
                if (!state.loading && state.page.items.isEmpty()) item { Text("当前供应商没有可显示的未交明细") }
            }
        }
    }
    if (state.editorOpen && state.meta.canRecordReply) state.editor?.let { PurchasePromiseEditor(it, state, actions) }
}

@Composable
private fun PurchaseLineCard(line: PurchaseUnfinishedLine, canEdit: Boolean, onEdit: () -> Unit) {
    Surface(Modifier.fillMaxWidth(), color = WorkbenchColors.Paper, shape = MaterialTheme.shapes.medium,
        border = BorderStroke(0.5.dp, WorkbenchColors.Line)) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Text("${line.productCode} · ${line.productName}", style = MaterialTheme.typography.titleMedium)
            Text("采购单 ${line.orderNo}")
            Text("未交 ${line.unfinished}${line.unit.ifBlank { "（单位待核对）" }} · 已完成 ${line.completed} / 订货 ${line.ordered}")
            Text("${line.warehouse} · 订货 ${line.orderDate}", color = WorkbenchColors.Muted, style = MaterialTheme.typography.bodySmall)
            if (line.arrivalDate.isNotBlank()) Text("原到货日期 ${line.arrivalDate}", style = MaterialTheme.typography.bodySmall)
            if (line.ambiguous) Text("同单商品明细不能唯一对应，请先核对", color = WorkbenchColors.Error)
            if (line.promisedDate.isNotBlank()) {
                Text("供应商回复交期 ${line.promisedDate}")
                Text(line.promiseNote, style = MaterialTheme.typography.bodySmall)
            }
            if (line.promiseRequiresConfirmation) {
                Text("历史回复待核对：${line.priorPromisedDate}", color = WorkbenchColors.Error)
                if (line.priorPromiseNote.isNotBlank()) Text(line.priorPromiseNote, style = MaterialTheme.typography.bodySmall)
                Text("快照已更新，需确认该回复仍适用于当前明细", style = MaterialTheme.typography.bodySmall)
            }
            TextButton(onClick = onEdit, enabled = canEdit) { Text("记录／核对回复交期") }
        }
    }
}

@Composable
private fun PurchasePromiseEditor(draft: PurchasePromiseDraft, state: PurchaseUnfinishedState, actions: WorkbenchActions) {
    var discardOpen by remember { mutableStateOf(false) }
    Dialog(onDismissRequest = actions.closePurchasePromise, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxWidth().padding(16.dp).heightIn(max = 760.dp), shape = MaterialTheme.shapes.large, color = WorkbenchColors.Paper) {
            Column(Modifier.verticalScroll(rememberScrollState()).padding(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("记录供应商回复交期", style = MaterialTheme.typography.titleLarge)
                Text(draft.line.supplierName)
                Text("${draft.line.orderNo} · ${draft.line.productCode} · 未交 ${draft.line.unfinished}${draft.line.unit}")
                if (draft.line.promiseRequiresConfirmation) StateNotice("上次回复 ${draft.line.priorPromisedDate}：${draft.line.priorPromiseNote}。请核实当前采购明细后填写本次确认结果。")
                OutlinedTextField(draft.promisedDate, { actions.updatePurchasePromise(it, draft.note) },
                    label = { Text("回复交期 YYYY-MM-DD") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                    enabled = !state.saving && !draft.outcomeUncertain)
                OutlinedTextField(draft.note, { actions.updatePurchasePromise(draft.promisedDate, it) },
                    label = { Text("回复来源和核对说明") }, minLines = 3, modifier = Modifier.fillMaxWidth(),
                    enabled = !state.saving && !draft.outcomeUncertain)
                draft.error?.let { StateNotice(it, error = true) }
                if (draft.outcomeUncertain) StateNotice("本次提交结果尚未确认。填写内容和提交标识已保留，重试会查询同一笔记录。")
                if (state.reviewing) LinearProgressIndicator(Modifier.fillMaxWidth())
                if (!draft.outcomeUncertain) TextButton(onClick = actions.reviewPurchasePromise, enabled = !state.saving && !state.reviewing) {
                    Text("重新读取当前明细（保留填写内容）")
                }
                draft.current?.let { current ->
                    StateNotice("当前明细：${current.line.orderNo} · ${current.line.productCode}，未交 ${current.line.unfinished}${current.line.unit}。当前已记录交期：${current.line.promisedDate.ifBlank { "未确认" }}。")
                    TextButton(onClick = actions.acceptPurchasePromiseReview, enabled = !state.saving && !state.reviewing) { Text("已核对当前明细，保留填写内容") }
                }
                Button(onClick = actions.savePurchasePromise, modifier = Modifier.fillMaxWidth(),
                    enabled = !state.saving && !state.reviewing && !draft.requiresReview &&
                        (draft.outcomeUncertain || (draft.promisedDate.isNotBlank() && draft.note.isNotBlank()))) {
                    Text(if (state.saving) "保存中…" else if (draft.outcomeUncertain) "重试确认同一提交" else "确认保存回复")
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    TextButton(onClick = { discardOpen = true }, enabled = !state.saving && !state.reviewing && !draft.outcomeUncertain) { Text("放弃草稿") }
                    TextButton(onClick = actions.closePurchasePromise, enabled = !state.saving && !state.reviewing) { Text("保留并返回") }
                }
            }
        }
    }
    if (discardOpen) AlertDialog(onDismissRequest = { discardOpen = false }, title = { Text("放弃这份回复草稿？") },
        text = { Text("只移除手机尚未提交的填写内容。已保存的服务器记录继续保留。") },
        confirmButton = { TextButton(onClick = { discardOpen = false; actions.discardPurchasePromise() }) { Text("放弃草稿") } },
        dismissButton = { TextButton(onClick = { discardOpen = false }) { Text("继续填写") } })
}

private fun purchaseTime(value: String): String = if (value.isBlank()) "尚未发布" else runCatching {
    OffsetDateTime.parse(value).atZoneSameInstant(ZoneId.of("Asia/Shanghai"))
        .format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm"))
}.getOrDefault(value)
