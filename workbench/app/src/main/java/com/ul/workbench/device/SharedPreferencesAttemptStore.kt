package com.ul.workbench.device

import android.content.Context

/** Synchronous commits bracket device I/O so process death cannot look like a confirmed success. */
internal class SharedPreferencesAttemptStore(context: Context) : PrintAttemptStore {
    private val preferences = context.getSharedPreferences("ul_printer_attempt", Context.MODE_PRIVATE)

    override fun read(): PrintAttempt? {
        val id = preferences.getString("id", null) ?: return null
        return PrintAttempt(
            id,
            preferences.getLong("startedAt", 0),
            preferences.getString("status", PrintOutcome.UNKNOWN) ?: PrintOutcome.UNKNOWN,
            preferences.getInt("bytesSent", 0),
            preferences.getInt("totalBytes", 0),
            preferences.getString("message", "") ?: "",
        )
    }

    override fun save(attempt: PrintAttempt) {
        check(preferences.edit()
            .putString("id", attempt.id)
            .putLong("startedAt", attempt.startedAtMillis)
            .putString("status", attempt.status)
            .putInt("bytesSent", attempt.bytesSent)
            .putInt("totalBytes", attempt.totalBytes)
            .putString("message", attempt.message)
            .commit()) { "无法保存打印发送记录，请检查手机存储空间" }
    }
}
