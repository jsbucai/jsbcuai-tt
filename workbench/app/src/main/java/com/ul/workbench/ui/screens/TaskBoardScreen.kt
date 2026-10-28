package com.ul.workbench.ui.screens

import android.widget.Toast
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.listSaver
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.ul.workbench.data.*
import com.ul.workbench.ui.components.*
import com.ul.workbench.ui.theme.WorkbenchColors
import com.ul.workbench.ui.theme.WorkbenchTokens
import com.ul.workbench.ui.AssistantActions
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZoneOffset
import kotlinx.coroutines.delay
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

private val boardZone = ZoneId.of("Asia/Shanghai")
private val packingRoles = setOf(ROLE_STOCK_PACKING, ROLE_ARRIVAL_PACKING)
private val simpleBoardRoles = setOf(ROLE_SALES, ROLE_STOCK_PACKING, ROLE_ARRIVAL_PACKING)
private val noteBlue = Color(0xFF1769AA)

internal fun isCurrentBoardSection(section: String): Boolean = section in setOf("active", "todo", "in_progress", "exception")

internal fun taskBoardSections(role: String, counts: Map<String, Int>): List<Pair<String, String>> =
    if (role in simpleBoardRoles) listOf("todo" to "待处理 ${counts["todo"] ?: 0}",
        "in_progress" to "处理中 ${counts["in_progress"] ?: 0}") +
        if (role == ROLE_ARRIVAL_PACKING) listOf("exception" to "异常 ${counts["exception"] ?: 0}") else emptyList()
    else listOf("active" to "待处理", "processed" to "已处理", "archived" to "历史")

/** A single compact header leaves the scrollable work itself in charge of the screen. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun TaskBoardScreen(state: DailyWorkState, actions: WorkbenchActions, modifier: Modifier = Modifier,
                             onStaff: () -> Unit, onTask: (String) -> Unit,
                             assistantActions: AssistantActions = AssistantActions(), visible: Boolean = true) {
    var roleMenu by remember { mutableStateOf(false) }
    var filterOpen by rememberSaveable { mutableStateOf(false) }
    var searchOpen by rememberSaveable { mutableStateOf(false) }
    var search by rememberSaveable { mutableStateOf(state.boardKeyword) }
    var noticesOpen by rememberSaveable { mutableStateOf(false) }
    var todayOpen by rememberSaveable { mutableStateOf(false) }
    var selecting by rememberSaveable { mutableStateOf(false) }
    var followUpOpen by rememberSaveable { mutableStateOf(false) }
    var packedConfirm by rememberSaveable { mutableStateOf(false) }
    var moreMenu by remember { mutableStateOf(false) }
    var noteGroupId by rememberSaveable { mutableStateOf("") }
    var noteDraft by rememberSaveable { mutableStateOf("") }
    var noteRevision by rememberSaveable { mutableIntStateOf(0) }
    var pendingQueryChange by remember { mutableStateOf<(() -> Unit)?>(null) }
    var resetLine by remember { mutableStateOf<TaskBoardLine?>(null) }
    var resetReason by rememberSaveable { mutableStateOf("") }
    val detail = state.boardDetail
    val historyDetail = state.taskHistoryDetail
    val packing = state.selectedRole in packingRoles
    val simplified = state.selectedRole in simpleBoardRoles
    val stock = usesPackingTaskBoard(state.selectedRole)
    val currentSection = isCurrentBoardSection(state.boardSection)
    val showingRecords = !currentSection
    val selected = state.boardSelection?.objectCount
        ?: (state.boardSelectedIds.size + state.boardSelectedTaskIds.size)
    val busy = state.boardSelectionBusy || state.boardPrinting || state.boardExportBusy || state.packingPrintPreviewBusy || state.packingResetBusy ||
        state.boardExport != null || state.actionBusyId != null
    val canAct = state.boardVerified && state.board.tasksCurrent && detail?.tasksCurrent != false && !busy
    val context = LocalContext.current
    fun changeQuery(action: () -> Unit) {
        if (stock && (state.boardSelectedIds.isNotEmpty() || state.boardSelectedTaskIds.isNotEmpty() || state.boardSelectionAll)) pendingQueryChange = action
        else action()
    }
    LaunchedEffect(state.boardExport?.id) {
        val snapshot = state.boardExport ?: return@LaunchedEffect
        try {
            val images = withContext(Dispatchers.IO) { OrderImageShare.render(context, snapshot) }
            OrderImageShare.share(context, images)
        } catch (failure: Exception) {
            Toast.makeText(context, "图片分享失败：${failure.message ?: "请稍后重试"}", Toast.LENGTH_LONG).show()
        } finally {
            actions.clearTaskBoardExport()
        }
    }
    // The query and column own their position, so returning from a detail or
    // switching columns never resets the list to the top.
    val listStates = rememberSaveable(saver = listSaver<MutableMap<String, LazyListState>, Any>(
        save = { positions -> positions.flatMap { (key, position) ->
            listOf(key, position.firstVisibleItemIndex, position.firstVisibleItemScrollOffset)
        } }, restore = { values -> values.chunked(3).associate { part ->
            part[0] as String to LazyListState(part[1] as Int, part[2] as Int)
        }.toMutableMap() })) { mutableMapOf() }
    val listKey = listOf(state.selectedRole, state.view, state.boardKeyword, state.boardStatus,
        state.boardDueFrom, state.boardDueTo, state.boardAssigneeId,
        if (simplified && state.boardSection == "active") "todo" else state.boardSection,
        if (stock) "${state.stockFilters}:${state.stockPage}" else "").joinToString("\u0000")
    val listState = listStates.getOrPut(listKey) { LazyListState() }
    val detailState = rememberLazyListState()
    val density = LocalDensity.current
    val currentItems by rememberUpdatedState(state.board.items.associateBy(TaskBoardItem::id))
    val currentSelectedIds by rememberUpdatedState(state.boardSelectedIds)
    val allSelected by rememberUpdatedState(state.boardSelectionAll)
    val paintedIds = remember { mutableSetOf<String>() }
    val lastPaintedIndex = remember { intArrayOf(-1) }
    var dragActive by remember { mutableStateOf(false) }
    var dragY by remember { mutableFloatStateOf(Float.NaN) }
    var suppressTap by remember { mutableStateOf(false) }
    LaunchedEffect(dragActive, suppressTap) {
        if (!dragActive && suppressTap) {
            delay(250)
            suppressTap = false
        }
    }
    fun paintAt(y: Float) {
        val visible = listState.layoutInfo.visibleItemsInfo
        val target = visible.firstOrNull { y >= it.offset && y < it.offset + it.size } ?: return
        if (currentItems[target.key as? String] == null) return
        val previous = lastPaintedIndex[0].takeIf { it >= 0 } ?: target.index
        val range = minOf(previous, target.index)..maxOf(previous, target.index)
        visible.filter { it.index in range }.forEach { row ->
            val id = row.key as? String ?: return@forEach
            val item = currentItems[id] ?: return@forEach
            if (canAct && !allSelected && (if (stock) item.canPrint else item.canAct) && id !in currentSelectedIds && paintedIds.add(id)) {
                selecting = true
                actions.toggleTaskBoardItem(id)
            }
        }
        lastPaintedIndex[0] = target.index
    }
    LaunchedEffect(dragActive, dragY, listState) {
        if (!dragActive || dragY.isNaN()) return@LaunchedEffect
        val edge = with(density) { 56.dp.toPx() }
        val step = with(density) { 18.dp.toPx() }
        while (dragActive) {
            val end = listState.layoutInfo.viewportEndOffset.toFloat()
            val direction = when {
                dragY < edge && listState.canScrollBackward -> -1f
                dragY > end - edge && listState.canScrollForward -> 1f
                else -> break
            }
            listState.scrollBy(step * direction)
            paintAt(dragY)
            delay(16)
        }
    }
    val notices = boardNotices(state)
    val hasSelection = !state.adminDashboardOpen && currentSection && (selecting || selected > 0)
    val filtered = state.boardStatus != "all" || state.boardKeyword.isNotBlank() ||
        state.boardDueFrom.isNotBlank() || state.boardDueTo.isNotBlank() || state.boardAssigneeId.isNotBlank() ||
        (stock && state.stockFilters.copy(pageSize = 50) != StockPackingFilters())
    LaunchedEffect(state.selectedRole, state.view, state.boardKeyword, state.boardStatus,
        state.boardDueFrom, state.boardDueTo, state.boardAssigneeId) {
        selecting = false
    }
    LaunchedEffect(state.boardSection) { selecting = false }
    LaunchedEffect(state.salesActionRevision, noteGroupId) {
        if (noteGroupId.isNotBlank() && state.salesActionRevision > noteRevision && state.salesActionSavedId == noteGroupId) {
            noteGroupId = ""
            noteDraft = ""
        }
    }
    LaunchedEffect(detail?.item?.id) { detailState.scrollToItem(0) }
    val nearEnd by remember(listState) {
        derivedStateOf {
            val last = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: -1
            last >= listState.layoutInfo.totalItemsCount - 5 && last >= 0
        }
    }
    LaunchedEffect(visible, nearEnd, state.board.items.size, state.board.hasMore, state.boardPageBusy, state.error, detail?.item?.id) {
        if (!stock && visible && !state.adminDashboardOpen && detail == null && currentSection && nearEnd &&
            state.board.hasMore && !state.boardPageBusy && state.error == null)
            actions.loadMoreTaskBoard()
    }

    Column(modifier.fillMaxSize().testTag("task-board")) {
        Column(Modifier.fillMaxWidth().testTag("task-board-header").padding(horizontal = 12.dp)) {
            Row(Modifier.fillMaxWidth().heightIn(min = 48.dp), verticalAlignment = Alignment.CenterVertically) {
                if (state.adminDashboardOpen) {
                    TextButton(onClick = actions.closeAdminDashboard, contentPadding = PaddingValues(horizontal = 4.dp)) {
                        Text("← 返回")
                    }
                    Text("管理看板", Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
                } else if (historyDetail != null) {
                    TextButton(onClick = actions.closeTaskHistoryDetail, enabled = !state.taskHistoryBusy,
                        contentPadding = PaddingValues(horizontal = 4.dp)) { Text("← 返回") }
                    Text(historyDetail.item.title, Modifier.weight(1f), style = MaterialTheme.typography.titleSmall, fontWeight = androidx.compose.ui.text.font.FontWeight.Bold,
                        maxLines = 1, overflow = TextOverflow.Ellipsis)
                } else if (simplified && showingRecords) {
                    TextButton(onClick = { actions.selectTaskBoardSection(state.boardSectionsByRole[state.selectedRole] ?: "todo") }, enabled = !busy,
                        contentPadding = PaddingValues(horizontal = 4.dp)) { Text("← 返回") }
                    Text("过往记录", Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
                } else if (detail != null) {
                    TextButton(onClick = actions.closeTaskBoardDetail, enabled = !busy, contentPadding = PaddingValues(horizontal = 4.dp)) { Text("← 返回") }
                    Text(detail.item.title, Modifier.weight(1f), style = MaterialTheme.typography.titleSmall, fontWeight = androidx.compose.ui.text.font.FontWeight.Bold,
                        maxLines = 1, overflow = TextOverflow.Ellipsis)
                    if (state.selectedRole == ROLE_SALES) Box {
                        TextButton(onClick = { moreMenu = true }, enabled = !busy,
                            modifier = Modifier.testTag("task-board-detail-more").semantics { contentDescription = "更多操作" }) { Text("⋮") }
                        DropdownMenu(moreMenu, { moreMenu = false }) {
                            if (detail.item.workState == "in_progress") DropdownMenuItem(text = { Text("退回待处理") },
                                enabled = canAct && detail.item.canAct && !hasSelection, onClick = {
                                    moreMenu = false; actions.resetTaskBoardSales(detail.item.id)
                                })
                            DropdownMenuItem(text = { Text("过往记录") }, enabled = !hasSelection,
                                onClick = {
                                    moreMenu = false; actions.closeTaskBoardDetail(); actions.selectTaskBoardSection("processed")
                                })
                        }
                    }
                } else if (searchOpen) {
                    OutlinedTextField(search, { search = it }, Modifier.weight(1f).heightIn(min = 48.dp),
                        placeholder = { Text(if (state.selectedRole == ROLE_SALES) "客户／订单号" else "商品／订单号") },
                        singleLine = true, textStyle = MaterialTheme.typography.bodyMedium,
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                        keyboardActions = KeyboardActions(onSearch = { actions.searchTaskBoard(search); searchOpen = false }),
                        shape = WorkbenchTokens.ControlShape)
                    IconButton(onClick = { actions.searchTaskBoard(search); searchOpen = false }) {
                        Icon(WorkbenchIcons.Search, "执行任务搜索")
                    }
                    IconButton(onClick = { searchOpen = false }) { Icon(WorkbenchIcons.Close, "关闭搜索") }
                } else {
                    Box(Modifier.weight(1f)) {
                        TextButton(onClick = { roleMenu = true }, enabled = !busy && !hasSelection, contentPadding = PaddingValues(horizontal = 2.dp)) {
                            Text("${dailyRoleLabel(state.selectedRole).takeUnless { state.selectedRole.isBlank() } ?: "选择岗位"} ▾",
                                style = MaterialTheme.typography.titleMedium, maxLines = 1)
                        }
                        DropdownMenu(roleMenu, { roleMenu = false }) {
                            (if (state.me?.isAdmin == true) DAILY_WORK_ROLES else state.me?.roles.orEmpty()).forEach { role ->
                                DropdownMenuItem(text = { Text(dailyRoleLabel(role)) }, onClick = {
                                    roleMenu = false; actions.selectDailyRole(role)
                                })
                            }
                            if (state.me?.isAdmin == true || ROLE_PURCHASE in state.me?.roles.orEmpty())
                                DropdownMenuItem(text = { Text("采购未交／供应商") }, onClick = {
                                    roleMenu = false; actions.openPurchaseUnfinished()
                                })
                            if (state.me?.isAdmin == true) DropdownMenuItem(text = { Text("人员管理") }, onClick = {
                                roleMenu = false; onStaff()
                            })
                            if (state.me?.isAdmin == true) DropdownMenuItem(text = { Text("管理看板") }, onClick = {
                                roleMenu = false; actions.openAdminDashboard()
                            })
                        }
                    }
                    IconButton(onClick = { search = state.boardKeyword; searchOpen = true },
                        enabled = !busy && !hasSelection && currentSection) { Icon(WorkbenchIcons.Search, "搜索任务") }
                    TextButton(onClick = { filterOpen = true }, enabled = !busy && (stock || !hasSelection) && currentSection,
                        contentPadding = PaddingValues(horizontal = 6.dp)) { Text("筛选") }
                    if (simplified) Box {
                        TextButton(onClick = { moreMenu = true }, enabled = !busy && (stock || !hasSelection),
                            contentPadding = PaddingValues(horizontal = 6.dp),
                            modifier = Modifier.testTag("task-board-more").semantics { contentDescription = "更多操作" }) { Text("⋮") }
                        DropdownMenu(moreMenu, { moreMenu = false }) {
                            if (stock && currentSection && detail == null) DropdownMenuItem(text = { Text("全选全部筛选结果") },
                                enabled = state.view == "mine" && state.board.tasksCurrent,
                                onClick = { moreMenu = false; selecting = true; actions.selectAllTaskBoard() })
                            DropdownMenuItem(text = { Text("过往记录") }, onClick = {
                                moreMenu = false; actions.selectTaskBoardSection("processed")
                            })
                            DropdownMenuItem(text = { Text("今日记录") }, onClick = { moreMenu = false; todayOpen = true })
                        }
                    } else TextButton(onClick = { todayOpen = true }, contentPadding = PaddingValues(horizontal = 6.dp)) { Text("今日记录") }
                }
            }
            Row(Modifier.fillMaxWidth().heightIn(min = 32.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(if (state.adminDashboardOpen) "公司整单与四岗位 · 每 30 秒更新" else if (historyDetail != null) "${historyDetail.events.size} 条处理记录" else if (showingRecords)
                    "${state.taskHistory.total} 项 · 本地处理记录" else if (detail != null) "${detail.lines.size} 条订单明细" else
                    "${if (state.view == "mine") "本人" else "本岗位"}${if (filtered) "筛选" else "全部"} · " +
                        if (stock) "${state.board.total} 卡／${state.board.orderCount} 单" +
                            (if (state.board.lineCount > 0) "／${state.board.lineCount} 行" else "") else "${state.board.total} 项",
                    style = MaterialTheme.typography.labelMedium, color = WorkbenchColors.SecondaryInk,
                    maxLines = 1, modifier = Modifier.weight(1f), overflow = TextOverflow.Ellipsis)
                Text("数据 ${compactBoardTime(if (state.adminDashboardOpen) state.adminDashboard?.sourceUpdatedAt.orEmpty() else state.board.sourceUpdatedAt)}", style = MaterialTheme.typography.labelSmall,
                    color = WorkbenchColors.Muted, modifier = Modifier.clickable { noticesOpen = true })
                if (notices.isNotEmpty()) IconButton(onClick = { noticesOpen = true }, modifier = Modifier.size(32.dp)) {
                    Icon(WorkbenchIcons.Alert, "查看任务状态提示", Modifier.size(18.dp),
                        tint = if (!state.board.tasksCurrent || state.error != null) WorkbenchColors.Red else WorkbenchColors.Muted)
                }
                if (!state.adminDashboardOpen && currentSection && detail == null && historyDetail == null &&
                    (state.me?.isAdmin == true || state.selectedRole in state.me?.roles.orEmpty())) {
                    if (state.selectedRole == ROLE_SALES) TextButton(onClick = assistantActions.openSales,
                        enabled = !busy && !hasSelection, contentPadding = PaddingValues(horizontal = 6.dp),
                        modifier = Modifier.testTag("open-sales-assistant")) { Text("录单", style = MaterialTheme.typography.labelMedium) }
                    if (state.selectedRole == ROLE_PURCHASE) TextButton(onClick = assistantActions.openPurchase,
                        enabled = !busy && !hasSelection, contentPadding = PaddingValues(horizontal = 6.dp),
                        modifier = Modifier.testTag("open-purchase-assistant")) { Text("采购助手", style = MaterialTheme.typography.labelMedium) }
                }
                if (!state.adminDashboardOpen && currentSection) TextButton(onClick = {
                    selecting = !hasSelection
                    if (!selecting) actions.clearTaskBoardSelection()
                }, enabled = !busy, contentPadding = PaddingValues(horizontal = 6.dp), modifier = Modifier.heightIn(min = 32.dp)) {
                    Text(if (hasSelection) "取消" else "多选", style = MaterialTheme.typography.labelMedium)
                }
            }
            if (!state.adminDashboardOpen && detail == null && historyDetail == null) Row(Modifier.fillMaxWidth().height(32.dp),
                horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                val sections = if (simplified && showingRecords) listOf("processed" to "处理记录", "archived" to "已归档")
                    else taskBoardSections(state.selectedRole, state.board.workStateCounts)
                sections.forEach { (section, label) ->
                    TextButton(onClick = { changeQuery { selecting = false; actions.selectTaskBoardSection(section) } },
                        enabled = !busy && (stock || !hasSelection), modifier = Modifier.height(32.dp),
                        contentPadding = PaddingValues(horizontal = 8.dp),
                        colors = ButtonDefaults.textButtonColors(contentColor =
                            if (state.boardSection == section || (simplified && state.boardSection == "active" && section == "todo")) WorkbenchColors.Red else WorkbenchColors.Muted)) {
                        Text(label)
                    }
                }
            }
        }
        if (state.refreshing || state.boardDetailBusy || state.boardSelectionBusy || state.boardPrinting ||
            state.boardExportBusy || state.taskHistoryBusy || state.adminDashboardBusy)
            LinearProgressIndicator(Modifier.fillMaxWidth().height(2.dp), color = WorkbenchColors.Red, trackColor = WorkbenchColors.Neutral)
        HorizontalDivider(color = WorkbenchColors.Line)
        Box(Modifier.weight(1f).fillMaxWidth().testTag("task-board-content")) {
            when {
                state.adminDashboardOpen -> AdminDashboardContent(state, actions)
                showingRecords -> TaskHistoryContent(state, actions)
                state.loading && state.board.items.isEmpty() -> CircularProgressIndicator(Modifier.align(Alignment.Center), color = WorkbenchColors.Red)
                detail != null -> LazyColumn(state = detailState, modifier = Modifier.fillMaxSize().testTag("task-board-detail-list"), contentPadding = PaddingValues(bottom = 12.dp)) {
                    item("detail-summary") {
                        Column(Modifier.fillMaxWidth().padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                            Text(boardItemSummary(detail.item), style = MaterialTheme.typography.bodyMedium)
                            if (detail.item.subtitle.isNotBlank()) Text(detail.item.subtitle, style = MaterialTheme.typography.bodySmall)
                            if (detail.item.specification.isNotBlank()) Text(detail.item.specification, style = MaterialTheme.typography.bodySmall)
                            detail.item.arrivalSources.forEach { Text(it, style = MaterialTheme.typography.bodySmall) }
                            if (detail.item.unitNote.isNotBlank()) Text(detail.item.unitNote,
                                style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                            if ((if (stock) detail.item.printBlockedBy else detail.item.blockedBy).isNotBlank()) Text(
                                if (stock) detail.item.printBlockedBy else detail.item.blockedBy, color = WorkbenchColors.Error,
                                style = MaterialTheme.typography.bodySmall)
                            state.pendingActions.firstOrNull { it.taskId == detail.item.id }?.let { pending ->
                                Text(if (pending.requiresReview) "处理记录待核对" else "待提交 · 处理记录已保存在手机",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = if (pending.requiresReview) WorkbenchColors.Error else WorkbenchColors.Muted)
                            }
                            if (state.selectedRole == ROLE_SALES) {
                                if (detail.item.latestNote.isNotBlank()) Text("最新备注：${detail.item.latestNote}",
                                    style = MaterialTheme.typography.bodySmall, color = noteBlue)
                                TextButton(onClick = {
                                    noteGroupId = detail.item.id; noteDraft = ""; noteRevision = state.salesActionRevision
                                }, enabled = canAct && detail.item.canAct && !hasSelection) { Text("备注") }
                            } else if (state.selectedRole !in packingRoles && detail.item.canAct) TextButton(onClick = {
                                if (detail.item.id !in state.boardSelectedIds) actions.toggleTaskBoardItem(detail.item.id)
                                followUpOpen = true
                            }) { Text("记录跟进") }
                            if (state.me?.isAdmin == true && state.selectedRole == ROLE_SALES)
                                TextButton(onClick = { actions.openCustomerOrder(detail.item.id) }) { Text("客户固定销售／历史事项") }
                            detail.item.customerSalesOwners.forEach { owner ->
                                Text("${owner.customer} · 固定销售 ${if (owner.reviewRequired) "待核对" else owner.ownerName.ifBlank { "待指定" }}",
                                    style = MaterialTheme.typography.bodySmall)
                            }
                        }
                    }
                    state.message?.let { message -> item("message") { BoardInlineMessage(message) } }
                    state.error?.let { error -> item("error") { BoardInlineMessage(error, true, actions.refreshDailyWork) } }
                    items(detail.lines, key = { it.id }) { line ->
                        TaskBoardLineRow(line, selecting = hasSelection && packing,
                            selected = state.boardSelectionAll || line.taskId in state.boardSelectedTaskIds,
                            enabled = canAct && !state.boardSelectionAll, onSelect = { actions.toggleTaskBoardLine(line.taskId) },
                            onOrder = { actions.openDailyOrder(line.orderNo) },
                            onManage = if (state.me?.isAdmin == true && line.taskId.isNotBlank()) ({ onTask(line.taskId) }) else null,
                            simplePrinting = usesPackingTaskBoard(state.selectedRole),
                            onReset = if (stock && line.printState in setOf("printed", "uncertain") && line.stateRevision.isNotBlank()) ({
                                if (resetLine?.orderNo != line.orderNo) resetReason = ""
                                resetLine = line
                            }) else null)
                    }
                    if (detail.relatedGroups.isNotEmpty()) item("related-title") {
                        Text("影响 ${detail.relatedGroups.size} 张客户整单", Modifier.padding(12.dp), style = MaterialTheme.typography.titleSmall)
                    }
                    items(detail.relatedGroups, key = { "related-${it.id}" }) { group ->
                        Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp)) {
                            Text("${group.customer} · ${group.customerOrderNo}", style = MaterialTheme.typography.bodyMedium)
                            Text("缺 ${group.missingCount} 行 · 销售 ${group.assigneeName.ifBlank { "待安排" }} · 交期 ${group.dueDate.ifBlank { "待核对" }}",
                                style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                        }
                    }
                    if (state.selectedRole == ROLE_SALES && detail.notes.isNotEmpty()) item("notes-title") {
                        Text("备注记录", Modifier.padding(12.dp), style = MaterialTheme.typography.titleSmall)
                    }
                    if (state.selectedRole == ROLE_SALES) itemsIndexed(detail.notes,
                        key = { index, _ -> "note-$index" }) { _, note ->
                        Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 7.dp),
                            verticalArrangement = Arrangement.spacedBy(3.dp)) {
                            Text(note.note, style = MaterialTheme.typography.bodyMedium, color = noteBlue)
                            Text(note.createdAt, style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                        }
                    }
                    val events = if (state.selectedRole == ROLE_SALES) detail.events.filterNot { event ->
                        detail.notes.any { note -> note.createdAt == event.createdAt && note.note == event.note }
                    } else detail.events
                    if (events.isNotEmpty()) item("events-title") {
                        Text("处理记录", Modifier.padding(12.dp), style = MaterialTheme.typography.titleSmall)
                    }
                    items(events, key = { "event-${it.id}" }) { BoardEventRow(it) }
                }
                state.board.items.isEmpty() -> Column(Modifier.align(Alignment.Center).padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(if (state.error != null) "清单读取失败" else "没有符合条件的事项", style = MaterialTheme.typography.titleMedium)
                    Text(state.error ?: "可在筛选中查看本岗位全部。", Modifier.padding(vertical = 8.dp), color = WorkbenchColors.Muted)
                    TextButton(onClick = actions.refreshDailyWork) { Text("重新读取") }
                }
                else -> LazyColumn(state = listState, modifier = Modifier.fillMaxSize().testTag("task-board-list")
                    .pointerInput(listState, canAct) {
                        awaitEachGesture {
                            val down = awaitFirstDown(requireUnconsumed = false, pass = PointerEventPass.Initial)
                            val longPressAborted = withTimeoutOrNull(viewConfiguration.longPressTimeoutMillis) {
                                while (true) {
                                    val change = awaitPointerEvent(PointerEventPass.Initial).changes
                                        .firstOrNull { it.id == down.id }
                                    if (change == null || !change.pressed ||
                                        (change.position - down.position).getDistance() > viewConfiguration.touchSlop) break
                                }
                                true
                            } == true
                            if (!longPressAborted) {
                                paintedIds.clear()
                                lastPaintedIndex[0] = -1
                                suppressTap = true
                                dragActive = true
                                dragY = down.position.y
                                paintAt(dragY)
                                try {
                                    while (true) {
                                        val change = awaitPointerEvent(PointerEventPass.Initial).changes
                                            .firstOrNull { it.id == down.id } ?: break
                                        change.consume()
                                        if (!change.pressed) break
                                        dragY = change.position.y
                                        paintAt(dragY)
                                    }
                                } finally {
                                    dragActive = false
                                    dragY = Float.NaN
                                }
                            }
                        }
                    }, contentPadding = PaddingValues(bottom = 12.dp)) {
                    state.message?.let { message -> item("message") { BoardInlineMessage(message) } }
                    state.error?.let { error -> item("error") { BoardInlineMessage(error, true, actions.refreshDailyWork) } }
                    if (!state.board.tasksCurrent) item("stale") {
                        BoardInlineMessage("任务尚未跟上业务数据，处理已暂停。", true, actions.refreshDailyWork)
                    }
                    items(state.board.items, key = { it.id }) { item ->
                        TaskBoardItemRow(item, selecting = hasSelection,
                            selected = state.boardSelectionAll || item.id in state.boardSelectedIds,
                            enabled = canAct && !state.boardSelectionAll,
                            onSelect = { if (!suppressTap) actions.toggleTaskBoardItem(item.id) },
                            onOpen = { if (!suppressTap) actions.openTaskBoardItem(item.id) },
                            onStart = if (state.selectedRole == ROLE_SALES && item.workState != "in_progress")
                                ({ if (!suppressTap) actions.startTaskBoardSales(item.id) }) else null,
                            pendingLabel = state.pendingActions.firstOrNull { it.taskId == item.id }?.let {
                                if (it.requiresReview) "处理记录待核对" else "待提交"
                            }.orEmpty())
                    }
                    if (state.boardPageBusy) item("loading") {
                        Box(Modifier.fillMaxWidth().padding(12.dp), contentAlignment = Alignment.Center) {
                            CircularProgressIndicator(Modifier.size(22.dp), color = WorkbenchColors.Red, strokeWidth = 2.dp)
                        }
                    } else if (!stock && !state.board.hasMore) item("end") {
                        Text("已显示全部 ${state.board.total} 项", Modifier.fillMaxWidth().padding(12.dp),
                            style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                    }
                }
            }
        }
        if (stock && currentSection && detail == null && !state.adminDashboardOpen) {
            val pages = ((state.board.total + state.stockFilters.pageSize - 1) / state.stockFilters.pageSize).coerceAtLeast(1)
            Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = { actions.changeStockPackingPage(state.stockPage - 1) },
                    enabled = !busy && !state.loading && !state.refreshing && !state.boardPageBusy && state.board.tasksCurrent && state.stockPage > 1,
                    modifier = Modifier.testTag("stock-page-prev")) { Text("上一页") }
                Text("第 ${state.stockPage}/$pages 页 · 每页 ${state.stockFilters.pageSize}", Modifier.weight(1f), style = MaterialTheme.typography.labelSmall)
                TextButton(onClick = { actions.changeStockPackingPage(state.stockPage + 1) },
                    enabled = !busy && !state.loading && !state.refreshing && !state.boardPageBusy && state.board.tasksCurrent && state.stockPage < pages,
                    modifier = Modifier.testTag("stock-page-next")) { Text("下一页") }
            }
        }
        if (hasSelection) {
            HorizontalDivider(color = WorkbenchColors.Line)
            if (state.view != "mine") Text("全选请切换本人全部；本岗位清单用于协作查看。",
                Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
                style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
            state.boardSelection?.takeIf { it.excludedCount > 0 }?.let { selection ->
                Text("已选可执行 ${selection.objectCount} 项；另 ${selection.excludedCount} 项受负责人或状态限制，未纳入操作。",
                    Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
                    style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
            }
            Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp).heightIn(min = 48.dp), verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = if (stock) actions.selectStockPackingPage else actions.selectAllTaskBoard,
                    enabled = canAct && state.view == "mine", contentPadding = PaddingValues(horizontal = 4.dp)) { Text(if (stock) "全选本页" else "全选") }
                Text(if (stock) "已选 $selected 卡 · ${if (state.stockSelectionCalculating) "核对订单中" else "${state.boardSelection?.orderNos?.size ?: 0} 单"}" else "已选 $selected",
                    Modifier.weight(1f), style = MaterialTheme.typography.labelSmall)
                if (packing) {
                    if (!usesPackingTaskBoard(state.selectedRole)) TextButton(onClick = { actions.submitTaskBoardBatch("start", "", "", "", "", "") }, enabled = canAct && selected > 0,
                        contentPadding = PaddingValues(horizontal = 5.dp)) { Text("开始") }
                    TextButton(onClick = { if (stock) actions.previewStockPackingPrint("") else actions.printTaskBoardSelection(null) },
                        enabled = canAct && selected > 0 && !state.stockSelectionCalculating,
                        contentPadding = PaddingValues(horizontal = 5.dp)) { Text("打印") }
                    if (!usesPackingTaskBoard(state.selectedRole)) TextButton(onClick = { packedConfirm = true }, enabled = canAct && selected > 0,
                        contentPadding = PaddingValues(horizontal = 5.dp)) { Text("已包装") }
                } else if (state.selectedRole != ROLE_SALES) TextButton(onClick = { followUpOpen = true }, enabled = canAct && selected > 0) { Text("记录跟进") }
            }
            TextButton(onClick = actions.exportTaskBoardImages, enabled = canAct && selected > 0,
                modifier = Modifier.fillMaxWidth().heightIn(min = 44.dp)) {
                Text(if (state.boardExportBusy || state.boardExport != null) "正在制作订单图片…" else "分享所选订单图片")
            }
        }
        if (state.boardPrinting) Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(if (usesPackingTaskBoard(state.selectedRole)) "打印中 · 停止后不会自动继续" else "正在顺序打印；停止后不会自动继续",
                Modifier.weight(1f), style = MaterialTheme.typography.bodySmall)
            TextButton(onClick = actions.cancelPrint) { Text("停止打印") }
        }
        if (state.selectedRole == ROLE_SALES && state.undoSalesStartGroupId.isNotBlank()) Surface(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
            shape = WorkbenchTokens.ControlShape, color = WorkbenchColors.Neutral) {
            Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).padding(start = 12.dp, end = 4.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Text("已开始处理", Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
                TextButton(onClick = actions.undoTaskBoardSalesStart, enabled = !busy) { Text("撤销") }
            }
        }
    }
    if (filterOpen) {
        if (stock) StockPackingFilterSheet(state, onLoadOptions = actions.loadPackingFilterOptions, onDismiss = { filterOpen = false }) { filters, view, dueFrom, dueTo, owner ->
            filterOpen = false
            changeQuery { selecting = false; actions.setStockPackingFilters(filters, view, dueFrom, dueTo, owner) }
        } else TaskBoardFilterSheet(state, actions, onStaff, onDismiss = { filterOpen = false })
    }
    pendingQueryChange?.let { change -> AlertDialog(onDismissRequest = { pendingQueryChange = null },
        title = { Text("清空当前选择？") }, text = { Text("改变筛选、每页数量或栏目会清空跨页选择。") },
        confirmButton = { TextButton(onClick = { pendingQueryChange = null; actions.clearTaskBoardSelection(); change() }) { Text("清空并继续") } },
        dismissButton = { TextButton(onClick = { pendingQueryChange = null }) { Text("保留选择") } }) }
    state.packingPrintPreview?.let { preview -> StockPackingPrintDialog(preview, busy, actions) }
    if (state.packingPrintRecoveryRequired) StockPackingRecoveryDialog(state, actions)
    LaunchedEffect(state.boardDetail, state.packingResetBusy) { if (state.boardDetail == null && !state.packingResetBusy) resetLine = null }
    resetLine?.let { line -> AlertDialog(onDismissRequest = { if (!state.packingResetBusy) resetLine = null },
        title = { Text("恢复为未打印？") }, text = { Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("订单 ${line.orderNo}。原打印记录保留，" + if (state.selectedRole == ROLE_ARRIVAL_PACKING) "恢复后按到货时间进入待处理或异常。" else "恢复后回到待处理。")
            (state.error ?: state.message?.takeIf { it.contains("原请求未执行") })?.let { Text(it, color = WorkbenchColors.Error, style = MaterialTheme.typography.bodySmall) }
            OutlinedTextField(resetReason, { resetReason = it }, label = { Text("恢复原因（必填）") }, enabled = !state.packingResetBusy)
        } }, confirmButton = { TextButton(onClick = {
            actions.resetStockPackingPrint(line.taskId, resetReason)
        }, enabled = resetReason.isNotBlank() && !state.packingResetBusy) { Text("确认恢复") } },
        dismissButton = { TextButton(onClick = { resetLine = null }, enabled = !state.packingResetBusy) { Text("取消") } }) }
    if (noticesOpen) AlertDialog(onDismissRequest = { noticesOpen = false }, title = { Text("任务状态") }, text = {
        Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("业务数据：${state.board.sourceUpdatedAt.ifBlank { "尚未同步" }}")
            notices.forEach { Text(it, style = MaterialTheme.typography.bodyMedium) }
            state.pendingActions.filter { it.requiresReview }.forEach { pending ->
                TextButton(onClick = { actions.discardDailyActionReview(pending.idempotencyKey) }) { Text("已核对，移除 ${pending.taskId.take(8)} 的手机记录") }
            }
            if (state.pendingActions.any { !it.requiresReview }) TextButton(onClick = actions.retryDailyActions) { Text("重试未提交记录") }
        }
    }, confirmButton = { TextButton(onClick = { noticesOpen = false; actions.refreshDailyWork() }) { Text("刷新任务") } },
        dismissButton = { TextButton(onClick = { noticesOpen = false }) { Text("关闭") } })
    if (todayOpen) TaskBoardTodayDialog(state.board, onDismiss = { todayOpen = false })
    if (noteGroupId.isNotBlank()) TaskBoardSalesNoteDialog(noteDraft, { noteDraft = it }, busy,
        error = state.actionError.takeIf { state.actionErrorId == noteGroupId },
        pending = state.pendingActions.any { it.taskId == noteGroupId && !it.requiresReview },
        onDismiss = { noteGroupId = "" },
        onSubmit = { actions.saveTaskBoardSalesNote(noteGroupId, noteDraft.trim()) })
    if (followUpOpen) TaskBoardFollowUpDialog(state.selectedRole, selected, busy,
        onDismiss = { followUpOpen = false }, onSubmit = { result, note, waiting, promised, date ->
            actions.submitTaskBoardBatch("followup", result, note, waiting, promised, date)
            followUpOpen = false
        })
    if (packedConfirm) AlertDialog(onDismissRequest = { packedConfirm = false }, title = { Text("确认已包装") },
        text = { Text("确认所选 $selected 项已按各订单数量装包并贴好标签。此操作记录已包装待发货，最终完成仍由 ERP 发货确认。") },
        confirmButton = { TextButton(onClick = {
            actions.submitTaskBoardBatch("packed", "", "", "", "", ""); packedConfirm = false
        }, enabled = canAct) { Text("确认已包装") } }, dismissButton = { TextButton(onClick = { packedConfirm = false }) { Text("返回核对") } })
}

@Composable
private fun TaskBoardItemRow(item: TaskBoardItem, selecting: Boolean, selected: Boolean, enabled: Boolean,
                             onSelect: () -> Unit, onOpen: () -> Unit, onStart: (() -> Unit)? = null,
                             pendingLabel: String = "") {
    val selectable = if (usesPackingTaskBoard(item.role)) item.canPrint else item.canAct
    Surface(color = if (item.urgent) WorkbenchColors.Tinted else WorkbenchColors.Paper) {
        Row(Modifier.fillMaxWidth().testTag("board-card-${item.id}").clickable {
            if (selecting) {
                if (selectable && enabled) onSelect()
            } else onOpen()
        }.padding(horizontal = 12.dp, vertical = 9.dp),
            verticalAlignment = Alignment.CenterVertically) {
            if (selecting) Checkbox(selected && selectable, { onSelect() }, enabled = selectable && enabled,
                modifier = Modifier.size(36.dp).padding(end = 4.dp).testTag("board-select-${item.id}"))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(when {
                        item.role == ROLE_SALES -> "${item.customerOrderNo} · ${item.title}"
                        usesPackingTaskBoard(item.role) -> "${item.sku} · ${if (item.kind == "combination") "多规格" else item.specification}"
                        else -> item.title
                    },
                        Modifier.weight(1f), style = MaterialTheme.typography.titleSmall, fontWeight = androidx.compose.ui.text.font.FontWeight.Bold,
                        maxLines = 1, overflow = TextOverflow.Ellipsis)
                    if (item.urgent) Text("急 ", color = WorkbenchColors.Red, style = MaterialTheme.typography.labelSmall)
                    if (item.role == ROLE_SALES && item.hasNote) Text("有备注 ", color = noteBlue,
                        style = MaterialTheme.typography.labelSmall, modifier = Modifier.testTag("board-note-${item.id}"))
                    Text(item.dueDate.takeLast(5).ifBlank { if (usesPackingTaskBoard(item.role)) "交期待确认" else "" }, style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                }
                Text(buildAnnotatedString {
                    if (usesPackingTaskBoard(item.role)) {
                        if (item.model.isNotBlank() && item.kind != "combination") {
                            withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { append(item.model) }
                            append(" · ")
                        }
                        append(item.productName.ifBlank { item.subtitle }.ifBlank { item.title })
                    } else append(boardItemSummary(item))
                }, style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    color = if (item.reviewCount > 0 || item.blockedBy.isNotBlank() || item.arrivalOverdue) WorkbenchColors.Error else WorkbenchColors.SecondaryInk)
                Text(if (usesPackingTaskBoard(item.role)) boardItemSummary(item) + " · " + item.workLabel else boardItemContext(item), style = MaterialTheme.typography.labelSmall, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    color = WorkbenchColors.Muted)
                
                if (usesPackingTaskBoard(item.role) && item.printState in setOf("printed", "uncertain")) Text(
                    packingPrintRecord(item.latestPrintActorName, item.latestPrintAt), maxLines = 1, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.labelSmall,
                    color = WorkbenchColors.Muted)
                if (pendingLabel.isNotBlank()) Text(pendingLabel, style = MaterialTheme.typography.labelSmall,
                    color = if (pendingLabel.contains("核对")) WorkbenchColors.Error else WorkbenchColors.Muted)
                if (item.role != ROLE_SALES && !usesPackingTaskBoard(item.role) && item.customerSalesOwners.isNotEmpty()) Text(
                    item.customerSalesOwners.joinToString(" · ") { "${it.customer}：${if (it.reviewRequired) "销售待核对" else it.ownerName.ifBlank { "销售待指定" }}" },
                    style = MaterialTheme.typography.labelSmall, maxLines = 1, overflow = TextOverflow.Ellipsis, color = WorkbenchColors.Muted)
            }
            if (!selecting && onStart != null) TextButton(onClick = onStart, enabled = item.canAct && enabled && pendingLabel.isBlank(),
                contentPadding = PaddingValues(horizontal = 8.dp), modifier = Modifier.testTag("board-start-${item.id}")) { Text("开始") }
            else if (!selecting) Icon(WorkbenchIcons.ChevronRight, "查看明细", Modifier.padding(start = 4.dp).size(18.dp), tint = WorkbenchColors.Muted)
        }
    }
    HorizontalDivider(color = WorkbenchColors.Line, thickness = 0.5.dp)
}

internal fun boardItemSummary(item: TaskBoardItem): String = when (item.role) {
    ROLE_SALES -> listOf("齐货 ${item.readyCount}/${item.totalCount} 行", "缺 ${item.missingCount} 行", "已包装 ${item.packedCount} 行",
        if (item.reviewCount > 0) "待核对 ${item.reviewCount}" else "").filter(String::isNotBlank).joinToString(" · ")
    ROLE_PURCHASE -> "需采购 ${item.shortage.ifBlank { "0" }}${item.unit} · 在途 ${item.pending.ifBlank { "0" }}${item.unit} · 影响 ${item.customerOrderImpact} 单"
    else -> listOf(item.location.ifBlank { "货位待核对" },
        if (item.arrivalPartial) "已到 ${item.arrivalReceived}／需求 ${item.arrivalRequired}／缺 ${item.arrivalShortage}" else if (item.kind == "combination") "多商品分别装包" else "${item.quantity.ifBlank { "0" }}${item.unit}",
        "${item.orderCount} 条订单").joinToString(" · ")
}

@Composable
private fun AdminDashboardContent(state: DailyWorkState, actions: WorkbenchActions) {
    val dashboard = state.adminDashboard
    LazyColumn(Modifier.fillMaxSize().testTag("admin-dashboard"),
        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 10.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)) {
        state.error?.let { error -> item("dashboard-error") { BoardInlineMessage(error, true) } }
        if (dashboard == null) {
            item("dashboard-loading") {
                Text(if (state.adminDashboardBusy) "正在读取管理看板…" else "管理看板暂不可用",
                    color = WorkbenchColors.Muted)
            }
        } else {
            item("company") {
                WorkbenchCard {
                    Text("公司概览", style = MaterialTheme.typography.titleMedium)
                    Text("客户整单 ${dashboard.customerOrders} · 未完成 ${dashboard.unfinishedCustomerOrders}",
                        style = MaterialTheme.typography.bodyMedium)
                    Text("待出库 ${dashboard.readyToShip} · 待核对 ${dashboard.needsReview}",
                        style = MaterialTheme.typography.bodyMedium)
                    Text("ERP 数据 ${dashboard.sourceUpdatedAt.ifBlank { "尚未同步" }} · 看板 ${dashboard.refreshedAt.ifBlank { "尚未刷新" }}",
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    Text(purchaseSnapshotLabel(dashboard), style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    Text(historyCoverageLabel(dashboard.historyCoverage),
                        style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                }
            }
            items(dashboard.roles, key = AdminDashboardRole::role) { role ->
                WorkbenchCard {
                    Text(dailyRoleLabel(role.role), style = MaterialTheme.typography.titleMedium)
                    Text("待办 ${role.todo} · 处理中 ${role.inProgress} · 等待 ${role.waiting} · 逾期 ${role.overdue}",
                        style = MaterialTheme.typography.bodyMedium)
                    Text("本岗位 ${role.total} 项 · 未分配 ${role.unassigned}",
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    TextButton(onClick = { actions.openDashboardRole(role.role, "") }) { Text("查看岗位任务") }
                    TextButton(onClick = { actions.openDashboardCompletions(role.role, "", "erp") }) {
                        Text("ERP 今日业务完成 ${role.businessCompletedToday} 项")
                    }
                    role.people.forEach { person ->
                        HorizontalDivider(color = WorkbenchColors.Line)
                        Column(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                            TextButton(onClick = { actions.openDashboardRole(role.role, person.userId) }) {
                                Text(person.displayName, style = MaterialTheme.typography.bodyMedium)
                            }
                            Text("待办 ${person.todo} · 处理中 ${person.inProgress} · 等待 ${person.waiting} · 逾期 ${person.overdue}",
                                style = MaterialTheme.typography.bodySmall)
                            TextButton(onClick = { actions.openDashboardCompletions(role.role, person.userId, "human") }) {
                                Text("今日完成 ${person.completedToday} 项 · 查看处理记录")
                            }
                            Text("最近动作 ${person.lastActionAt.ifBlank { "暂无记录" }}",
                                style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                        }
                    }
                }
            }
        }
    }
    if (state.dashboardCompletionsOpen) DashboardCompletionDialog(state, actions)
}

internal fun purchaseSnapshotLabel(dashboard: AdminDashboard): String {
    val status = when (dashboard.purchaseStatus) {
        "ready", "validated" -> "可用 ${dashboard.purchaseRows} 行"
        "review" -> "可查看 ${dashboard.purchaseRows} 行，单位或范围待核对"
        "invalid", "stale" -> "校验失败或已过期"
        else -> "尚不可用"
    }
    return "采购快照 $status · 最近成功 ${dashboard.purchaseSourceUpdatedAt.ifBlank { "尚无" }}"
}

internal fun historyCoverageLabel(status: String): String = when (status) {
    "current_snapshot_only" -> "整单统计仅覆盖当前业务快照，已记账发货历史尚未补齐。"
    "partial_archive" -> "已收录部分发货历史；整单统计仍按当前快照，历史覆盖尚未完整。"
    else -> "历史覆盖范围待核对，当前数字不代表完整历史总量。"
}

@Composable
private fun DashboardCompletionDialog(state: DailyWorkState, actions: WorkbenchActions) {
    val result = state.dashboardCompletions
    AlertDialog(onDismissRequest = actions.closeDashboardCompletions,
        title = { Text(if (state.dashboardCompletionsSource == "erp") "ERP 今日业务完成" else "今日人工完成记录") },
        text = { Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("${dailyRoleLabel(state.dashboardCompletionsRole)} · ${result?.total ?: 0} 项")
            state.dashboardCompletionsError?.let { Text(it, color = WorkbenchColors.Error) }
            if (state.dashboardCompletionsBusy) LinearProgressIndicator(Modifier.fillMaxWidth())
            LazyColumn(Modifier.heightIn(max = 420.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(result?.items.orEmpty()) { entry ->
                    Column {
                        Text(entry.title, style = MaterialTheme.typography.titleSmall)
                        if (entry.orderNo.isNotBlank()) Text(entry.orderNo, style = MaterialTheme.typography.bodySmall)
                        Text("${entry.createdAt} · ${if (entry.source == "erp") "ERP 业务完成" else "人员完成"}", style = MaterialTheme.typography.labelSmall)
                        if (entry.note.isNotBlank()) Text(entry.note, style = MaterialTheme.typography.bodySmall)
                        HorizontalDivider()
                    }
                }
                if (result?.hasMore == true || state.dashboardCompletionsError != null) item {
                    TextButton(onClick = actions.loadMoreDashboardCompletions, enabled = !state.dashboardCompletionsBusy) { Text("加载更多／重试") }
                }
            }
        } }, confirmButton = { TextButton(onClick = actions.closeDashboardCompletions) { Text("关闭") } })
}

@Composable
private fun TaskHistoryContent(state: DailyWorkState, actions: WorkbenchActions) {
    val detail = state.taskHistoryDetail
    LazyColumn(Modifier.fillMaxSize().testTag("task-history-list"),
        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 10.dp),
        verticalArrangement = Arrangement.spacedBy(9.dp)) {
        state.error?.let { error -> item("history-error") { BoardInlineMessage(error, true) } }
        if (detail != null) {
            item("history-detail") {
                WorkbenchCard {
                    Text(detail.item.title, style = MaterialTheme.typography.titleMedium)
                    Text(listOf(detail.item.customer, detail.item.orderNos.joinToString("、"), detail.item.sku)
                        .filter(String::isNotBlank).joinToString(" · "), style = MaterialTheme.typography.bodyMedium)
                    Text("状态 ${detail.item.status} · 负责人 ${detail.item.assigneeName.ifBlank { "待指定" }}",
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    if (detail.item.humanCompletedAt.isNotBlank()) Text("处理 ${detail.item.humanCompletedAt}",
                        style = MaterialTheme.typography.bodySmall)
                    if (detail.item.businessCompletedAt.isNotBlank()) Text("业务完成 ${detail.item.businessCompletedAt}",
                        style = MaterialTheme.typography.bodySmall)
                    if (detail.item.followUpDate.isNotBlank()) Text("下次跟进 ${detail.item.followUpDate}",
                        style = MaterialTheme.typography.bodySmall)
                    if (detail.item.reason.isNotBlank()) Text(detail.item.reason,
                        style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    detail.item.orderNos.firstOrNull()?.let { number ->
                        TextButton(onClick = { actions.openDailyOrder(number) }) { Text("查看订单 $number") }
                    }
                }
            }
            item("history-events-heading") { Text("处理记录", style = MaterialTheme.typography.titleSmall) }
            if (detail.events.isEmpty()) item("history-events-empty") {
                Text("暂无处理记录", color = WorkbenchColors.Muted)
            }
            items(detail.events, key = { "event-${it.id}" }) { BoardEventRow(it) }
            if (detail.hasMore) item("history-more") {
                QuietAction("加载更多处理记录", actions.loadMoreTaskHistory, Modifier.fillMaxWidth(),
                    enabled = !state.taskHistoryPageBusy)
            }
        } else {
            item("history-source") {
                Text(if (state.boardSection == "processed") "已处理事项包含仍在进行的跟进记录。" else
                    "历史事项来自本地任务归档。", style = MaterialTheme.typography.bodySmall,
                    color = WorkbenchColors.Muted)
            }
            if (state.taskHistory.items.isEmpty() && !state.taskHistoryBusy) item("history-empty") {
                Text("暂无${if (state.boardSection == "processed") "已处理" else "历史"}事项",
                    color = WorkbenchColors.Muted)
            }
            items(state.taskHistory.items, key = DailyWorkItem::id) { item ->
                Surface(shape = WorkbenchTokens.ControlShape, color = WorkbenchColors.Paper,
                    border = BorderStroke(1.dp, WorkbenchColors.Line)) {
                    Column(Modifier.fillMaxWidth().clickable { actions.openTaskHistoryItem(item.id) }
                        .padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(item.title, style = MaterialTheme.typography.titleSmall)
                        Text(listOf(item.customer, item.orderNos.joinToString("、"), item.sku)
                            .filter(String::isNotBlank).joinToString(" · "), style = MaterialTheme.typography.bodySmall)
                        Text("${item.assigneeName.ifBlank { "待指定" }} · ${item.status} · ${item.humanCompletedAt.ifBlank { item.businessCompletedAt }.ifBlank { item.followUpDate }}",
                            style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                    }
                }
            }
            if (state.taskHistory.hasMore) item("history-more") {
                QuietAction("继续加载 · 已显示 ${state.taskHistory.items.size}/${state.taskHistory.total} 项",
                    actions.loadMoreTaskHistory, Modifier.fillMaxWidth(), enabled = !state.taskHistoryPageBusy)
            }
        }
    }
}

private fun boardItemContext(item: TaskBoardItem): String {
    val owner = item.assigneeName.ifBlank { item.ownerNames.joinToString("、") }.ifBlank { "待安排" }
    if (item.blockedBy.isNotBlank()) return item.blockedBy
    if (item.role == ROLE_SALES) return listOf(
        item.missingItems.take(2).joinToString("、") {
            "${it.sku.ifBlank { it.productId }} ${if (it.shortage.isNotBlank() && it.shortage != "0") "缺${it.shortage}" else "待到${it.pending}"}"
        }, "固定销售 $owner"
    ).filter(String::isNotBlank).joinToString(" · ")
    return listOf(
        if (item.role == ROLE_SALES) item.missingItems.take(2).joinToString("、") {
            "${it.sku.ifBlank { it.productId }} ${if (it.shortage.isNotBlank() && it.shortage != "0") "缺${it.shortage}" else "待到${it.pending}"}"
        } else "",
        when {
            item.waitingFor.isNotBlank() -> "等待 ${item.waitingFor}"
            item.role == ROLE_PURCHASE && item.nearCompleteCustomerOrders > 0 -> "${item.nearCompleteCustomerOrders} 单只差此商品"
            else -> if (item.role == ROLE_SALES) "" else listOf(item.subtitle, item.specification)
                .filter(String::isNotBlank).distinct().joinToString(" · ")
        }, if (item.promisedDate.isNotBlank()) "承诺 ${item.promisedDate}" else "",
        if (item.nextFollowUpDate.isNotBlank()) "跟进 ${item.nextFollowUpDate}" else "",
        if (item.role == ROLE_SALES) "固定销售 $owner" else "执行人 $owner"
    ).filter(String::isNotBlank).joinToString(" · ")
}

@Composable
private fun TaskBoardLineRow(line: TaskBoardLine, selecting: Boolean, selected: Boolean, enabled: Boolean,
                             onSelect: () -> Unit, onOrder: () -> Unit, onManage: (() -> Unit)?, simplePrinting: Boolean = false,
                             onReset: (() -> Unit)? = null) {
    val dangerousPrint = line.printState in setOf("printing", "reserved") || (!simplePrinting && line.printState == "uncertain")
    Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 9.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (selecting) Checkbox(selected && (if (simplePrinting) line.canPrint else line.canAct), { onSelect() },
                enabled = (if (simplePrinting) line.canPrint else line.canAct) && enabled && !dangerousPrint,
                modifier = Modifier.size(36.dp).padding(end = 4.dp).testTag("board-line-select-${line.id}"))
            Text(line.orderNo, Modifier.weight(1f), style = MaterialTheme.typography.titleSmall)
            TextButton(onClick = onOrder, contentPadding = PaddingValues(horizontal = 4.dp)) { Text("订单") }
            onManage?.let { TextButton(onClick = it, contentPadding = PaddingValues(horizontal = 4.dp)) { Text("管理") } }
        }
        if (line.productLines.size > 1) {
            Text(listOf(line.customer, line.location, "组合包装：以下商品共同完成此单").filter(String::isNotBlank).joinToString(" · "),
                style = MaterialTheme.typography.bodyMedium)
            line.productLines.forEach { product ->
                Text("${product.sku} · ${product.quantity}${product.unit}", style = MaterialTheme.typography.bodyMedium)
                if (product.unitNote.isNotBlank()) Text(product.unitNote,
                    style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                if (product.summary.isNotBlank()) Text("摘要：${product.summary}", style = MaterialTheme.typography.bodySmall)
            }
        } else {
            Text(listOf(line.customer, line.sku, "${line.quantity}${line.unit}", line.location).filter(String::isNotBlank).joinToString(" · "),
                style = MaterialTheme.typography.bodyMedium)
            if (line.summary.isNotBlank()) Text("摘要：${line.summary}", style = MaterialTheme.typography.bodySmall)
            if (line.unitNote.isNotBlank()) Text(line.unitNote,
                style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
        }
        Text(listOf(line.fulfillmentLabel.ifBlank { boardStatusLabel(line.status) }, boardPrintLabel(line.printState, simplePrinting), line.assigneeName.ifBlank { "待安排" },
            if (line.dueDate.isNotBlank()) "交期 ${line.dueDate}" else "").filter(String::isNotBlank).joinToString(" · "),
            style = MaterialTheme.typography.bodySmall, color = if (dangerousPrint) WorkbenchColors.Error else WorkbenchColors.Muted,
            fontWeight = if (dangerousPrint) FontWeight.Bold else FontWeight.Normal)
        line.arrivalSources.forEach { Text(it, style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted) }
        val blocked = if (simplePrinting) line.printBlockedBy else line.blockedBy
        if (blocked.isNotBlank()) Text(blocked, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error)
        if (simplePrinting && line.printState == "reserved" && line.currentPrintActorName.isNotBlank()) Text("正在打印：${line.currentPrintActorName}；请等待该打印完成",
            style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Error)
        if (simplePrinting && line.printState in setOf("printed", "uncertain")) Text(packingPrintRecord(line.latestPrintActorName, line.latestPrintAt),
            style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
        onReset?.let { TextButton(onClick = it, enabled = enabled) { Text("恢复未打印") } }
    }
    HorizontalDivider(color = WorkbenchColors.Line, thickness = 0.5.dp)
}

private fun boardNotices(state: DailyWorkState): List<String> = buildList {
    if (!state.board.tasksCurrent) add("任务未跟上业务数据，处理、转交和打印已暂停。请刷新任务。")
    if (state.snapshot.stale) add("业务数据不是今天同步；现有事项保留显示，新的包装安排等待更新订单。")
    if (state.snapshot.assignmentEnabled == false) add("自动派工尚未启用；管理员可在人员管理中启用。")
    state.error?.let(::add)
    state.message?.let(::add)
    state.pendingActions.filter { it.requiresReview }.forEach { add("${it.taskId.take(8)} 待核对：${it.lastError}") }
    val waiting = state.pendingActions.count { !it.requiresReview }
    if (waiting > 0) add("$waiting 条处理记录保存在手机，恢复网络后幂等提交。")
}

private fun compactBoardTime(value: String): String = when {
    value.isBlank() -> "尚未同步"
    value.length >= 16 -> value.substring(5, 16).replace('T', ' ')
    else -> value
}

private fun boardStatusLabel(value: String) = when (value) {
    "todo" -> "待处理"; "in_progress" -> "处理中"; "waiting" -> "等待中"; "packed" -> "已包装待发货"
    "review" -> "待核对"; "resolved" -> "已提交结果"; "ready" -> "实物已齐"; "arrival" -> "待到货"
    "procurement" -> "需采购"; "shipped_unposted" -> "已发货／包装完成（ERP）"; "waiting_outbound" -> "待出库"; else -> value
}

private fun boardPrintLabel(value: String, simplified: Boolean) = when (value) {
    "uncertain" -> if (simplified) "打印待核对，确认后可重打" else "出纸待核对，禁止重印"
    "reserved", "printing" -> if (simplified) "打印中" else "打印已锁定"; "printed" -> "已打印"
    "unprinted" -> "未打印"; else -> value
}

@Composable
private fun BoardInlineMessage(text: String, error: Boolean = false, retry: (() -> Unit)? = null) {
    Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(text, Modifier.weight(1f), style = MaterialTheme.typography.bodySmall,
            color = if (error) WorkbenchColors.Error else WorkbenchColors.SecondaryInk)
        retry?.let { TextButton(onClick = it) { Text("重试") } }
    }
}

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
private fun TaskBoardFilterSheet(state: DailyWorkState, actions: WorkbenchActions, onStaff: () -> Unit, onDismiss: () -> Unit) {
    var view by rememberSaveable { mutableStateOf(state.view) }
    var status by rememberSaveable { mutableStateOf(state.boardStatus) }
    var dueFrom by rememberSaveable { mutableStateOf(state.boardDueFrom) }
    var dueTo by rememberSaveable { mutableStateOf(state.boardDueTo) }
    var owner by rememberSaveable { mutableStateOf(state.boardAssigneeId) }
    var dateField by remember { mutableStateOf<String?>(null) }
    val options = when (state.selectedRole) {
        ROLE_SALES -> listOf("all" to "全部", "shortage" to "待补齐", "packing" to "待包装", "ready_to_ship" to "待发货", "review" to "待核对")
        ROLE_STOCK_PACKING -> listOf("all" to "全部", "review" to "待核对")
        ROLE_PURCHASE -> listOf("all" to "全部", "procurement" to "需采购", "arrival" to "待到货", "review" to "待核对", "waiting" to "等待")
        else -> listOf("all" to "全部", "todo" to "待包装", "in_progress" to "包装中", "packed" to "已包装", "review" to "待核对", "waiting" to "等待")
    }
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = WorkbenchColors.Paper) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 18.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("筛选工作清单", style = MaterialTheme.typography.titleMedium)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("mine" to "本人全部", "role" to "本岗位全部").forEach { (value, label) ->
                    FilterChip(view == value, { view = value; if (value == "mine") owner = "" }, label = { Text(label) })
                }
            }
            Text("状态", style = MaterialTheme.typography.labelLarge)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                options.forEach { (value, label) -> FilterChip(status == value, { status = value }, label = { Text(label) }) }
            }
            Text("交期范围（含起止日期）", style = MaterialTheme.typography.labelLarge)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { dateField = "from" }, modifier = Modifier.weight(1f)) { Text(dueFrom.ifBlank { "开始日期" }) }
                OutlinedButton(onClick = { dateField = "to" }, modifier = Modifier.weight(1f)) { Text(dueTo.ifBlank { "结束日期" }) }
            }
            if (view == "role") {
                Text("负责人", style = MaterialTheme.typography.labelLarge)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    FilterChip(owner.isBlank(), { owner = "" }, label = { Text("全部人员") })
                    state.board.assignees.forEach { person -> FilterChip(owner == person.id, { owner = person.id }, label = { Text(person.displayName) }) }
                }
            }
            if (dueFrom.isNotBlank() && dueTo.isNotBlank() && dueFrom > dueTo)
                Text("结束日期不能早于开始日期", color = WorkbenchColors.Error, style = MaterialTheme.typography.bodySmall)
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = { view = "mine"; status = "all"; dueFrom = ""; dueTo = ""; owner = "" }) { Text("重置") }
                Spacer(Modifier.weight(1f))
                Button(onClick = {
                    actions.setTaskBoardFilters(status, view, dueFrom, dueTo, owner); onDismiss()
                }, enabled = dueFrom.isBlank() || dueTo.isBlank() || dueFrom <= dueTo) { Text("应用筛选") }
            }
            if (state.me?.isAdmin == true) TextButton(onClick = { onDismiss(); onStaff() }) { Text("人员管理") }
            Spacer(Modifier.height(16.dp))
        }
    }
    dateField?.let { which -> BoardDateDialog(if (which == "from") dueFrom else dueTo, onDismiss = { dateField = null }) {
        if (which == "from") dueFrom = it else dueTo = it
        dateField = null
    } }
}

@Composable
private fun TaskBoardSalesNoteDialog(note: String, onChange: (String) -> Unit, busy: Boolean,
                                    error: String?, pending: Boolean, onDismiss: () -> Unit, onSubmit: () -> Unit) {
    val maxHeight = (LocalConfiguration.current.screenHeightDp * 0.85f).dp
    Dialog(onDismissRequest = { if (!busy) onDismiss() }, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxWidth().padding(horizontal = 20.dp).heightIn(max = maxHeight).testTag("sales-note-dialog"),
            shape = WorkbenchTokens.CardShape, color = WorkbenchColors.Paper) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("备注", style = MaterialTheme.typography.titleMedium)
                Column(Modifier.weight(1f, fill = false).verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(note, onChange, Modifier.fillMaxWidth().testTag("sales-note-input"),
                        label = { Text("备注（选填）") }, minLines = 3, maxLines = 7, enabled = !busy)
                    if (pending) Text("待提交 · 备注已保存在手机", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    error?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Error) }
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(onClick = onDismiss, enabled = !busy) { Text("关闭") }
                    TextButton(onClick = onSubmit, enabled = !busy && !pending && note.isNotBlank()) { Text("保存备注") }
                }
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun TaskBoardFollowUpDialog(role: String, count: Int, busy: Boolean, onDismiss: () -> Unit,
                                    onSubmit: (String, String, String, String, String) -> Unit) {
    val choices = if (role == ROLE_PURCHASE) listOf("已安排采购", "已催到货", "已确认到货时间", "等待协同", "需核对", "其他原因")
        else listOf("已协调交期", "已反馈客户", "已催补齐", "等待协同", "需核对", "其他原因")
    var result by rememberSaveable { mutableStateOf(choices.first()) }
    var note by rememberSaveable { mutableStateOf("") }
    var waiting by rememberSaveable { mutableStateOf("") }
    var promised by rememberSaveable { mutableStateOf("") }
    var next by rememberSaveable { mutableStateOf(nextBoardWorkday()) }
    var dateField by remember { mutableStateOf<String?>(null) }
    val noteRequired = result in setOf("需核对", "其他原因")
    val maxHeight = (LocalConfiguration.current.screenHeightDp * 0.85f).dp
    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxWidth().padding(horizontal = 20.dp).heightIn(max = maxHeight),
            shape = WorkbenchTokens.CardShape, color = WorkbenchColors.Paper) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("批量跟进 · $count 项", style = MaterialTheme.typography.titleMedium)
                Column(Modifier.weight(1f, fill = false).verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        choices.forEach { value ->
                            val selected = result == value
                            Surface(shape = WorkbenchTokens.ControlShape,
                                color = if (selected) WorkbenchColors.Tinted else WorkbenchColors.Paper,
                                border = BorderStroke(1.dp, if (selected) WorkbenchColors.Red else WorkbenchColors.Line),
                                modifier = Modifier.padding(vertical = 4.dp).selectable(selected = selected,
                                    interactionSource = remember(value) { MutableInteractionSource() }, indication = null,
                                    role = Role.RadioButton, enabled = !busy, onClick = { result = value })) {
                                Text(value, Modifier.padding(horizontal = 10.dp, vertical = 8.dp),
                                    color = if (selected) WorkbenchColors.Red else WorkbenchColors.Ink,
                                    style = MaterialTheme.typography.labelLarge)
                            }
                        }
                    }
                    OutlinedTextField(waiting, { waiting = it }, Modifier.fillMaxWidth(), label = { Text("等待对象（选填）") }, singleLine = true)
                    OutlinedButton(onClick = { dateField = "promised" }, modifier = Modifier.fillMaxWidth()) { Text("到货承诺：${promised.ifBlank { "未填写" }}") }
                    OutlinedButton(onClick = { dateField = "next" }, modifier = Modifier.fillMaxWidth()) { Text("下次跟进：$next") }
                    OutlinedTextField(note, { note = it }, Modifier.fillMaxWidth(), label = { Text(if (noteRequired) "简短说明（必填）" else "备注（选填）") })
                    Text("记录处理后等待业务数据确认解决。", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(onClick = onDismiss, enabled = !busy) { Text("取消") }
                    TextButton(onClick = { onSubmit(result, note.trim(), waiting.trim(), promised, next) },
                        enabled = !busy && (!noteRequired || note.isNotBlank()) && next.isNotBlank()) { Text("保存记录") }
                }
            }
        }
    }
    dateField?.let { field -> BoardDateDialog(if (field == "next") next else promised, onDismiss = { dateField = null }) {
        if (field == "next") next = it else promised = it
        dateField = null
    } }
}

private fun nextBoardWorkday(): String {
    var day = LocalDate.now(boardZone).plusDays(1)
    while (day.dayOfWeek.value >= 6) day = day.plusDays(1)
    return day.toString()
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun BoardDateDialog(value: String, onDismiss: () -> Unit, onChoose: (String) -> Unit) {
    val millis = runCatching { LocalDate.parse(value).atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli() }.getOrNull()
    val picker = rememberDatePickerState(initialSelectedDateMillis = millis)
    DatePickerDialog(onDismissRequest = onDismiss, confirmButton = { TextButton(onClick = {
        picker.selectedDateMillis?.let { onChoose(Instant.ofEpochMilli(it).atZone(ZoneOffset.UTC).toLocalDate().toString()) }
    }, enabled = picker.selectedDateMillis != null) { Text("确定") } }, dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } }) {
        DatePicker(picker)
    }
}

@Composable
private fun TaskBoardTodayDialog(page: TaskBoardPage, onDismiss: () -> Unit) {
    val eventGroups = listOf("跟进动作" to page.todayEvents.filter { it.eventType !in setOf("packed", "business_completed") },
        "包装完成" to page.todayEvents.filter { it.eventType == "packed" },
        "ERP 确认完成" to page.todayEvents.filter { it.eventType == "business_completed" })
    AlertDialog(onDismissRequest = onDismiss, title = { Text("今日记录") }, text = {
        LazyColumn(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            eventGroups.forEach { (title, events) ->
                item("title-$title") { Text("$title ${events.size}", style = MaterialTheme.typography.titleSmall) }
                if (events.isEmpty()) item("empty-$title") { Text("暂无记录", color = WorkbenchColors.Muted, style = MaterialTheme.typography.bodySmall) }
                items(events, key = { "$title-${it.id}" }) { BoardEventRow(it) }
            }
        }
    }, confirmButton = { TextButton(onClick = onDismiss) { Text("关闭") } })
}

@Composable
private fun BoardEventRow(event: DailyWorkEvent) {
    Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 7.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
        Text(event.title, style = MaterialTheme.typography.bodyMedium)
        if (event.note.isNotBlank()) Text(event.note, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.SecondaryInk)
        Text(event.createdAt, style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
    }
}
