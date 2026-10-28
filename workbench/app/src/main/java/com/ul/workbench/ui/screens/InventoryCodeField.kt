package com.ul.workbench.ui.screens

import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.text.intl.LocaleList
import com.ul.workbench.ui.theme.WorkbenchTokens

private val CompleteInventoryCode = Regex("[A-Z]{2}[0-9]{4}")
private val PartialInventoryCode = Regex("(?:[A-Z]{0,2}|[A-Z]{2}[0-9]{0,4})")

/** IME preferences only: Android and the chosen keyboard still own the actual key layout. */
internal fun inventoryCodeKeyboardOptions(value: TextFieldValue): KeyboardOptions {
    val afterPrefix = value.text.length >= 2 && value.text.take(2).all { it in 'A'..'Z' } &&
        value.selection.start >= 2 && value.selection.end >= 2
    return KeyboardOptions(
        capitalization = KeyboardCapitalization.Characters,
        autoCorrectEnabled = false,
        keyboardType = if (afterPrefix) KeyboardType.Number else KeyboardType.Ascii,
        imeAction = ImeAction.Search,
        hintLocales = LocaleList("en-US")
    )
}

/** Only a real edit can submit; displaying or restoring the form never starts a query. */
@Composable
internal fun InventoryCodeField(modifier: Modifier = Modifier, onComplete: (String) -> Unit) {
    var value by rememberSaveable(stateSaver = TextFieldValue.Saver) { mutableStateOf(TextFieldValue()) }
    var submitted by rememberSaveable { mutableStateOf(false) }
    fun complete(code: String) {
        if (!submitted && CompleteInventoryCode.matches(code)) {
            submitted = true
            onComplete(code)
        }
    }
    val malformed = value.text.isNotEmpty() && !PartialInventoryCode.matches(value.text)
    OutlinedTextField(value = value, onValueChange = { edited ->
        if (!submitted) {
            // ASCII-only normalization preserves cursor positions and rejects unexpected pasted text.
            val normalized = edited.text.map { if (it in 'a'..'z') it.uppercaseChar() else it }.joinToString("")
            value = edited.copy(text = normalized)
            complete(normalized)
        }
    }, modifier = modifier.fillMaxWidth(), label = { Text("商品编号") }, placeholder = { Text("LS0001") },
        singleLine = true, shape = WorkbenchTokens.ControlShape, isError = malformed,
        supportingText = { Text(if (malformed) "编号需为 2 位英文字母和 4 位数字" else "2 位字母 + 4 位数字，输完自动查") },
        keyboardOptions = inventoryCodeKeyboardOptions(value),
        keyboardActions = KeyboardActions(onSearch = { complete(value.text) }))
}
