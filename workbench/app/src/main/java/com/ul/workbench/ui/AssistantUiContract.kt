package com.ul.workbench.ui

/** UI projection only; service/controllers own identities, versions and durable drafts. */
enum class AssistantPanel { Sales, Purchase }

data class AssistantUiState(
    val panel: AssistantPanel? = null,
    val sales: SalesAssistantUiState = SalesAssistantUiState(),
    val purchase: PurchaseAssistantUiState = PurchaseAssistantUiState(),
    val webOperations: List<WebOperationUi> = emptyList(),
    val webOperationsError: String? = null
)

data class WebOperationUi(val id: String, val label: String, val status: String,
                          val stage: String, val detail: String, val step: Int,
                          val totalSteps: Int, val createdAt: String)

data class AssistantLocalAttachment(val uri: String, val name: String, val mimeType: String, val sizeBytes: Long? = null)
data class SalesDocumentProcessingUi(val status: String, val message: String = "", val errorCode: String = "",
                                     val pageCount: Int = 0, val lineCount: Int = 0, val candidateCount: Int = 0,
                                     val unreadyPages: List<Int> = emptyList(), val resultHash: String = "",
                                     val adoptedVersion: Int? = null)
data class AssistantAttachmentUi(val id: String, val name: String, val status: String = "ready", val detail: String = "",
                                 val documentProcessing: SalesDocumentProcessingUi? = null, val mimeType: String = "",
                                 val sha256: String = "", val sizeBytes: Long = 0)
data class AssistantMessageUi(val id: String, val role: String, val text: String,
                              val attachments: List<AssistantAttachmentUi> = emptyList())
data class SalesSessionUi(val id: String, val title: String, val updatedAt: String = "",
                          val messages: List<AssistantMessageUi> = emptyList(), val profileId: String = "",
                          val documentAttachments: List<AssistantAttachmentUi> = emptyList(), val dataVersion: String = "")
data class SalesCustomerChoiceUi(val customerName: String, val ownerName: String)
data class SalesNewProductSuggestionUi(val id: String, val productName: String, val specification: String = "",
                                       val proposedCode: String = "", val reason: String = "", val status: String = "pending_review",
                                       val canPrepare: Boolean = false, val material: String = "")
data class SalesNewProductPlanUi(val id: String, val status: String, val suggestionId: String, val jobId: String,
                                 val sourceResultHash: String, val planHash: String = "", val productName: String = "",
                                 val specification: String = "", val model: String = "", val code: String = "",
                                 val baseUnit: String = "", val classificationPath: String = "", val issues: List<String> = emptyList(),
                                 val canConfirm: Boolean = false, val canPrepare: Boolean = false,
                                 val createdCode: String = "", val errorCode: String = "", val material: String = "") {
    val running: Boolean get() = status in setOf("preparing", "queued", "creating")
    val created: Boolean get() = status == "created" && createdCode.isNotBlank() && createdCode == code
}
/** Codex proposes a separate result. It never changes or confirms the actual draft on refresh. */
data class SalesCodexJobUi(val id: String, val status: String, val baseVersion: Int,
                          val resultHash: String = "", val assistantText: String = "",
                          val issues: List<String> = emptyList(), val preview: SalesDraftUi? = null,
                          val canAdopt: Boolean = false,
                          val newProductSuggestions: List<SalesNewProductSuggestionUi> = emptyList()) {
    val running: Boolean get() = status in setOf("queued", "running")
    fun blocksConfirmation(version: Int) = baseVersion == version && status !in setOf("adopted", "stale")
}
data class SalesSourceRowUi(val id: String, val label: String, val preview: String, val status: String,
                           val reason: String, val lineId: String = "", val canExclude: Boolean = true)
data class SalesSourceActionUi(val id: String, val reason: String)
data class SalesSourceProductUi(val productName: String = "", val spec: String = "", val model: String = "",
                                val grade: String = "", val surface: String = "", val standard: String = "")
data class SalesSourceReviewUi(val required: Boolean, val reasons: List<String> = emptyList(), val confirmed: Boolean = false,
                               val evidenceHash: String = "", val original: SalesSourceProductUi = SalesSourceProductUi(),
                               val catalog: SalesSourceProductUi = SalesSourceProductUi())
/** Read-only recommendations; selecting one only changes the local product code. */
data class SalesMatchCandidateUi(val productCode: String, val productName: String = "", val spec: String = "", val model: String = "",
                                 val method: String = "", val evidenceCount: Int = 0, val lastOrderDate: String = "",
                                 val available: Boolean = false, val message: String = "")
data class SalesMatchStatusUi(val status: String = "", val message: String = "", val coveredFrom: String = "", val coveredTo: String = "")
data class SalesDraftLineUi(val id: String, val productCode: String, val productName: String,
                            val specification: String = "", val quantity: String = "", val unit: String = "",
                            // unitPrice is the customer's original quote, before tax conversion.
                            val unitPrice: String = "", val warehouse: String = "", val issue: String = "", val dueDate: String = "",
                            val taxBasis: String = "tax_inclusive", val taxRate: String = "13",
                            val untaxedUnitPrice: String = "", val untaxedLineAmount: String = "",
                            val summaryDimension: String = "", val projectNo: String = "", val drawingNo: String = "",
                            val grade: String = "", val surface: String = "", val summaryMetadataConfirmed: Boolean = false,
                            val summaryDimensionSource: String = "", val sourceLabel: String = "", val sourcePreview: String = "",
                            val sourceReview: SalesSourceReviewUi? = null, val sourceMatchConfirmed: Boolean = false,
                            val sourceAttachmentId: String = "", val matchCandidates: List<SalesMatchCandidateUi> = emptyList(),
                            val matchStatus: SalesMatchStatusUi? = null,
                            val previewOriginal: SalesSourceProductUi? = null, val previewCatalog: SalesSourceProductUi? = null,
                            val customerLineNo: String = "", val originalMaterial: String = "", val catalogArea: String = "",
                            val catalogMaterial: String = "")
data class SalesDraftUi(val id: String, val version: Int, val status: String = "draft",
                       val customerName: String = "", val salesOwnerName: String = "", val customerOrderNo: String = "",
                       val lines: List<SalesDraftLineUi> = emptyList(), val issues: List<String> = emptyList(),
                       val canConfirm: Boolean = false, val canSubmit: Boolean = false,
                       val confirmedVersion: Int? = null, val erpOrderNo: String = "", val dueDate: String = "",
                       val taxBasis: String = "tax_inclusive", val taxRate: String = "13",
                       val sourceRows: List<SalesSourceRowUi> = emptyList(), val sourceActions: List<SalesSourceActionUi> = emptyList(),
                       val note: String = "", val submissionCompletionBasis: String = "", val erpReadbackPerformed: Boolean = false,
                       val erpOrderNumbers: List<String> = emptyList(), val erpDocumentCount: Int = 0, val erpReferenceScope: String = "") {
    val hasCompleteLineDocumentReceipt: Boolean get() = erpReferenceScope == "first_line_document" &&
        status == "submitted" && submissionCompletionBasis == "web_import_confirmation" && erpDocumentCount > 0 &&
        erpOrderNumbers.size == erpDocumentCount && erpOrderNumbers.all(String::isNotBlank) &&
        erpOrderNumbers.distinct().size == erpOrderNumbers.size && erpOrderNo.isNotBlank() && erpOrderNumbers.firstOrNull() == erpOrderNo
}
data class AssistantJobUi(val id: String, val status: String, val label: String,
                         val completed: Int = 0, val total: Int = 0, val detail: String = "",
                         val requiresReview: Boolean = false)
data class SalesAssistantUiState(val loading: Boolean = false, val busy: Boolean = false,
                                 val accountLabel: String = "",
                                 val sessions: List<SalesSessionUi> = emptyList(), val session: SalesSessionUi? = null,
                                 val customerChoices: List<SalesCustomerChoiceUi> = emptyList(),
                                 val customerChoicesLoading: Boolean = false, val customerChoicesError: String? = null,
                                 val customerChoicesTotal: Int = 0, val customerChoicesHasMore: Boolean = false,
                                 val input: String = "", val attachments: List<AssistantAttachmentUi> = emptyList(),
                                 val draft: SalesDraftUi? = null, val job: AssistantJobUi? = null,
                                 val editor: SalesDraftUi? = null, val editorOpen: Boolean = false,
                                 val hasLocalDraftEdit: Boolean = false, val pendingDraftSave: Boolean = false,
                                 val pendingDocumentAdoptionId: String = "",
                                 val viewingAttachmentId: String = "",
                                 val editorBusy: Boolean = false, val editorError: String? = null,
                                 val codexEnabled: Boolean = false, val codexJob: SalesCodexJobUi? = null,
                                 val pendingCodexOperation: String = "",
                                 val newProductAdmin: Boolean = false, val newProductPlans: List<SalesNewProductPlanUi> = emptyList(),
                                 val pendingNewProductOperation: String = "",
                                 val newProductReferences: Map<String, String> = emptyMap(),
                                 val error: String? = null, val notice: String? = null, val createError: String? = null)
data class PurchasePlanItemUi(val id: String, val version: Int, val productCode: String, val productName: String,
                             val specification: String = "", val supplierName: String = "",
                             val quantity: String = "", val unit: String = "", val warehouse: String = "",
                             val platformName: String = "", val priceLimit: String = "",
                             val sourceOrderCount: Int = 0, val state: String = "todo", val eligible: Boolean = false,
                             val canEnroll: Boolean = false, val reason: String = "", val job: AssistantJobUi? = null,
                             val planId: String = "", val lastFailure: String = "") {
    val hasPlan: Boolean get() = planId.isNotBlank() || job?.id?.isNotBlank() == true
}
data class PurchaseWhitelistCounts(val candidateTotal: Int, val verifiedTotal: Int,
                                   val reviewTotal: Int, val matchedDemandTotal: Int)
data class PurchaseAssistantUiState(val loading: Boolean = false, val busy: Boolean = false,
                                    val planId: String = "", val generatedAt: String = "", val sourceVersion: String = "",
                                    val whitelistVersion: String = "", val items: List<PurchasePlanItemUi> = emptyList(),
                                    val whitelistCounts: PurchaseWhitelistCounts? = null,
                                    val messages: List<AssistantMessageUi> = emptyList(), val input: String = "", val conversationEnabled: Boolean = false,
                                    val job: AssistantJobUi? = null, val error: String? = null, val notice: String? = null)

data class AssistantActions(
    val openSales: () -> Unit = {}, val openPurchase: () -> Unit = {}, val close: () -> Unit = {},
    val refreshSales: () -> Unit = {}, val newSalesSession: (String) -> Unit = {}, val selectSalesSession: (String) -> Unit = {},
    val searchSalesCustomers: (String) -> Unit = {},
    val updateSalesInput: (String) -> Unit = {}, val attachSalesFile: (AssistantLocalAttachment) -> Unit = {},
    val attachmentError: (String) -> Unit = {},
    val removeSalesAttachment: (String) -> Unit = {}, val sendSalesMessage: () -> Unit = {},
    val retrySalesAttachment: (String) -> Unit = {},
    val confirmSalesDraft: (String, Int) -> Unit = { _, _ -> }, val submitSalesDraft: (String, Int) -> Unit = { _, _ -> },
    val beginSalesDraftEdit: () -> Unit = {}, val updateSalesDraftEdit: (SalesDraftUi) -> Unit = {},
    val saveSalesDraftEdit: () -> Unit = {}, val adoptLatestSalesDraftVersion: () -> Unit = {},
    val closeSalesDraftEditor: () -> Unit = {}, val discardSalesDraftEdit: () -> Unit = {},
    val retryUnknownSalesDraftSave: () -> Unit = {},
    val adoptSalesDocument: (String, String) -> Unit = { _, _ -> }, val retryUnknownSalesDocumentAdoption: () -> Unit = {},
    val viewSalesAttachment: (String) -> Unit = {},
    val adoptSalesCodexDraft: (String, String) -> Unit = { _, _ -> },
    val retryUnknownSalesCodexOperation: () -> Unit = {},
    val prepareSalesNewProductSuggestion: (String, String, String) -> Unit = { _, _, _ -> },
    val updateSalesNewProductReference: (String, String) -> Unit = { _, _ -> },
    val confirmSalesNewProduct: (String, String) -> Unit = { _, _ -> },
    val retryUnknownSalesNewProductOperation: () -> Unit = {},
    val refreshPurchase: () -> Unit = {}, val enrollPurchaseItem: (String, Int) -> Unit = { _, _ -> },
    val updatePurchaseInput: (String) -> Unit = {}, val sendPurchaseMessage: () -> Unit = {},
    val refreshJob: (String) -> Unit = {}, val refreshWebOperations: () -> Unit = {}
)
