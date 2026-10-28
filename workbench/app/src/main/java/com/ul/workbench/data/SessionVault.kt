package com.ul.workbench.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Sessions are encrypted with Android Keystore and scoped to one server origin. */
class SessionVault(context: Context) {
    private val preferences = context.getSharedPreferences("workbench-sessions-v1", Context.MODE_PRIVATE)
    private val alias = "ul-session-v1"
    private fun slot(server: String) = MessageDigest.getInstance("SHA-256")
        .digest(server.toByteArray()).joinToString("") { "%02x".format(it) }
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    @Synchronized fun load(server: String): String? = runCatching {
        val encoded = preferences.getString(slot(server), null) ?: return null
        val parts = encoded.split(":", limit = 2)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, Base64.decode(parts[0], Base64.NO_WRAP)))
        cipher.updateAAD(server.toByteArray())
        String(cipher.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)), Charsets.UTF_8)
    }.getOrNull()
    @Synchronized fun save(server: String, token: String) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key()); cipher.updateAAD(server.toByteArray())
        val value = Base64.encodeToString(cipher.iv, Base64.NO_WRAP) + ":" +
            Base64.encodeToString(cipher.doFinal(token.toByteArray()), Base64.NO_WRAP)
        check(preferences.edit().putString(slot(server), value).commit()) { "无法安全保存登录状态" }
    }
    @Synchronized fun clear(server: String) { preferences.edit().remove(slot(server)).commit() }
}
