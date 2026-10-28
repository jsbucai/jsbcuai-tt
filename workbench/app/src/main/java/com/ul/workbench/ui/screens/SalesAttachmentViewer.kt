package com.ul.workbench.ui.screens

import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import com.ul.workbench.data.SalesAttachmentFile
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.util.UUID

/** Only verified bytes enter private cache. No server URL or credential leaves the app. */
internal object SalesAttachmentViewer {
    fun prepare(context: Context, attachment: SalesAttachmentFile): Pair<File, Intent> {
        require(attachment.bytes.isNotEmpty() && attachment.bytes.size <= 10 * 1024 * 1024) { "原附件为空或超过 10 MB" }
        val extension = when (attachment.mimeType) {
            "application/pdf" -> ".pdf"
            "image/png" -> ".png"
            "image/jpeg" -> ".jpg"
            "image/webp" -> ".webp"
            else -> error("此原附件类型暂不支持查看")
        }
        val root = File(context.cacheDir, "sales_attachments")
        check(root.mkdirs() || root.isDirectory) { "无法创建原附件临时目录" }
        root.listFiles()?.filter { it.isFile && System.currentTimeMillis() - it.lastModified() > 86_400_000L }?.forEach(File::delete)
        val file = File(root, UUID.randomUUID().toString() + extension)
        try {
            file.outputStream().use { it.write(attachment.bytes) }
            val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
            val view = Intent(Intent.ACTION_VIEW).setDataAndType(uri, attachment.mimeType).apply {
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                clipData = ClipData.newUri(context.contentResolver, "订单原附件", uri)
            }
            return file to Intent.createChooser(view, "查看订单原附件").apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION)
                clipData = view.clipData
            }
        } catch (failure: Exception) { file.delete(); throw failure }
    }

    suspend fun open(context: Context, attachment: SalesAttachmentFile, stillCurrent: () -> Boolean) {
        val (file, intent) = withContext(Dispatchers.IO) { prepare(context, attachment) }
        withContext(Dispatchers.Main) {
            if (!stillCurrent()) { withContext(Dispatchers.IO) { file.delete() }; return@withContext }
            try { context.startActivity(intent) }
            catch (_: ActivityNotFoundException) { file.delete(); error("手机没有可查看此文件的应用，请安装 PDF 或图片查看器") }
        }
    }
}
