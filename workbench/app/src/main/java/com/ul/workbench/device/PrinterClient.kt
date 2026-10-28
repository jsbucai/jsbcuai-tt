package com.ul.workbench.device

import kotlinx.coroutines.flow.StateFlow

/** UI-facing device boundary; tests supply an in-memory printer and never open Bluetooth. */
interface PrinterClient {
    val state: StateFlow<PrinterState>
    fun scan()
    fun connect(address: String)
    fun disconnect()
    fun close()
    fun cancelSending()
    fun acknowledgeUnknownAttempt(): Boolean
    suspend fun sendPages(
        pages: List<ByteArray>,
        shouldCancel: () -> Boolean = { false },
    ): PrintOutcome
}
