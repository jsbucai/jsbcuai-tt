package com.ul.workbench.ui.theme

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

object WorkbenchColors {
    val Background = Color(0xFFF5F5F6)
    val Paper = Color(0xFFFFFFFF)
    val Ink = Color(0xFF1C1D20)
    val Muted = Color(0xFF61646B)
    val Red = Color(0xFFD71920)
    val Tinted = Color(0xFFFDEDED)
    val Line = Color(0xFFE4E5E7)
    val Neutral = Color(0xFFEEEFF1)
    val SecondaryInk = Color(0xFF34363C)
    val Error = Color(0xFF9B4039)
    val ErrorSurface = Color(0xFFFFEDEA)
}

object WorkbenchTokens {
    val ScreenPadding = 18.dp
    val SectionGap = 18.dp
    val ItemGap = 10.dp
    val TouchTarget = 48.dp
    val CardShape = RoundedCornerShape(18.dp)
    val ControlShape = RoundedCornerShape(14.dp)
    const val MotionMillis = 160
}

private val WorkbenchTypography = Typography(
    headlineLarge = TextStyle(fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.Bold, fontSize = 30.sp, lineHeight = 38.sp),
    headlineMedium = TextStyle(fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.SemiBold, fontSize = 26.sp, lineHeight = 34.sp),
    titleLarge = TextStyle(fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.SemiBold, fontSize = 20.sp, lineHeight = 28.sp),
    titleMedium = TextStyle(fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.SemiBold, fontSize = 16.sp, lineHeight = 24.sp),
    titleSmall = TextStyle(fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.Medium, fontSize = 14.sp, lineHeight = 21.sp),
    bodyLarge = TextStyle(fontFamily = FontFamily.SansSerif, fontSize = 16.sp, lineHeight = 25.sp),
    bodyMedium = TextStyle(fontFamily = FontFamily.SansSerif, fontSize = 14.sp, lineHeight = 22.sp),
    bodySmall = TextStyle(fontFamily = FontFamily.SansSerif, fontSize = 12.sp, lineHeight = 18.sp),
    labelLarge = TextStyle(fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.SemiBold, fontSize = 14.sp, lineHeight = 20.sp),
    labelMedium = TextStyle(fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.Medium, fontSize = 12.sp, lineHeight = 18.sp),
    labelSmall = TextStyle(fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.Medium, fontSize = 11.sp, lineHeight = 16.sp)
)

@Composable
fun WorkbenchTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = lightColorScheme(
            primary = WorkbenchColors.Red,
            onPrimary = Color.White,
            primaryContainer = WorkbenchColors.Tinted,
            onPrimaryContainer = WorkbenchColors.Ink,
            secondary = WorkbenchColors.Red,
            onSecondary = Color.White,
            secondaryContainer = WorkbenchColors.Neutral,
            onSecondaryContainer = WorkbenchColors.SecondaryInk,
            tertiary = WorkbenchColors.Red,
            onTertiary = Color.White,
            tertiaryContainer = WorkbenchColors.Tinted,
            onTertiaryContainer = WorkbenchColors.Ink,
            background = WorkbenchColors.Background,
            onBackground = WorkbenchColors.Ink,
            surface = WorkbenchColors.Paper,
            onSurface = WorkbenchColors.Ink,
            surfaceVariant = WorkbenchColors.Tinted,
            onSurfaceVariant = WorkbenchColors.Muted,
            outline = WorkbenchColors.Muted,
            outlineVariant = WorkbenchColors.Line,
            error = WorkbenchColors.Error,
            onError = Color.White,
            errorContainer = WorkbenchColors.ErrorSurface,
            onErrorContainer = WorkbenchColors.Error,
            surfaceTint = WorkbenchColors.Red,
            surfaceBright = WorkbenchColors.Paper,
            surfaceDim = Color(0xFFE2E3E5),
            surfaceContainerLowest = WorkbenchColors.Paper,
            surfaceContainerLow = Color(0xFFF8F8F8),
            surfaceContainer = Color(0xFFF3F3F4),
            surfaceContainerHigh = Color(0xFFEFF0F2),
            surfaceContainerHighest = Color(0xFFE8E9EB),
            inverseSurface = WorkbenchColors.Ink,
            inverseOnSurface = Color.White,
            inversePrimary = Color(0xFFFFB3B5)
        ),
        typography = WorkbenchTypography,
        shapes = Shapes(
            extraSmall = RoundedCornerShape(6.dp),
            small = RoundedCornerShape(10.dp),
            medium = WorkbenchTokens.ControlShape,
            large = WorkbenchTokens.CardShape,
            extraLarge = RoundedCornerShape(26.dp)
        ),
        content = content
    )
}
