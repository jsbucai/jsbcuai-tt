package com.ul.workbench.device

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Mutex
import java.util.UUID
import java.util.concurrent.atomic.AtomicLong

/** Transport-independent transfer engine. It never queues duplicate calls or retries packets. */
internal class PrinterTransfer(
    private val store: PrintAttemptStore,
    private val onAttempt: (PrintAttempt, Boolean) -> Unit,
    private val pause: suspend (Long) -> Unit = { delay(it) },
    private val now: () -> Long = System::currentTimeMillis,
) {
    private val mutex = Mutex()
    private val cancellationGeneration = AtomicLong(0)
    @Volatile private var activeWriter: PrinterChunkWriter? = null

    val busy: Boolean get() = mutex.isLocked

    fun recover(): PrintAttempt? {
        val last = store.read() ?: return null
        if (last.status != "sending") return last
        return last.copy(
            status = PrintOutcome.UNKNOWN,
            message = "上次发送被中断，出纸情况未知。请检查标签后决定是否重新打印；不会自动补打。",
        ).also(store::save)
    }

    fun cancel() {
        cancellationGeneration.incrementAndGet()
        activeWriter?.abort()
    }

    /** Only the user's explicit check dismisses an unknown attempt; no device data is resent. */
    fun acknowledgeUnknownAttempt(): Boolean {
        if (!mutex.tryLock()) return false
        try {
            val last = store.read() ?: return true
            if (last.status == "sending") return false
            if (last.status != PrintOutcome.UNKNOWN) return true
            val acknowledged = last.copy(status = "acknowledged", message = "已核对上次标签，可以继续操作。")
            store.save(acknowledged)
            onAttempt(acknowledged, false)
            return true
        } catch (_: Exception) {
            return false
        } finally {
            mutex.unlock()
        }
    }

    suspend fun send(
        pages: List<ByteArray>,
        writer: PrinterChunkWriter,
        shouldCancel: () -> Boolean = { false },
    ): PrintOutcome {
        val generation = cancellationGeneration.get()
        if (!mutex.tryLock()) return PrintOutcome(PrintOutcome.FAILED, 0, "已有标签正在发送，请等待当前任务结束。")
        try {
            if (pages.isEmpty() || pages.any { it.isEmpty() }) {
                return PrintOutcome(PrintOutcome.FAILED, 0, "标签数据为空，未发送。")
            }
            val totalLong = pages.sumOf { it.size.toLong() }
            if (totalLong > Int.MAX_VALUE) return PrintOutcome(PrintOutcome.FAILED, 0, "标签数据过大，未发送。")
            // Freeze bytes once before touching the device; rendering/encoding belongs to the server.
            val frozen = pages.map(ByteArray::copyOf)
            val total = totalLong.toInt()
            val chunkSize = writer.maxChunkBytes.coerceIn(1, CHUNK_BYTES)
            activeWriter = writer
            var sent = 0
            var attemptedChunk = 0
            var attempt = PrintAttempt(UUID.randomUUID().toString(), now(), "sending", 0, total, "正在发送标签数据")
            try {
                store.save(attempt)
            } catch (error: Exception) {
                activeWriter = null
                return PrintOutcome(PrintOutcome.FAILED, 0, "无法保存发送记录，未发送标签数据：${error.message ?: "存储错误"}")
            }
            onAttempt(attempt, true)
            try {
                for ((pageIndex, page) in frozen.withIndex()) {
                    var offset = 0
                    while (offset < page.size) {
                        currentCoroutineContext().ensureActive()
                        if (shouldCancel() || cancellationGeneration.get() != generation) throw PrinterWriteException("发送已取消")
                        val end = (offset + chunkSize).coerceAtMost(page.size)
                        val chunk = page.copyOfRange(offset, end)
                        attemptedChunk = chunk.size
                        writer.write(chunk)
                        sent += chunk.size
                        attemptedChunk = 0
                        offset = end
                        attempt = attempt.copy(bytesSent = sent)
                        onAttempt(attempt, true)
                        pause(CHUNK_GAP_MILLIS)
                    }
                    // Durable at start, page boundaries and final outcome, avoiding a disk flush per packet.
                    store.save(attempt)
                    if (pageIndex < frozen.lastIndex) pause(PAGE_GAP_MILLIS)
                }
                currentCoroutineContext().ensureActive()
                if (shouldCancel() || cancellationGeneration.get() != generation) throw PrinterWriteException("发送已取消")
                val outcome = PrintOutcome(PrintOutcome.SUBMITTED, sent, "已发送 ${frozen.size} 张标签数据，请以实际出纸为准。")
                attempt = attempt.copy(status = outcome.status, bytesSent = sent, message = outcome.message)
                store.save(attempt)
                onAttempt(attempt, false)
                return outcome
            } catch (error: Exception) {
                // Accepted-but-unacknowledged bytes are a conservative upper bound, not proof of output.
                val acceptedPending = error is PrinterWriteException && error.accepted
                val interruptedPending = error is CancellationException && attemptedChunk > 0
                if (acceptedPending || interruptedPending) sent += attemptedChunk
                val uncertain = sent > 0 || acceptedPending || interruptedPending
                val status = if (uncertain) PrintOutcome.UNKNOWN else PrintOutcome.FAILED
                val detail = when (error) {
                    is CancellationException -> "发送已取消"
                    else -> error.message ?: "蓝牙发送失败"
                }
                val message = if (uncertain) "$detail；可能已发送部分标签，请检查出纸，不会自动补打。" else "$detail；未发送标签数据。"
                val outcome = PrintOutcome(status, sent, message)
                attempt = attempt.copy(status = status, bytesSent = sent, message = message)
                // If final persistence fails, the durable "sending" record remains conservatively unknown.
                runCatching { store.save(attempt) }
                onAttempt(attempt, false)
                return outcome
            } finally {
                activeWriter = null
            }
        } finally {
            mutex.unlock()
        }
    }

    companion object {
        const val CHUNK_BYTES = 180
        const val CHUNK_GAP_MILLIS = 45L
        const val PAGE_GAP_MILLIS = 120L
    }
}
