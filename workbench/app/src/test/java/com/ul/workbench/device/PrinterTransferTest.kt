package com.ul.workbench.device

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PrinterTransferTest {
    private class MemoryStore : PrintAttemptStore {
        var value: PrintAttempt? = null
        val saved = mutableListOf<PrintAttempt>()
        var failAfter: Int? = null
        override fun read() = value
        override fun save(attempt: PrintAttempt) {
            if (failAfter != null && saved.size >= failAfter!!) throw IllegalStateException("mock disk full")
            value = attempt
            saved += attempt
        }
    }

    private class Writer(
        override val maxChunkBytes: Int = 180,
        val action: suspend (ByteArray, Int) -> Unit = { _, _ -> },
    ) : PrinterChunkWriter {
        val chunks = mutableListOf<ByteArray>()
        var aborts = 0
        override suspend fun write(bytes: ByteArray) {
            chunks += bytes.copyOf()
            action(bytes, chunks.size)
        }
        override fun abort() { aborts++ }
    }

    @Test fun frozenPagesPreserveEveryByteIncludingSpacesAndQrPayload() = runTest {
        val text = "! 0 200 200 800 1\r\nTEXT 0 0 1 1 DIN933  M12x40\r\nB QR 1 1 M 2 U 6\r\nMA,MOCK SHIPPING 001\r\nENDQR\r\n"
        val first = text.toByteArray() + byteArrayOf(0xc4.toByte(), 0xe3.toByte(), 0xba.toByte(), 0xc3.toByte()) + ByteArray(200) { (it % 256).toByte() }
        val second = "FORM\r\nPRINT\r\n".toByteArray()
        val expected = first.copyOf() + second.copyOf()
        val delays = mutableListOf<Long>()
        val writer = Writer { _, packet -> if (packet == 1) { first.fill(0); second.fill(0) } }
        val store = MemoryStore()
        val progress = mutableListOf<PrintAttempt>()
        val transfer = PrinterTransfer(store, { attempt, _ -> progress += attempt }, { delays += it })
        val outcome = transfer.send(listOf(first, second), writer)
        assertEquals(PrintOutcome.SUBMITTED, outcome.status)
        assertEquals(expected.size, outcome.bytesSent)
        assertArrayEquals(expected, writer.chunks.fold(byteArrayOf()) { all, chunk -> all + chunk })
        assertTrue(writer.chunks.all { it.size <= 180 })
        assertEquals(writer.chunks.size, delays.count { it == 45L })
        assertEquals(1, delays.count { it == 120L })
        assertEquals("sending", store.saved.first().status)
        assertEquals(PrintOutcome.SUBMITTED, store.value!!.status)
        assertTrue(outcome.message.contains("请以实际出纸为准"))
        assertFalse(outcome.message.contains("出纸成功"))
        assertEquals(expected.size, progress.last().bytesSent)
        assertFalse(transfer.busy)
    }

    @Test fun negotiatedSmallMtuUsesSafePayloadInsteadOfTruncating() = runTest {
        val writer = Writer(maxChunkBytes = 20)
        val transfer = PrinterTransfer(MemoryStore(), { _, _ -> }, {})
        assertEquals(PrintOutcome.SUBMITTED, transfer.send(listOf(ByteArray(45)), writer).status)
        assertEquals(listOf(20, 20, 5), writer.chunks.map { it.size })
    }

    @Test fun explicitlyRejectedFirstPacketIsFailedAndNeverRetried() = runTest {
        val writer = Writer { _, _ -> throw PrinterWriteException("mock immediate rejection", accepted = false) }
        val transfer = PrinterTransfer(MemoryStore(), { _, _ -> }, {})
        val result = transfer.send(listOf(ByteArray(400)), writer)
        assertEquals(PrintOutcome.FAILED, result.status)
        assertEquals(0, result.bytesSent)
        assertEquals(1, writer.chunks.size)
    }

    @Test fun acceptedPacketWithFailedCallbackIsUnknownEvenForTheFirstPacket() = runTest {
        val writer = Writer { _, _ -> throw PrinterWriteException("mock GATT callback status 133", accepted = true) }
        val store = MemoryStore()
        val transfer = PrinterTransfer(store, { _, _ -> }, {})
        val result = transfer.send(listOf(ByteArray(400)), writer)
        assertEquals(PrintOutcome.UNKNOWN, result.status)
        assertEquals(180, result.bytesSent)
        assertEquals(1, writer.chunks.size)
        assertEquals(PrintOutcome.UNKNOWN, store.value!!.status)
    }

    @Test fun acceptedTimeoutOrDisconnectIncludesThePendingPacketAndStopsRemainingPages() = runTest {
        val writer = Writer { _, packet -> if (packet == 2) throw PrinterWriteException("mock timeout", accepted = true) }
        val transfer = PrinterTransfer(MemoryStore(), { _, _ -> }, {})
        val result = transfer.send(listOf(ByteArray(500), ByteArray(500)), writer)
        assertEquals(PrintOutcome.UNKNOWN, result.status)
        assertEquals(360, result.bytesSent)
        assertEquals(2, writer.chunks.size)
    }

    @Test fun rejectionAfterAPreviousPacketIsStillUnknown() = runTest {
        val writer = Writer { _, packet -> if (packet == 2) throw PrinterWriteException("mock disconnected before next write") }
        val transfer = PrinterTransfer(MemoryStore(), { _, _ -> }, {})
        val result = transfer.send(listOf(ByteArray(500)), writer)
        assertEquals(PrintOutcome.UNKNOWN, result.status)
        assertEquals(180, result.bytesSent)
        assertEquals(2, writer.chunks.size)
    }

    @Test fun concurrentSubmissionIsRejectedWithoutQueuingOrSendingItLater() = runTest {
        val started = CompletableDeferred<Unit>()
        val gate = CompletableDeferred<Unit>()
        val writer = Writer { _, _ -> started.complete(Unit); gate.await() }
        val transfer = PrinterTransfer(MemoryStore(), { _, _ -> }, {})
        val first = async { transfer.send(listOf(ByteArray(10)), writer) }
        started.await()
        val secondWriter = Writer()
        val second = transfer.send(listOf(ByteArray(15)), secondWriter)
        assertEquals(PrintOutcome.FAILED, second.status)
        assertTrue(secondWriter.chunks.isEmpty())
        gate.complete(Unit)
        assertEquals(PrintOutcome.SUBMITTED, first.await().status)
        assertTrue(secondWriter.chunks.isEmpty())
    }

    @Test fun cancellationStopsRemainingPacketsAndPreservesAnUnknownAttempt() = runTest {
        val writer = Writer()
        val store = MemoryStore()
        lateinit var transfer: PrinterTransfer
        transfer = PrinterTransfer(store, { _, _ -> }, { transfer.cancel() })
        val result = transfer.send(listOf(ByteArray(400), ByteArray(200)), writer)
        assertEquals(PrintOutcome.UNKNOWN, result.status)
        assertEquals(180, result.bytesSent)
        assertEquals(1, writer.chunks.size)
        assertEquals(1, writer.aborts)
        assertEquals(PrintOutcome.UNKNOWN, store.value!!.status)
    }

    @Test fun callerCancellationBeforeFirstWriteNeverTouchesTheDevice() = runTest {
        val writer = Writer()
        val transfer = PrinterTransfer(MemoryStore(), { _, _ -> }, {})
        val result = transfer.send(listOf(ByteArray(400)), writer) { true }
        assertEquals(PrintOutcome.FAILED, result.status)
        assertEquals(0, result.bytesSent)
        assertTrue(writer.chunks.isEmpty())
    }

    @Test fun callerCancellationIsRecheckedBeforeEveryPacketWithoutCancellingTheCoroutine() = runTest {
        var cancelled = false
        val writer = Writer()
        val transfer = PrinterTransfer(MemoryStore(), { _, _ -> }, { cancelled = true })
        val result = transfer.send(listOf(ByteArray(400)), writer) { cancelled }
        assertEquals(PrintOutcome.UNKNOWN, result.status)
        assertEquals(180, result.bytesSent)
        assertEquals(1, writer.chunks.size)
    }

    @Test fun invalidBatchIsRejectedBeforeAnyPageIsSent() = runTest {
        val writer = Writer()
        val store = MemoryStore()
        val transfer = PrinterTransfer(store, { _, _ -> }, {})
        assertEquals(PrintOutcome.FAILED, transfer.send(listOf(ByteArray(10), byteArrayOf()), writer).status)
        assertEquals(PrintOutcome.FAILED, transfer.send(emptyList(), writer).status)
        assertTrue(writer.chunks.isEmpty())
        assertTrue(store.saved.isEmpty())
    }

    @Test fun processDeathRecoveryTurnsSendingIntoUnknownWithoutAnyResubmission() {
        val store = MemoryStore()
        store.value = PrintAttempt("prior", 1234, "sending", 180, 600, "pending")
        val transfer = PrinterTransfer(store, { _, _ -> }, {})
        val recovered = transfer.recover()!!
        assertEquals(PrintOutcome.UNKNOWN, recovered.status)
        assertEquals(180, recovered.bytesSent)
        assertEquals(600, recovered.totalBytes)
        assertTrue(recovered.message.contains("不会自动补打"))
        assertEquals(recovered, store.value)
        assertFalse(transfer.busy)
    }

    @Test fun completedTransportRecordIsNotReclassifiedOrResubmitted() {
        val store = MemoryStore()
        val old = PrintAttempt("prior", 1234, PrintOutcome.SUBMITTED, 600, 600, "submitted")
        store.value = old
        val transfer = PrinterTransfer(store, { _, _ -> }, {})
        assertEquals(old, transfer.recover())
        assertTrue(store.saved.isEmpty())
    }

    @Test fun explicitAcknowledgementPersistsWithoutPretendingThePrinterCompleted() {
        val store = MemoryStore()
        store.value = PrintAttempt("prior", 1234, PrintOutcome.UNKNOWN, 180, 600, "unknown")
        val updates = mutableListOf<PrintAttempt>()
        val transfer = PrinterTransfer(store, { attempt, _ -> updates += attempt }, {})
        assertTrue(transfer.acknowledgeUnknownAttempt())
        assertEquals("acknowledged", store.value!!.status)
        assertEquals(180, store.value!!.bytesSent)
        assertEquals("acknowledged", updates.single().status)
        assertEquals("acknowledged", transfer.recover()!!.status)
        assertFalse(store.value!!.message.contains("打印成功"))
    }

    @Test fun acknowledgementCannotDismissAnInProgressPrint() = runTest {
        val started = CompletableDeferred<Unit>()
        val gate = CompletableDeferred<Unit>()
        val store = MemoryStore()
        val transfer = PrinterTransfer(store, { _, _ -> }, {})
        val writer = Writer { _, _ -> started.complete(Unit); gate.await() }
        val sending = async { transfer.send(listOf(ByteArray(10)), writer) }
        started.await()
        assertFalse(transfer.acknowledgeUnknownAttempt())
        assertEquals("sending", store.value!!.status)
        gate.complete(Unit)
        sending.await()
    }

    @Test fun acknowledgementPersistenceFailureKeepsTheUnknownRecord() {
        val store = MemoryStore().apply {
            value = PrintAttempt("prior", 1234, PrintOutcome.UNKNOWN, 180, 600, "unknown")
            failAfter = 0
        }
        val transfer = PrinterTransfer(store, { _, _ -> }, {})
        assertFalse(transfer.acknowledgeUnknownAttempt())
        assertEquals(PrintOutcome.UNKNOWN, store.value!!.status)
    }

    @Test fun persistenceFailureBeforeSendingBlocksHardwareAndReleasesTheMutex() = runTest {
        val store = MemoryStore().apply { failAfter = 0 }
        val writer = Writer()
        val transfer = PrinterTransfer(store, { _, _ -> }, {})
        val result = transfer.send(listOf(ByteArray(40)), writer)
        assertEquals(PrintOutcome.FAILED, result.status)
        assertTrue(writer.chunks.isEmpty())
        assertFalse(transfer.busy)
    }

    @Test fun persistenceFailureAfterSendingRemainsUnknownAndDoesNotThrowOrRetry() = runTest {
        val store = MemoryStore().apply { failAfter = 1 }
        val writer = Writer()
        val transfer = PrinterTransfer(store, { _, _ -> }, {})
        val result = transfer.send(listOf(ByteArray(40), ByteArray(20)), writer)
        assertEquals(PrintOutcome.UNKNOWN, result.status)
        assertEquals(40, result.bytesSent)
        assertEquals(1, writer.chunks.size)
        assertEquals("sending", store.value!!.status)
        assertFalse(transfer.busy)
    }
}
