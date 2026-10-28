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
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.ul.workbench.ui.theme.WorkbenchTokens
import com.ul.workbench.ui.theme.WorkbenchColors
import com.ul.workbench.data.*

internal fun packingPrintRecord(actor: String, at: String): String =
    if (actor.isBlank() || at.isBlank()) "历史打印记录（人员／时间未记录）" else "最近打印：$actor · $at"

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
internal fun StockPackingFilterSheet(state: DailyWorkState, onLoadOptions: (String) -> Unit = {}, onDismiss: () -> Unit,
    onApply: (StockPackingFilters, String, String, String, String) -> Unit) {
    var filter by rememberSaveable(stateSaver = androidx.compose.runtime.saveable.Saver<StockPackingFilters, List<String>>(
        save = { listOf(it.customer, it.sku, it.orderNo, it.orderDateFrom, it.orderDateTo, it.printState, it.sort, it.pageSize.toString(), it.customerKey, it.supplier, it.receiptFrom, it.receiptTo) },
        restore = { StockPackingFilters(it[0], it[1], it[2], it[3], it[4], it[5], it[6], it[7].toInt(), it.getOrElse(8){""}, it.getOrElse(9){""}, it.getOrElse(10){""}, it.getOrElse(11){""}) })) { mutableStateOf(state.stockFilters) }
    var view by rememberSaveable { mutableStateOf(state.view) }
    var dueFrom by rememberSaveable { mutableStateOf(state.boardDueFrom) }
    var dueTo by rememberSaveable { mutableStateOf(state.boardDueTo) }
    var owner by rememberSaveable { mutableStateOf(state.boardAssigneeId) }
    var pageSize by rememberSaveable { mutableStateOf(filter.pageSize.toString()) }
    var dateField by remember { mutableStateOf<String?>(null) }
    var picker by remember { mutableStateOf("") }
    val arrival = state.selectedRole == ROLE_ARRIVAL_PACKING
    LaunchedEffect(view, state.selectedRole) { onLoadOptions(view) }
    val size = pageSize.toIntOrNull()
    val valid = size in 1..100 && filter.valid() && (dueFrom.isBlank() || dueTo.isBlank() || dueFrom <= dueTo)
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 18.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(if (arrival) "筛选来货包装" else "筛选库存包装", style = MaterialTheme.typography.titleMedium)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                listOf("mine" to "本人全部", "role" to "本岗位全部").forEach { (value, label) ->
                    FilterChip(view == value, { view = value; if (value == "mine") owner = "" }, label = { Text(label) })
                }
            }
            OutlinedButton(onClick = { picker = "customer" }, modifier = Modifier.fillMaxWidth()) { Text(filter.customer.ifBlank { "全部客户 · 点击选择" }) }
            if (arrival) {
                OutlinedButton(onClick = { picker = "supplier" }, modifier = Modifier.fillMaxWidth()) { Text(state.packingOptions.suppliers.firstOrNull { it.id == filter.supplier }?.name ?: "全部供应商／内部产出") }
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    OutlinedButton(onClick = { dateField = "receipt_from" }, modifier = Modifier.weight(1f)) { Text(filter.receiptFrom.ifBlank { "入库开始日期" }) }
                    OutlinedButton(onClick = { dateField = "receipt_to" }, modifier = Modifier.weight(1f)) { Text(filter.receiptTo.ifBlank { "入库结束日期" }) }
                }
            }
            if (state.packingOptionsBusy) Text("正在加载可选客户和来源…", style = MaterialTheme.typography.labelSmall)
            state.packingOptionsError?.let { Text(it, color = WorkbenchColors.Error) }
            OutlinedTextField(filter.sku, { filter = filter.copy(sku = it) }, label = { Text("商品编码") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
            OutlinedTextField(filter.orderNo, { filter = filter.copy(orderNo = it) }, label = { Text("ERP 单号／客户订单号（可输入部分）") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                OutlinedButton(onClick = { dateField = "order_from" }, modifier = Modifier.weight(1f)) { Text(filter.orderDateFrom.ifBlank { "订货开始日期" }) }
                OutlinedButton(onClick = { dateField = "order_to" }, modifier = Modifier.weight(1f)) { Text(filter.orderDateTo.ifBlank { "订货结束日期" }) }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                OutlinedButton(onClick = { dateField = "due_from" }, modifier = Modifier.weight(1f)) { Text(dueFrom.ifBlank { "交期开始日期" }) }
                OutlinedButton(onClick = { dateField = "due_to" }, modifier = Modifier.weight(1f)) { Text(dueTo.ifBlank { "交期结束日期" }) }
            }
            Text("打印状态", style = MaterialTheme.typography.labelLarge)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                listOf("all" to "全部", "unprinted" to "未打印", "printed" to "已打印", "uncertain" to "待核对", "reserved" to "打印中").forEach { (value, label) ->
                    FilterChip(filter.printState == value, { filter = filter.copy(printState = value) }, label = { Text(label) })
                }
            }
            Text("排序", style = MaterialTheme.typography.labelLarge)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                listOf("default" to "原优先顺序", "location" to "货位", "order_date" to "订货日期", "delivery_date" to "交期").plus(if (arrival) listOf("receipt_date" to "入库时间") else emptyList()).forEach { (value, label) ->
                    FilterChip(filter.sort == value, { filter = filter.copy(sort = value) }, label = { Text(label) })
                }
            }
            Text("每页数量", style = MaterialTheme.typography.labelLarge)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) { listOf(10, 20, 50, 100).forEach { count ->
                FilterChip(pageSize == count.toString(), { pageSize = count.toString() }, label = { Text(count.toString()) })
            } }
            OutlinedTextField(pageSize, { pageSize = it }, label = { Text("自定义 1–100") }, singleLine = true,
                isError = size !in 1..100, modifier = Modifier.testTag("stock-page-size"))
            if (view == "role") FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                FilterChip(owner.isBlank(), { owner = "" }, label = { Text("全部执行人") })
                state.board.assignees.forEach { person -> FilterChip(owner == person.id, { owner = person.id }, label = { Text(person.displayName) }) }
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                TextButton(onClick = { filter = StockPackingFilters(); pageSize = "50"; dueFrom = ""; dueTo = ""; owner = ""; view = "mine" }) { Text("重置") }
                Button(onClick = { onApply(filter.copy(pageSize = size!!), view, dueFrom, dueTo, owner) }, enabled = valid) { Text("应用筛选") }
            }
            Spacer(Modifier.height(16.dp))
        }
    }
    if (picker.isNotBlank()) PackingOptionPicker(if (picker == "customer") "选择客户" else "选择供应商／来源",
        if (picker == "customer") state.packingOptions.customers else state.packingOptions.suppliers,
        onDismiss = { picker = "" }) { option ->
        filter = if (picker == "customer") filter.copy(customerKey = option?.id.orEmpty(), customer = option?.name.orEmpty())
            else filter.copy(supplier = option?.id.orEmpty())
        picker = ""
    }
    dateField?.let { field ->
        val value = when (field) { "order_from" -> filter.orderDateFrom; "order_to" -> filter.orderDateTo; "receipt_from" -> filter.receiptFrom; "receipt_to" -> filter.receiptTo; "due_from" -> dueFrom; else -> dueTo }
        BoardDateDialog(value, onDismiss = { dateField = null }) { selected ->
            when (field) { "order_from" -> filter = filter.copy(orderDateFrom = selected); "order_to" -> filter = filter.copy(orderDateTo = selected)
                "receipt_from" -> filter = filter.copy(receiptFrom = selected); "receipt_to" -> filter = filter.copy(receiptTo = selected); "due_from" -> dueFrom = selected; else -> dueTo = selected }
            dateField = null
        }
    }
}

@Composable
internal fun StockPackingPrintDialog(preview: PackingPrintPreview, busy: Boolean, actions: WorkbenchActions) {
    var reprint by rememberSaveable(preview.id) { mutableStateOf(false) }
    var uncertain by rememberSaveable(preview.id) { mutableStateOf(false) }
    var maximum by rememberSaveable(preview.id) { mutableStateOf(preview.maxPerPackage) }
    val changed = maximum.trim() != preview.maxPerPackage
    Dialog(onDismissRequest = { if (!busy) actions.dismissStockPackingPrint() }, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxWidth().fillMaxHeight(0.86f).padding(horizontal = 18.dp), shape = WorkbenchTokens.CardShape,
            color = WorkbenchColors.Paper) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("确认打印范围", style = MaterialTheme.typography.titleLarge)
                Text("${preview.orderCount} 单 · ${preview.lineCount} 行 · ${preview.labelCount} 张标签 · 已打印 ${preview.reprintCount} 单 · 待核对 ${preview.uncertainCount} 单")
                OutlinedTextField(maximum, { maximum = it }, label = { Text("每张标签数量（空为每行一张）") }, enabled = !busy,
                    modifier = Modifier.fillMaxWidth().testTag("stock-print-maximum"), singleLine = true)
                if (changed) TextButton(onClick = { actions.previewStockPackingPrint(maximum) }, enabled = !busy) { Text("重新预览数量") }
                if (preview.reprintCount > 0) Row { Checkbox(reprint, { reprint = it }, enabled = !busy); Text("这些订单已打印，确认再次打印", Modifier.weight(1f)) }
                if (preview.uncertainCount > 0) Row { Checkbox(uncertain, { uncertain = it }, enabled = !busy); Text("已核对实际出纸，确认重打待核对订单", Modifier.weight(1f)) }
                LazyColumn(Modifier.weight(1f).fillMaxWidth()) { items(preview.orders, key = { it.orderNo }) { order ->
                    Column(Modifier.padding(vertical = 6.dp)) {
                        Text("${order.orderNo} · ${order.labelCount} 张 · ${when (order.printState) { "printed" -> "重打"; "uncertain" -> "待核对重打"; else -> "首次打印" }}",
                            style = MaterialTheme.typography.bodySmall)
                        if (order.printState != "unprinted") Text(packingPrintRecord(order.actorName, order.printedAt), style = MaterialTheme.typography.labelSmall)
                        order.arrivalSources.forEach { Text(it, style = MaterialTheme.typography.labelSmall) }
                        order.lines.forEach { line ->
                            Text("${line.sku} · ${line.quantity} 支 · ${line.labelCount} 张", style = MaterialTheme.typography.bodySmall)
                            Text(listOf(line.name, line.spec).filter(String::isNotBlank).joinToString(" · "), style = MaterialTheme.typography.labelSmall)
                            if (line.warnings.isNotEmpty()) Text(line.warnings.joinToString(" · "), style = MaterialTheme.typography.labelSmall,
                                color = WorkbenchColors.Muted)
                        }
                    }
                } }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(onClick = actions.dismissStockPackingPrint, enabled = !busy) { Text("取消") }
                    Button(onClick = { actions.confirmStockPackingPrint(reprint, uncertain) },
                        enabled = !busy && !changed && preview.valid() && (preview.reprintCount == 0 || reprint) && (preview.uncertainCount == 0 || uncertain),
                        modifier = Modifier.testTag("stock-print-confirm")) { Text("确认发送") }
                }
            }
        }
    }
}

@Composable
internal fun StockPackingRecoveryDialog(state: DailyWorkState, actions: WorkbenchActions) {
    var checked by rememberSaveable { mutableStateOf(false) }
    AlertDialog(onDismissRequest = actions.dismissStockPackingPrint, title = { Text("先核对上次打印") }, text = {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(state.error ?: "旧打印任务须完成恢复；不能直接清除占用。")
            Row { Checkbox(checked, { checked = it }, enabled = !state.packingPrintPreviewBusy); Text("我已检查实际出纸，确认继续核对") }
        }
    }, confirmButton = { TextButton(onClick = actions.acknowledgeStockPackingPrint,
        enabled = checked && !state.packingPrintPreviewBusy) { Text("核对并恢复") } },
        dismissButton = { TextButton(onClick = actions.dismissStockPackingPrint) { Text("稍后处理") } })
}

@Composable
private fun PackingOptionPicker(title: String, options: List<PackingFilterOption>, onDismiss: () -> Unit, onSelect: (PackingFilterOption?) -> Unit) {
    var keyword by rememberSaveable { mutableStateOf("") }
    AlertDialog(onDismissRequest = onDismiss, title = { Text(title) }, text = {
        Column {
            OutlinedTextField(keyword, { keyword = it }, label = { Text("搜索名称") }, singleLine = true)
            TextButton(onClick = { onSelect(null) }) { Text("全部") }
            LazyColumn(Modifier.heightIn(max = 320.dp)) {
                items(options.filter { it.name.contains(keyword.trim(), ignoreCase = true) }, key = { it.id }) { option ->
                    TextButton(onClick = { onSelect(option) }, modifier = Modifier.fillMaxWidth()) { Text(option.name) }
                }
            }
        }
    }, confirmButton = { TextButton(onClick = onDismiss) { Text("关闭") } })
}
