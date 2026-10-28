package com.ul.workbench

import com.ul.workbench.data.*
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.*
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.net.ConnectException

@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class PurchaseUnfinishedFeatureTest {
    private fun line(version: Int = 4) = PurchaseUnfinishedJson.line(JSONObject("""{
      "line_key":"line-1","line_number":1,"supplier_code":"S01","supplier_name":"供应商甲",
      "order_no":"PO1","product_code":"FB0001","product_name":"螺栓","warehouse":"主仓",
      "order_date":"2026-10-01","arrival_date":"","ordered_quantity":"10","completed_quantity":"2",
      "unfinished_quantity":"8","unit":"","identity_ambiguous":false,"promise_version":$version
    }"""))
    private class Fake : WorkbenchRepository by HttpWorkbenchRepository(baseUrl = { "http://127.0.0.1" }) {
        var meta = PurchaseSnapshotMeta(status="ready", snapshotId="snapshot-1", capturedAt="2026-10-06T00:00:00Z",
            accountScope="account-1", accountScopeVerified=true, stale=false)
        var lines = emptyList<PurchaseUnfinishedLine>()
        var pageTwoSnapshot: String? = null
        var fail: Exception? = null
        var gate: CompletableDeferred<Unit>? = null
        var statusGate: CompletableDeferred<Unit>? = null
        var statusMeta: PurchaseSnapshotMeta? = null
        val requests = mutableListOf<String>()
        val reads = mutableListOf<Triple<String,Int,String>>()
        override suspend fun purchaseUnfinishedStatus(): PurchaseSnapshotMeta {
            val result = statusMeta ?: meta
            statusGate?.await()
            return result
        }
        override suspend fun purchaseSuppliers(): PurchaseSuppliers {
            gate?.await()
            return PurchaseSuppliers(meta, listOf(PurchaseSupplier("S01","供应商甲",lines.size,1)))
        }
        override suspend fun purchaseUnfinished(supplierCode: String, page: Int, orderNo: String): PurchaseUnfinishedPage {
            reads += Triple(supplierCode,page,orderNo)
            return PurchaseUnfinishedPage(if(page>1 && pageTwoSnapshot!=null) meta.copy(snapshotId=pageTwoSnapshot!!) else meta,
                lines,page,if(pageTwoSnapshot!=null)100 else lines.size, page==1 && pageTwoSnapshot!=null)
        }
        override suspend fun purchaseReminder(supplierCode: String) = PurchaseReminderGate(meta.snapshotId,
            "单位和覆盖待核验",sendAllowed=false,draftOnly=true)
        override suspend fun savePurchasePromise(lineKey: String, frozenRequest: String) {
            requests += frozenRequest
            fail?.let { throw it }
        }
    }
    private class Fixture(val repository: Fake, scope: CoroutineScope) {
        var state = PurchaseUnfinishedState()
        var actorContext = "https://service\nbuyer-1"
        var allowed = true
        val disk = mutableMapOf<String,String>()
        private val scope = scope
        fun controller() = PurchaseUnfinishedController(repository,scope,{state},{state=it(state)},
            {actorContext},{allowed},{disk[it]},{key,value -> if(value==null)disk.remove(key)else disk[key]=value;true})
    }

    @Test fun parserDoesNotInventVerifiedUnitsOrAttachHistoricalReply() {
        val meta = PurchaseUnfinishedJson.meta(JSONObject("""{"status":"ready","snapshot_id":"s1",
          "collector":{"status":"needs_human","reason":"captcha_required"}}"""))
        assertFalse(meta.unitsVerified); assertFalse(meta.coverageComplete); assertFalse(meta.accountScopeVerified)
        assertEquals("needs_human",meta.collectorStatus)
        val row = PurchaseUnfinishedJson.line(JSONObject("""{"line_key":"a","promised_date":"",
          "prior_promised_date":"2026-10-12","prior_promise_note":"旧供应商回复",
          "promise_requires_confirmation":true,"source_snapshot_id":"old","promise_version":7}"""))
        assertEquals("",row.promisedDate)
        assertEquals("2026-10-12",row.priorPromisedDate)
        assertEquals(7,row.promiseVersion)
        assertTrue(row.ambiguous); assertTrue(row.promiseRequiresConfirmation)
        val gate=PurchaseUnfinishedJson.reminder(JSONObject("{}"))
        assertFalse(gate.sendAllowed);assertTrue(gate.draftOnly)
    }

    @Test fun internalTrialParsesAsReadableButNeverAllowsReplyWrites() = runTest {
        val raw = JSONObject("""{"status":"review","publication_stage":"internal_trial",
          "snapshot_id":"trial-1","account_scope":"account-1","account_scope_verified":true,
          "row_count":1,"supplier_count":1}""")
        val parsed = PurchaseUnfinishedJson.meta(raw)
        assertTrue(parsed.readable)
        assertTrue(parsed.internalTrial)
        assertFalse(parsed.canRecordReply)

        val repo = Fake().apply {
            meta = meta.copy(status="review", publicationStage="internal_trial")
            lines = listOf(line())
        }
        val fixture = Fixture(repo, this)
        val controller = fixture.controller()
        controller.open(); advanceUntilIdle()
        assertEquals("供应商甲", fixture.state.suppliers.single().name)
        controller.supplier(fixture.state.suppliers.single()); advanceUntilIdle()
        assertEquals("PO1", fixture.state.page.items.single().orderNo)
        controller.edit(fixture.state.page.items.single())
        assertNull(fixture.state.editor)

        val oldDraft = PurchasePromiseDraft(repo.meta.snapshotId, repo.meta.accountScope,
            fixture.state.page.items.single(), promisedDate="2026-10-20", note="试用前保留的草稿")
        fixture.disk[fixture.actorContext] = PurchaseUnfinishedJson.draftJson(oldDraft)
        fixture.state = PurchaseUnfinishedState()
        controller.open(); advanceUntilIdle()
        assertNotNull(fixture.state.editor)
        controller.resume(); controller.save(); advanceUntilIdle()
        assertFalse(fixture.state.editorOpen)
        assertTrue(repo.requests.isEmpty())
        assertEquals("试用前保留的草稿", fixture.state.editor!!.note)
    }

    @Test fun snapshotConflictKeepsInputAndRequiresExplicitRebindWithANewKey() = runTest {
        val repo=Fake().apply{lines=listOf(line())}
        val fixture=Fixture(repo,this);val controller=fixture.controller()
        controller.open();advanceUntilIdle()
        controller.supplier(fixture.state.suppliers.single());advanceUntilIdle()
        controller.edit(fixture.state.page.items.single())
        controller.change("2026-10-20","电话确认，需下午送达")
        repo.fail=ServiceException("snapshot_version_changed","采购快照已更新",409)
        controller.save();advanceUntilIdle()
        assertTrue(fixture.state.editor!!.requiresReview)
        assertEquals("2026-10-20",fixture.state.editor!!.promisedDate)
        assertEquals("电话确认，需下午送达",fixture.state.editor!!.note)
        controller.save();advanceUntilIdle()
        assertEquals(1,repo.requests.size)
        repo.meta=repo.meta.copy(snapshotId="snapshot-2")
        repo.lines=listOf(line(5).copy(unfinished="6"))
        controller.review();advanceUntilIdle()
        assertEquals("snapshot-1",fixture.state.editor!!.snapshotId)
        assertEquals("6",fixture.state.editor!!.current!!.line.unfinished)
        assertEquals("PO1",repo.reads.last().third)
        assertEquals(1,repo.requests.size)
        controller.acceptReview()
        assertEquals("snapshot-2",fixture.state.editor!!.snapshotId)
        assertEquals("2026-10-20",fixture.state.editor!!.promisedDate)
        repo.fail=null
        controller.save();advanceUntilIdle()
        val first=JSONObject(repo.requests[0]);val second=JSONObject(repo.requests[1])
        assertEquals("snapshot-1",first.getString("snapshot_id"));assertEquals(4,first.getInt("version"))
        assertEquals("snapshot-2",second.getString("snapshot_id"));assertEquals(5,second.getInt("version"))
        assertNotEquals(first.getString("idempotency_key"),second.getString("idempotency_key"))
        assertEquals("电话确认，需下午送达",second.getString("note"))
        assertNull(fixture.state.editor);assertTrue(fixture.disk.isEmpty())
    }

    @Test fun timeoutAndRestartRetryExactlyTheDurableRequestWithoutLosingInput() = runTest {
        val repo=Fake().apply{lines=listOf(line())}
        val fixture=Fixture(repo,this);val controller=fixture.controller()
        controller.open();advanceUntilIdle()
        controller.supplier(fixture.state.suppliers.single());advanceUntilIdle()
        controller.edit(fixture.state.page.items.single());controller.change("2026-10-21","供应商微信回复")
        repo.fail=ConnectException("连接中断")
        controller.save();advanceUntilIdle()
        val frozen=repo.requests.single()
        assertTrue(fixture.state.editor!!.outcomeUncertain)
        controller.change("2026-10-25","不应替换原提交");controller.discard()
        assertEquals("2026-10-21",fixture.state.editor!!.promisedDate)
        assertFalse(fixture.disk.isEmpty())
        fixture.state=PurchaseUnfinishedState()
        repo.meta=repo.meta.copy(snapshotId="snapshot-2")
        val restarted=fixture.controller()
        restarted.open();advanceUntilIdle()
        assertEquals(frozen,fixture.state.editor!!.frozenRequest)
        assertTrue(fixture.state.editor!!.outcomeUncertain)
        repo.fail=null
        restarted.save();advanceUntilIdle()
        assertEquals(listOf(frozen,frozen),repo.requests)
        assertTrue(fixture.disk.isEmpty());assertNull(fixture.state.editor)
        assertEquals("回复记录已保存，请核对刷新后的当前明细",fixture.state.message)
    }

    @Test fun changingSnapshotDuringPaginationDoesNotMixGenerations() = runTest {
        val repo=Fake().apply{lines=listOf(line());pageTwoSnapshot="snapshot-2"}
        val fixture=Fixture(repo,this);val controller=fixture.controller()
        controller.open();advanceUntilIdle()
        controller.supplier(fixture.state.suppliers.single());advanceUntilIdle()
        val before=fixture.state.page
        controller.more();advanceUntilIdle()
        assertEquals(before,fixture.state.page)
        assertTrue(fixture.state.error.orEmpty().contains("快照已更新"))
        assertFalse(fixture.state.loadingMore)
    }

    @Test fun openClearsPriorContextListsAndDraftButCanRestoreOriginalUsersDraft() = runTest {
        val repo=Fake().apply{lines=listOf(line())}
        val fixture=Fixture(repo,this);val controller=fixture.controller()
        controller.open();advanceUntilIdle()
        controller.supplier(fixture.state.suppliers.single());advanceUntilIdle()
        controller.edit(fixture.state.page.items.single());controller.change("2026-10-21","仅属于原账号")
        val oldContext=fixture.actorContext
        fixture.actorContext="https://different-service\nother-buyer"
        // Even without a host ViewModel reset, the controller cannot reuse old UI data.
        controller.save();advanceUntilIdle()
        assertTrue(repo.requests.isEmpty())
        repo.gate=CompletableDeferred()
        controller.open();runCurrent()
        assertNull(fixture.state.editor);assertNull(fixture.state.selectedSupplier)
        assertTrue(fixture.state.page.items.isEmpty());assertTrue(fixture.state.suppliers.isEmpty())
        assertEquals(fixture.actorContext,fixture.state.sessionContext)
        repo.gate!!.complete(Unit);advanceUntilIdle()
        fixture.actorContext=oldContext
        controller.open();advanceUntilIdle()
        assertEquals("仅属于原账号",fixture.state.editor!!.note)
        assertEquals(oldContext,fixture.state.sessionContext)
    }

    @Test fun collectorHumanVerificationIsVisibleWithoutAnyPublishedSnapshot() = runTest {
        val repo=Fake().apply{meta=PurchaseSnapshotMeta(status="unavailable",collectorStatus="needs_human",collectorReason="captcha_required")}
        val fixture=Fixture(repo,this);val controller=fixture.controller()
        controller.open();advanceUntilIdle()
        assertEquals("needs_human",fixture.state.meta.collectorStatus)
        assertFalse(fixture.state.meta.readable)
        assertTrue(fixture.state.suppliers.isEmpty())
        assertFalse(fixture.state.loading)
    }

    @Test fun supplierRefreshUpdatesCollectorStatusAndIgnoresLatePriorContextResponse() = runTest {
        val repo=Fake().apply{lines=listOf(line())}
        val fixture=Fixture(repo,this);val controller=fixture.controller()
        controller.open();advanceUntilIdle()
        controller.supplier(fixture.state.suppliers.single());advanceUntilIdle()
        repo.statusMeta=repo.meta.copy(collectorStatus="needs_human",collectorReason="captcha_required")
        controller.refresh();advanceUntilIdle()
        assertEquals("needs_human",fixture.state.meta.collectorStatus)
        assertEquals("captcha_required",fixture.state.meta.collectorReason)
        assertEquals("snapshot-1",fixture.state.page.meta.snapshotId)
        repo.statusMeta=repo.meta.copy(collectorStatus="failed",collectorReason="download_timeout")
        repo.statusGate=CompletableDeferred()
        val readsBefore=repo.reads.size
        controller.refresh();runCurrent()
        fixture.actorContext="https://different-service\nother-user"
        fixture.state=PurchaseUnfinishedState()
        repo.statusGate!!.complete(Unit);advanceUntilIdle()
        assertEquals(readsBefore,repo.reads.size)
        assertEquals("",fixture.state.meta.collectorStatus)
        assertTrue(fixture.state.page.items.isEmpty())
    }

    @Test fun lateResponseCannotLeakPreviousUsersSuppliersAndAmbiguousLineCannotBeEdited() = runTest {
        val repo=Fake().apply{lines=listOf(line());gate=CompletableDeferred()}
        val fixture=Fixture(repo,this);val controller=fixture.controller()
        controller.open();runCurrent()
        fixture.actorContext="https://service\nother-user"
        fixture.state=PurchaseUnfinishedState()
        repo.gate!!.complete(Unit);advanceUntilIdle()
        assertTrue(fixture.state.suppliers.isEmpty())
        controller.open();advanceUntilIdle()
        controller.supplier(fixture.state.suppliers.single());advanceUntilIdle()
        controller.edit(line().copy(ambiguous=true))
        assertNull(fixture.state.editor)
        fixture.allowed=false
        controller.edit(line())
        assertNull(fixture.state.editor)
    }
}
