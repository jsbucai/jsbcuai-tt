@file:Suppress("DEPRECATION")

package com.ul.workbench.device

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import java.util.Locale
import java.util.concurrent.atomic.AtomicBoolean

/** Native BLE transport. Construction never scans, reconnects, or submits a previous attempt. */
@SuppressLint("MissingPermission")
class BlePrinter(context: Context) : PrinterClient {
    private val context = context.applicationContext
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val mutableState = MutableStateFlow(PrinterState())
    override val state: StateFlow<PrinterState> = mutableState.asStateFlow()
    private val store = SharedPreferencesAttemptStore(this.context)
    private val transfer = PrinterTransfer(store, { attempt, busy ->
        mutableState.update {
            it.copy(status = attempt.message, busy = busy, lastAttempt = attempt,
                bytesSent = attempt.bytesSent, totalBytes = attempt.totalBytes)
        }
    })
    private var scanJob: Job? = null
    private var connectJob: Job? = null
    @Volatile private var scanner: BluetoothLeScanner? = null
    @Volatile private var currentSession: Session? = null
    @Volatile private var closed = false

    init {
        runCatching { transfer.recover() }.onSuccess { attempt ->
            mutableState.update { it.copy(lastAttempt = attempt, status =
                if (attempt?.status == PrintOutcome.UNKNOWN) attempt.message else it.status) }
        }.onFailure { error -> mutableState.update { it.copy(status = error.message ?: "无法读取上次打印记录") } }
    }

    override fun scan() {
        if (closed || transfer.busy) return
        if (!hasPermissions(scan = true)) { setStatus("请先授予蓝牙和附近设备权限"); return }
        stopScan()
        try {
            val adapter = adapter()
            if (adapter == null || !adapter.isEnabled) { setStatus("请先打开手机蓝牙"); return }
            val availableScanner = adapter.bluetoothLeScanner
            if (availableScanner == null) { setStatus("当前设备不支持 BLE 扫描"); return }
            scanner = availableScanner
            mutableState.update { it.copy(status = "正在查找 A300E 打印机…", devices = emptyList(), scanning = true) }
            availableScanner.startScan(scanCallback)
            scanJob = scope.launch {
                delay(SCAN_MILLIS)
                stopScan()
                setStatus(if (state.value.devices.isEmpty()) "未找到打印机，请确认已开机并靠近手机" else "请选择要连接的打印机")
            }
        } catch (error: Exception) {
            stopScan()
            setStatus(permissionMessage(error, "扫描失败"))
        }
    }

    override fun connect(address: String) {
        if (closed || transfer.busy) return
        if (!hasPermissions(scan = false)) { setStatus("请先授予蓝牙连接权限"); return }
        if (!BluetoothAdapter.checkBluetoothAddress(address)) { setStatus("打印机地址无效"); return }
        stopScan()
        connectJob?.cancel()
        closeSession(currentSession, "正在更换连接")
        connectJob = scope.launch {
            var session: Session? = null
            try {
                val adapter = adapter()
                if (adapter == null || !adapter.isEnabled) { setStatus("请先打开手机蓝牙"); return@launch }
                val device = adapter.getRemoteDevice(address)
                val selected = PrinterDevice(address, safeName(device))
                val connecting = Session(selected)
                session = connecting
                currentSession = connecting
                mutableState.update { it.copy(status = "正在连接 ${selected.name}…", connected = false, connectedDevice = null) }
                connecting.gatt = device.connectGatt(context, false, callback(connecting), BluetoothDevice.TRANSPORT_LE)
                    ?: throw PrinterWriteException("无法建立蓝牙连接")
                withTimeout(CONNECT_TIMEOUT_MILLIS) { connecting.ready.await() }
                if (currentSession === connecting) {
                    mutableState.update { it.copy(status = "${selected.name} 已连接", connected = true, connectedDevice = selected) }
                }
            } catch (error: Exception) {
                val message = when (error) {
                    is TimeoutCancellationException -> "连接打印机超时，请重新连接"
                    is CancellationException -> "连接已取消"
                    else -> permissionMessage(error, "连接失败")
                }
                closeSession(session, message)
            }
        }
    }

    override fun cancelSending() = transfer.cancel()

    /** Call only after the user has checked physical output; false means the acknowledgement was not saved. */
    override fun acknowledgeUnknownAttempt(): Boolean = transfer.acknowledgeUnknownAttempt()

    override fun disconnect() {
        transfer.cancel()
        stopScan()
        connectJob?.cancel()
        closeSession(currentSession, "打印机已断开")
    }

    override suspend fun sendPages(pages: List<ByteArray>, shouldCancel: () -> Boolean): PrintOutcome = withContext(Dispatchers.IO) {
        if (shouldCancel()) return@withContext PrintOutcome(PrintOutcome.FAILED, 0, "发送已取消，未发送标签数据。")
        val session = currentSession
        if (closed || session?.characteristic == null || !state.value.connected) {
            return@withContext PrintOutcome(PrintOutcome.FAILED, 0, "请先连接 A300E 打印机，未发送标签数据。")
        }
        if (!hasPermissions(scan = false)) {
            return@withContext PrintOutcome(PrintOutcome.FAILED, 0, "蓝牙连接权限已被取消，未发送标签数据。")
        }
        transfer.send(pages, session, shouldCancel)
    }

    override fun close() {
        if (closed) return
        closed = true
        disconnect()
        scope.cancel()
    }

    private fun adapter(): BluetoothAdapter? = context.getSystemService(BluetoothManager::class.java)?.adapter

    private fun hasPermissions(scan: Boolean): Boolean {
        val permissions = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (scan) listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
            else listOf(Manifest.permission.BLUETOOTH_CONNECT)
        } else if (scan) listOf(Manifest.permission.ACCESS_FINE_LOCATION) else emptyList()
        return permissions.all { context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }
    }

    private fun setStatus(message: String) = mutableState.update { it.copy(status = message) }

    private fun safeName(device: BluetoothDevice): String = try {
        device.name?.takeIf(String::isNotBlank) ?: "A300E 打印机"
    } catch (_: SecurityException) { "A300E 打印机" }

    private fun permissionMessage(error: Exception, fallback: String) =
        if (error is SecurityException) "蓝牙权限不足，请在系统设置中允许附近设备权限"
        else error.message ?: fallback

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) = addDevice(result)
        override fun onBatchScanResults(results: MutableList<ScanResult>) = results.forEach(::addDevice)
        override fun onScanFailed(errorCode: Int) {
            if (!state.value.scanning) return
            stopScan()
            setStatus("蓝牙扫描失败（$errorCode），请稍后重试")
        }
    }

    private fun addDevice(result: ScanResult) {
        if (!state.value.scanning) return
        try {
            val name = result.scanRecord?.deviceName ?: result.device.name ?: return
            val upper = name.uppercase(Locale.ROOT)
            if (listOf("A300", "HPRT", "HM-").none(upper::contains)) return
            val item = PrinterDevice(result.device.address, name)
            mutableState.update { old -> old.copy(devices = (old.devices.filterNot { it.address == item.address } + item).sortedBy { it.name }) }
        } catch (error: SecurityException) {
            stopScan()
            setStatus(permissionMessage(error, "无法读取蓝牙设备"))
        }
    }

    private fun stopScan() {
        scanJob?.cancel()
        scanJob = null
        val previous = scanner
        scanner = null
        try { previous?.stopScan(scanCallback) } catch (_: Exception) { /* Permission can be revoked while scanning. */ }
        mutableState.update { it.copy(scanning = false) }
    }

    private fun callback(session: Session) = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
            if (currentSession !== session) return
            session.gatt = gatt
            if (status != BluetoothGatt.GATT_SUCCESS || newState == BluetoothProfile.STATE_DISCONNECTED) {
                closeSession(session, if (status == BluetoothGatt.GATT_SUCCESS) "打印机连接已断开" else "蓝牙连接异常（$status）")
                return
            }
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                setStatus("正在准备打印通道…")
                try {
                    // Default ATT payload is 20 bytes until MTU negotiation actually succeeds.
                    if (!gatt.requestMtu(185)) discoverServices(session)
                    else scope.launch { delay(1_500); discoverServices(session) }
                } catch (error: Exception) { closeSession(session, permissionMessage(error, "无法准备打印通道")) }
            }
        }

        override fun onMtuChanged(gatt: BluetoothGatt, mtu: Int, status: Int) {
            if (currentSession !== session) return
            if (status == BluetoothGatt.GATT_SUCCESS) session.payloadLimit = (mtu - 3).coerceIn(20, PrinterTransfer.CHUNK_BYTES)
            discoverServices(session)
        }

        override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
            if (currentSession !== session) return
            if (status != BluetoothGatt.GATT_SUCCESS) { closeSession(session, "发现打印服务失败（$status）"); return }
            try {
                val candidates = gatt.services.flatMap { it.characteristics }.filter {
                    it.properties and (BluetoothGattCharacteristic.PROPERTY_WRITE or BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0
                }
                session.characteristic = candidates.firstOrNull { it.service.uuid.toString() in PREFERRED_SERVICES } ?: candidates.firstOrNull()
                if (session.characteristic == null) closeSession(session, "打印机没有可用的写入通道")
                else session.ready.complete(Unit)
            } catch (error: Exception) { closeSession(session, permissionMessage(error, "读取打印通道失败")) }
        }

        override fun onCharacteristicWrite(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int) {
            if (currentSession !== session || characteristic.uuid != session.characteristic?.uuid) return
            val pending = session.pending ?: return
            if (status == BluetoothGatt.GATT_SUCCESS) pending.done.complete(Unit)
            else pending.done.completeExceptionally(PrinterWriteException("蓝牙写入回调异常（$status）", pending.accepted))
        }
    }

    private fun discoverServices(session: Session) {
        if (currentSession !== session || !session.discoveryStarted.compareAndSet(false, true)) return
        try {
            if (session.gatt?.discoverServices() != true) closeSession(session, "无法开始发现打印服务")
        } catch (error: Exception) { closeSession(session, permissionMessage(error, "发现服务失败")) }
    }

    private fun closeSession(session: Session?, message: String) {
        if (session == null) return
        val wasCurrent = currentSession === session
        if (wasCurrent) currentSession = null
        session.pending?.let { it.done.completeExceptionally(PrinterWriteException(message, it.accepted)) }
        session.ready.completeExceptionally(PrinterWriteException(message))
        session.characteristic = null
        val gatt = session.gatt
        session.gatt = null
        try { gatt?.disconnect() } catch (_: Exception) { }
        try { gatt?.close() } catch (_: Exception) { }
        if (wasCurrent) mutableState.update { it.copy(status = message, connected = false, connectedDevice = null) }
    }

    private class PendingWrite {
        val done = CompletableDeferred<Unit>()
        @Volatile var accepted = false
    }

    private inner class Session(val device: PrinterDevice) : PrinterChunkWriter {
        val ready = CompletableDeferred<Unit>()
        val discoveryStarted = AtomicBoolean(false)
        @Volatile var gatt: BluetoothGatt? = null
        @Volatile var characteristic: BluetoothGattCharacteristic? = null
        @Volatile var pending: PendingWrite? = null
        @Volatile var payloadLimit = 20
        override val maxChunkBytes: Int get() = payloadLimit

        override suspend fun write(bytes: ByteArray) {
            val connection = gatt ?: throw PrinterWriteException("打印机已断开")
            val channel = characteristic ?: throw PrinterWriteException("打印通道不可用")
            if (currentSession !== this) throw PrinterWriteException("打印机连接已更换")
            val operation = PendingWrite()
            pending = operation
            try {
                val type = if (channel.properties and BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE != 0)
                    BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE else BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                // Mark conservatively before entering Android; callbacks can arrive on another thread.
                operation.accepted = true
                val accepted = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    connection.writeCharacteristic(channel, bytes, type) == BluetoothStatusCodes.SUCCESS
                } else {
                    channel.writeType = type
                    channel.value = bytes
                    connection.writeCharacteristic(channel)
                }
                operation.accepted = accepted
                if (!accepted) throw PrinterWriteException("手机蓝牙拒绝写入")
                withTimeout(WRITE_TIMEOUT_MILLIS) { operation.done.await() }
            } catch (error: Exception) {
                val wrapped = when (error) {
                    is PrinterWriteException -> error
                    is SecurityException -> PrinterWriteException("蓝牙权限被取消", false, error)
                    is TimeoutCancellationException -> PrinterWriteException("蓝牙写入超时", operation.accepted, error)
                    is CancellationException -> PrinterWriteException("发送已取消", operation.accepted, error)
                    else -> PrinterWriteException(error.message ?: "蓝牙发送失败", operation.accepted, error)
                }
                // Discard this GATT session so a late callback cannot acknowledge a future packet.
                closeSession(this, wrapped.message ?: "打印机已断开")
                throw wrapped
            } finally {
                if (pending === operation) pending = null
            }
        }

        override fun abort() = closeSession(this, "发送已取消")
    }

    companion object {
        private const val SCAN_MILLIS = 8_000L
        private const val CONNECT_TIMEOUT_MILLIS = 18_000L
        private const val WRITE_TIMEOUT_MILLIS = 5_000L
        private val PREFERRED_SERVICES = setOf(
            "0000ffe0-0000-1000-8000-00805f9b34fb",
            "0000ff00-0000-1000-8000-00805f9b34fb",
            "0000fff0-0000-1000-8000-00805f9b34fb",
            "49535343-fe7d-4ae5-8fa9-9fafd205e455",
            "000018f0-0000-1000-8000-00805f9b34fb",
        )
    }
}
