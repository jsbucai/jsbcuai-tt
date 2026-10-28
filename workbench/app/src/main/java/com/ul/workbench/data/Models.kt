package com.ul.workbench.data

enum class Destination { Tasks, Orders, Inventory, Devices }

const val ROLE_SALES = "sales"
const val ROLE_PURCHASE = "purchase"
const val ROLE_STOCK_PACKING = "stock_packing"
const val ROLE_ARRIVAL_PACKING = "arrival_packing"

val DAILY_WORK_ROLES = listOf(ROLE_SALES, ROLE_PURCHASE, ROLE_STOCK_PACKING, ROLE_ARRIVAL_PACKING)

fun usesPackingTaskBoard(role: String) = role in setOf(ROLE_STOCK_PACKING, ROLE_ARRIVAL_PACKING)
fun usesTwoStageTaskBoard(role: String) = role == ROLE_SALES || usesPackingTaskBoard(role)
fun defaultTaskBoardSection(role: String) = if (usesTwoStageTaskBoard(role)) "todo" else "active"
fun isTaskBoardHistorySection(section: String) = section in setOf("processed", "archived")

fun dailyRoleLabel(role: String): String = when (role) {
    ROLE_SALES -> "销售"
    ROLE_PURCHASE -> "采购"
    ROLE_STOCK_PACKING -> "库存件包装"
    ROLE_ARRIVAL_PACKING -> "来货件包装"
    else -> role.ifBlank { "未分配岗位" }
}

data class CurrentUser(
    val id: String = "", val username: String = "", val displayName: String = "",
    val roles: List<String> = emptyList(), val isAdmin: Boolean = false,
    val active: Boolean = true, val packingLimit: Int = 3
)

data class DailyWorkSummary(
    val todo: Int = 0, val inProgress: Int = 0, val waiting: Int = 0,
    val todayActions: Int = 0, val businessCompleted: Int = 0,
    val unassigned: Int = 0, val overdue: Int = 0, val packedWaiting: Int = 0
)

data class DailyWorkItem(
    val id: String, val version: Int, val role: String, val title: String,
    val reason: String = "", val nextAction: String = "", val status: String = "todo",
    val priority: Int = 0, val dueDate: String = "", val followUpDate: String = "",
    val assigneeId: String = "", val assigneeName: String = "", val orderNos: List<String> = emptyList(),
    val sku: String = "", val customer: String = "", val quantity: String = "", val location: String = "",
    val logisticsCompany: String = "", val printState: String = "", val packingState: String = "",
    val blockedBy: String = "", val urgent: Boolean = false,
    val humanCompletedAt: String = "", val businessCompletedAt: String = "", val profileId: String = "",
    val readOnly: Boolean = false, val taskType: String = "",
    val missingItems: List<DailyMissingItem> = emptyList(), val linkedPurchaseBusinessKey: String = "",
    val customerOrderImpact: Int = 0, val nearCompleteCustomerOrders: Int = 0
)

data class DailyMissingItem(
    val sku: String = "", val productId: String = "", val shortage: String = "", val pending: String = "",
    val orderNo: String = ""
)

data class CustomerOrderGroup(
    val id: String, val customerOrderNo: String, val customer: String = "",
    val assigneeId: String = "", val assigneeName: String = "", val followUpVersion: Int = 0,
    val nextFollowUpDate: String = "", val dueDateFrom: String = "", val dueDateTo: String = "",
    val orderCount: Int = 0, val unshippedCount: Int = 0, val readyCount: Int = 0,
    val packedCount: Int = 0, val procurementCount: Int = 0, val arrivalCount: Int = 0,
    val reviewCount: Int = 0, val missingCount: Int = 0, val missingProductCount: Int = 0,
    val myPackingCount: Int = 0, val purchaseTaskCount: Int = 0,
    val readyToShip: Boolean = false, val customerReview: Boolean = false,
    val ownerReview: Boolean = false, val urgent: Boolean = false, val linesChanged: Boolean = false,
    val status: String = "", val missingPreview: List<DailyMissingItem> = emptyList(),
    val profileId: String = "", val fixedOwnerId: String = "", val deputyId: String = "",
    val deputyName: String = "", val deputyUntil: String = "", val customerOwnerVersion: Int = 0
)

data class CustomerOwner(
    val profileId: String, val customer: String, val ownerId: String = "", val ownerName: String = "",
    val deputyId: String = "", val deputyName: String = "", val deputyUntil: String = "",
    val status: String = "unassigned", val source: String = "", val version: Int = 0,
    val reviewReasons: List<String> = emptyList(), val activeTaskCount: Int = 0,
    val activeFollowupCount: Int = 0, val canSetOwner: Boolean = false, val canSetDeputy: Boolean = false,
    val identityConflicts: List<String> = emptyList()
) {
    val key get() = "$profileId:$customer"
}

data class CustomerOwnerDraft(val profileId: String, val customer: String, val version: Int,
    val personId: String, val reason: String, val deputy: Boolean = false, val deputyUntil: String = "")

data class CustomerOrderLine(
    val orderNo: String, val status: String, val packed: Boolean,
    val printState: String, val dueDate: String,
    val taskIds: List<String> = emptyList(), val skus: List<String> = emptyList(),
    val fulfillmentLabel: String = "", val packingBasis: String = ""
)

data class CustomerOrderFollowup(val note: String, val waitingFor: String, val nextFollowUpDate: String, val createdAt: String)

data class CustomerOrderDetail(val group: CustomerOrderGroup, val lines: List<CustomerOrderLine>,
    val missingItems: List<DailyMissingItem>, val purchaseTasks: List<DailyWorkItem>,
    val followups: List<CustomerOrderFollowup>, val tasksCurrent: Boolean)

data class CustomerOrderPage(val items: List<CustomerOrderGroup> = emptyList(),
    val total: Int = 0, val page: Int = 1, val hasMore: Boolean = false,
    val dataVersion: String = "", val taskVersion: String = "", val tasksCurrent: Boolean = true,
    val counts: Map<String, Int> = emptyMap())

data class TaskBoardItem(
    val id: String, val kind: String, val role: String, val title: String,
    val subtitle: String = "", val sku: String = "", val productName: String = "",
    val specification: String = "", val unit: String = "", val location: String = "",
    val quantity: String = "", val shortage: String = "", val pending: String = "",
    val orderCount: Int = 0, val customerOrderNo: String = "", val customer: String = "",
    val dueDate: String = "", val status: String = "", val blockedBy: String = "",
    val assigneeId: String = "", val assigneeName: String = "", val ownerNames: List<String> = emptyList(),
    val canAct: Boolean = false, val urgent: Boolean = false,
    val readyCount: Int = 0, val missingCount: Int = 0, val reviewCount: Int = 0,
    val packedCount: Int = 0, val totalCount: Int = 0, val customerOrderImpact: Int = 0,
    val nearCompleteCustomerOrders: Int = 0, val waitingFor: String = "", val promisedDate: String = "",
    val nextFollowUpDate: String = "", val version: Int = 0, val missingItems: List<DailyMissingItem> = emptyList(),
    val profileId: String = "", val unitNote: String = "",
    val customerSalesOwners: List<CustomerSalesOwner> = emptyList(), val businessStatus: String = "",
    val workState: String = "", val hasNote: Boolean = false, val latestNote: String = "",
    val printState: String = "", val workLabel: String = "",
    val latestPrintActorName: String = "", val latestPrintAt: String = "", val historicalPrint: Boolean = false,
    val canPrint: Boolean = false, val printBlockedBy: String = "",
    val arrivalSources: List<String> = emptyList(), val model: String = "", val arrivalReceived: String = "", val arrivalRequired: String = "",
    val arrivalShortage: String = "", val arrivalPartial: Boolean = false, val arrivalOverdue: Boolean = false
)

data class CustomerSalesOwner(val customer: String, val ownerId: String, val ownerName: String,
    val reviewRequired: Boolean)

data class TaskBoardProductLine(val recordId: String, val sku: String, val quantity: String, val unit: String, val summary: String, val unitNote: String = "")

data class TaskBoardLine(
    val id: String, val taskId: String, val version: Int, val orderNo: String,
    val sku: String = "", val productName: String = "", val quantity: String = "", val unit: String = "",
    val location: String = "", val summary: String = "", val status: String = "",
    val printState: String = "", val assigneeId: String = "", val assigneeName: String = "",
    val canAct: Boolean = false, val blockedBy: String = "", val customer: String = "", val dueDate: String = "",
    val productLines: List<TaskBoardProductLine> = emptyList(), val unitNote: String = "",
    val fulfillmentLabel: String = "", val packingBasis: String = "",
    val stateRevision: String = "", val labelFingerprint: String = "",
    val latestPrintActorName: String = "", val latestPrintAt: String = "", val historicalPrint: Boolean = false,
    val canPrint: Boolean = false, val printBlockedBy: String = "", val currentPrintActorName: String = "", val arrivalSources: List<String> = emptyList()
)

data class TaskBoardRelatedGroup(val id: String, val customerOrderNo: String, val customer: String,
    val assigneeName: String, val missingCount: Int, val dueDate: String)

data class TaskBoardAssignee(val id: String, val displayName: String)

data class TaskBoardPage(
    val items: List<TaskBoardItem> = emptyList(), val total: Int = 0, val page: Int = 1,
    val hasMore: Boolean = false, val dataVersion: String = "", val taskVersion: String = "",
    val sourceUpdatedAt: String = "", val tasksCurrent: Boolean = true, val businessDate: String = "",
    val summary: DailyWorkSummary = DailyWorkSummary(), val todayEvents: List<DailyWorkEvent> = emptyList(),
    val assignees: List<TaskBoardAssignee> = emptyList(), val stale: Boolean = false, val assignmentEnabled: Boolean? = null,
    val workStateCounts: Map<String, Int> = emptyMap(), val printStateVersion: String = "",
    val orderCount: Int = 0, val orderWorkStateCounts: Map<String, Int> = emptyMap(), val lineCount: Int = 0
)

data class SalesGroupActionResult(val version: Int, val taskVersion: String,
    val workState: String, val hasNote: Boolean, val latestNote: String)

data class TaskBoardDetail(val item: TaskBoardItem, val lines: List<TaskBoardLine> = emptyList(),
    val relatedGroups: List<TaskBoardRelatedGroup> = emptyList(), val events: List<DailyWorkEvent> = emptyList(),
    val dataVersion: String = "", val taskVersion: String = "", val tasksCurrent: Boolean = true,
    val notes: List<TaskBoardNote> = emptyList(), val printStateVersion: String = "")

data class TaskBoardNote(val note: String, val createdAt: String, val actorId: String, val eventType: String)

data class TaskHistoryPage(val items: List<DailyWorkItem> = emptyList(), val total: Int = 0,
    val page: Int = 1, val hasMore: Boolean = false)
data class TaskHistoryDetail(val item: DailyWorkItem, val events: List<DailyWorkEvent> = emptyList(),
    val page: Int = 1, val hasMore: Boolean = false)

data class AdminDashboardPerson(val userId: String, val displayName: String, val todo: Int,
    val inProgress: Int, val waiting: Int, val overdue: Int, val total: Int,
    val completedToday: Int, val lastActionAt: String)
data class AdminDashboardRole(val role: String, val total: Int, val todo: Int, val inProgress: Int,
    val waiting: Int, val overdue: Int, val unassigned: Int, val people: List<AdminDashboardPerson>,
    val businessCompletedToday: Int = 0)
data class DashboardCompletion(val taskId: String, val actorId: String, val eventType: String,
    val note: String, val createdAt: String, val role: String, val title: String,
    val orderNo: String, val source: String)
data class DashboardCompletions(val items: List<DashboardCompletion> = emptyList(), val total: Int = 0,
    val page: Int = 1, val hasMore: Boolean = false)
data class AdminDashboard(val customerOrders: Int, val unfinishedCustomerOrders: Int,
    val readyToShip: Int, val needsReview: Int, val sourceUpdatedAt: String,
    val purchaseSourceUpdatedAt: String, val refreshedAt: String,
    val historyCoverage: String, val roles: List<AdminDashboardRole>,
    val purchaseStatus: String = "unavailable", val purchaseRows: Int = 0)

data class TaskBoardSelection(val id: String, val objectCount: Int, val orderNos: List<String>,
    val dataVersion: String, val taskVersion: String, val role: String, val profileId: String, val excludedCount: Int = 0,
    val printStateVersion: String = "")

data class StockPackingFilters(val customer: String = "", val sku: String = "", val orderNo: String = "",
    val orderDateFrom: String = "", val orderDateTo: String = "", val printState: String = "all",
    val sort: String = "default", val pageSize: Int = 50, val customerKey: String = "",
    val supplier: String = "", val receiptFrom: String = "", val receiptTo: String = "") {
    fun valid(): Boolean = pageSize in 1..100 && printState in setOf("all", "unprinted", "printed", "uncertain", "reserved") &&
        sort in setOf("default", "location", "order_date", "delivery_date", "receipt_date") &&
        (receiptFrom.isBlank() || receiptTo.isBlank() || receiptFrom <= receiptTo) &&
        (orderDateFrom.isBlank() || orderDateTo.isBlank() || orderDateFrom <= orderDateTo)
}

data class PackingFilterOption(val id: String, val name: String)
data class PackingFilterOptions(val customers: List<PackingFilterOption> = emptyList(), val suppliers: List<PackingFilterOption> = emptyList())

data class PackingPrintOrder(val profileId: String, val orderNo: String, val printState: String,
    val stateRevision: String, val labelFingerprint: String, val actorName: String = "", val printedAt: String = "", val labelCount: Int = 0,
    val lines: List<PackingPrintLine> = emptyList(), val arrivalSources: List<String> = emptyList())
data class PackingPrintLine(val id: String, val sku: String, val quantity: String, val labelCount: Int,
    val name: String = "", val spec: String = "", val warnings: List<String> = emptyList())
data class PackingPrintSegment(val index: Int, val orderNos: List<String>, val orderCount: Int, val labelCount: Int)
data class PackingPrintPreview(val id: String, val hash: String, val orderCount: Int, val labelCount: Int,
    val reprintCount: Int, val uncertainCount: Int, val orders: List<PackingPrintOrder>, val segments: List<PackingPrintSegment>,
    val dataVersion: String = "", val maxPerPackage: String = "", val lineCount: Int = 0) {
    fun valid(): Boolean = id.isNotBlank() && hash.matches(Regex("[a-fA-F0-9]{64}")) && orderCount > 0 && labelCount > 0 &&
        orders.size == orderCount && orders.map { it.orderNo }.distinct().size == orderCount &&
        orders.all { it.orderNo.isNotBlank() && it.profileId.isNotBlank() && it.stateRevision.isNotBlank() && it.labelFingerprint.isNotBlank() && it.labelCount > 0 && it.printState in setOf("unprinted", "printed", "uncertain") } &&
        reprintCount == orders.count { it.printState == "printed" } && uncertainCount == orders.count { it.printState == "uncertain" } &&
        segments.isNotEmpty() && segments.map { it.index } == (1..segments.size).toList() &&
        segments.all { it.orderCount in 1..100 && it.labelCount in 1..100 && it.orderNos.size == it.orderCount } &&
        segments.flatMap { it.orderNos } == orders.map { it.orderNo } &&
        orders.sumOf { it.labelCount.toLong() } == labelCount.toLong() &&
        segments.all { segment -> orders.filter { it.orderNo in segment.orderNos }.sumOf { it.labelCount.toLong() } == segment.labelCount.toLong() } && segments.sumOf { it.labelCount.toLong() } == labelCount.toLong()
}

data class BoardExportLine(val orderNo: String, val customer: String, val sku: String,
    val productName: String, val quantity: String, val unit: String, val location: String,
    val summary: String, val taskId: String = "", val specification: String = "",
    val deliveryDate: String = "")
data class BoardExportOrder(val orderNo: String, val items: List<BoardExportLine>)
data class BoardExportCard(val card: TaskBoardItem, val lines: List<BoardExportLine>,
    val orders: List<BoardExportOrder>)
data class BoardExportSnapshot(val id: String, val exportedAt: String, val dataVersion: String,
    val taskVersion: String, val items: List<BoardExportCard>, val printStateVersion: String = "")

data class DailyWorkEvent(
    val id: String, val taskId: String, val eventType: String, val title: String,
    val note: String = "", val createdAt: String = ""
)

data class DailyWorkSnapshot(
    val businessDate: String = "", val dataVersion: String = "", val sourceUpdatedAt: String = "",
    val taskVersion: String = "", val stale: Boolean = false, val roles: List<String> = emptyList(),
    val summary: DailyWorkSummary = DailyWorkSummary(), val items: List<DailyWorkItem> = emptyList(),
    val completedItems: List<DailyWorkItem> = emptyList(), val todayEvents: List<DailyWorkEvent> = emptyList(),
    val assignmentEnabled: Boolean? = null,
    val taskDataVersion: String = "", val taskSourceUpdatedAt: String = "", val tasksCurrent: Boolean? = null,
    val staffLoad: List<StaffLoad> = emptyList(), val printStateVersion: String = ""
)

data class StaffLoad(
    val userId: String, val displayName: String, val roles: List<String>, val onDuty: Boolean,
    val packingLimit: Int, val todo: Int = 0, val inProgress: Int = 0, val waiting: Int = 0,
    val packed: Int = 0, val total: Int = 0
)

data class AssignmentRule(
    val id: String, val role: String, val matchType: String, val matchValue: String,
    val staffId: String, val staffName: String, val priority: Int = 100, val active: Boolean = true
)

data class StaffMember(
    val id: String, val username: String, val displayName: String,
    val roles: List<String>, val active: Boolean, val packingLimit: Int, val isAdmin: Boolean = false
)

data class StaffDraft(
    val username: String = "", val displayName: String = "", val password: String = "",
    val roles: Set<String> = emptySet(), val active: Boolean = true, val packingLimit: Int = 3,
    val isAdmin: Boolean = false
)

data class DailyWorkSettings(val assignmentEnabled: Boolean, val packingLimitDefault: Int = 3)

data class DailyWorkVersion(
    val businessDate: String = "", val dataVersion: String = "", val taskVersion: String = "",
    val tasksCurrent: Boolean? = null, val sourceUpdatedAt: String = "", val printStateVersion: String = ""
)

data class PendingDailyAction(
    val idempotencyKey: String, val origin: String, val userId: String, val taskId: String,
    val action: String, val note: String, val nextFollowUpDate: String, val version: Int,
    val createdAt: String, val lastError: String = "", val requiresReview: Boolean = false,
    val waitingFor: String = "", val frozenRequest: String = ""
)

data class DailyWorkState(
    val me: CurrentUser? = null, val selectedRole: String = "", val view: String = "mine",
    val snapshot: DailyWorkSnapshot = DailyWorkSnapshot(),
    val board: TaskBoardPage = TaskBoardPage(), val boardDetail: TaskBoardDetail? = null,
    val boardSection: String = "active", val taskHistory: TaskHistoryPage = TaskHistoryPage(),
    val boardSectionsByRole: Map<String, String> = emptyMap(),
    val undoSalesStartGroupId: String = "", val undoSalesStartUntil: Long = 0,
    val salesActionSavedId: String = "", val salesActionRevision: Int = 0,
    val taskHistoryDetail: TaskHistoryDetail? = null, val taskHistoryBusy: Boolean = false,
    val taskHistoryPageBusy: Boolean = false,
    val adminDashboardOpen: Boolean = false, val adminDashboardBusy: Boolean = false,
    val adminDashboard: AdminDashboard? = null,
    val dashboardCompletions: DashboardCompletions? = null, val dashboardCompletionsOpen: Boolean = false,
    val dashboardCompletionsBusy: Boolean = false, val dashboardCompletionsError: String? = null,
    val dashboardCompletionsRole: String = "", val dashboardCompletionsActor: String = "",
    val dashboardCompletionsSource: String = "human",
    val boardStatus: String = "all", val boardKeyword: String = "",
    val boardDueFrom: String = "", val boardDueTo: String = "", val boardAssigneeId: String = "",
    val stockFilters: StockPackingFilters = StockPackingFilters(), val stockPage: Int = 1,
    val stockSelectionCalculating: Boolean = false, val boardVerified: Boolean = true,
    val packingOptions: PackingFilterOptions = PackingFilterOptions(), val packingOptionsBusy: Boolean = false, val packingOptionsError: String? = null,
    val packingPrintPreview: PackingPrintPreview? = null, val packingPrintPreviewBusy: Boolean = false,
    val packingPrintRecoveryRequired: Boolean = false, val packingResetBusy: Boolean = false,
    val boardPageBusy: Boolean = false, val boardDetailBusy: Boolean = false,
    val boardSelectionBusy: Boolean = false, val boardPrinting: Boolean = false,
    val boardExportBusy: Boolean = false, val boardExport: BoardExportSnapshot? = null,
    val boardSelectedIds: Set<String> = emptySet(), val boardSelectedTaskIds: Set<String> = emptySet(),
    val boardSelection: TaskBoardSelection? = null, val boardSelectionAll: Boolean = false,
    val customerOrders: CustomerOrderPage = CustomerOrderPage(),
    val customerOrderDetail: CustomerOrderDetail? = null,
    val customerOrderFilter: String = "all", val customerOrderKeyword: String = "",
    val customerOrderPageBusy: Boolean = false, val customerOrderDetailBusy: Boolean = false,
    val customerOrderActionBusy: Boolean = false, val loading: Boolean = false,
    val refreshing: Boolean = false, val error: String? = null, val message: String? = null,
    val actionBusyId: String? = null, val actionErrorId: String? = null, val actionError: String? = null,
    val actionSucceededId: String? = null, val pendingActions: List<PendingDailyAction> = emptyList(),
    val pendingReview: String? = null,
    val staff: List<StaffMember> = emptyList(), val staffLoading: Boolean = false,
    val staffSaving: Boolean = false, val staffError: String? = null, val staffSaveRevision: Int = 0,
    val attendance: Map<String, Boolean> = emptyMap(), val attendanceAvailable: Boolean = false,
    val attendanceSavingId: String? = null, val attendanceError: String? = null,
    val settingsSaving: Boolean = false,
    val purchaseUnfinished: PurchaseUnfinishedState = PurchaseUnfinishedState(),
    val customerOwners: List<CustomerOwner> = emptyList(), val customerOwnersLoading: Boolean = false,
    val customerOwnersSaving: Boolean = false, val customerOwnersError: String? = null,
    val customerOwnersSaveRevision: Int = 0,
    val customerOwnersSavedKey: String = "",
    val assignmentRules: List<AssignmentRule> = emptyList(), val rulesLoading: Boolean = false,
    val rulesSaving: Boolean = false, val rulesError: String? = null, val rulesSaveRevision: Int = 0
)

data class WorkItem(
    val id: String, val sku: String, val name: String,
    val spec: String = "", val orderNo: String = "", val buyer: String = "",
    val quantity: String = "", val logistics: String = "", val location: String = "",
    val available: String? = null, val stock: String? = null,
    val note: String = "", val model: String = "",
    val warehouseId: String = "", val warehouseName: String = "", val productId: String = "", val unitId: String = "",
    val profileId: String = "", val skuId: String = "",
    val calculationStatus: String = "", val calculationIssues: List<String> = emptyList(), val printable: Boolean = true,
    val printBlockReason: String = "",
    val material: String = "", val unitName: String = "",
    val orderDate: String = "", val deliveryDate: String = "",
    val printState: String = "unprinted", val printStateUpdatedAt: String = "",
    val printContentChanged: Boolean = false,
    val lifecycleState: String = "", val processState: String = "",
    val lifecyclePendingReview: Boolean = false,
    val printWarnings: List<String> = emptyList(), val summaryPerson: String = ""
)

const val SHIPPED_UNPOSTED = "shipped_unposted"
const val SETTLEMENT_LOGISTICS = "已完成订单需结存"

val WorkItem.requiresSettlement: Boolean
    get() = !lifecyclePendingReview &&
        (lifecycleState == SHIPPED_UNPOSTED || processState == "500" || logistics == SETTLEMENT_LOGISTICS)

fun WorkItem.isPrintEligible(destination: Destination): Boolean =
    printable && !lifecyclePendingReview && !requiresSettlement &&
        (destination == Destination.Inventory || printState == "unprinted")

data class OrderFilters(
    val printState: String = "all",
    val orderDateFrom: String = "", val orderDateTo: String = "",
    val deliveryDateFrom: String = "", val deliveryDateTo: String = "",
    val sort: String = "default", val customer: String = ""
) {
    fun parameters(): Map<String, String> = linkedMapOf(
        "print_state" to printState,
        "order_date_from" to orderDateFrom,
        "order_date_to" to orderDateTo,
        "delivery_date_from" to deliveryDateFrom,
        "delivery_date_to" to deliveryDateTo,
        "sort" to sort.takeUnless { it == "default" }.orEmpty(),
        "customer" to customer.trim()
    ).filterValues(String::isNotBlank)
}

data class PrintStateCounts(val all: Int = 0, val unprinted: Int = 0, val printed: Int = 0,
                            val reserved: Int = 0, val uncertain: Int = 0)

data class InventoryMovement(
    val id: String, val postedAt: String, val date: String, val documentNo: String,
    val documentType: String, val party: String, val quantity: String,
    val direction: String, val balanceAfter: String?, val reconciliationStatus: String
)

data class InventoryHistory(
    val recordId: String, val status: String,
    val physicalQuantity: String?, val saleableQuantity: String?, val asOf: String,
    val coverageFrom: String, val coverageTo: String, val positiveInbounds: Int,
    val truncated: Boolean, val movements: List<InventoryMovement>,
    val pendingAdjustments: List<String> = emptyList(),
    val total: Int = movements.size, val hasMore: Boolean = false
)

data class ProductImage(
    val id: String, val productId: String, val contentUrl: String,
    val mimeType: String, val width: Int = 0, val height: Int = 0,
    val etag: String = "", val updatedAt: String = ""
)

data class ProductDetailState(
    val item: WorkItem? = null, val loading: Boolean = false,
    val history: InventoryHistory? = null, val images: List<ProductImage> = emptyList(),
    val imageBytes: ByteArray? = null, val error: String? = null,
    val historyDays: Int = 90, val historyBusy: Boolean = false
) {
    override fun equals(other: Any?): Boolean = other is ProductDetailState &&
        item == other.item && loading == other.loading && history == other.history && images == other.images &&
        historyDays == other.historyDays && historyBusy == other.historyBusy &&
        ((imageBytes == null && other.imageBytes == null) ||
            (imageBytes != null && other.imageBytes != null && imageBytes.contentEquals(other.imageBytes))) && error == other.error
    override fun hashCode(): Int = listOf(item, loading, history, images, imageBytes?.contentHashCode(), error,
        historyDays, historyBusy).hashCode()
}

/** These are independent, AND-combined fields; model is the source's mixed grade/model text. */
data class InventoryFilters(
    val name: String = "", val spec: String = "", val model: String = "", val material: String = "", val sku: String = ""
) {
    fun parameters(): Map<String, String> = linkedMapOf("name" to name.trim(), "spec" to spec.trim(),
        "model" to model.trim(), "material" to material.trim(), "sku" to sku.trim().uppercase()).filterValues(String::isNotBlank)
    fun trimmed() = InventoryFilters(name.trim(), spec.trim(), model.trim(), material.trim(), sku.trim().uppercase())
}
data class NearbyPrinter(val address: String, val name: String)
data class WorkbenchState(
    val destination: Destination = Destination.Orders,
    val query: String = "", val queryMode: String = "sku",
    val inventoryFilters: InventoryFilters = InventoryFilters(), val queryRevision: Int = 0,
    val scope: String = "all", val logistics: String = "", val orderFilters: OrderFilters = OrderFilters(),
    val items: List<WorkItem> = emptyList(), val selectedIds: Set<String> = emptySet(),
    val total: Int = 0, val page: Int = 1, val hasMore: Boolean = false,
    val demand: String? = null, val available: String? = null, val stock: String? = null,
    val productName: String = "", val loading: Boolean = false, val queried: Boolean = false,
    val dataVersion: String = "", val sourceUpdatedAt: String = "",
    val error: String? = null, val receipt: String? = null,
    val serverUrl: String = "http://192.168.1.5:5191", val serverStatus: String = "等待连接",
    val serverOnline: Boolean = false,
    val connectionRequired: Boolean = false,
    val loginRequired: Boolean = false, val loginBusy: Boolean = false,
    val apiUpdates: Boolean = false, val logisticsWrite: Boolean = false,
    val businessConfirmations: List<String> = emptyList(), val businessConfirmationError: String? = null,
    val adjustmentUpload: Boolean = true, val adjustmentServerStatus: String = "等待局域网上传电脑连接",
    val updateBusy: Boolean = false, val updateMessage: String = "",
    val updateTask: UpdateTask? = null, val updateDetailsOpen: Boolean = false, val resolvingFreight: Boolean = false,
    val printerStatus: String = "尚未连接打印机", val printerConnected: Boolean = false,
    val printerName: String = "", val printers: List<NearbyPrinter> = emptyList(),
    val printBusy: Boolean = false, val printMessage: String = "",
    val splitRequested: Boolean = false,
    val uncertainPrint: Boolean = false,
    val adjustments: AdjustmentState = AdjustmentState(),
    val printStateCounts: PrintStateCounts = PrintStateCounts(),
    val customerOptions: List<String> = emptyList(),
    val productDetail: ProductDetailState = ProductDetailState(),
    val dailyWork: DailyWorkState = DailyWorkState(),
    val deviceId: String = ""
)

/** Only page-owned query data is retained. Connection and print safety state stay global. */
data class QueryWorkspace(
    val query: String = "", val mode: String = "sku", val filters: InventoryFilters = InventoryFilters(),
    val scope: String = "all", val logistics: String = "", val orderFilters: OrderFilters = OrderFilters(),
    val items: List<WorkItem> = emptyList(), val selectedIds: Set<String> = emptySet(),
    val total: Int = 0, val page: Int = 1, val hasMore: Boolean = false,
    val demand: String? = null, val available: String? = null, val stock: String? = null,
    val productName: String = "", val queried: Boolean = false,
    val dataVersion: String = "", val sourceUpdatedAt: String = "", val printStateCounts: PrintStateCounts = PrintStateCounts(),
    val customerOptions: List<String> = emptyList(), val revision: Int = 0,
    val pendingPage: Int? = null
)

fun WorkbenchState.workspace(pendingPage: Int? = null) = QueryWorkspace(query, queryMode, inventoryFilters,
    scope, logistics, orderFilters, items, selectedIds, total, page, hasMore, demand, available, stock,
    productName, queried, dataVersion, sourceUpdatedAt, printStateCounts, customerOptions, queryRevision, pendingPage)

fun WorkbenchState.withWorkspace(workspace: QueryWorkspace) = copy(
    query = workspace.query, queryMode = workspace.mode, inventoryFilters = workspace.filters,
    scope = workspace.scope, logistics = workspace.logistics, orderFilters = workspace.orderFilters, items = workspace.items, selectedIds = workspace.selectedIds,
    total = workspace.total, page = workspace.page, hasMore = workspace.hasMore,
    demand = workspace.demand, available = workspace.available, stock = workspace.stock,
    productName = workspace.productName, queried = workspace.queried, dataVersion = workspace.dataVersion,
    sourceUpdatedAt = workspace.sourceUpdatedAt, printStateCounts = workspace.printStateCounts,
    customerOptions = workspace.customerOptions, queryRevision = workspace.revision, loading = false,
    splitRequested = false, receipt = null, error = if (uncertainPrint) error else null
)

/** Stable UI boundary: screens render values and emit intentions; no network or BLE code. */
data class WorkbenchActions(
    val navigate: (Destination) -> Unit,
    val search: (String, String) -> Unit,
    val changeScope: (String, String) -> Unit,
    val changeOrderFilters: (OrderFilters) -> Unit = {},
    val toggleSelection: (String) -> Unit,
    val selectAll: () -> Unit,
    val loadMore: () -> Unit,
    val printSelected: (Int?) -> Unit,
    val printAll: (Int?) -> Unit,
    val cancelPrint: () -> Unit,
    val requestSplit: () -> Unit,
    val dismissSplit: () -> Unit,
    val editingChanged: (Boolean) -> Unit,
    val scanPrinters: () -> Unit,
    val connectPrinter: (String) -> Unit,
    val disconnectPrinter: () -> Unit,
    val saveServer: (String) -> Unit,
    val checkServer: () -> Unit,
    val clearMessage: () -> Unit,
    val searchInventory: (String, InventoryFilters) -> Unit = { _, _ -> },
    val searchInventoryCode: (String) -> Unit = {},
    val clearQuery: () -> Unit = {},
    val addAdjustment: (WorkItem, AdjustmentType, String, String) -> AdjustmentSaveResult = { _, _, _, _ ->
        AdjustmentSaveResult(false, "当前页面暂不能保存库存清单，请重新打开商品详情") },
    val removeAdjustment: (AdjustmentType, String) -> Unit = { _, _ -> },
    val uploadAdjustments: (AdjustmentType) -> Unit = {},
    val retryAdjustment: (String) -> Unit = {},
    val restoreAdjustment: (String) -> Unit = {},
    val confirmAdjustment: (String) -> Unit = {},
    val refreshAdjustments: () -> Unit = {},
    val login: (String) -> Unit = {}, val loginWithUsername: (String, String) -> Unit = { _, password -> login(password) },
    val logout: () -> Unit = {}, val updateData: () -> Unit = {},
    val showUpdateDetails: () -> Unit = {}, val dismissUpdateDetails: () -> Unit = {},
    val resolveFreight: (String, String, String) -> Unit = { _, _, _ -> },
    val openProductDetail: (WorkItem) -> Unit = {}, val openOrderProductDetail: (WorkItem) -> Unit = {},
    val closeProductDetail: () -> Unit = {}, val setInventoryHistoryDays: (Int) -> Unit = {},
    val loadMoreInventoryHistory: () -> Unit = {},
    val resetOrderPrintState: (String) -> Unit = {},
    val loadBusinessConfirmations: () -> Unit = {},
    val loadPackingFilterOptions: (String) -> Unit = {},
    val dailyWorkVisible: (Boolean) -> Unit = {}, val refreshDailyWork: () -> Unit = {},
    val retryDailyActions: () -> Unit = {},
    val selectDailyRole: (String) -> Unit = {}, val selectDailyView: (String) -> Unit = {},
    val submitDailyAction: (String, String, String, String, Int) -> Unit = { _, _, _, _, _ -> },
    val loadStaff: () -> Unit = {}, val createStaff: (StaffDraft) -> Unit = {},
    val updateStaff: (String, StaffDraft) -> Unit = { _, _ -> },
    val resetStaffPassword: (String, String) -> Unit = { _, _ -> },
    val setStaffAttendance: (String, Boolean) -> Unit = { _, _ -> },
    val setAssignmentEnabled: (Boolean) -> Unit = {},
    val transferDailyTask: (String, String, String, Int) -> Unit = { _, _, _, _ -> },
    val saveAssignmentRule: (String, String, String) -> Unit = { _, _, _ -> },
    val openPurchaseUnfinished: () -> Unit = {}, val closePurchaseUnfinished: () -> Unit = {},
    val refreshPurchaseUnfinished: () -> Unit = {}, val openPurchaseSupplier: (PurchaseSupplier) -> Unit = {},
    val closePurchaseSupplier: () -> Unit = {}, val loadMorePurchaseUnfinished: () -> Unit = {},
    val editPurchasePromise: (PurchaseUnfinishedLine) -> Unit = {}, val resumePurchasePromise: () -> Unit = {},
    val closePurchasePromise: () -> Unit = {}, val discardPurchasePromise: () -> Unit = {},
    val updatePurchasePromise: (String, String) -> Unit = { _, _ -> }, val savePurchasePromise: () -> Unit = {},
    val reviewPurchasePromise: () -> Unit = {}, val acceptPurchasePromiseReview: () -> Unit = {},
    val loadCustomerOwners: () -> Unit = {},
    val saveCustomerOwner: (CustomerOwnerDraft) -> Unit = {},
    val deleteAssignmentRule: (String) -> Unit = {},
    val setDailyTaskUrgent: (String, Boolean, String, Int) -> Unit = { _, _, _, _ -> },
    val discardDailyActionReview: (String) -> Unit = {},
    val resetDailyOrderPrintState: (String, String) -> Unit = { _, _ -> },
    val openDailyOrder: (String) -> Unit = {},
    val selectCustomerOrderFilter: (String) -> Unit = {},
    val searchCustomerOrders: (String) -> Unit = {},
    val loadMoreCustomerOrders: () -> Unit = {},
    val openCustomerOrder: (String) -> Unit = {},
    val closeCustomerOrder: () -> Unit = {},
    val recordCustomerOrderFollowup: (String, String, String, String, Int) -> Unit = { _, _, _, _, _ -> },
    val transferCustomerOrder: (String, String, String, Int) -> Unit = { _, _, _, _ -> },
    val loadMoreTaskBoard: () -> Unit = {}, val openTaskBoardItem: (String) -> Unit = {},
    val closeTaskBoardDetail: () -> Unit = {}, val searchTaskBoard: (String) -> Unit = {},
    val selectTaskBoardStatus: (String) -> Unit = {}, val toggleTaskBoardItem: (String) -> Unit = {},
    val startTaskBoardSales: (String) -> Unit = {}, val resetTaskBoardSales: (String) -> Unit = {},
    val saveTaskBoardSalesNote: (String, String) -> Unit = { _, _ -> }, val undoTaskBoardSalesStart: () -> Unit = {},
    val toggleTaskBoardLine: (String) -> Unit = {}, val selectAllTaskBoard: () -> Unit = {},
    val clearTaskBoardSelection: () -> Unit = {},
    val submitTaskBoardBatch: (String, String, String, String, String, String) -> Unit = { _, _, _, _, _, _ -> },
    val printTaskBoardSelection: (Int?) -> Unit = {},
    val setStockPackingFilters: (StockPackingFilters, String, String, String, String) -> Unit = { _, _, _, _, _ -> },
    val changeStockPackingPage: (Int) -> Unit = {}, val selectStockPackingPage: () -> Unit = {},
    val previewStockPackingPrint: (String) -> Unit = {},
    val confirmStockPackingPrint: (Boolean, Boolean) -> Unit = { _, _ -> },
    val dismissStockPackingPrint: () -> Unit = {}, val acknowledgeStockPackingPrint: () -> Unit = {},
    val resetStockPackingPrint: (String, String) -> Unit = { _, _ -> },
    val exportTaskBoardImages: () -> Unit = {}, val clearTaskBoardExport: () -> Unit = {},
    val selectTaskBoardSection: (String) -> Unit = {}, val loadMoreTaskHistory: () -> Unit = {},
    val openTaskHistoryItem: (String) -> Unit = {}, val closeTaskHistoryDetail: () -> Unit = {},
    val openAdminDashboard: () -> Unit = {}, val closeAdminDashboard: () -> Unit = {},
    val openDashboardRole: (String, String) -> Unit = { _, _ -> },
    val openDashboardCompletions: (String, String, String) -> Unit = { _, _, _ -> },
    val loadMoreDashboardCompletions: () -> Unit = {}, val closeDashboardCompletions: () -> Unit = {},
    val setTaskBoardFilters: (String, String, String, String, String) -> Unit = { _, _, _, _, _ -> }
)
