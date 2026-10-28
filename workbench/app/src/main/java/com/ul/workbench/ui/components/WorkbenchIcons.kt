package com.ul.workbench.ui.components

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.PathBuilder
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.unit.dp

private fun lineIcon(name: String, draw: PathBuilder.() -> Unit): ImageVector =
    ImageVector.Builder(name = name, defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f).apply {
        path(fill = null, stroke = SolidColor(Color.Black), strokeLineWidth = 1.7f, strokeLineCap = StrokeCap.Round, strokeLineJoin = StrokeJoin.Round, pathBuilder = draw)
    }.build()

/** Small, consistent original line icons; no external icon font or raster asset. */
object WorkbenchIcons {
    val Tasks by lazy { lineIcon("Tasks") {
        moveTo(9f, 5f); lineTo(21f, 5f); moveTo(9f, 12f); lineTo(21f, 12f); moveTo(9f, 19f); lineTo(21f, 19f)
        moveTo(3f, 5f); lineTo(5f, 7f); lineTo(8f, 3f); moveTo(3f, 12f); lineTo(5f, 14f); lineTo(8f, 10f)
        moveTo(3f, 19f); lineTo(5f, 21f); lineTo(8f, 17f)
    } }
    val People by lazy { lineIcon("People") {
        moveTo(9f, 11f); curveTo(6.8f, 11f, 5f, 9.2f, 5f, 7f); curveTo(5f, 4.8f, 6.8f, 3f, 9f, 3f); curveTo(11.2f, 3f, 13f, 4.8f, 13f, 7f); curveTo(13f, 9.2f, 11.2f, 11f, 9f, 11f); close()
        moveTo(2f, 21f); curveTo(2f, 16f, 5f, 13f, 9f, 13f); curveTo(13f, 13f, 16f, 16f, 16f, 21f)
        moveTo(16f, 4f); curveTo(18f, 4f, 19.5f, 5.5f, 19.5f, 7.5f); curveTo(19.5f, 9.5f, 18f, 11f, 16f, 11f)
        moveTo(16f, 14f); curveTo(20f, 14f, 22f, 17f, 22f, 21f)
    } }
    val Orders by lazy { lineIcon("Orders") {
        moveTo(7f, 3f); lineTo(17f, 3f); lineTo(20f, 6f); lineTo(20f, 21f); lineTo(4f, 21f); lineTo(4f, 3f); lineTo(7f, 3f)
        moveTo(8f, 9f); lineTo(16f, 9f); moveTo(8f, 13f); lineTo(16f, 13f); moveTo(8f, 17f); lineTo(13f, 17f)
    } }
    val Inventory by lazy { lineIcon("Inventory") {
        moveTo(3f, 7f); lineTo(12f, 3f); lineTo(21f, 7f); lineTo(21f, 17f); lineTo(12f, 21f); lineTo(3f, 17f); close()
        moveTo(3f, 7f); lineTo(12f, 11f); lineTo(21f, 7f); moveTo(12f, 11f); lineTo(12f, 21f); moveTo(7.5f, 5f); lineTo(16.5f, 9f); lineTo(16.5f, 13f)
    } }
    val Scan by lazy { lineIcon("Scan") {
        moveTo(3f, 8f); lineTo(3f, 4f); lineTo(7f, 4f); moveTo(17f, 4f); lineTo(21f, 4f); lineTo(21f, 8f)
        moveTo(3f, 16f); lineTo(3f, 20f); lineTo(7f, 20f); moveTo(17f, 20f); lineTo(21f, 20f); lineTo(21f, 16f)
        moveTo(7f, 8f); lineTo(7f, 16f); moveTo(10f, 8f); lineTo(10f, 16f); moveTo(14f, 8f); lineTo(14f, 16f); moveTo(17f, 8f); lineTo(17f, 16f)
    } }
    val Printer by lazy { lineIcon("Printer") {
        moveTo(7f, 8f); lineTo(7f, 3f); lineTo(17f, 3f); lineTo(17f, 8f)
        moveTo(7f, 17f); lineTo(3f, 17f); lineTo(3f, 8f); lineTo(21f, 8f); lineTo(21f, 17f); lineTo(17f, 17f)
        moveTo(7f, 13f); lineTo(17f, 13f); lineTo(17f, 21f); lineTo(7f, 21f); close(); moveTo(17f, 10.5f); lineTo(18f, 10.5f)
    } }
    val Devices by lazy { lineIcon("Devices") {
        moveTo(4f, 7f); lineTo(20f, 7f); moveTo(4f, 17f); lineTo(20f, 17f)
        moveTo(9f, 3f); lineTo(9f, 11f); moveTo(15f, 13f); lineTo(15f, 21f)
    } }
    val Bluetooth by lazy { lineIcon("Bluetooth") {
        moveTo(7f, 7f); lineTo(17f, 17f); lineTo(12f, 21f); lineTo(12f, 3f); lineTo(17f, 7f); lineTo(7f, 17f)
    } }
    val Search by lazy { lineIcon("Search") {
        moveTo(17f, 10f); curveTo(17f, 13.9f, 13.9f, 17f, 10f, 17f); curveTo(6.1f, 17f, 3f, 13.9f, 3f, 10f); curveTo(3f, 6.1f, 6.1f, 3f, 10f, 3f); curveTo(13.9f, 3f, 17f, 6.1f, 17f, 10f); close(); moveTo(15f, 15f); lineTo(21f, 21f)
    } }
    val Keyboard by lazy { lineIcon("Keyboard") {
        moveTo(3f, 5f); lineTo(21f, 5f); lineTo(21f, 19f); lineTo(3f, 19f); close()
        moveTo(6f, 9f); lineTo(7f, 9f); moveTo(10f, 9f); lineTo(11f, 9f); moveTo(14f, 9f); lineTo(15f, 9f); moveTo(18f, 9f); lineTo(18f, 9.1f)
        moveTo(6f, 13f); lineTo(7f, 13f); moveTo(10f, 13f); lineTo(11f, 13f); moveTo(14f, 13f); lineTo(15f, 13f); moveTo(18f, 13f); lineTo(18f, 13.1f); moveTo(8f, 16f); lineTo(16f, 16f)
    } }
    val Check by lazy { lineIcon("Check") { moveTo(5f, 12f); lineTo(10f, 17f); lineTo(20f, 7f) } }
    val Close by lazy { lineIcon("Close") { moveTo(6f, 6f); lineTo(18f, 18f); moveTo(18f, 6f); lineTo(6f, 18f) } }
    val ChevronRight by lazy { lineIcon("ChevronRight") { moveTo(9f, 5f); lineTo(16f, 12f); lineTo(9f, 19f) } }
    val Refresh by lazy { lineIcon("Refresh") {
        moveTo(20f, 8f); curveTo(18.5f, 4.7f, 15.5f, 3f, 12f, 3f); curveTo(7f, 3f, 3f, 7f, 3f, 12f); curveTo(3f, 17f, 7f, 21f, 12f, 21f); curveTo(16f, 21f, 19f, 18.5f, 20f, 15f)
        moveTo(20f, 3f); lineTo(20f, 8f); lineTo(15f, 8f)
    } }
    val Calendar by lazy { lineIcon("Calendar") {
        moveTo(5f, 4f); lineTo(19f, 4f); curveTo(20.1f, 4f, 21f, 4.9f, 21f, 6f); lineTo(21f, 20f); lineTo(3f, 20f); lineTo(3f, 6f); curveTo(3f, 4.9f, 3.9f, 4f, 5f, 4f)
        moveTo(7f, 2f); lineTo(7f, 6f); moveTo(17f, 2f); lineTo(17f, 6f); moveTo(3f, 9f); lineTo(21f, 9f)
        moveTo(7f, 13f); lineTo(7.1f, 13f); moveTo(12f, 13f); lineTo(12.1f, 13f); moveTo(17f, 13f); lineTo(17.1f, 13f)
        moveTo(7f, 17f); lineTo(7.1f, 17f); moveTo(12f, 17f); lineTo(12.1f, 17f)
    } }
    val Split by lazy { lineIcon("Split") {
        moveTo(12f, 21f); lineTo(12f, 14f); curveTo(12f, 10f, 5f, 10f, 5f, 6f); lineTo(5f, 3f)
        moveTo(2f, 6f); lineTo(5f, 3f); lineTo(8f, 6f); moveTo(12f, 14f); curveTo(12f, 10f, 19f, 10f, 19f, 6f); lineTo(19f, 3f); moveTo(16f, 6f); lineTo(19f, 3f); lineTo(22f, 6f)
    } }
    val Alert by lazy { lineIcon("Alert") {
        moveTo(12f, 3f); lineTo(22f, 20f); lineTo(2f, 20f); close(); moveTo(12f, 9f); lineTo(12f, 13f); moveTo(12f, 16.5f); lineTo(12f, 16.6f)
    } }
    val Link by lazy { lineIcon("Link") {
        moveTo(10f, 14f); lineTo(14f, 10f); moveTo(8f, 16f); lineTo(6f, 18f); curveTo(3f, 21f, 0f, 16f, 4f, 13f); lineTo(8f, 9f); curveTo(10f, 7f, 12f, 8f, 13f, 9f)
        moveTo(16f, 8f); lineTo(18f, 6f); curveTo(21f, 3f, 24f, 8f, 20f, 11f); lineTo(16f, 15f); curveTo(14f, 17f, 12f, 16f, 11f, 15f)
    } }
}
