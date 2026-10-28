package com.ul.workbench.ui.screens

import android.content.ClipData
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.net.Uri
import android.text.Layout
import android.text.StaticLayout
import android.text.TextPaint
import androidx.core.content.FileProvider
import com.ul.workbench.data.BoardExportCard
import com.ul.workbench.data.BoardExportLine
import com.ul.workbench.data.BoardExportSnapshot
import java.io.File
import java.io.FileOutputStream

/** Render only a version-checked, server-provided read snapshot; never touch print or task APIs. */
internal object OrderImageShare {
    private const val WIDTH = 1080
    private const val HEIGHT = 1520
    private const val MARGIN = 72f
    private const val BOTTOM = 1400f

    private data class Block(val text: String, val size: Float = 31f, val bold: Boolean = false,
        val color: Int = Color.rgb(35, 35, 35), val gap: Float = 14f)

    fun render(context: Context, snapshot: BoardExportSnapshot): List<Uri> {
        require(snapshot.items.isNotEmpty()) { "没有可导出的订单" }
        val root = File(context.cacheDir, "order_images")
        root.mkdirs()
        root.listFiles()?.filter { it.isDirectory && System.currentTimeMillis() - it.lastModified() > 86_400_000L }
            ?.forEach(File::deleteRecursively)
        val directory = File(root, snapshot.id.filter { it.isLetterOrDigit() }.ifBlank { "export" })
        require(directory.mkdirs() || directory.isDirectory) { "无法创建图片缓存" }
        val files = mutableListOf<File>()
        try {
            snapshot.items.forEach { entry ->
                if (entry.orders.isEmpty()) {
                    val rows = entry.lines.ifEmpty { listOf(BoardExportLine("", entry.card.customer, entry.card.sku,
                        entry.card.productName, entry.card.quantity, entry.card.unit, entry.card.location,
                        entry.card.subtitle)) }
                    renderOne(directory, files, snapshot, entry, "", rows)
                } else entry.orders.forEach { order ->
                    val rows = order.items.ifEmpty { entry.lines.filter { it.orderNo == order.orderNo } }
                    renderOne(directory, files, snapshot, entry, order.orderNo, rows)
                }
            }
            require(files.isNotEmpty()) { "没有可导出的订单明细" }
            return files.map { FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", it) }
        } catch (failure: Exception) {
            directory.deleteRecursively()
            throw failure
        }
    }

    fun share(context: Context, images: List<Uri>) {
        require(images.isNotEmpty()) { "没有可分享的图片" }
        val multiple = images.size > 1
        val intent = Intent(if (multiple) Intent.ACTION_SEND_MULTIPLE else Intent.ACTION_SEND).apply {
            type = "image/png"
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            if (multiple) putParcelableArrayListExtra(Intent.EXTRA_STREAM, ArrayList(images))
            else putExtra(Intent.EXTRA_STREAM, images.single())
            clipData = ClipData.newUri(context.contentResolver, "UL随行订单", images.first()).also { clip ->
                images.drop(1).forEach { clip.addItem(ClipData.Item(it)) }
            }
        }
        context.startActivity(Intent.createChooser(intent, "发送订单图片").addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
    }

    private fun renderOne(directory: File, files: MutableList<File>, snapshot: BoardExportSnapshot,
                          entry: BoardExportCard, orderNo: String, rows: List<BoardExportLine>) {
        val card = entry.card
        val customer = rows.firstOrNull { it.customer.isNotBlank() }?.customer ?: card.customer
        val title = orderNo.ifBlank { card.customerOrderNo.ifBlank { card.title } }
        val header = listOfNotNull(
            customer.takeIf(String::isNotBlank)?.let { Block("客户：$it", 34f, true) },
            card.title.takeIf { it.isNotBlank() && it != title }?.let { Block("事项：$it") },
            card.dueDate.takeIf(String::isNotBlank)?.let { Block("交期：$it") },
            card.assigneeName.takeIf(String::isNotBlank)?.let { Block("负责人：$it") }
        )
        val blocks = header + rows.flatMapIndexed { index, line ->
            listOfNotNull(
                Block("${index + 1}. ${line.sku.ifBlank { "商品" }}  ${line.productName}", 33f, true, gap = 8f),
                Block("数量 ${line.quantity.ifBlank { "—" }}${line.unit}    货位 ${line.location.ifBlank { "待核对" }}"),
                line.specification.takeIf(String::isNotBlank)?.let { Block("规格：$it", 28f) },
                line.deliveryDate.takeIf(String::isNotBlank)?.let { Block("到货／交期：$it", 28f) },
                line.summary.takeIf(String::isNotBlank)?.let { Block("备注：$it", 28f, gap = 28f) }
            )
        }
        var canvas: Canvas? = null
        var bitmap: Bitmap? = null
        var y = 0f
        var localPage = 0
        fun saveCurrent() {
            val current = bitmap ?: return
            val drawing = canvas ?: return
            val footer = textPaint(24f, false, Color.GRAY)
            drawing.drawText("UL随行 · ${snapshot.exportedAt} · 第 ${localPage} 页", MARGIN, 1450f, footer)
            val file = File(directory, "order-${files.size + 1}.png")
            FileOutputStream(file).use { output -> check(current.compress(Bitmap.CompressFormat.PNG, 100, output)) }
            files += file
            current.recycle()
            bitmap = null
            canvas = null
        }
        fun newPage() {
            saveCurrent()
            localPage++
            bitmap = Bitmap.createBitmap(WIDTH, HEIGHT, Bitmap.Config.ARGB_8888)
            canvas = Canvas(bitmap!!).apply {
                drawColor(Color.WHITE)
                drawRect(0f, 0f, 12f, HEIGHT.toFloat(), Paint().apply { color = Color.rgb(187, 25, 38) })
                drawText("UL随行 订单图片", MARGIN, 88f, textPaint(30f, true, Color.rgb(187, 25, 38)))
            }
            y = 146f
            val heading = if (localPage == 1) title else "$title（续）"
            y += drawBlock(canvas!!, heading, y, Block(heading, 43f, true, gap = 28f))
        }
        newPage()
        blocks.forEach { block ->
            var remaining = block.text
            while (remaining.isNotEmpty()) {
                val rendered = layout(remaining, block)
                if (y + rendered.height + block.gap <= BOTTOM) {
                    y += drawBlock(canvas!!, remaining, y, block)
                    break
                }
                val available = BOTTOM - y
                val fitting = (0 until rendered.lineCount).takeWhile { rendered.getLineBottom(it) <= available }.size
                if (fitting == 0) {
                    newPage()
                    continue
                }
                val end = rendered.getLineEnd(fitting - 1)
                y += drawBlock(canvas!!, remaining.substring(0, end), y, block.copy(gap = 0f))
                remaining = remaining.substring(end).trimStart()
                if (remaining.isNotEmpty()) newPage()
            }
        }
        saveCurrent()
    }

    private fun textPaint(size: Float, bold: Boolean, color: Int) = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = size
        isFakeBoldText = bold
        this.color = color
    }

    private fun layout(text: String, block: Block): StaticLayout = StaticLayout.Builder.obtain(text, 0, text.length,
        textPaint(block.size, block.bold, block.color), (WIDTH - MARGIN * 2).toInt())
        .setAlignment(Layout.Alignment.ALIGN_NORMAL).setIncludePad(false).setLineSpacing(5f, 1f).build()

    private fun drawBlock(canvas: Canvas, text: String, y: Float, block: Block): Float {
        val rendered = layout(text, block)
        canvas.save()
        canvas.translate(MARGIN, y)
        rendered.draw(canvas)
        canvas.restore()
        return rendered.height + block.gap
    }
}
