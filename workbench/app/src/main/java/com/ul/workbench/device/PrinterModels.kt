package com.ul.workbench.device

data class PrinterDevice(val address: String, val name: String)

data class PrintAttempt(
    val id: String,
    val startedAtMillis: Long,
    val status: String,
    val bytesSent: Int,
    val totalBytes: Int,
    val message: String,
)

data class PrinterState(
    val status: String = "尚未连接打印机",
    val connected: Boolean = false,
    val busy: Boolean = false,
    val devices: List<PrinterDevice> = emptyList(),
    val connectedDevice: PrinterDevice? = null,
    val lastAttempt: PrintAttempt? = null,
    val scanning: Boolean = false,
    val bytesSent: Int = 0,
    val totalBytes: Int = 0,
)

/** submitted means accepted by the Bluetooth transport, never confirmed physical output. */
data class PrintOutcome(val status: String, val bytesSent: Int, val message: String) {
    companion object {
        const val SUBMITTED = "submitted"
        const val FAILED = "failed"
        const val UNKNOWN = "unknown"
    }
}

internal interface PrintAttemptStore {
    fun read(): PrintAttempt?
    fun save(attempt: PrintAttempt)
}

internal interface PrinterChunkWriter {
    val maxChunkBytes: Int
    suspend fun write(bytes: ByteArray)
    fun abort()
}

/** accepted is true once this packet may have entered the Bluetooth stack. */
internal class PrinterWriteException(
    message: String,
    val accepted: Boolean = false,
    cause: Throwable? = null,
) : Exception(message, cause)
