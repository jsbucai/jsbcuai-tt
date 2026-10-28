package com.ul.workbench.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class HardwareScannerTest {
    @Test fun sixCharactersSubmitWithoutFocusOrEnterAndTerminatorsDoNotReplay() {
        val scanner = HardwareScanner()
        val results = "qa0001".mapIndexedNotNull { index, char -> scanner.feed(char, index * 8L) }
        assertEquals(listOf("QA0001"), results)
        assertNull(scanner.enter(50))
        assertNull(scanner.feed('\r', 60))
        assertNull(scanner.feed('\n', 61))
    }

    @Test fun consecutiveDifferentOrIdenticalScansRemainIndependent() {
        val scanner = HardwareScanner()
        val results = "QA0001QA0002QA0002".mapIndexedNotNull { index, char -> scanner.feed(char, index * 5L) }
        assertEquals(listOf("QA0001", "QA0002", "QA0002"), results)
    }

    @Test fun functionCodesUseTheSameFramingAndDuplicateEnterIsIgnored() {
        val scanner = HardwareScanner()
        var time = 0L
        val codes = listOf("pall01", "pbox01").map { text ->
            val values = text.mapNotNull { scanner.feed(it, time.also { time += 10 }) }
            assertNull(scanner.enter(time++))
            assertNull(scanner.feed('\r', time++))
            assertNull(scanner.feed('\n', time++))
            assertNull(scanner.enter(time++))
            values.single()
        }
        assertEquals(listOf("PALL01", "PBOX01"), codes)
    }

    @Test fun completedCodeOwnsItsSuffixEvenAfterTheUiResetsTheInputBuffer() {
        val scanner = HardwareScanner()
        "PALL01".forEachIndexed { index, char -> scanner.feed(char, 100 + index * 10L) }
        scanner.reset() // Opening split/printing or recomposition resets only partial input.
        assertTrue(scanner.ownsTerminator(160))
        assertTrue(scanner.ownsTerminator(649))
        assertFalse(scanner.ownsTerminator(650))
        assertFalse(scanner.ownsTerminator(149))
        assertNull(scanner.enter(160))
    }

    @Test fun idleTimeoutDiscardsAPartialCodeInsteadOfJoiningTheNextScan() {
        val scanner = HardwareScanner()
        "QA0".forEachIndexed { index, char -> assertNull(scanner.feed(char, index * 10L)) }
        scanner.flush(520)
        val results = "ST0001".mapIndexedNotNull { index, char -> scanner.feed(char, 530 + index * 10L) }
        assertEquals(listOf("ST0001"), results)
    }

    @Test fun gapAtTimeoutBoundaryAndClockGoingBackwardsResetTheBuffer() {
        val scanner = HardwareScanner()
        scanner.feed('Q', 100)
        scanner.feed('A', 110)
        assertNull(scanner.feed('0', 610))
        assertNull(scanner.enter(611))
        scanner.feed('Q', 1000)
        scanner.feed('A', 1010)
        val result = "ST0001".mapIndexedNotNull { index, char -> scanner.feed(char, 5L * index) }
        assertEquals(listOf("ST0001"), result)
    }

    @Test fun resetWhenEnteringAnEditorOrChangingPagesPreventsLeakingFragments() {
        val scanner = HardwareScanner()
        "QA0".forEachIndexed { index, char -> scanner.feed(char, index * 5L) }
        scanner.reset()
        "001".forEachIndexed { index, char -> assertNull(scanner.feed(char, 100 + index * 5L)) }
        assertNull(scanner.enter(120))
    }

    @Test fun invalidCharactersAndShortEnterDoNotCreateAnOrderQuery() {
        val scanner = HardwareScanner()
        "QA-001".forEachIndexed { index, char -> assertNull(scanner.feed(char, index * 5L)) }
        assertNull(scanner.enter(50))
        assertNull(scanner.feed('中', 60))
        assertNull(scanner.feed(' ', 70))
    }
}
