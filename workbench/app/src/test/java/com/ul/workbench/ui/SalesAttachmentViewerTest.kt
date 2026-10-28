package com.ul.workbench.ui

import android.app.Application
import android.content.Intent
import androidx.test.core.app.ApplicationProvider
import com.ul.workbench.data.SalesAttachmentFile
import com.ul.workbench.ui.screens.SalesAttachmentViewer
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SalesAttachmentViewerTest {
    @Test fun originalIsOpenedThroughPrivateContentUriWithReadPermissionOnly() {
        val app = ApplicationProvider.getApplicationContext<Application>()
        val bytes = "%PDF-original-content".toByteArray()
        val (file, chooser) = SalesAttachmentViewer.prepare(app, SalesAttachmentFile(bytes, "application/pdf"))
        try {
            assertArrayEquals(bytes, file.readBytes())
            assertEquals("sales_attachments", file.parentFile!!.name)
            assertEquals(Intent.ACTION_CHOOSER, chooser.action)
            @Suppress("DEPRECATION")
            val view = chooser.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)!!
            assertEquals(Intent.ACTION_VIEW, view.action); assertEquals("application/pdf", view.type)
            assertEquals("content", view.data!!.scheme)
            assertEquals("${app.packageName}.fileprovider", view.data!!.authority)
            assertEquals(Intent.FLAG_GRANT_READ_URI_PERMISSION, view.flags)
            assertFalse(view.flags and Intent.FLAG_GRANT_WRITE_URI_PERMISSION != 0)
            assertFalse(view.data!!.toString().contains("http"))
            app.contentResolver.openInputStream(view.data!!)!!.use { assertArrayEquals(bytes, it.readBytes()) }
            assertEquals(view.data, view.clipData!!.getItemAt(0).uri)
        } finally { file.delete() }
    }

    @Test fun htmlAndEmptyOriginalCannotBePreparedForExternalViewer() {
        val app = ApplicationProvider.getApplicationContext<Application>()
        for (file in listOf(SalesAttachmentFile("<script>".toByteArray(), "text/html"), SalesAttachmentFile(byteArrayOf(), "application/pdf"))) {
            try { SalesAttachmentViewer.prepare(app, file); fail("Unvalidated type must be rejected") }
            catch (_: IllegalStateException) { }
            catch (_: IllegalArgumentException) { }
        }
    }
}
