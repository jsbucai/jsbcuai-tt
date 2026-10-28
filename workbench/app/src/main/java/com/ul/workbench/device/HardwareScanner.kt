package com.ul.workbench.device

/**
 * Pure scanner framing. Activity must exclude manual editors and dialogs before forwarding keys.
 * Six ASCII letters/digits complete a frame immediately; Enter only completes a pending frame.
 * Call reset when changing page, entering an editor/dialog, or consuming a function code.
 */
class HardwareScanner(private val idleTimeoutMillis: Long = 500L) {
    private val buffer = StringBuilder(6)
    private var lastCharacterAt: Long? = null
    private var completedAt: Long? = null

    @Synchronized
    fun feed(character: Char, timestampMillis: Long): String? {
        if (character == '\r' || character == '\n') return enter(timestampMillis)
        expire(timestampMillis)
        if (!(character in '0'..'9' || character in 'a'..'z' || character in 'A'..'Z')) {
            reset()
            return null
        }
        buffer.append(character.uppercaseChar())
        lastCharacterAt = timestampMillis
        return if (buffer.length == 6) complete(timestampMillis) else null
    }

    @Synchronized
    fun enter(timestampMillis: Long): String? {
        expire(timestampMillis)
        // Short fragments must never become a different order query.
        return if (buffer.length == 6) complete(timestampMillis) else {
            reset()
            null
        }
    }

    /** A caller timer can expire partial input even when no further key arrives. */
    @Synchronized
    fun flush(timestampMillis: Long): String? {
        expire(timestampMillis)
        return null
    }

    @Synchronized
    fun reset() {
        buffer.setLength(0)
        lastCharacterAt = null
    }

    /** The HID suffix still belongs to a frame after that frame changes the UI. */
    @Synchronized
    fun ownsTerminator(timestampMillis: Long): Boolean = completedAt?.let {
        timestampMillis >= it && timestampMillis - it < idleTimeoutMillis
    } ?: false

    private fun expire(now: Long) {
        val previous = lastCharacterAt ?: return
        if (now < previous || now - previous >= idleTimeoutMillis) reset()
    }

    private fun complete(timestampMillis: Long): String = buffer.toString().also {
        completedAt = timestampMillis
        reset()
    }
}
