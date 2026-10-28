package com.ul.workbench.ui.screens

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.ul.workbench.data.*
import com.ul.workbench.ui.components.*
import com.ul.workbench.ui.theme.WorkbenchColors
import com.ul.workbench.ui.theme.WorkbenchTokens
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun WorkScreen(state: WorkbenchState, actions: WorkbenchActions, modifier: Modifier = Modifier,
               manualOpen: Boolean, onManual: (Boolean) -> Unit, onDetails: (WorkItem) -> Unit,
               onInventoryDetails: (WorkItem) -> Unit = {}, onScopeDetails: () -> Unit) {
    val inventory = state.destination == Destination.Inventory
    var orderFiltersOpen by rememberSaveable { mutableStateOf(false) }
    val listState = rememberLazyListState()
    var presentedRevision by rememberSaveable { mutableIntStateOf(state.queryRevision) }
    var presentedUncertainPrint by rememberSaveable { mutableStateOf(false) }
    LaunchedEffect(state.queryRevision, state.uncertainPrint) {
        if (presentedRevision != state.queryRevision || (state.uncertainPrint && !presentedUncertainPrint)) {
            listState.scrollToItem(0)
        }
        presentedRevision = state.queryRevision
        presentedUncertainPrint = state.uncertainPrint
    }
    LaunchedEffect(orderFiltersOpen) { actions.editingChanged(orderFiltersOpen) }
    val querySummary = if (inventory) with(state.inventoryFilters) {
        listOf(state.query, sku, name, spec, model, material).filter { it.isNotBlank() }.distinct().joinToString(" · ")
    } else state.query
    LazyColumn(modifier.fillMaxSize().semantics { contentDescription = if (inventory) "库存结果列表" else "订单结果列表" },
        state = listState, contentPadding = PaddingValues(horizontal = WorkbenchTokens.ScreenPadding, vertical = if (state.queried) 10.dp else WorkbenchTokens.ScreenPadding), verticalArrangement = Arrangement.spacedBy(if (state.queried) 10.dp else 14.dp)) {
        item("heading") {
            PageHeading(if (inventory) "库存" else "订单", if (inventory) "查库存，找货位" else "扫码 · 核对 · 打印", compact = state.queried,
                action = if (!inventory && state.apiUpdates && state.dailyWork.me?.isAdmin != false) ({
                    TextButton(onClick = actions.updateData, enabled = !state.updateBusy && !state.printBusy && state.serverOnline,
                        modifier = Modifier.heightIn(min = 48.dp)) { Text(if (state.updateBusy) "正在更新…" else "更新订单") }
                }) else null)
        }
        if (!inventory && state.apiUpdates) item("update-status") { UpdateStatus(state, actions) }
        if (state.uncertainPrint) item("uncertain") {
            StateNotice(state.error ?: state.printMessage.ifBlank { "打印结果不确定，请先检查实际出纸。" }, error = true,
                onDismiss = actions.clearMessage, confirmLabel = "已核对出纸")
        }
        if (!state.error.isNullOrBlank() && !state.uncertainPrint) item("error") { StateNotice(state.error, error = true, onDismiss = actions.clearMessage) }
        if (!state.receipt.isNullOrBlank() && !state.uncertainPrint) item("receipt") { StateNotice(state.receipt, onDismiss = actions.clearMessage) }
        if (!inventory) item("scope") {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                ScopeSelector(state.scope, state.logistics, actions.changeScope, onScopeDetails, enabled = !state.printBusy)
                OrderFilterBar(state.orderFilters, state.printStateCounts, onOpen = { orderFiltersOpen = true }, enabled = !state.printBusy)
            }
        }
        item("scan") {
            if (manualOpen && inventory) InventoryManualSearch(if (state.queryMode == "sku_exact") "" else state.query, state.inventoryFilters,
                onDismiss = { onManual(false) }, onSubmit = { query, filters -> onManual(false); actions.searchInventory(query, filters) },
                onSubmitCode = { code -> onManual(false); actions.searchInventoryCode(code) })
            else if (manualOpen) ManualSearch(state.query, state.queryMode,
                onDismiss = { onManual(false) }, onSubmit = { query, mode -> onManual(false); actions.search(query, mode) })
            else ScanPrompt(querySummary, state.loading, onManual = { onManual(true) }, enabled = !state.printBusy, compact = state.queried)
        }
        if (state.queried) {
            item("summary") {
                MetricStrip(state.demand, state.available, state.stock, inventory)
            }
            item("results-heading") {
                Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
                    if (state.productName.isNotBlank()) Text(state.productName, style = MaterialTheme.typography.bodyMedium, color = WorkbenchColors.Muted)
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(if (inventory) "商品明细" else "待处理订单", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                        Text("${state.total} ${if (inventory) "项" else "行"}", style = MaterialTheme.typography.labelMedium, color = WorkbenchColors.Muted)
                        TextButton(onClick = actions.clearQuery, enabled = !state.printBusy && !manualOpen,
                            modifier = Modifier.heightIn(min = 48.dp), contentPadding = PaddingValues(horizontal = 8.dp),
                            colors = ButtonDefaults.textButtonColors(contentColor = WorkbenchColors.Muted)) { Text("清空") }
                    }
                }
            }
        }
        if (!state.queried && !state.loading) item("initial") {
            EmptyWorkspace("准备好，开始扫码", if (inventory) "扫描商品编号，即可查看库存和货位。" else "范围会应用到每一次扫码与标签打印。", if (inventory) WorkbenchIcons.Inventory else WorkbenchIcons.Scan)
        }
        if (state.queried && state.items.isEmpty() && !state.loading) item("empty") {
            EmptyWorkspace("没有匹配的结果", "检查条码，或调整查询范围后再试。", WorkbenchIcons.Search)
        }
        items(state.items, key = { it.id }) { item ->
            WorkItemCard(item, state.selectedIds.contains(item.id), inventory, enabled = !state.printBusy,
                onSelect = { actions.toggleSelection(item.id) }, onDetails = { onDetails(item) },
                onProduct = { onInventoryDetails(item) })
        }
        if (state.hasMore) item("more") {
            QuietAction(if (state.loading) "正在读取…" else "查看更多", actions.loadMore, Modifier.fillMaxWidth(), enabled = !state.loading && !state.printBusy)
        }
        if (state.sourceUpdatedAt.isNotBlank() && (inventory || !state.apiUpdates)) item("source") {
            Text("数据更新 ${state.sourceUpdatedAt}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        }
        item("tail") { Spacer(Modifier.height(4.dp)) }
    }
    if (orderFiltersOpen) OrderFiltersDialog(state.orderFilters, state.customerOptions,
        onApply = { actions.changeOrderFilters(it); orderFiltersOpen = false },
        onDismiss = { orderFiltersOpen = false })
}

@Composable
private fun OrderFilterBar(filters: OrderFilters, counts: PrintStateCounts, onOpen: () -> Unit, enabled: Boolean) {
    val stateLabel = when (filters.printState) { "unprinted" -> "未打印"; "printed" -> "已打印"; else -> "全部" }
    val extras = listOf(filters.customer.takeIf(String::isNotBlank)?.let { "客户 $it" },
        dateRangeLabel("订货", filters.orderDateFrom, filters.orderDateTo),
        dateRangeLabel("交期", filters.deliveryDateFrom, filters.deliveryDateTo),
        filters.sort.takeUnless { it == "default" }?.let { when (it) { "location" -> "按货位"; "order_date" -> "按订货日期"; else -> "按交期" } }).filterNotNull()
    Surface(shape = WorkbenchTokens.ControlShape, color = WorkbenchColors.Paper, border = BorderStroke(1.dp, WorkbenchColors.Line)) {
        Row(Modifier.fillMaxWidth().heightIn(min = 50.dp).clickable(enabled = enabled, onClick = onOpen).padding(horizontal = 13.dp),
            verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(9.dp)) {
            Text("筛选", style = MaterialTheme.typography.labelLarge)
            Text((listOf(stateLabel) + extras).joinToString(" · "), modifier = Modifier.weight(1f),
                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted, maxLines = 2)
            if (counts.all > 0) Text("未打印 ${counts.unprinted} 行／共 ${counts.all} 行", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
            Icon(WorkbenchIcons.ChevronRight, "打开订单筛选", Modifier.size(18.dp), tint = WorkbenchColors.Muted)
        }
    }
}

private fun dateRangeLabel(prefix: String, from: String, to: String): String? {
    fun short(value: String) = value.removePrefix("${LocalDate.now().year}-")
    return when {
        from.isNotBlank() && to.isNotBlank() && from == to -> "$prefix ${short(from)}"
        from.isNotBlank() && to.isNotBlank() -> "$prefix ${short(from)}–${short(to)}"
        from.isNotBlank() -> "$prefix ${short(from)}起"
        to.isNotBlank() -> "$prefix 至${short(to)}"
        else -> null
    }
}

@OptIn(ExperimentalLayoutApi::class, ExperimentalMaterial3Api::class)
@Composable
private fun OrderFiltersDialog(initial: OrderFilters, customerOptions: List<String>,
                               onApply: (OrderFilters) -> Unit, onDismiss: () -> Unit) {
    var printState by rememberSaveable { mutableStateOf(initial.printState) }
    var orderFrom by rememberSaveable { mutableStateOf(initial.orderDateFrom) }
    var orderTo by rememberSaveable { mutableStateOf(initial.orderDateTo) }
    var deliveryFrom by rememberSaveable { mutableStateOf(initial.deliveryDateFrom) }
    var deliveryTo by rememberSaveable { mutableStateOf(initial.deliveryDateTo) }
    var sort by rememberSaveable { mutableStateOf(initial.sort) }
    var customer by rememberSaveable { mutableStateOf(initial.customer) }
    var customerMenuOpen by remember { mutableStateOf(false) }
    var dateTarget by remember { mutableStateOf<DateTarget?>(null) }
    val customers = remember(customerOptions, initial.customer) {
        (customerOptions + initial.customer).map(String::trim).filter(String::isNotBlank).distinct().sorted()
    }
    val valid = (orderFrom.isBlank() || orderTo.isBlank() || orderFrom <= orderTo) &&
        (deliveryFrom.isBlank() || deliveryTo.isBlank() || deliveryFrom <= deliveryTo)
    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 24.dp).widthIn(max = 520.dp).heightIn(max = 720.dp),
            shape = WorkbenchTokens.CardShape, color = WorkbenchColors.Paper, border = BorderStroke(1.dp, WorkbenchColors.Line)) {
            Column {
                Row(Modifier.fillMaxWidth().padding(start = 18.dp, end = 8.dp, top = 10.dp, bottom = 8.dp),
                    verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Box(Modifier.size(4.dp, 22.dp).background(WorkbenchColors.Red))
                    Text("筛选订单", style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                    TextButton(onClick = {
                        printState = "all"; orderFrom = ""; orderTo = ""; deliveryFrom = ""; deliveryTo = ""; sort = "default"; customer = ""
                    }, modifier = Modifier.heightIn(min = 48.dp), colors = ButtonDefaults.textButtonColors(contentColor = WorkbenchColors.Muted)) { Text("重置") }
                }
                HorizontalDivider(color = WorkbenchColors.Line)
                LazyColumn(Modifier.weight(1f, fill = false).padding(horizontal = 18.dp),
                    contentPadding = PaddingValues(vertical = 14.dp), verticalArrangement = Arrangement.spacedBy(18.dp)) {
            item {
                FilterSectionTitle("客户")
                Spacer(Modifier.height(7.dp))
                ExposedDropdownMenuBox(expanded = customerMenuOpen,
                    onExpandedChange = { customerMenuOpen = !customerMenuOpen }) {
                    OutlinedTextField(value = customer.ifBlank { "全部客户" }, onValueChange = {}, readOnly = true,
                        modifier = Modifier.fillMaxWidth().menuAnchor(MenuAnchorType.PrimaryNotEditable), singleLine = true,
                        trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(customerMenuOpen) },
                        shape = WorkbenchTokens.ControlShape)
                    ExposedDropdownMenu(expanded = customerMenuOpen, onDismissRequest = { customerMenuOpen = false }) {
                        DropdownMenuItem(text = { Text("全部客户") }, onClick = { customer = ""; customerMenuOpen = false })
                        customers.forEach { value ->
                            DropdownMenuItem(text = { Text(value) }, onClick = { customer = value; customerMenuOpen = false })
                        }
                    }
                }
                if (customers.isEmpty()) Text("更新订单后显示已填写客户。", style = MaterialTheme.typography.bodySmall,
                    color = WorkbenchColors.Muted)
            }
            item {
                FilterSectionTitle("打印状态")
                Spacer(Modifier.height(7.dp))
                FlowRow(horizontalArrangement = Arrangement.spacedBy(7.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
                    listOf("all" to "全部", "unprinted" to "未打印", "printed" to "已打印").forEach { (value, label) ->
                        WorkbenchFilterChip(printState == value, { printState = value }, label)
                    }
                }
            }
            item {
                DateRangeSelector("订货日期", orderFrom, orderTo,
                    onFrom = { dateTarget = DateTarget.OrderFrom }, onTo = { dateTarget = DateTarget.OrderTo },
                    onClear = { orderFrom = ""; orderTo = "" })
            }
            item {
                DateRangeSelector("交期", deliveryFrom, deliveryTo,
                    onFrom = { dateTarget = DateTarget.DeliveryFrom }, onTo = { dateTarget = DateTarget.DeliveryTo },
                    onClear = { deliveryFrom = ""; deliveryTo = "" })
            }
            item {
                FilterSectionTitle("排序")
                Spacer(Modifier.height(7.dp))
                FlowRow(horizontalArrangement = Arrangement.spacedBy(7.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
                    listOf("default" to "默认", "location" to "货位", "order_date" to "订货日期", "delivery_date" to "交期").forEach { (value, label) ->
                        WorkbenchFilterChip(sort == value, { sort = value }, label)
                    }
                }
            }
            if (!valid) item { Text("开始日期不能晚于结束日期。", color = WorkbenchColors.Error, style = MaterialTheme.typography.bodySmall) }
                }
                HorizontalDivider(color = WorkbenchColors.Line)
                Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp), horizontalArrangement = Arrangement.End,
                    verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = onDismiss, modifier = Modifier.heightIn(min = 48.dp), colors = ButtonDefaults.textButtonColors(contentColor = WorkbenchColors.Muted)) { Text("取消") }
                    Spacer(Modifier.width(4.dp))
                    Button(onClick = { onApply(OrderFilters(printState, orderFrom, orderTo, deliveryFrom, deliveryTo, sort, customer)) },
                        enabled = valid, modifier = Modifier.heightIn(min = 48.dp), shape = WorkbenchTokens.ControlShape) { Text("应用筛选") }
                }
            }
        }
    }
    dateTarget?.let { target ->
        val current = when (target) {
            DateTarget.OrderFrom -> orderFrom; DateTarget.OrderTo -> orderTo
            DateTarget.DeliveryFrom -> deliveryFrom; DateTarget.DeliveryTo -> deliveryTo
        }
        OrderDatePicker(target.label, current, onDismiss = { dateTarget = null }) { selected ->
            when (target) {
                DateTarget.OrderFrom -> orderFrom = selected; DateTarget.OrderTo -> orderTo = selected
                DateTarget.DeliveryFrom -> deliveryFrom = selected; DateTarget.DeliveryTo -> deliveryTo = selected
            }
            dateTarget = null
        }
    }
}

@Composable
private fun FilterSectionTitle(title: String, trailing: (@Composable () -> Unit)? = null) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(title, style = MaterialTheme.typography.labelLarge, modifier = Modifier.weight(1f))
        trailing?.invoke()
    }
}

@Composable
private fun WorkbenchFilterChip(selected: Boolean, onClick: () -> Unit, label: String,
                                modifier: Modifier = Modifier, enabled: Boolean = true) {
    FilterChip(selected = selected, onClick = onClick, enabled = enabled, modifier = modifier.heightIn(min = 48.dp),
        label = { Text(label) }, leadingIcon = if (selected) ({ Icon(WorkbenchIcons.Check, null, Modifier.size(16.dp)) }) else null,
        colors = FilterChipDefaults.filterChipColors(selectedContainerColor = WorkbenchColors.Ink,
            selectedLabelColor = androidx.compose.ui.graphics.Color.White,
            selectedLeadingIconColor = androidx.compose.ui.graphics.Color.White),
        border = FilterChipDefaults.filterChipBorder(enabled = enabled, selected = selected,
            borderColor = WorkbenchColors.Line, selectedBorderColor = WorkbenchColors.Ink))
}

@Composable
private fun DateRangeSelector(title: String, from: String, to: String, onFrom: () -> Unit, onTo: () -> Unit, onClear: () -> Unit) {
    FilterSectionTitle(title, trailing = if (from.isNotBlank() || to.isNotBlank()) ({
        TextButton(onClick = onClear, modifier = Modifier.heightIn(min = 40.dp), contentPadding = PaddingValues(horizontal = 6.dp),
            colors = ButtonDefaults.textButtonColors(contentColor = WorkbenchColors.Muted)) { Text("清除") }
    }) else null)
    Spacer(Modifier.height(7.dp))
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        DateChoice("开始", from, onFrom, Modifier.weight(1f), "选择${title}开始日期")
        DateChoice("结束", to, onTo, Modifier.weight(1f), "选择${title}结束日期")
    }
}

@Composable
private fun DateChoice(label: String, value: String, onClick: () -> Unit, modifier: Modifier, description: String) {
    Surface(onClick = onClick, modifier = modifier.heightIn(min = 54.dp).semantics { contentDescription = description },
        shape = WorkbenchTokens.ControlShape, color = WorkbenchColors.Paper,
        border = BorderStroke(1.dp, if (value.isBlank()) WorkbenchColors.Line else WorkbenchColors.Ink)) {
        Row(Modifier.padding(horizontal = 11.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Icon(WorkbenchIcons.Calendar, null, Modifier.size(19.dp), tint = if (value.isBlank()) WorkbenchColors.Muted else WorkbenchColors.Red)
            Column {
                Text(label, style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                Text(value.ifBlank { "不限" }, style = MaterialTheme.typography.bodyMedium,
                    color = if (value.isBlank()) WorkbenchColors.Muted else WorkbenchColors.Ink, maxLines = 1)
            }
        }
    }
}

private enum class DateTarget(val label: String) {
    OrderFrom("订货开始日期"), OrderTo("订货结束日期"),
    DeliveryFrom("交期开始日期"), DeliveryTo("交期结束日期")
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun OrderDatePicker(title: String, initial: String, onDismiss: () -> Unit, onSelect: (String) -> Unit) {
    val initialMillis = remember(initial) {
        runCatching { LocalDate.parse(initial).atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli() }.getOrNull()
    }
    val picker = rememberDatePickerState(initialSelectedDateMillis = initialMillis)
    DatePickerDialog(onDismissRequest = onDismiss,
        confirmButton = { TextButton(onClick = {
            picker.selectedDateMillis?.let { millis ->
                onSelect(Instant.ofEpochMilli(millis).atZone(ZoneOffset.UTC).toLocalDate().toString())
            }
        }, enabled = picker.selectedDateMillis != null) { Text("确定") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } }) {
        DatePicker(state = picker, title = { Text(title, Modifier.padding(start = 24.dp, top = 16.dp)) }, showModeToggle = false)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ScopeSelector(scope: String, logistics: String, onChange: (String, String) -> Unit, onDetails: () -> Unit, enabled: Boolean) {
    val mainCategories = listOf("all" to "全部", "异常" to "异常", "需采购" to "需采购", "待到货" to "待到货", "催货" to "催货", "来货件" to "来货件", "stock" to "库存")
    val selected = if (scope == "stock") "stock" else logistics.takeIf { value -> mainCategories.any { it.first == value } } ?: "all"
    val listState = rememberLazyListState()
    LaunchedEffect(selected) {
        val selectedIndex = mainCategories.indexOfFirst { it.first == selected }.coerceAtLeast(0)
        listState.animateScrollToItem((selectedIndex - 2).coerceAtLeast(0))
    }
    Surface(shape = WorkbenchTokens.ControlShape, color = WorkbenchColors.Paper, border = BorderStroke(1.dp, WorkbenchColors.Line)) {
            LazyRow(Modifier.fillMaxWidth().padding(vertical = 7.dp).semantics { contentDescription = "订单分类列表" }, state = listState,
                contentPadding = PaddingValues(horizontal = 8.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                items(mainCategories, key = { it.first }) { (value, label) ->
                    val shownLabel = if (value == "stock" && selected == "stock" && logistics.isNotBlank())
                        "库存 ${logistics.removeSuffix("库存件")}" else label
                    WorkbenchFilterChip(selected == value, onClick = {
                        when (value) {
                            "all" -> onChange("all", "")
                            "stock" -> onDetails()
                            else -> onChange("all", value)
                        }
                    }, label = shownLabel, enabled = enabled,
                        modifier = Modifier.semantics { contentDescription = "订单分类 $label" })
                }
            }
    }
}

@Composable
fun LogisticsDialog(selected: String, onSelect: (String) -> Unit, onDismiss: () -> Unit) {
    AlertDialog(onDismissRequest = onDismiss, title = { Text("库存分类") }, text = {
        LazyColumn {
            items(listOf("" to "五类全部") + listOf("LS", "LD", "LM", "DQ", "QT").map { "${it}库存件" to "${it}库存件" }) { (value, label) ->
                Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).selectable(selected = selected == value, role = Role.RadioButton, onClick = { onSelect(value) }), verticalAlignment = Alignment.CenterVertically) {
                    RadioButton(selected = selected == value, onClick = null, modifier = Modifier.size(48.dp))
                    Text(label, style = MaterialTheme.typography.bodyLarge)
                }
            }
        }
    }, confirmButton = { TextButton(onClick = onDismiss, modifier = Modifier.heightIn(min = 48.dp)) { Text("取消") } })
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ManualSearch(initial: String, initialMode: String, onDismiss: () -> Unit, onSubmit: (String, String) -> Unit) {
    var value by rememberSaveable { mutableStateOf(initial) }
    var mode by rememberSaveable { mutableStateOf(when (initialMode) { "note" -> "summary"; "sku_exact" -> "sku"; else -> initialMode }) }
    val focus = remember { FocusRequester() }
    val keyboard = LocalSoftwareKeyboardController.current
    val focusManager = LocalFocusManager.current
    fun finish() { if (value.isNotBlank()) { keyboard?.hide(); focusManager.clearFocus(); onSubmit(value.trim(), mode) } }
    LaunchedEffect(Unit) { focus.requestFocus() }
    WorkbenchCard {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("手动查询", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            IconButton(onClick = { keyboard?.hide(); focusManager.clearFocus(); onDismiss() }, modifier = Modifier.size(48.dp)) { Icon(WorkbenchIcons.Close, "收起手动输入") }
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("sku" to "商品", "buyer" to "客户", "summary" to "摘要").forEach { (key, label) ->
                FilterChip(selected = mode == key, onClick = { mode = key }, modifier = Modifier.heightIn(min = 48.dp), label = { Text(label) })
            }
        }
        if (mode == "sku") {
            InventoryCodeField(Modifier.focusRequester(focus), onComplete = { code ->
                keyboard?.hide(); focusManager.clearFocus(); onSubmit(code, "sku")
            })
        } else {
            OutlinedTextField(value = value, onValueChange = { value = it }, modifier = Modifier.fillMaxWidth().focusRequester(focus),
                label = { Text(if (mode == "buyer") "客户名称" else "订单摘要") },
                singleLine = true, shape = WorkbenchTokens.ControlShape,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search), keyboardActions = KeyboardActions(onSearch = { finish() }))
            PrimaryAction("查询", { finish() }, Modifier.fillMaxWidth(), enabled = value.isNotBlank(), icon = WorkbenchIcons.Search)
        }
    }
}

@Composable
private fun InventoryManualSearch(initial: String, initialFilters: InventoryFilters, onDismiss: () -> Unit,
                                  onSubmit: (String, InventoryFilters) -> Unit, onSubmitCode: (String) -> Unit) {
    var keyword by rememberSaveable { mutableStateOf(initial) }
    var name by rememberSaveable { mutableStateOf(initialFilters.name) }
    var spec by rememberSaveable { mutableStateOf(initialFilters.spec) }
    var model by rememberSaveable { mutableStateOf(initialFilters.model) }
    var material by rememberSaveable { mutableStateOf(initialFilters.material) }
    var moreOpen by rememberSaveable { mutableStateOf(initial.isNotBlank() || initialFilters.material.isNotBlank()) }
    var submitted by remember { mutableStateOf(false) }
    val focus = remember { FocusRequester() }
    val keyboard = LocalSoftwareKeyboardController.current
    val focusManager = LocalFocusManager.current
    val canSubmit = listOf(keyword, name, spec, model, material).any { it.isNotBlank() }
    fun finish() {
        if (canSubmit && !submitted) {
            submitted = true
            keyboard?.hide()
            focusManager.clearFocus()
            onSubmit(keyword.trim(), InventoryFilters(name.trim(), spec.trim(), model.trim(), material.trim()))
        }
    }
    LaunchedEffect(Unit) { focus.requestFocus() }
    WorkbenchCard {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("手动查询", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            IconButton(onClick = { keyboard?.hide(); focusManager.clearFocus(); onDismiss() }, modifier = Modifier.size(48.dp)) {
                Icon(WorkbenchIcons.Close, "收起手动输入")
            }
        }
        InventoryCodeField(Modifier.focusRequester(focus), onComplete = { code ->
            if (!submitted) {
                submitted = true
                keyboard?.hide()
                focusManager.clearFocus()
                onSubmitCode(code)
            }
        })
        HorizontalDivider(color = WorkbenchColors.Line)
        Text("也可填写以下条件组合查询", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        InventoryField(name, { name = it }, "名称", Modifier.fillMaxWidth(), onSearch = { finish() })
        if (LocalDensity.current.fontScale > 1.2f) {
            InventoryField(spec, { spec = it }, "规格", Modifier.fillMaxWidth(), onSearch = { finish() })
            InventoryField(model, { model = it }, "等级 / 型号", Modifier.fillMaxWidth(), onSearch = { finish() })
        } else Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            InventoryField(spec, { spec = it }, "规格", Modifier.weight(1f), onSearch = { finish() })
            InventoryField(model, { model = it }, "等级 / 型号", Modifier.weight(1f), onSearch = { finish() })
        }
        TextButton(onClick = { moreOpen = !moreOpen }, modifier = Modifier.heightIn(min = 48.dp),
            contentPadding = PaddingValues(horizontal = 0.dp), colors = ButtonDefaults.textButtonColors(contentColor = WorkbenchColors.Muted)) {
            val filledExtras = listOf(keyword, material).count { it.isNotBlank() }
            Text(if (moreOpen) "收起更多条件" else if (filledExtras > 0) "更多条件 · 已填 $filledExtras 项" else "更多条件 · 货位、材质")
        }
        if (moreOpen) {
            InventoryField(keyword, { keyword = it }, "关键词 / 货位", Modifier.fillMaxWidth(), onSearch = { finish() })
            InventoryField(material, { material = it }, "材质", Modifier.fillMaxWidth(), onSearch = { finish() })
        }
        PrimaryAction("查询", { finish() }, Modifier.fillMaxWidth(), enabled = canSubmit, icon = WorkbenchIcons.Search)
    }
}

@Composable
private fun InventoryField(value: String, onChange: (String) -> Unit, label: String, modifier: Modifier,
                           onSearch: () -> Unit) {
    OutlinedTextField(value = value, onValueChange = onChange, modifier = modifier,
        label = { Text(label) }, singleLine = true, shape = WorkbenchTokens.ControlShape,
        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
        keyboardActions = KeyboardActions(onSearch = { onSearch() }))
}

@Composable
private fun WorkItemCard(item: WorkItem, selected: Boolean, inventory: Boolean, enabled: Boolean,
                         onSelect: () -> Unit, onDetails: () -> Unit, onProduct: () -> Unit) {
    val canSelect = item.isPrintEligible(if (inventory) Destination.Inventory else Destination.Orders)
    Surface(shape = WorkbenchTokens.CardShape, color = WorkbenchColors.Paper,
        border = BorderStroke(if (selected) 1.5.dp else 1.dp, if (selected) WorkbenchColors.Red else WorkbenchColors.Line)) {
        Column(Modifier.fillMaxWidth().padding(start = 12.dp, end = 16.dp, top = 10.dp, bottom = 16.dp), verticalArrangement = Arrangement.spacedBy(11.dp)) {
            Row(verticalAlignment = Alignment.Top) {
                Checkbox(checked = selected, onCheckedChange = { onSelect() }, enabled = enabled && canSelect, modifier = Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp))
                Column(Modifier.weight(1f).clickable(enabled = enabled, onClick = onDetails).padding(top = 8.dp, bottom = 5.dp)) {
                    Text(if (inventory) item.name else item.buyer.ifBlank { item.name }, style = MaterialTheme.typography.titleMedium)
                    Text(if (inventory) item.sku else item.orderNo.ifBlank { item.sku }, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                }
                if (!inventory) Column(Modifier.padding(top = 8.dp, start = 5.dp), horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    if (item.logistics.isNotBlank()) SmallTag(item.logistics, selected)
                    if (!item.requiresSettlement && !item.lifecyclePendingReview) SmallTag(when (item.printState) {
                        "reserved" -> "打印中"; "printed" -> "已打印"; "uncertain" -> "待核对"; else -> "未打印"
                    }, item.printState == "unprinted")
                }
                if (inventory) IconButton(onClick = onDetails, enabled = enabled, modifier = Modifier.size(48.dp)) {
                    Icon(WorkbenchIcons.ChevronRight, "库存详情与调整", tint = WorkbenchColors.Muted)
                }
            }
            Column(Modifier.padding(start = 12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                if (!inventory) Text(item.name, style = MaterialTheme.typography.bodyMedium)
                if (!inventory && item.printWarnings.isNotEmpty()) Text(item.printWarnings.joinToString(" · "),
                    style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                if (!inventory && item.summaryPerson.isNotBlank()) Text("摘要人员：${item.summaryPerson}",
                    style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                if (item.spec.isNotBlank()) Text(item.spec, style = MaterialTheme.typography.bodyMedium, color = WorkbenchColors.Muted)
                if (!inventory) Text("型号 ${item.model.ifBlank { "—" }}", style = MaterialTheme.typography.bodyMedium, color = WorkbenchColors.Muted)
                if (!inventory) Text("商品 ${item.sku}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                if (!inventory && (item.orderDate.isNotBlank() || item.deliveryDate.isNotBlank())) Text(
                    "订货 ${item.orderDate.ifBlank { "缺失" }} · 交期 ${item.deliveryDate.ifBlank { "缺失" }}",
                    style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                if (!inventory && item.location.isNotBlank()) Text("货位 ${item.location}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                if (!inventory && item.printContentChanged) Text("订单内容已变化，需重新打印", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
                if (item.warehouseName.isNotBlank() || item.warehouseId.isNotBlank()) Text("仓库 ${item.warehouseName.ifBlank { item.warehouseId }}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                if (item.calculationStatus == "blocked" || item.calculationIssues.isNotEmpty()) Text(
                    "分类待处理：${item.calculationIssues.firstOrNull() ?: "资料尚未完整"}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
                if (!item.printable) Text("暂不可打印：${item.printBlockReason.ifBlank { "打印资料不完整" }}", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
            }
            HorizontalDivider(color = WorkbenchColors.Line, modifier = Modifier.padding(start = 12.dp))
            Row(Modifier.fillMaxWidth().padding(start = 12.dp), horizontalArrangement = Arrangement.spacedBy(16.dp), verticalAlignment = Alignment.CenterVertically) {
                if (inventory) {
                    LabelValue("货位", item.location, Modifier.weight(1f))
                    LabelValue("可销售", item.available ?: "—", Modifier.weight(1f))
                    LabelValue("库存", item.stock ?: "—", Modifier.weight(1f))
                } else {
                    Text("需求数量", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    Text(item.quantity.ifBlank { "—" }, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                    TextButton(onClick = onProduct, enabled = enabled && item.sku.isNotBlank(),
                        contentPadding = PaddingValues(horizontal = 4.dp)) { Text("库存／图纸") }
                    IconButton(onClick = onDetails, enabled = enabled, modifier = Modifier.size(48.dp)) { Icon(WorkbenchIcons.ChevronRight, "查看订单详情", tint = WorkbenchColors.Muted) }
                }
            }
        }
    }
}

@Composable
fun ItemDetails(item: WorkItem, onDismiss: () -> Unit, onResetPrint: (() -> Unit)? = null) {
    AlertDialog(onDismissRequest = onDismiss, title = { Text(item.name.ifBlank { item.sku }) },
        text = { LazyColumn(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            items(listOf("商品编号" to item.sku, "订单号" to item.orderNo, "客户" to item.buyer, "仓库" to item.warehouseName.ifBlank { item.warehouseId },
                "订单状态" to when {
                    item.lifecyclePendingReview -> "订单状态待核对"
                    item.requiresSettlement -> "已出库，等待ERP结存"
                    else -> ""
                },
                "待处理资料" to item.calculationIssues.joinToString("\n"), "打印限制" to if (item.printable) "" else item.printBlockReason.ifBlank { "资料不完整，暂不可打印" },
                "规格" to item.spec, "型号" to item.model.ifBlank { "—" }, "物流公司" to item.logistics,
                "订货日期" to item.orderDate, "交期" to item.deliveryDate,
                "需求数量" to item.quantity, "可销售" to (item.available ?: "—"), "库存" to (item.stock ?: "—"), "货位" to item.location,
                "打印状态" to when (item.printState) { "reserved" -> "打印中"; "printed" -> "已打印"; "uncertain" -> "待核对"; else -> "未打印" },
                "摘要" to item.note).filter { it.second.isNotBlank() }) { (label, value) -> LabelValue(label, value) }
        } }, confirmButton = { TextButton(onClick = onDismiss, modifier = Modifier.heightIn(min = 48.dp)) { Text("完成") } },
        dismissButton = onResetPrint?.let { reset -> ({ TextButton(onClick = reset, modifier = Modifier.heightIn(min = 48.dp)) { Text("改为未打印") } }) })
}

@Composable
fun SplitEditor(state: WorkbenchState, actions: WorkbenchActions, modifier: Modifier = Modifier) {
    var amount by rememberSaveable { mutableStateOf("") }
    val limit = amount.toIntOrNull()?.takeIf { it > 0 }
    val focus = remember { FocusRequester() }
    val keyboard = LocalSoftwareKeyboardController.current
    val focusManager = LocalFocusManager.current
    val listState = rememberLazyListState()
    LaunchedEffect(state.error, state.uncertainPrint) {
        if (!state.error.isNullOrBlank() || state.uncertainPrint) {
            keyboard?.hide()
            focusManager.clearFocus()
            listState.scrollToItem(0)
        }
    }
    fun cancel() { keyboard?.hide(); focusManager.clearFocus(); actions.dismissSplit() }
    fun submit() {
        if (limit != null && !state.printBusy && !state.uncertainPrint) {
            keyboard?.hide()
            focusManager.clearFocus()
            // The state owner preserves whether PBOX requested all results or selected orders.
            actions.printSelected(limit)
        }
    }
    // This editor shares the activity window with ManualSearch; Scaffold owns IME insets.
    LazyColumn(modifier.fillMaxSize(), state = listState, contentPadding = PaddingValues(WorkbenchTokens.ScreenPadding), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item("heading") {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Icon(WorkbenchIcons.Split, null, Modifier.size(25.dp), tint = WorkbenchColors.Red)
                Text("拆箱打印", style = MaterialTheme.typography.headlineMedium, modifier = Modifier.weight(1f))
                TextButton(onClick = { cancel() }, modifier = Modifier.heightIn(min = 48.dp), enabled = !state.printBusy) { Text("取消") }
            }
        }
        if (state.uncertainPrint) item("uncertain") {
            StateNotice(state.error ?: state.printMessage.ifBlank { "打印结果不确定，请先检查实际出纸。" }, error = true,
                onDismiss = actions.clearMessage, confirmLabel = "已核对出纸")
        }
        if (!state.error.isNullOrBlank() && !state.uncertainPrint) item("error") {
            StateNotice(state.error, error = true, onDismiss = actions.clearMessage)
        }
        item("context") {
            Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
                Text(state.query.ifBlank { "当前查询结果" }, style = MaterialTheme.typography.titleMedium)
                Text("本次查询 ${state.total} 行" + (state.demand?.takeIf { it.isNotBlank() }?.let { " · 需求 $it" } ?: ""), style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            }
        }
        item("editor") {
            // Run after this lazy item's text field is composed, as in ManualSearch.
            LaunchedEffect(Unit) { if (state.error.isNullOrBlank() && !state.uncertainPrint) focus.requestFocus() }
            WorkbenchCard {
                Text("填写每箱数量，按订单数量自动拆分标签。", style = MaterialTheme.typography.bodyMedium)
                OutlinedTextField(value = amount, onValueChange = { amount = it.filter(Char::isDigit).take(8) }, label = { Text("每箱数量") }, singleLine = true,
                    modifier = Modifier.fillMaxWidth().focusRequester(focus), enabled = !state.printBusy,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number, imeAction = ImeAction.Done), keyboardActions = KeyboardActions(onDone = { submit() }), shape = WorkbenchTokens.ControlShape)
                PrimaryAction("确认拆箱打印", { submit() }, Modifier.fillMaxWidth(), enabled = limit != null && !state.printBusy && !state.uncertainPrint, icon = WorkbenchIcons.Printer)
            }
        }
    }
}
