package com.ul.workbench

import com.ul.workbench.data.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.time.LocalDate
import java.util.UUID

/** Local reply drafts are durable and scoped to the signed-in user and service. */
internal class PurchaseUnfinishedController(
    private val repository: WorkbenchRepository, private val scope: CoroutineScope,
    private val read: () -> PurchaseUnfinishedState,
    private val update: ((PurchaseUnfinishedState) -> PurchaseUnfinishedState) -> Unit,
    private val context: () -> String, private val authorized: () -> Boolean,
    private val loadDraft: (String) -> String?,
    private val writeDraft: (String, String?) -> Boolean
) {
    private var request: Job? = null
    private var generation = 0
    private fun bound() = authorized() && read().sessionContext == context()
    private fun stillCurrent(key: String) = context() == key && read().sessionContext == key && authorized()
    private fun failureText(error: Exception) = error.message?.takeIf(String::isNotBlank) ?: "连接失败，请稍后重试"
    private fun persist(draft: PurchasePromiseDraft?): Boolean = writeDraft(context(), draft?.let(PurchaseUnfinishedJson::draftJson))

    fun open() {
        if (!authorized()) return
        val key = context()
        val restored = loadDraft(key)?.let(PurchaseUnfinishedJson::draft)
        update { if (it.sessionContext == key) it.copy(open = true, editor = it.editor ?: restored, editorOpen = false)
                 else PurchaseUnfinishedState(sessionContext = key, open = true, editor = restored) }
        refresh()
    }
    fun close() {
        if (read().saving) return
        generation++; request?.cancel()
        update { it.copy(open = false, loading = false, loadingMore = false, reviewing = false, editorOpen = false) }
    }
    fun refresh() {
        if (!bound() || read().saving) return
        val selected = read().selectedSupplier
        if (selected != null) { supplier(selected); return }
        val key = context(); val run = ++generation
        request?.cancel()
        update { it.copy(loading = true, error = null, message = null) }
        request = scope.launch {
            try {
                val status = repository.purchaseUnfinishedStatus()
                val values = if (status.readable) repository.purchaseSuppliers() else null
                if (!stillCurrent(key) || run != generation) return@launch
                val meta = values?.meta?.copy(collectorStatus = status.collectorStatus,
                    collectorReason = status.collectorReason,
                    publicationStage = values.meta.publicationStage.ifBlank { status.publicationStage }) ?: status
                update { it.copy(meta = meta, suppliers = values?.items.orEmpty(), loading = false,
                    editorOpen = it.editorOpen && meta.canRecordReply) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (stillCurrent(key) && run == generation)
                    update { it.copy(loading = false, error = "采购读取失败，当前展示保留的数据：${failureText(error)}") }
            }
        }
    }
    fun supplier(value: PurchaseSupplier) {
        if (!bound() || read().saving) return
        val key = context(); val run = ++generation
        request?.cancel()
        update { it.copy(selectedSupplier = value, loading = true, loadingMore = false, error = null,
            page = if (it.selectedSupplier?.code == value.code) it.page else PurchaseUnfinishedPage(),
            reminder = PurchaseReminderGate()) }
        request = scope.launch {
            try {
                val status = repository.purchaseUnfinishedStatus()
                if (!stillCurrent(key) || run != generation) return@launch
                val result = repository.purchaseUnfinished(value.code, 1)
                if (!stillCurrent(key) || run != generation) return@launch
                val gate = try { repository.purchaseReminder(value.code) }
                    catch (cancelled: CancellationException) { throw cancelled }
                    catch (_: Exception) { PurchaseReminderGate(reason = "催货校验暂不可用，分享保持关闭") }
                if (!stillCurrent(key) || run != generation) return@launch
                val meta = result.meta.copy(collectorStatus = status.collectorStatus,
                    collectorReason = status.collectorReason,
                    publicationStage = result.meta.publicationStage.ifBlank { status.publicationStage })
                update { it.copy(page = result.copy(meta = meta), meta = meta, reminder = gate,
                    loading = false, editorOpen = it.editorOpen && meta.canRecordReply) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (stillCurrent(key) && run == generation)
                    update { it.copy(loading = false, error = "采购明细读取失败：${failureText(error)}") }
            }
        }
    }
    fun suppliers() {
        if (read().saving) return
        generation++; request?.cancel()
        update { it.copy(selectedSupplier = null, page = PurchaseUnfinishedPage(), loading = false, loadingMore = false) }
        refresh()
    }
    fun more() {
        val before = read(); val supplier = before.selectedSupplier ?: return
        if (!bound() || before.loading || before.loadingMore || before.saving || !before.page.hasMore) return
        val key = context(); val run = generation
        update { it.copy(loadingMore = true, error = null) }
        request = scope.launch {
            try {
                val page = repository.purchaseUnfinished(supplier.code, before.page.page + 1)
                if (!stillCurrent(key) || run != generation) return@launch
                if (page.meta.snapshotId != before.page.meta.snapshotId || page.meta.accountScope != before.page.meta.accountScope)
                    throw ServiceException("snapshot_version_changed", "采购快照已更新，请刷新后继续；填写内容仍保留", 409)
                update { it.copy(page = page.copy(items = it.page.items + page.items), loadingMore = false) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (stillCurrent(key) && run == generation) update { it.copy(loadingMore = false, error = failureText(error)) }
            }
        }
    }
    fun edit(line: PurchaseUnfinishedLine) {
        val current = read()
        if (!bound() || current.saving || !current.meta.canRecordReply ||
            !current.page.meta.canRecordReply || line.ambiguous) return
        if (current.editor != null) {
            update { it.copy(editorOpen = true, message = "先处理已保留的回复草稿，再记录其他明细") }; return
        }
        val draft = PurchasePromiseDraft(current.page.meta.snapshotId, current.page.meta.accountScope, line,
            promisedDate = line.promisedDate, note = line.promiseNote)
        if (!persist(draft)) { update { it.copy(error = "手机未能保存回复草稿，本次没有发送") }; return }
        update { it.copy(editor = draft, editorOpen = true, message = null) }
    }
    fun resume() { if (bound() && read().meta.canRecordReply && read().editor != null)
        update { it.copy(editorOpen = true) } }
    fun hideEditor() { if (!read().saving && !read().reviewing) update { it.copy(editorOpen = false) } }
    fun discard() {
        val draft = read().editor ?: return
        if (!bound() || read().saving || read().reviewing || draft.outcomeUncertain) return
        if (persist(null)) update { it.copy(editor = null, editorOpen = false) }
    }
    fun change(date: String, note: String) {
        val draft = read().editor ?: return
        if (!bound() || read().saving || draft.outcomeUncertain) return
        val changed = draft.copy(promisedDate = date, note = note.take(1000), frozenRequest = "", error = null)
        if (persist(changed)) update { it.copy(editor = changed) }
        else update { it.copy(editor = draft.copy(error = "手机未能保存输入，请重试")) }
    }
    fun save() {
        val current = read(); val draft = current.editor ?: return
        if (!bound() || !current.meta.canRecordReply || current.saving || current.reviewing ||
            draft.requiresReview || draft.line.ambiguous) return
        if (current.meta.accountScope != draft.accountScope || !current.meta.accountScopeVerified) {
            update { it.copy(editor = draft.copy(requiresReview = true, error = "采购账套已变化，请重新核对，填写内容已保留")) }; return
        }
        if (!draft.outcomeUncertain && (runCatching { LocalDate.parse(draft.promisedDate) }.isFailure || draft.note.isBlank())) {
            update { it.copy(editor = draft.copy(error = "请填写有效日期（YYYY-MM-DD）和回复来源说明")) }; return
        }
        val body = draft.frozenRequest.ifBlank { JSONObject().put("snapshot_id", draft.snapshotId)
            .put("version", draft.line.promiseVersion).put("idempotency_key", UUID.randomUUID().toString())
            .put("promised_date", draft.promisedDate).put("note", draft.note.trim()).toString() }
        val frozen = draft.copy(frozenRequest = body, outcomeUncertain = true, error = null)
        if (!persist(frozen)) { update { it.copy(editor = draft.copy(error = "手机未能保存提交记录，本次没有发送")) }; return }
        val key = context()
        update { it.copy(saving = true, editor = frozen) }
        scope.launch {
            try {
                repository.savePurchasePromise(draft.line.lineKey, body)
                if (!stillCurrent(key)) return@launch
                if (!writeDraft(key, null)) {
                    update { it.copy(saving = false, editor = frozen.copy(error = "服务已保存，手机清理记录失败；重试同一提交可安全确认")) }
                    return@launch
                }
                update { it.copy(saving = false, editor = null, editorOpen = false, message = "回复记录已保存，请核对刷新后的当前明细") }
                read().selectedSupplier?.let(::supplier)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (!stillCurrent(key)) return@launch
                val rejected = error is ServiceException && error.status?.let { it in 400..499 && it !in setOf(408,429) } == true
                val failed = frozen.copy(outcomeUncertain = !rejected, requiresReview = rejected,
                    frozenRequest = if (rejected) "" else body,
                    error = failureText(error) + if (rejected) "；填写内容已保留，请读取当前明细后核对" else "；提交结果待确认，只能重试同一提交")
                writeDraft(key, PurchaseUnfinishedJson.draftJson(failed))
                update { it.copy(saving = false, editor = failed) }
            }
        }
    }
    fun review() {
        val draft = read().editor ?: return
        if (!bound() || !read().meta.canRecordReply || read().saving || read().reviewing || draft.outcomeUncertain) return
        val key = context()
        update { it.copy(reviewing = true, editor = draft.copy(current = null, error = null)) }
        scope.launch {
            try {
                var pageNumber = 1
                var firstMeta: PurchaseSnapshotMeta? = null
                val matches = mutableListOf<PurchaseUnfinishedLine>()
                do {
                    val page = repository.purchaseUnfinished(draft.line.supplierCode, pageNumber, draft.line.orderNo)
                    if (firstMeta == null) firstMeta = page.meta
                    if (page.meta.snapshotId != firstMeta.snapshotId || page.meta.accountScope != firstMeta.accountScope)
                        throw ServiceException("snapshot_version_changed", "核对期间快照再次变化，请重新读取",409)
                    matches += page.items.filter { it.lineKey == draft.line.lineKey }
                    if (!page.hasMore) break
                    require(page.page == pageNumber && pageNumber < 2000) { "采购明细分页异常，暂不能确认" }
                    pageNumber++
                } while (true)
                val meta = requireNotNull(firstMeta)
                if (!stillCurrent(key) || read().editor?.line?.lineKey != draft.line.lineKey) return@launch
                if (!meta.accountScopeVerified || meta.accountScope != draft.accountScope || matches.size != 1 || matches.single().ambiguous)
                    throw ServiceException("purchase_line_conflict", "当前账套或明细无法唯一对应，请保留草稿并核实原订单",409)
                val latest = read().editor ?: return@launch
                update { it.copy(reviewing = false, editor = latest.copy(
                    requiresReview = true, current = PurchasePromiseReview(meta.snapshotId, meta.accountScope, matches.single()))) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (stillCurrent(key)) update { it.copy(reviewing = false, editor = it.editor?.copy(
                    requiresReview = true, current = null, error = failureText(error))) }
            }
        }
    }
    fun acceptReview() {
        val draft = read().editor ?: return
        val current = draft.current ?: return
        if (!bound() || !read().meta.canRecordReply || read().saving || read().reviewing ||
            draft.outcomeUncertain || current.line.ambiguous) return
        val accepted = draft.copy(snapshotId = current.snapshotId, accountScope = current.accountScope,
            line = current.line, requiresReview = false, frozenRequest = "", current = null, error = null)
        if (persist(accepted)) update { it.copy(editor = accepted) }
    }
}
