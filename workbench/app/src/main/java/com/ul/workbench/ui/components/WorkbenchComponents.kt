package com.ul.workbench.ui.components

import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.ul.workbench.ui.theme.WorkbenchColors
import com.ul.workbench.ui.theme.WorkbenchTokens

@Composable
fun WorkbenchCard(modifier: Modifier = Modifier, tinted: Boolean = false, content: @Composable ColumnScope.() -> Unit) {
    Surface(modifier = modifier.fillMaxWidth(), shape = WorkbenchTokens.CardShape,
        color = if (tinted) WorkbenchColors.Tinted else WorkbenchColors.Paper,
        border = BorderStroke(1.dp, if (tinted) Color(0xFFF2D0D2) else WorkbenchColors.Line)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp), content = content)
    }
}

@Composable
fun PageHeading(title: String, subtitle: String, onRefresh: (() -> Unit)? = null, compact: Boolean = false,
                action: (@Composable () -> Unit)? = null) {
    Row(Modifier.fillMaxWidth().heightIn(min = 48.dp), horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
        if (compact) {
            Box(Modifier.size(4.dp, 22.dp).background(WorkbenchColors.Red))
            Text(title, modifier = Modifier.weight(1f), style = MaterialTheme.typography.headlineMedium.copy(fontSize = 23.sp, lineHeight = 30.sp))
            if (action == null) Text("UL 工作台", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
        } else {
        Column(Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(7.dp)) {
                Box(Modifier.size(5.dp, 17.dp).background(WorkbenchColors.Red))
                Text("UL  工作台", color = WorkbenchColors.Muted, style = MaterialTheme.typography.labelSmall)
            }
            Spacer(Modifier.height(9.dp))
            Text(title, style = MaterialTheme.typography.headlineMedium)
            Text(subtitle, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted, modifier = Modifier.padding(top = 4.dp))
        }
        }
        action?.invoke()
        if (onRefresh != null) IconButton(onClick = onRefresh, modifier = Modifier.size(48.dp)) {
            Icon(WorkbenchIcons.Refresh, contentDescription = "刷新数据", tint = WorkbenchColors.Ink)
        }
    }
}

@Composable
fun PrimaryAction(label: String, onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true, icon: ImageVector? = null) {
    Button(onClick = onClick, enabled = enabled, modifier = modifier.heightIn(min = WorkbenchTokens.TouchTarget),
        shape = WorkbenchTokens.ControlShape, contentPadding = PaddingValues(horizontal = 14.dp, vertical = 12.dp)) {
        if (icon != null) { Icon(icon, contentDescription = null, modifier = Modifier.size(20.dp)); Spacer(Modifier.width(7.dp)) }
        Text(label, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
fun QuietAction(label: String, onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true, icon: ImageVector? = null) {
    OutlinedButton(onClick = onClick, enabled = enabled, modifier = modifier.heightIn(min = WorkbenchTokens.TouchTarget),
        shape = WorkbenchTokens.ControlShape, border = BorderStroke(1.dp, WorkbenchColors.Line),
        colors = ButtonDefaults.outlinedButtonColors(contentColor = WorkbenchColors.Ink), contentPadding = PaddingValues(horizontal = 12.dp, vertical = 12.dp)) {
        if (icon != null) { Icon(icon, contentDescription = null, modifier = Modifier.size(19.dp)); Spacer(Modifier.width(6.dp)) }
        Text(label, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
fun ScanPrompt(query: String, loading: Boolean, onManual: () -> Unit, enabled: Boolean = true, compact: Boolean = false) {
    if (compact) {
        Surface(shape = WorkbenchTokens.ControlShape, color = WorkbenchColors.Paper, border = BorderStroke(1.dp, WorkbenchColors.Line)) {
            Column {
                Row(Modifier.fillMaxWidth().heightIn(min = 56.dp).padding(start = 12.dp, end = 4.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(9.dp)) {
                    Icon(WorkbenchIcons.Scan, null, Modifier.size(23.dp), tint = WorkbenchColors.Red)
                    Column(Modifier.weight(1f)) {
                        Text(query.ifBlank { "扫描商品条码" }, maxLines = 1, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.titleMedium)
                        Text(if (!enabled) "标签发送中" else if (loading) "正在查询…" else "扫码已就绪", style = MaterialTheme.typography.labelSmall, color = WorkbenchColors.Muted)
                    }
                    TextButton(onClick = onManual, enabled = enabled, modifier = Modifier.heightIn(min = 48.dp), contentPadding = PaddingValues(horizontal = 9.dp), colors = ButtonDefaults.textButtonColors(contentColor = WorkbenchColors.Ink)) {
                        Icon(WorkbenchIcons.Keyboard, null, Modifier.size(18.dp)); Spacer(Modifier.width(5.dp)); Text("手动输入")
                    }
                }
                if (loading) LinearProgressIndicator(modifier = Modifier.fillMaxWidth().height(2.dp), color = WorkbenchColors.Red, trackColor = WorkbenchColors.Neutral)
            }
        }
        return
    }
    WorkbenchCard {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Surface(shape = WorkbenchTokens.ControlShape, color = WorkbenchColors.Tinted) {
                Icon(WorkbenchIcons.Scan, contentDescription = null, tint = WorkbenchColors.Red, modifier = Modifier.padding(14.dp).size(30.dp))
            }
            Column(Modifier.weight(1f)) {
                Text(if (loading) "正在查询…" else query.ifBlank { "直接扫描商品条码" }, style = MaterialTheme.typography.titleMedium)
                Text(if (enabled) "无需点输入框 · 扫完自动查" else "打印完成后可继续扫码", style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
            }
        }
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Box(Modifier.size(6.dp).background(WorkbenchColors.Red, CircleShape))
                Text(if (!enabled) "标签发送中" else if (loading) "正在读取数据" else "扫码已就绪", style = MaterialTheme.typography.labelMedium, color = WorkbenchColors.Muted)
            }
            TextButton(onClick = onManual, enabled = enabled, modifier = Modifier.heightIn(min = 48.dp), colors = ButtonDefaults.textButtonColors(contentColor = WorkbenchColors.Ink)) {
                Icon(WorkbenchIcons.Keyboard, contentDescription = null, modifier = Modifier.size(18.dp)); Spacer(Modifier.width(5.dp)); Text("手动输入")
            }
        }
        if (loading) LinearProgressIndicator(modifier = Modifier.fillMaxWidth().height(2.dp), color = WorkbenchColors.Red, trackColor = WorkbenchColors.Neutral)
    }
}

@Composable
fun MetricStrip(demand: String?, available: String?, stock: String?, inventory: Boolean = false) {
    Surface(shape = WorkbenchTokens.CardShape, color = WorkbenchColors.Paper, border = BorderStroke(1.dp, WorkbenchColors.Line)) {
        Row(Modifier.fillMaxWidth().padding(vertical = 12.dp), horizontalArrangement = Arrangement.SpaceEvenly) {
            if (!inventory) MetricValue("需求数量", demand, Modifier.weight(1f), true)
            MetricValue("可销售", available, Modifier.weight(1f), inventory)
            MetricValue("库存", stock, Modifier.weight(1f))
        }
    }
}

@Composable
private fun MetricValue(label: String, value: String?, modifier: Modifier, primary: Boolean = false) {
    Column(modifier.padding(horizontal = 10.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(label, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        val shown = value?.takeIf { it.isNotBlank() } ?: "—"
        Text(shown, color = if (primary) WorkbenchColors.Red else WorkbenchColors.Ink,
            style = MaterialTheme.typography.titleLarge.copy(fontSize = if (shown.length > 7) 19.sp else 25.sp, fontFeatureSettings = "tnum"),
            fontWeight = FontWeight.SemiBold)
    }
}

@Composable
fun StateNotice(message: String, error: Boolean = false, onDismiss: (() -> Unit)? = null, confirmLabel: String? = null) {
    Surface(shape = WorkbenchTokens.ControlShape, color = if (error) WorkbenchColors.ErrorSurface else WorkbenchColors.Neutral) {
        Row(Modifier.fillMaxWidth().padding(start = 14.dp, top = 10.dp, bottom = 10.dp, end = 6.dp), horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.Top) {
            Icon(if (error) WorkbenchIcons.Alert else WorkbenchIcons.Check, contentDescription = null,
                tint = if (error) WorkbenchColors.Error else WorkbenchColors.Ink, modifier = Modifier.padding(top = 3.dp).size(21.dp))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(message, style = MaterialTheme.typography.bodyMedium,
                    color = if (error) WorkbenchColors.Error else WorkbenchColors.Ink)
                if (onDismiss != null && confirmLabel != null) QuietAction(confirmLabel, onDismiss)
            }
            if (onDismiss != null && confirmLabel == null) IconButton(onClick = onDismiss, modifier = Modifier.size(48.dp)) { Icon(WorkbenchIcons.Close, "收起提示", Modifier.size(18.dp)) }
        }
    }
}

@Composable
fun EmptyWorkspace(title: String, explanation: String, icon: ImageVector = WorkbenchIcons.Scan) {
    Column(Modifier.fillMaxWidth().padding(vertical = 28.dp, horizontal = 20.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Surface(shape = WorkbenchTokens.CardShape, color = WorkbenchColors.Neutral) { Icon(icon, null, Modifier.padding(18.dp).size(32.dp), tint = WorkbenchColors.Muted) }
        Spacer(Modifier.height(18.dp))
        Text(title, style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(6.dp))
        Text(explanation, style = MaterialTheme.typography.bodyMedium, color = WorkbenchColors.Muted)
    }
}

@Composable
fun LabelValue(label: String, value: String, modifier: Modifier = Modifier) {
    Column(modifier) {
        Text(label, style = MaterialTheme.typography.bodySmall, color = WorkbenchColors.Muted)
        Text(value.ifBlank { "—" }, style = MaterialTheme.typography.bodyMedium, color = WorkbenchColors.Ink)
    }
}

@Composable
fun SmallTag(text: String, selected: Boolean = false) {
    Surface(shape = androidx.compose.foundation.shape.RoundedCornerShape(6.dp), color = if (selected) WorkbenchColors.Tinted else WorkbenchColors.Neutral) {
        Text(text, modifier = Modifier.padding(horizontal = 7.dp, vertical = 4.dp), style = MaterialTheme.typography.labelSmall, color = if (selected) WorkbenchColors.Red else WorkbenchColors.Muted)
    }
}
