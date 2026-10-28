package com.ul.workbench

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import com.ul.workbench.data.*
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.net.SocketTimeoutException

@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
class AdjustmentOutboxTest {
    private val item = WorkItem("i1", "LS0001", "螺栓", spec = "DIN933  M12*40", model = "8.8", stock = "10")
    private val prefs get() = ApplicationProvider.getApplicationContext<Application>().getSharedPreferences("outbox-tests", 0)
    @Before fun reset() { prefs.edit().clear().commit() }

    @Test fun profileWarehouseBucketMigratesLegacyDraftOnceAndKeepsStableIdentity() = runTest {
        val repo = FakeRepository()
        val legacy = AdjustmentOutbox(prefs, this, { repo }, {})
        legacy.switchServer("https://ul.yingyinghome.com:8443")
        legacy.add(item.copy(profileId = "profile-a", warehouseId = "29871293290508390", productId = "p1", skuId = "s1", unitId = "u1"),
            AdjustmentType.Loss, "2", "旧草稿")

        val migrated = AdjustmentOutbox(prefs, this, { repo }, {})
        migrated.useProfileBucket("profile-a", "http://192.168.1.5:5191", listOf("https://ul.yingyinghome.com:8443"))
        assertEquals(1, migrated.state.drafts.size)
        assertEquals("p1", migrated.state.drafts.single().line.productId)
        assertEquals("29871293290508390", migrated.state.drafts.single().line.warehouseId)

        val restarted = AdjustmentOutbox(prefs, this, { repo }, {})
        restarted.useProfileBucket("profile-a", "http://192.168.1.5:5191", listOf("https://ul.yingyinghome.com:8443"))
        assertEquals("2", restarted.state.drafts.single().line.quantity)
    }

    @Test fun resolvingRealProfileAfterStartupDoesNotDoubleLegacyDrafts() = runTest {
        val repo = FakeRepository()
        val legacy = AdjustmentOutbox(prefs, this, { repo }, {})
        legacy.switchServer("https://ul.yingyinghome.com:8443")
        legacy.add(item, AdjustmentType.Loss, "2", "旧草稿")
        val current = AdjustmentOutbox(prefs, this, { repo }, {})
        current.useProfileBucket("default", "http://192.168.1.5:5191", listOf("https://ul.yingyinghome.com:8443"))
        assertEquals("2", current.state.drafts.single().line.quantity)
        current.useProfileBucket("profile-a", "http://192.168.1.5:5191", listOf("https://ul.yingyinghome.com:8443"))
        assertEquals("2", current.state.drafts.single().line.quantity)
    }

    @Test fun movingToN150PreservesOldUnknownAndNeverResendsIt() = runTest {
        val repo = FakeRepository()
        repo.submit = { throw SocketTimeoutException("lost") }
        val legacy = AdjustmentOutbox(prefs, this, { repo }, {})
        legacy.useProfileBucket("profile-a", "http://192.168.1.5:5191")
        legacy.add(item, AdjustmentType.Loss, "2", "")
        legacy.upload(AdjustmentType.Loss)
        advanceUntilIdle()
        assertEquals(1, repo.requests.size)
        val oldId = legacy.state.batches.single().id
        legacy.add(item, AdjustmentType.Overflow, "1", "new draft")
        val current = AdjustmentOutbox(prefs, this, { repo }, {})
        current.useProfileBucket("profile-a", "https://ul.yingyinghome.com:8443")
        current.holdLegacyBatches("http://192.168.1.5:5191")
        assertEquals(oldId, current.state.batches.single().id)
        assertEquals("legacy_unknown", current.state.batches.single().status)
        current.refresh(); advanceUntilIdle()
        assertEquals(1, repo.requests.size)
        repo.submit = { it.copy(status = "succeeded") }
        current.upload(AdjustmentType.Overflow); advanceUntilIdle()
        assertEquals(2, repo.requests.size)
        assertEquals("https://ul.yingyinghome.com:8443", repo.requests.last().uploadServer)
        assertEquals(oldId, current.state.batches.single().id)
    }

    private class FakeRepository : WorkbenchRepository {
        val requests = mutableListOf<AdjustmentBatch>()
        val records = mutableMapOf<String, AdjustmentBatch>()
        var retries = 0
        var confirmations = 0
        var submit: (AdjustmentBatch) -> AdjustmentBatch = { it.copy(status = "succeeded") }
        var get: (AdjustmentBatch) -> AdjustmentBatch = { it.copy(status = "succeeded") }
        var confirm: (AdjustmentBatch) -> AdjustmentBatch = { it.copy(status = "succeeded", completionSource = "user_verified") }
        override suspend fun submitAdjustment(batch: AdjustmentBatch): AdjustmentBatch { requests += batch; return submit(batch) }
        override suspend fun adjustment(id: String, clientId: String) = get(records.getValue(id))
        override suspend fun retryAdjustment(id: String, clientId: String): AdjustmentBatch { retries++; return records.getValue(id).copy(status = "succeeded") }
        override suspend fun confirmAdjustment(id: String, clientId: String): AdjustmentBatch { confirmations++; return confirm(records.getValue(id)) }
        override suspend fun health() = error("unused")
        override suspend fun query(destination: Destination, q: String, mode: String, scope: String, logistics: String, page: Int, inventoryFilters: InventoryFilters, expectedDataVersion: String) = error("unused")
        override suspend fun prepare(state: WorkbenchState, all: Boolean, maximum: Int?) = error("unused")
        override suspend fun claim(id: String, actionKey: String) = error("unused")
        override suspend fun report(id: String, token: String, status: String, message: String, bytes: Int) = Unit
    }

    @Test fun returningToLanHoldsN150AttemptsAcrossRestartAndOnlyMovesDrafts() = runTest {
        val repo = FakeRepository().apply { submit = { throw SocketTimeoutException() } }
        val n150 = "https://ul.yingyinghome.com:8443"
        val original = AdjustmentOutbox(prefs, this, { repo }, {})
        original.useProfileBucket("profile-a", n150)
        original.add(item, AdjustmentType.Loss, "2", "原报损")
        original.upload(AdjustmentType.Loss); advanceUntilIdle()
        val frozen = original.state.batches.single()
        original.add(item, AdjustmentType.Overflow, "3", "新草稿")
        val routed = mutableListOf<String>()
        val current = AdjustmentOutbox(prefs, this, { routed += it; repo }, {})
        current.useProfileBucket("profile-a", LAN_ADJUSTMENT_SERVER)
        current.holdBatchesOutsideServer(n150)
        val held = current.state.batches.single()
        assertEquals(frozen.id, held.id)
        assertEquals(frozen.items, held.items)
        assertEquals(n150, held.uploadServer)
        assertEquals("legacy_unknown", held.status)
        current.refresh(); current.retry(held.id); current.confirm(held.id); advanceUntilIdle()
        assertTrue(routed.isEmpty())
        assertEquals(1, repo.requests.size)
        val restarted = AdjustmentOutbox(prefs, this, { routed += it; repo }, {})
        restarted.useProfileBucket("profile-a", LAN_ADJUSTMENT_SERVER)
        restarted.holdBatchesOutsideServer(n150)
        repo.submit = { it.copy(status = "succeeded") }
        restarted.upload(AdjustmentType.Overflow); advanceUntilIdle()
        assertEquals(listOf(LAN_ADJUSTMENT_SERVER), routed)
        assertEquals("3", repo.requests.last().items.single().quantity)
        assertEquals(held, restarted.state.batches.single())
    }

    @Test fun addingAndMergingAreOfflineAndSurviveRecreationPerServer() = runTest {
        val repo = FakeRepository()
        val outbox = AdjustmentOutbox(prefs, this, { repo }, {})
        outbox.switchServer("http://192.168.1.5:5191")
        outbox.add(item, AdjustmentType.Loss, "1.5", "破损")
        outbox.add(item, AdjustmentType.Loss, "2.50", "")
        outbox.add(item, AdjustmentType.Overflow, "2", "盘点")
        assertEquals(listOf("4", "2"), outbox.state.drafts.map { it.line.quantity })
        assertTrue(repo.requests.isEmpty())
        val restored = AdjustmentOutbox(prefs, this, { repo }, {})
        restored.switchServer("http://192.168.1.5:5191")
        assertEquals(outbox.state.drafts, restored.state.drafts)
        restored.switchServer("http://127.0.0.1:5191")
        assertTrue(restored.state.drafts.isEmpty())
        restored.switchServer("http://192.168.1.5:5191")
        assertEquals(2, restored.state.drafts.size)
    }

    @Test fun lossUploadIsOneBatchAndSuccessPreservesOverflowAndNewDrafts() = runTest {
        val repo = FakeRepository()
        repo.submit = { batch -> batch.copy(status = "running").also { repo.records[it.id] = it } }
        val outbox = AdjustmentOutbox(prefs, this, { repo }, {})
        outbox.switchServer("server")
        outbox.add(item, AdjustmentType.Loss, "2", "")
        outbox.add(item.copy(id = "i2", sku = "LS0002"), AdjustmentType.Loss, "1", "")
        outbox.add(item, AdjustmentType.Overflow, "3", "")
        outbox.upload(AdjustmentType.Loss)
        outbox.upload(AdjustmentType.Loss) // second tap while the outbox is in flight
        runCurrent()
        outbox.add(item, AdjustmentType.Loss, "7", "新盘点")
        advanceUntilIdle()
        assertEquals(1, repo.requests.size)
        assertEquals(2, repo.requests.single().items.size)
        assertTrue(outbox.state.batches.isEmpty())
        assertEquals(setOf("3", "7"), outbox.state.drafts.map { it.line.quantity }.toSet())
        assertTrue(outbox.state.message!!.contains("已上传"))
    }

    @Test fun lostAcknowledgementAndRestartResubmitIdenticalFrozenIdOnly() = runTest {
        val repo = FakeRepository()
        var erpCalls = 0
        repo.submit = { batch ->
            if (batch.id !in repo.records) {
                erpCalls++
                repo.records[batch.id] = batch.copy(status = "succeeded")
                throw SocketTimeoutException()
            }
            repo.records.getValue(batch.id)
        }
        val outbox = AdjustmentOutbox(prefs, this, { repo }, {})
        outbox.switchServer("server")
        outbox.add(item, AdjustmentType.Loss, "3", "盘点")
        outbox.upload(AdjustmentType.Loss)
        advanceUntilIdle()
        val frozen = outbox.state.batches.single()
        assertEquals("submitting", frozen.status)
        val restarted = AdjustmentOutbox(prefs, this, { repo }, {})
        restarted.switchServer("server")
        restarted.refresh()
        advanceUntilIdle()
        assertEquals(1, erpCalls)
        assertEquals(listOf(frozen.id, frozen.id), repo.requests.map { it.id })
        assertTrue(restarted.state.batches.isEmpty())
    }

    @Test fun unknownRetainedAcrossRestartAndNeverRetried() = runTest {
        val repo = FakeRepository()
        repo.submit = { it.copy(status = "unknown", message = "请核对 ERP").also { row -> repo.records[row.id] = row } }
        repo.get = { it }
        val outbox = AdjustmentOutbox(prefs, this, { repo }, {})
        outbox.switchServer("server")
        outbox.add(item, AdjustmentType.Overflow, "1", "")
        outbox.upload(AdjustmentType.Overflow)
        advanceUntilIdle()
        val batch = outbox.state.batches.single()
        outbox.retry(batch.id)
        outbox.refresh()
        advanceUntilIdle()
        assertEquals(0, repo.retries)
        assertEquals(1, repo.requests.size)
        assertEquals("unknown", outbox.state.batches.single().status)
        val restarted = AdjustmentOutbox(prefs, this, { repo }, {})
        restarted.switchServer("server")
        assertEquals(batch, restarted.state.batches.single())
    }

    @Test fun explicitSafeFailureCanBeRetriedButNotAutomatically() = runTest {
        val repo = FakeRepository()
        repo.submit = { it.copy(status = "failed", retryable = true).also { row -> repo.records[row.id] = row } }
        val outbox = AdjustmentOutbox(prefs, this, { repo }, {})
        outbox.switchServer("server")
        outbox.add(item, AdjustmentType.Loss, "1", "")
        outbox.upload(AdjustmentType.Loss)
        advanceUntilIdle()
        assertEquals(0, repo.retries)
        val id = outbox.state.batches.single().id
        outbox.retry(id)
        advanceUntilIdle()
        assertEquals(1, repo.retries)
        assertTrue(outbox.state.batches.isEmpty())
    }

    @Test fun malformedStorageIsPreservedAndCannotBeOverwrittenOrSubmitted() = runTest {
        val repo = FakeRepository()
        val outbox = AdjustmentOutbox(prefs, this, { repo }, {})
        outbox.switchServer("server")
        outbox.add(item, AdjustmentType.Loss, "1", "")
        val key = prefs.all.keys.single { it.startsWith("adjustment-outbox-") }
        prefs.edit().putString(key, "broken-original-data").commit()
        outbox.switchServer("server")
        assertTrue(outbox.state.storageError)
        outbox.add(item, AdjustmentType.Loss, "5", "")
        outbox.upload(AdjustmentType.Loss)
        outbox.remove(AdjustmentType.Loss, item.sku)
        advanceUntilIdle()
        assertEquals("broken-original-data", prefs.getString(key, ""))
        assertTrue(repo.requests.isEmpty())
    }

    @Test fun invalidQuantitiesNeverEnterDraftsAndDecimalMergingHasNoRoundingError() = runTest {
        val outbox = AdjustmentOutbox(prefs, this, { FakeRepository() }, {})
        outbox.switchServer("server")
        listOf("0", "-1", "NaN", "Infinity", "1e3", "100000001", "1.0000001", "").forEach {
            outbox.add(item, AdjustmentType.Loss, it, "")
        }
        assertTrue(outbox.state.drafts.isEmpty())
        outbox.add(item, AdjustmentType.Loss, "0.1", "")
        outbox.add(item, AdjustmentType.Loss, "0.2", "")
        assertEquals("0.3", outbox.state.drafts.single().line.quantity)
    }

    @Test fun frozenPayloadRoundTripsExactlyWithoutStatusInRequest() {
        val value = AdjustmentBatch("id", "client", AdjustmentType.Loss, listOf(AdjustmentLine("LS0001", "螺栓", "DIN933  M12*40", "8.8", "2.5", "盘点")))
        assertEquals(value, AdjustmentJson.batch(AdjustmentJson.batch(value)))
        assertFalse(AdjustmentJson.payload(value).has("status"))
        assertEquals(value.items.single(), AdjustmentJson.line(AdjustmentJson.line(value.items.single())))
    }

    @Test fun definiteValidationRejectionReturnsEditableDraftAndDoesNotKeepResubmitting() = runTest {
        val repo = FakeRepository()
        repo.submit = { throw ServiceException("invalid_request", "商品不在当前库存数据中，请重新查询") }
        val outbox = AdjustmentOutbox(prefs, this, { repo }, {})
        outbox.switchServer("server")
        outbox.add(item, AdjustmentType.Loss, "2", "")
        outbox.upload(AdjustmentType.Loss)
        outbox.add(item, AdjustmentType.Loss, "3", "后来盘点")
        advanceUntilIdle()
        assertTrue(outbox.state.batches.isEmpty())
        assertEquals("5", outbox.state.drafts.single().line.quantity)
        assertTrue(outbox.state.message!!.contains("退回"))
        outbox.refresh()
        advanceUntilIdle()
        assertEquals(1, repo.requests.size)
        outbox.remove(AdjustmentType.Loss, item.sku)
        assertTrue(outbox.state.drafts.isEmpty())
    }

    @Test fun mismatchedResponseCannotClearOrReplaceFrozenItems() = runTest {
        val repo = FakeRepository()
        repo.submit = { it.copy(status = "succeeded", items = it.items.map { line -> line.copy(quantity = "99") }) }
        val outbox = AdjustmentOutbox(prefs, this, { repo }, {})
        outbox.switchServer("server")
        outbox.add(item, AdjustmentType.Loss, "2", "")
        outbox.upload(AdjustmentType.Loss)
        advanceUntilIdle()
        assertEquals("submitting", outbox.state.batches.single().status)
        assertEquals("2", outbox.state.batches.single().items.single().quantity)
    }

    @Test fun confirmingUnknownClearsOnlyThatBatchAndNeverUploadsAgain() = runTest {
        val repo = FakeRepository()
        repo.submit = { it.copy(status = "unknown").also { batch -> repo.records[batch.id] = batch } }
        val outbox = AdjustmentOutbox(prefs, this, { repo }, {})
        outbox.switchServer("server")
        outbox.add(item, AdjustmentType.Loss, "2", "")
        outbox.upload(AdjustmentType.Loss)
        advanceUntilIdle()
        outbox.add(item, AdjustmentType.Overflow, "3", "新盘点")
        val id = outbox.state.batches.single().id
        outbox.confirm(id)
        outbox.confirm(id)
        advanceUntilIdle()
        assertEquals(1, repo.requests.size)
        assertEquals(1, repo.confirmations)
        assertEquals(0, repo.retries)
        assertTrue(outbox.state.batches.isEmpty())
        assertEquals("3", outbox.state.drafts.single().line.quantity)
        assertTrue(outbox.state.message!!.contains("人工核对"))
    }

    @Test fun lostConfirmationAcknowledgementSurvivesRestartWithoutAnotherErpUpload() = runTest {
        val repo = FakeRepository()
        repo.submit = { it.copy(status = "unknown").also { batch -> repo.records[batch.id] = batch } }
        repo.confirm = { batch ->
            if (batch.status == "unknown") {
                repo.records[batch.id] = batch.copy(status = "succeeded", completionSource = "user_verified")
                throw SocketTimeoutException()
            }
            batch
        }
        val outbox = AdjustmentOutbox(prefs, this, { repo }, {})
        outbox.switchServer("server")
        outbox.add(item, AdjustmentType.Loss, "2", "")
        outbox.upload(AdjustmentType.Loss)
        advanceUntilIdle()
        outbox.confirm(outbox.state.batches.single().id)
        advanceUntilIdle()
        assertTrue(outbox.state.batches.single().confirmationPending)
        val restarted = AdjustmentOutbox(prefs, this, { repo }, {})
        restarted.switchServer("server")
        restarted.refresh()
        advanceUntilIdle()
        assertEquals(1, repo.requests.size)
        assertEquals(2, repo.confirmations)
        assertTrue(restarted.state.batches.isEmpty())
    }

    @Test fun confirmingOneUnknownNeverSubmitsAnUnrelatedOfflineBatch() = runTest {
        val repo = FakeRepository()
        repo.get = { it }
        repo.submit = {
            if (it.type == AdjustmentType.Loss) it.copy(status = "unknown").also { batch -> repo.records[batch.id] = batch }
            else throw SocketTimeoutException()
        }
        val outbox = AdjustmentOutbox(prefs, this, { repo }, {})
        outbox.switchServer("server")
        outbox.add(item, AdjustmentType.Loss, "2", "")
        outbox.upload(AdjustmentType.Loss)
        advanceUntilIdle()
        val unknown = outbox.state.batches.single()
        outbox.add(item, AdjustmentType.Overflow, "3", "")
        outbox.upload(AdjustmentType.Overflow)
        advanceUntilIdle()
        val offline = outbox.state.batches.single { it.status == "submitting" }
        val previousCalls = repo.requests.size
        repo.submit = { it.copy(status = "succeeded") }
        outbox.confirm(unknown.id)
        advanceUntilIdle()
        assertEquals(previousCalls, repo.requests.size)
        assertEquals(1, repo.confirmations)
        assertEquals(listOf(offline), outbox.state.batches)
    }
}
