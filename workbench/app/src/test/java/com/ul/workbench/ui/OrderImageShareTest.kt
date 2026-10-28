package com.ul.workbench.ui

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import com.ul.workbench.data.*
import com.ul.workbench.ui.screens.OrderImageShare
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [35])
class OrderImageShareTest {
    @Test fun longOrderRendersEveryLineAcrossReadablePngPages() {
        val app = ApplicationProvider.getApplicationContext<Application>()
        val lines = (1..45).map { number -> BoardExportLine("PO-1", "测试客户", "LS0001",
            "商品 $number", "1", "件", "A01", "规格说明 $number") }
        val snapshot = BoardExportSnapshot("testlongorder", "10-06 08:00", "data-1", "task-1",
            listOf(BoardExportCard(TaskBoardItem("card-1", "packing", ROLE_STOCK_PACKING, "包装任务", customer = "测试客户"),
                emptyList(), listOf(BoardExportOrder("PO-1", lines)))))
        val images = OrderImageShare.render(app, snapshot)
        assertTrue(images.size > 1)
        images.forEach { uri ->
            app.contentResolver.openInputStream(uri).use { stream ->
                assertNotNull(stream)
                assertTrue(stream!!.readBytes().size > 1_000)
            }
        }
        File(app.cacheDir, "order_images/testlongorder").deleteRecursively()
    }
}
