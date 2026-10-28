package com.ul.workbench.data

import android.content.SharedPreferences
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.math.BigDecimal
import java.security.MessageDigest
import java.util.UUID

/** Durable per-server outbox. No ERP request is sent when adding a product. */
class AdjustmentOutbox(
    private val preferences: SharedPreferences,
    private val scope: CoroutineScope,
    private val repositoryFor: (String) -> WorkbenchRepository,
    private val changed: (AdjustmentState) -> Unit,
    private val uploaded: (Set<String>) -> Unit = {}
) {
    var state = AdjustmentState()
        private set
    private var server = ""
    private var generation = 0
    private var syncJob: Job? = null
    private var clientId = ""
    private var profileKey: String? = null
    private val key get() = profileKey ?: ("adjustment-outbox-" + MessageDigest.getInstance("SHA-256")
        .digest(server.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
        )

    /** Production storage follows the ERP profile and warehouse, never the public query URL. */
    fun useProfileBucket(bucket: String, uploadServer: String, legacyServers: List<String> = emptyList()) {
        syncJob?.cancel(); generation++
        server = uploadServer
        val previousProfileKey = profileKey
        profileKey = "adjustment-profile-${bucket.replace(Regex("[^A-Za-z0-9._-]"), "_")}-08390-v2"
        ensureClientId()
        val current = preferences.getString(key, null)
        if (current != null) {
            load(current)
            return
        }
        val defaultBucket = "adjustment-profile-default-08390-v2"
        val migrationKeys = if (previousProfileKey == defaultBucket && preferences.contains(defaultBucket)) setOf(defaultBucket) else {
            val knownKeys = legacyServers.map { legacyKey(it) }.toSet()
            val rawKeys = preferences.all.keys.filter { it.startsWith("adjustment-outbox-") && it != key }
            knownKeys + rawKeys
        }
        val candidates = migrationKeys.mapNotNull { legacyKey ->
            preferences.getString(legacyKey, null)?.let { raw -> runCatching { AdjustmentJson.decode(raw) }.getOrNull() }
        }
        val merged = merge(candidates, legacyServers.firstOrNull() ?: uploadServer)
        if (!preferences.edit().putString(key, AdjustmentJson.encode(merged)).commit()) {
            emit(AdjustmentState(storageError = true, message = "无法迁移报损报溢草稿，请检查手机存储后重启 App"))
        } else emit(merged.copy(message = if (candidates.isEmpty()) null else "原有报损报溢草稿已迁移到 08390 清单"))
    }

    fun switchServer(value: String) {
        syncJob?.cancel(); generation++
        server = value
        profileKey = null
        ensureClientId()
        val raw = preferences.getString(key, null)
        if (raw == null) emit(AdjustmentState()) else load(raw)
    }

    private fun ensureClientId() {
        clientId = preferences.getString("adjustment-client", null).orEmpty()
        if (clientId.isBlank()) {
            clientId = UUID.randomUUID().toString()
            if (!preferences.edit().putString("adjustment-client", clientId).commit())
                emit(AdjustmentState(storageError = true, message = "无法保存设备标识，请检查手机存储后重启 App"))
        }
    }

    private fun load(raw: String) {
        try { emit(AdjustmentJson.decode(raw)) }
        catch (_: Exception) { emit(AdjustmentState(storageError = true, message = "库存清单读取失败，原始记录已保留；请联系管理员处理，勿清除 App 数据")) }
    }

    private fun legacyKey(value: String) = "adjustment-outbox-" + MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }

    /** Preserve old possibly-submitted intents; only editable drafts move hosts. */
    fun holdLegacyBatches(legacyServer: String) {
        val values = state.batches.map { batch ->
            if ((batch.uploadServer.isBlank() || batch.uploadServer.trimEnd('/') == legacyServer) &&
                batch.status !in setOf("rejected", "succeeded", "legacy_unknown"))
                batch.copy(uploadServer = legacyServer, status = "legacy_unknown", retryable = false,
                    confirmationPending = false,
                    message = "旧电脑提交记录已保留，需核对原 ERP 单据；不会在 N150 自动重传。")
            else batch
        }
        if (values != state.batches) persist(state.copy(batches = values))
    }

    /** Routing changes move editable drafts only, never an already frozen intent. */
    fun holdBatchesOutsideServer(previousDefault: String) {
        val values = state.batches.map { batch ->
            val origin = batch.uploadServer.ifBlank { previousDefault }.trimEnd('/')
            if (origin != server.trimEnd('/') && batch.status !in setOf("rejected", "succeeded", "legacy_unknown"))
                batch.copy(uploadServer = origin, status = "legacy_unknown", retryable = false,
                    confirmationPending = false,
                    message = "原上传通道已停用，原单据和明细保留待核对；不会转交局域网电脑重复上传。")
            else batch
        }
        if (values != state.batches) persist(state.copy(batches = values))
    }

    private fun merge(values: List<AdjustmentState>, target: String): AdjustmentState {
        val drafts = linkedMapOf<String, AdjustmentDraft>()
        values.flatMap { it.drafts }.forEach { draft ->
            val identity = listOf(draft.type.wire, draft.line.profileId, draft.line.warehouseId, draft.line.productId,
                draft.line.skuId, draft.line.unitId, draft.line.sku.uppercase()).joinToString("|")
            val previous = drafts[identity]
            drafts[identity] = if (previous == null) draft else previous.copy(line = previous.line.copy(
                quantity = adjustmentQuantity((BigDecimal(previous.line.quantity) + BigDecimal(draft.line.quantity)).toPlainString()),
                note = listOf(previous.line.note, draft.line.note).filter(String::isNotBlank).distinct().joinToString("；").take(200)))
        }
        val batches = values.flatMap { it.batches }.distinctBy { it.id }.map { if (it.uploadServer.isBlank()) it.copy(uploadServer = target) else it }
        return AdjustmentState(drafts.values.toList(), batches)
    }

    private fun emit(value: AdjustmentState) { state = value; changed(value) }
    private fun persist(value: AdjustmentState): Boolean {
        if (state.storageError) return false
        if (!preferences.edit().putString(key, AdjustmentJson.encode(value)).commit()) {
            emit(state.copy(storageError = true, syncing = false, message = "清单保存失败，已停止提交；请检查手机存储后重启 App"))
            return false
        }
        emit(value)
        return true
    }

    fun add(item: WorkItem, type: AdjustmentType, quantity: String, note: String): AdjustmentSaveResult {
        if (state.storageError) return AdjustmentSaveResult(false,
            state.message ?: "本地清单无法保存，请检查手机存储后重试")
        return try {
            require(item.sku.isNotBlank()) { "商品编号不能为空" }
            val amount = adjustmentQuantity(quantity)
            val identity = AdjustmentLine(item.sku, item.name, item.spec, item.model, amount, note.trim(),
                item.profileId, item.warehouseId.ifBlank { "29871293290508390" }, item.productId, item.skuId, item.unitId)
            val current = state.drafts.firstOrNull { it.type == type && it.line.identityKey() == identity.identityKey() }
            require(current != null || state.drafts.count { it.type == type } < 100) { "每份清单最多 100 款商品，请先上传现有清单" }
            val combined = listOf(current?.line?.note.orEmpty(), note.trim()).filter(String::isNotBlank).distinct().joinToString("；")
            require(combined.length <= 200) { "合并后的备注不能超过 200 字，请精简备注" }
            val total = adjustmentQuantity((BigDecimal(current?.line?.quantity ?: "0") + BigDecimal(amount)).toPlainString())
            val row = AdjustmentDraft(type, identity.copy(quantity = total, note = combined))
            val drafts = if (current == null) state.drafts + row else state.drafts.map { if (it == current) row else it }
            val saved = persist(state.copy(drafts = drafts, message = "已加入${type.label}清单，到设备页统一上传"))
            AdjustmentSaveResult(saved, state.message.orEmpty())
        } catch (error: Exception) {
            val message = error.message ?: "未能保存本地清单，请稍后重试"
            emit(state.copy(message = message))
            AdjustmentSaveResult(false, message)
        }
    }

    fun remove(type: AdjustmentType, identity: String) {
        persist(state.copy(drafts = state.drafts.filterNot { it.type == type &&
            (it.line.identityKey() == identity || it.line.sku == identity) }, message = null))
    }

    fun upload(type: AdjustmentType) {
        if (state.storageError || state.syncing || state.batches.any { it.type == type && it.active }) return
        val items = state.drafts.filter { it.type == type }.map { it.line }
        if (items.isEmpty()) return
        val batch = AdjustmentBatch(UUID.randomUUID().toString(), clientId, type, items, uploadServer = server)
        // One atomic local write freezes rows before any request can leave the phone.
        if (persist(state.copy(drafts = state.drafts.filterNot { it.type == type }, batches = state.batches + batch, message = null))) sync()
    }

    fun retry(id: String) {
        if (state.storageError || state.syncing) return
        val batch = state.batches.find { it.id == id && it.status == "failed" && it.retryable } ?: return
        sync(retryId = batch.id)
    }

    fun refresh() {
        if (!state.storageError && !state.syncing && state.batches.isNotEmpty()) {
            val confirmations = state.batches.filter { it.confirmationPending }.map { it.id }.toSet()
            sync(confirmationIds = confirmations.takeIf { it.isNotEmpty() })
        }
    }

    fun confirm(id: String) {
        if (state.storageError || state.syncing) return
        val batch = state.batches.find { it.id == id && it.status == "unknown" && !it.confirmationPending } ?: return
        if (persist(state.copy(batches = state.batches.map {
            if (it.id == batch.id) it.copy(confirmationPending = true, message = "正在保存人工核对结果") else it
        }, message = null))) sync(confirmationIds = setOf(batch.id))
    }

    /** Only a definite pre-enqueue HTTP validation rejection can become editable again. */
    fun restore(id: String) {
        if (state.storageError) return
        val batch = state.batches.find { it.id == id && it.status == "rejected" } ?: return
        try {
            val drafts = state.drafts.toMutableList()
            batch.items.forEach { line ->
                val index = drafts.indexOfFirst { it.type == batch.type && it.line.identityKey() == line.identityKey() }
                val previous = drafts.getOrNull(index)?.line
                val amount = adjustmentQuantity((BigDecimal(previous?.quantity ?: "0") + BigDecimal(line.quantity)).toPlainString())
                val note = listOf(previous?.note.orEmpty(), line.note).filter(String::isNotBlank).distinct().joinToString("；")
                require(note.length <= 200) { "有同商品备注过长，请先处理待上传草稿再退回" }
                val draft = AdjustmentDraft(batch.type, line.copy(quantity = amount, note = note))
                if (index < 0) drafts += draft else drafts[index] = draft
            }
            require(drafts.count { it.type == batch.type } <= 100) { "退回后超过 100 款，请先处理该类型草稿再退回" }
            persist(state.copy(drafts = drafts, batches = state.batches.filterNot { it.id == id },
                message = "服务未接收该清单：${batch.message}。已退回待上传清单，请修改后再提交。"))
        } catch (error: Exception) { emit(state.copy(message = error.message)) }
    }

    private fun sync(retryId: String? = null, confirmationIds: Set<String>? = null) {
        val revision = generation
        emit(state.copy(syncing = true, message = null))
        syncJob = scope.launch {
            try {
                var retry = retryId
                do {
                    val frozen = state.batches.filter { confirmationIds == null || it.id in confirmationIds }
                    for (batch in frozen) {
                        if (batch.status in setOf("rejected", "legacy_unknown")) continue
                        val repository = repositoryFor(batch.uploadServer.ifBlank { server })
                        val result = try {
                            when {
                                batch.confirmationPending -> repository.confirmAdjustment(batch.id, batch.clientId)
                                retry == batch.id -> repository.retryAdjustment(batch.id, batch.clientId).also { retry = null }
                                batch.status == "submitting" -> repository.submitAdjustment(batch)
                                else -> repository.adjustment(batch.id, batch.clientId)
                            }
                        } catch (failure: ServiceException) {
                            if (revision == generation && batch.status == "submitting" && failure.code == "invalid_request") {
                                if (persist(state.copy(batches = state.batches.map { if (it.id == batch.id) it.copy(status = "rejected", message = failure.message.orEmpty()) else it }))) restore(batch.id)
                                continue
                            }
                            throw failure
                        }
                        if (revision != generation) return@launch
                        require(result.id == batch.id && result.clientId == batch.clientId && result.type == batch.type) { "服务器返回的清单不匹配，已保留原记录" }
                        require(result.status in setOf("queued", "running", "succeeded", "failed", "unknown") &&
                            result.items.associate { it.sku.uppercase() to adjustmentQuantity(it.quantity) } == batch.items.associate { it.sku.uppercase() to adjustmentQuantity(it.quantity) } && result.items.size == batch.items.size) {
                            "服务器返回的明细不匹配，已保留原记录"
                        }
                        val done = result.status == "succeeded"
                        val updated = if (done) state.batches.filterNot { it.id == batch.id }
                            else state.batches.map { if (it.id == batch.id) it.copy(status = result.status, message = result.message, documentNo = result.documentNo,
                                retryable = result.retryable, completionSource = result.completionSource) else it }
                        val completedMessage = if (result.completionSource == "user_verified") "已保存人工核对结果，${result.type.label}清单已收起"
                            else "${result.type.label}清单已上传为 ERP 草稿，尚未过账；可销售库存正在同步"
                        if (!persist(state.copy(batches = updated, message = if (done) completedMessage else state.message))) return@launch
                        if (done && result.completionSource != "user_verified") uploaded(batch.items.mapNotNull { it.productId.takeIf(String::isNotBlank) }.toSet())
                    }
                    if (state.batches.none { it.active && (confirmationIds == null || it.id in confirmationIds) }) break
                    delay(2500)
                } while (revision == generation)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) {
                if (revision == generation) emit(state.copy(message = if (confirmationIds != null)
                    "核对结果暂未保存，清单已保留。恢复连接后点刷新继续确认。"
                    else "暂时无法确认上传进度，清单已保留。恢复连接后点刷新，不会另建重复单据。"))
            } finally {
                if (revision == generation) emit(state.copy(syncing = false))
            }
        }
    }
}
