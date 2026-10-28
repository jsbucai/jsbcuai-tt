package com.ul.workbench.ui.screens

import android.Manifest
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas as AndroidCanvas
import android.graphics.Color as AndroidColor
import android.graphics.Matrix
import android.graphics.Paint
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.rememberTransformableState
import androidx.compose.foundation.gestures.transformable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.core.content.ContextCompat
import com.ul.workbench.data.*
import com.ul.workbench.ui.components.*
import com.ul.workbench.ui.theme.WorkbenchColors
import com.ul.workbench.ui.theme.WorkbenchTokens
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import kotlin.math.min
import kotlin.math.roundToInt

@Composable
fun InventoryDetailScreen(detail: ProductDetailState, actions: WorkbenchActions, modifier: Modifier = Modifier,
                          onDismiss: () -> Unit, onAdjust: () -> Unit,
                          localPendingAdjustments: List<String> = emptyList()) {
    val item = detail.item
    var viewerOpen by remember { mutableStateOf(false) }
    val decoded = remember(detail.imageBytes) { detail.imageBytes?.let { decodeSampled(it, 4096) } }
    LazyColumn(modifier.fillMaxSize(), contentPadding = PaddingValues(WorkbenchTokens.ScreenPadding),
        verticalArrangement = Arrangement.spacedBy(14.dp)) {
        item("heading") {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("商品详情", style = MaterialTheme.typography.headlineMedium, modifier = Modifier.weight(1f))
                TextButton(onClick = onDismiss, modifier = Modifier.heightIn(min = 48.dp)) { Text("返回") }
            }
        }
        if (detail.loading) item("loading") { LinearProgressIndicator(Modifier.fillMaxWidth(), color = WorkbenchColors.Red) }
        if (!detail.error.isNullOrBlank()) item("error") { StateNotice(detail.error, error = true) }
        if (item != null) item("product") {
            WorkbenchCard {
                Text(item.name.ifBlank { item.sku }, style = MaterialTheme.typography.titleLarge)
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    LabelValue("商品编号", item.sku, Modifier.weight(1f))
                    LabelValue("单位", item.unitName.ifBlank { item.unitId }, Modifier.weight(1f))
                }
                if (item.spec.isNotBlank()) LabelValue("规格", item.spec)
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    LabelValue("型号", item.model, Modifier.weight(1f))
                    LabelValue("材质", item.material, Modifier.weight(1f))
                }
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    LabelValue("货位", item.location, Modifier.weight(1f))
                    LabelValue("可销售", item.available ?: "—", Modifier.weight(1f))
                    LabelValue("实物库存", item.stock ?: "—", Modifier.weight(1f))
                }
            }
        }
        item("image") {
            WorkbenchCard {
                Text("产品图纸", style = MaterialTheme.typography.titleMedium)
                when {
                    decoded != null -> {
                        androidx.compose.foundation.Image(decoded.asImageBitmap(), "打开产品图纸",
                            Modifier.fillMaxWidth().heightIn(min = 180.dp, max = 320.dp)
                                .background(WorkbenchColors.Neutral, WorkbenchTokens.ControlShape)
                                .pointerInput(decoded) { detectTapGestures { viewerOpen = true } },
                            contentScale = androidx.compose.ui.layout.ContentScale.Fit)
                        Text("点击查看，可缩放、下载和编辑", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
                    }
                    detail.loading -> Text("正在读取产品图纸…", color = WorkbenchColors.Muted)
                    else -> Text("ERP 暂无产品主图", color = WorkbenchColors.Muted)
                }
            }
        }
        item("adjust") { PrimaryAction("临时报损 / 报溢", onAdjust, Modifier.fillMaxWidth(), enabled = item != null) }
        item("history-heading") {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("实物库存变动", style = MaterialTheme.typography.titleMedium)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf(30, 90, 180).forEach { days ->
                        TextButton(onClick = { actions.setInventoryHistoryDays(days) },
                            enabled = item != null && !detail.loading && !detail.historyBusy,
                            colors = ButtonDefaults.textButtonColors(contentColor =
                                if (days == detail.historyDays) WorkbenchColors.Red else WorkbenchColors.Muted)) {
                            Text("$days 天")
                        }
                    }
                }
                val history = detail.history
                Text(when (history?.status) {
                    "reconciled" -> "余额已与当前实物库存核对"
                    "incomplete" -> "历史覆盖不完整，余额待核对"
                    else -> if (detail.loading) "正在读取台账…" else "暂无可用台账"
                }, style = MaterialTheme.typography.bodySmall,
                    color = if (history?.status == "reconciled") WorkbenchColors.Muted else WorkbenchColors.Error)
                if (detail.historyBusy) LinearProgressIndicator(Modifier.fillMaxWidth(), color = WorkbenchColors.Red)
            }
        }
        detail.history?.let { history ->
            val pending = (localPendingAdjustments + history.pendingAdjustments).distinct()
            if (pending.isNotEmpty()) item("pending") {
                StateNotice("影响可售、未计入实物台账：${pending.joinToString("；")}")
            }
            items(history.movements, key = { it.id }) { movement -> MovementRow(movement) }
            if (history.hasMore) item("history-more") {
                QuietAction("继续加载 · 已显示 ${history.movements.size}${if (history.total >= 0) "/${history.total}" else ""} 条",
                    actions.loadMoreInventoryHistory, Modifier.fillMaxWidth(), enabled = !detail.historyBusy)
            }
            item("coverage") {
                Text("覆盖 ${history.coverageFrom.ifBlank { "未知" }} 至 ${history.coverageTo.ifBlank { "现在" }} · ${history.positiveInbounds} 次入库" +
                    if (history.hasMore) " · 可继续加载" else if (history.truncated) " · 历史覆盖不完整" else "",
                    style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            }
        }
        if (detail.history == null && localPendingAdjustments.isNotEmpty()) item("local-pending") {
            StateNotice("影响可售、未计入实物台账：${localPendingAdjustments.distinct().joinToString("；")}")
        }
        item("tail") { Spacer(Modifier.height(12.dp)) }
    }
    if (viewerOpen && decoded != null && item != null && detail.imageBytes != null) {
        ProductImageViewer(decoded, detail.imageBytes, detail.images.firstOrNull()?.mimeType ?: "image/jpeg", item.sku,
            onDismiss = { viewerOpen = false })
    }
}

@Composable
private fun MovementRow(value: InventoryMovement) {
    WorkbenchCard(tinted = value.direction == "in") {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text(value.date.ifBlank { value.postedAt }, style = MaterialTheme.typography.labelLarge)
                Text(value.party.ifBlank { value.documentType.ifBlank { "库存变动" } }, style = MaterialTheme.typography.bodyMedium)
                if (value.documentNo.isNotBlank()) Text(value.documentNo, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            }
            Column(horizontalAlignment = Alignment.End) {
                Text((if (value.direction == "out") "−" else "+") + value.quantity.removePrefix("-"),
                    style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold,
                    color = if (value.direction == "out") WorkbenchColors.Error else WorkbenchColors.Ink)
                value.balanceAfter?.let { Text("库存 $it", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted) }
            }
        }
    }
}

private enum class EditTool { None, Pen, Mosaic }

@Composable
private fun ProductImageViewer(original: Bitmap, originalBytes: ByteArray, mimeType: String, sku: String, onDismiss: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var editing by remember { mutableStateOf(false) }
    var pendingSave by remember { mutableStateOf<(() -> Unit)?>(null) }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val action = pendingSave
        pendingSave = null
        if (granted) action?.invoke() else Toast.makeText(context, "需要存储权限才能保存图片", Toast.LENGTH_SHORT).show()
    }
    fun save(action: suspend () -> Unit) {
        val execute: () -> Unit = {
            scope.launch {
                runCatching { withContext(Dispatchers.IO) { action() } }
                    .onSuccess { Toast.makeText(context, "已保存到相册", Toast.LENGTH_SHORT).show() }
                    .onFailure { Toast.makeText(context, it.message ?: "图片保存失败", Toast.LENGTH_SHORT).show() }
            }
            Unit
        }
        if (Build.VERSION.SDK_INT <= 28 && ContextCompat.checkSelfPermission(context, Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            pendingSave = execute
            permission.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
        } else execute()
    }
    if (editing) {
        LocalImageEditor(original, onDismiss = { editing = false }, onSave = { bitmap ->
            save { saveBitmap(context, bitmap, "${sku}_编辑_${timestamp()}.png") }
        })
        return
    }
    var scale by remember { mutableFloatStateOf(1f) }
    var offset by remember { mutableStateOf(Offset.Zero) }
    val transform = rememberTransformableState { zoom, pan, _ ->
        scale = (scale * zoom).coerceIn(1f, 6f)
        offset = if (scale == 1f) Offset.Zero else offset + pan
    }
    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false, decorFitsSystemWindows = false)) {
        Surface(Modifier.fillMaxSize(), color = Color.Black) {
            Box(Modifier.fillMaxSize()) {
                androidx.compose.foundation.Image(original.asImageBitmap(), "产品图纸",
                    Modifier.fillMaxSize().graphicsLayer(scaleX = scale, scaleY = scale,
                        translationX = offset.x, translationY = offset.y)
                        .transformable(transform).pointerInput(Unit) { detectTapGestures(onDoubleTap = {
                            if (scale > 1f) { scale = 1f; offset = Offset.Zero } else scale = 2.5f
                        }) }, contentScale = androidx.compose.ui.layout.ContentScale.Fit)
                Row(Modifier.align(Alignment.TopCenter).safeDrawingPadding().fillMaxWidth().padding(10.dp),
                    horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = onDismiss, colors = ButtonDefaults.textButtonColors(contentColor = Color.White)) { Text("关闭") }
                    Row {
                        TextButton(onClick = { save { saveBytes(context, originalBytes, mimeType, "${sku}_${timestamp()}") } },
                            colors = ButtonDefaults.textButtonColors(contentColor = Color.White)) { Text("下载") }
                        TextButton(onClick = { editing = true }, colors = ButtonDefaults.textButtonColors(contentColor = Color.White)) { Text("编辑") }
                    }
                }
            }
        }
    }
}

@Composable
private fun LocalImageEditor(original: Bitmap, onDismiss: () -> Unit, onSave: (Bitmap) -> Unit) {
    var bitmap by remember { mutableStateOf(original.copy(Bitmap.Config.ARGB_8888, true)) }
    var revision by remember { mutableIntStateOf(0) }
    var tool by remember { mutableStateOf(EditTool.None) }
    val undo = remember { mutableStateListOf<Bitmap>() }
    fun checkpoint() {
        undo += bitmap.copy(Bitmap.Config.ARGB_8888, true)
        if (undo.size > 10) undo.removeAt(0)
    }
    fun replace(value: Bitmap) { checkpoint(); bitmap = value.copy(Bitmap.Config.ARGB_8888, true); revision++ }
    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false, decorFitsSystemWindows = false)) {
        Surface(Modifier.fillMaxSize(), color = WorkbenchColors.Background) {
            Column(Modifier.fillMaxSize().safeDrawingPadding()) {
                Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = onDismiss) { Text("取消") }
                    Text("编辑图纸", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                    TextButton(onClick = { onSave(bitmap) }) { Text("保存副本") }
                }
                EditableBitmap(bitmap, revision, tool, onStrokeStart = { checkpoint() }, onChanged = { revision++ },
                    modifier = Modifier.weight(1f).fillMaxWidth())
                LazyColumn(Modifier.fillMaxWidth().heightIn(max = 170.dp), contentPadding = PaddingValues(10.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    item {
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            QuietAction("旋转", { replace(Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, Matrix().apply { postRotate(90f) }, true)) }, Modifier.weight(1f))
                            QuietAction("裁剪", {
                                val insetX = (bitmap.width * .05f).roundToInt().coerceIn(0, (bitmap.width - 1) / 2)
                                val insetY = (bitmap.height * .05f).roundToInt().coerceIn(0, (bitmap.height - 1) / 2)
                                val width = (bitmap.width - insetX * 2).coerceAtLeast(1)
                                val height = (bitmap.height - insetY * 2).coerceAtLeast(1)
                                replace(Bitmap.createBitmap(bitmap, insetX, insetY, width, height))
                            }, Modifier.weight(1f))
                            QuietAction("撤销", {
                                if (undo.isNotEmpty()) { bitmap = undo.removeAt(undo.lastIndex); revision++ }
                            }, Modifier.weight(1f), enabled = undo.isNotEmpty())
                        }
                    }
                    item {
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            FilterChip(selected = tool == EditTool.Pen, onClick = { tool = if (tool == EditTool.Pen) EditTool.None else EditTool.Pen }, label = { Text("画笔") }, modifier = Modifier.weight(1f))
                            FilterChip(selected = tool == EditTool.Mosaic, onClick = { tool = if (tool == EditTool.Mosaic) EditTool.None else EditTool.Mosaic }, label = { Text("马赛克") }, modifier = Modifier.weight(1f))
                            QuietAction("重置", { replace(original) }, Modifier.weight(1f))
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun EditableBitmap(bitmap: Bitmap, revision: Int, tool: EditTool, onStrokeStart: () -> Unit, onChanged: () -> Unit, modifier: Modifier) {
    var canvasSize by remember { mutableStateOf(IntSize.Zero) }
    var last by remember { mutableStateOf<Offset?>(null) }
    Canvas(modifier.background(Color(0xFFEEEEEE)).onSizeChanged { canvasSize = it }.pointerInput(tool, bitmap) {
        if (tool != EditTool.None) detectDragGestures(onDragStart = { point -> onStrokeStart(); last = point }, onDragEnd = { last = null; onChanged() }) { change, _ ->
            change.consume()
            val size = canvasSize
            if (size.width <= 0 || size.height <= 0) return@detectDragGestures
            val fit = min(size.width.toFloat() / bitmap.width, size.height.toFloat() / bitmap.height)
            val shownW = bitmap.width * fit
            val shownH = bitmap.height * fit
            val left = (size.width - shownW) / 2f
            val top = (size.height - shownH) / 2f
            fun pixel(point: Offset) = Offset(((point.x - left) / fit).coerceIn(0f, bitmap.width - 1f),
                ((point.y - top) / fit).coerceIn(0f, bitmap.height - 1f))
            val here = pixel(change.position)
            val before = pixel(last ?: change.position)
            val canvas = AndroidCanvas(bitmap)
            if (tool == EditTool.Pen) canvas.drawLine(before.x, before.y, here.x, here.y, Paint(Paint.ANTI_ALIAS_FLAG).apply {
                color = AndroidColor.RED; strokeWidth = (bitmap.width / 180f).coerceAtLeast(5f); strokeCap = Paint.Cap.ROUND
            }) else {
                val block = (bitmap.width / 35).coerceIn(12, 60)
                val x = here.x.roundToInt().coerceIn(0, bitmap.width - 1)
                val y = here.y.roundToInt().coerceIn(0, bitmap.height - 1)
                val color = bitmap.getPixel(x, y)
                canvas.drawRect((x - block).coerceAtLeast(0).toFloat(), (y - block).coerceAtLeast(0).toFloat(),
                    (x + block).coerceAtMost(bitmap.width).toFloat(), (y + block).coerceAtMost(bitmap.height).toFloat(), Paint().apply { this.color = color })
            }
            last = change.position
            onChanged()
        }
    }) {
        @Suppress("UNUSED_EXPRESSION") revision
        val fit = min(size.width / bitmap.width, size.height / bitmap.height)
        val shown = IntSize((bitmap.width * fit).roundToInt(), (bitmap.height * fit).roundToInt())
        drawImage(bitmap.asImageBitmap(), dstOffset = IntOffset(((size.width - shown.width) / 2f).roundToInt(), ((size.height - shown.height) / 2f).roundToInt()), dstSize = shown)
    }
}

private fun timestamp() = LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss"))

private fun decodeSampled(bytes: ByteArray, maximumDimension: Int): Bitmap? {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
    if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
    var sample = 1
    while (bounds.outWidth / sample > maximumDimension || bounds.outHeight / sample > maximumDimension) sample *= 2
    return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, BitmapFactory.Options().apply {
        inSampleSize = sample
        inPreferredConfig = Bitmap.Config.ARGB_8888
    })
}

private fun saveBytes(context: Context, bytes: ByteArray, mime: String, stem: String) {
    val extension = when (mime.lowercase()) { "image/png" -> ".png"; "image/webp" -> ".webp"; else -> ".jpg" }
    saveToPictures(context, "$stem$extension", mime) { it.write(bytes) }
}

private fun saveBitmap(context: Context, bitmap: Bitmap, name: String) {
    saveToPictures(context, name, "image/png") { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
}

private fun saveToPictures(context: Context, name: String, mime: String, writer: (java.io.OutputStream) -> Unit) {
    if (Build.VERSION.SDK_INT >= 29) {
        val values = ContentValues().apply {
            put(MediaStore.Images.Media.DISPLAY_NAME, name)
            put(MediaStore.Images.Media.MIME_TYPE, mime)
            put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES + "/UL随行")
            put(MediaStore.Images.Media.IS_PENDING, 1)
        }
        val uri = checkNotNull(context.contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values))
        try {
            context.contentResolver.openOutputStream(uri)?.use(writer) ?: error("无法打开相册文件")
            values.clear(); values.put(MediaStore.Images.Media.IS_PENDING, 0)
            context.contentResolver.update(uri, values, null, null)
        } catch (failure: Exception) {
            context.contentResolver.delete(uri, null, null)
            throw failure
        }
    } else {
        val folder = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES), "UL随行").apply { mkdirs() }
        FileOutputStream(File(folder, name)).use(writer)
    }
}
