package com.ul.workbench

import android.view.KeyEvent
import android.view.View
import android.widget.Button
import android.os.Looper
import androidx.lifecycle.ViewModelProvider
import androidx.test.core.app.ApplicationProvider
import android.app.Application
import com.ul.workbench.data.Destination
import org.junit.Assert.*
import org.junit.Test
import org.junit.Before
import org.junit.After
import java.net.InetAddress
import java.net.ServerSocket
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.test.resetMain
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.LooperMode

/** Native KeyEvent routing tests. No Bluetooth permission, scan or printer writes. */
@RunWith(RobolectricTestRunner::class)
@LooperMode(LooperMode.Mode.PAUSED)
@Config(sdk = [35])
@OptIn(ExperimentalCoroutinesApi::class)
class ScannerActivityTest {
    private val dispatcher = StandardTestDispatcher()
    private lateinit var server: ServerSocket
    private lateinit var serverThread: Thread

    @Before fun connectedLocalService() {
        Dispatchers.setMain(dispatcher)
        server = ServerSocket(0, 20, InetAddress.getByName("127.0.0.1"))
        serverThread = kotlin.concurrent.thread(isDaemon = true) {
            while (!server.isClosed) {
                try {
                    server.accept().use { socket ->
                        socket.soTimeout = 2000
                        val reader = socket.getInputStream().bufferedReader()
                        while (!reader.readLine().isNullOrEmpty()) { }
                        val body = """{"ok":true,"version":"fixture","counts":{"orders":0,"inventory":0},"items":[],"batches":[],"total":0,"data_version":"fixture"}""".toByteArray()
                        socket.getOutputStream().apply {
                            write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray())
                            write(body); flush()
                        }
                    }
                } catch (_: java.io.IOException) { if (server.isClosed) break }
            }
        }
    }

    @After fun stopLocalService() {
        server.close(); serverThread.join(1000)
        dispatcher.scheduler.runCurrent()
        Dispatchers.resetMain()
    }

    private fun awaitConnected(model: WorkbenchViewModel) {
        val deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(5)
        while (!model.state.value.serverOnline && System.nanoTime() < deadline) {
            dispatcher.scheduler.runCurrent()
            shadowOf(Looper.getMainLooper()).idle()
            Thread.sleep(10)
        }
        shadowOf(Looper.getMainLooper()).idle()
        assertTrue("Local test service must connect before scanning: ${model.state.value.serverStatus}", model.state.value.serverOnline)
        assertFalse(model.state.value.connectionRequired)
    }

    @Test fun hardwareEventsDoNotNeedTextFocusAndEditingOwnsItsInput() {
        val app = ApplicationProvider.getApplicationContext<Application>()
        app.getSharedPreferences("workbench-settings-v1", 0).edit().clear().putString("server", "http://127.0.0.1:${server.localPort}").commit()
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        try {
            val activity = controller.get()
            val model = ViewModelProvider(activity)[WorkbenchViewModel::class.java]
            model.navigate(Destination.Orders)
            model.saveServer("http://127.0.0.1:${server.localPort}")
            awaitConnected(model)
            assertFalse(activity.currentFocus?.onCheckIsTextEditor() == true)
            type(activity, "DH0019")
            assertEquals("DH0019", model.state.value.query)
            activity.dispatchKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_ENTER))
            assertEquals("DH0019", model.state.value.query)
            model.editingChanged(true)
            type(activity, "AB1234")
            assertEquals("DH0019", model.state.value.query)
            model.editingChanged(false)
            model.navigate(Destination.Inventory)
            type(activity, "AB1234")
            assertEquals(Destination.Inventory, model.state.value.destination)
            assertEquals("AB1234", model.state.value.query)
        } finally { controller.pause().stop().destroy() }
    }

    @Test fun incompleteScanAndInvalidCharacterCannotJoinIntoAnotherSku() {
        val app = ApplicationProvider.getApplicationContext<Application>()
        app.getSharedPreferences("workbench-settings-v1", 0).edit().clear().putString("server", "http://127.0.0.1:${server.localPort}").commit()
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        try {
            val activity = controller.get()
            val model = ViewModelProvider(activity)[WorkbenchViewModel::class.java]
            model.navigate(Destination.Orders)
            model.saveServer("http://127.0.0.1:${server.localPort}")
            awaitConnected(model)
            type(activity, "QA")
            activity.dispatchKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_SPACE))
            type(activity, "0001")
            assertEquals("", model.state.value.query)
        } finally { controller.pause().stop().destroy() }
    }

    @Test fun scannerReleaseAndEnterCannotActivateAButtonAfterTheScanChangesInputMode() {
        val app = ApplicationProvider.getApplicationContext<Application>()
        app.getSharedPreferences("workbench-settings-v1", 0).edit().clear().putString("server", "http://127.0.0.1:${server.localPort}").commit()
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        try {
            val activity = controller.get()
            val model = ViewModelProvider(activity)[WorkbenchViewModel::class.java]
            model.navigate(Destination.Orders)
            model.saveServer("http://127.0.0.1:${server.localPort}")
            awaitConnected(model)
            var escaped = 0
            val stopButton = Button(activity).apply {
                text = "停止发送"
                isFocusableInTouchMode = true
                setOnKeyListener { _, _, _ -> escaped++; true }
            }
            activity.setContentView(stopButton)
            controller.visible().windowFocusChanged(true)
            // Finish detaching Compose before asserting input ownership. Its
            // DisposableEffect resets editing asynchronously when removed.
            shadowOf(Looper.getMainLooper()).idle()
            activity.window.decorView.apply {
                measure(View.MeasureSpec.makeMeasureSpec(360, View.MeasureSpec.EXACTLY),
                    View.MeasureSpec.makeMeasureSpec(640, View.MeasureSpec.EXACTLY))
                layout(0, 0, 360, 640)
            }
            assertTrue(stopButton.requestFocus())
            // Robolectric shadows Activity.getCurrentFocus independently of View.requestFocus.
            shadowOf(activity).setCurrentFocus(stopButton)
            assertSame(stopButton, activity.currentFocus)
            // Prove the framework/control route is live before the negative test.
            model.editingChanged(true)
            assertTrue(activity.dispatchKeyEvent(KeyEvent(10, 10, KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_ENTER, 0)))
            assertEquals(1, escaped)
            activity.dispatchKeyEvent(KeyEvent(10, 11, KeyEvent.ACTION_UP, KeyEvent.KEYCODE_ENTER, 0))
            escaped = 0
            model.editingChanged(false)
            val keys = listOf(KeyEvent.KEYCODE_P, KeyEvent.KEYCODE_A, KeyEvent.KEYCODE_L,
                KeyEvent.KEYCODE_L, KeyEvent.KEYCODE_0, KeyEvent.KEYCODE_1)
            keys.forEachIndexed { index, key ->
                val at = 100L + index * 10L
                assertTrue(activity.dispatchKeyEvent(KeyEvent(at, at, KeyEvent.ACTION_DOWN, key, 0)))
                if (index == keys.lastIndex) model.editingChanged(true) // Same !acceptsScan transition as a print/split.
                assertTrue(activity.dispatchKeyEvent(KeyEvent(at, at + 1, KeyEvent.ACTION_UP, key, 0)))
            }
            assertTrue(activity.dispatchKeyEvent(KeyEvent(160, 160, KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_ENTER, 0)))
            assertTrue(activity.dispatchKeyEvent(KeyEvent(160, 161, KeyEvent.ACTION_UP, KeyEvent.KEYCODE_ENTER, 0)))
            assertEquals(0, escaped)
            // The guard expires: a later deliberate editor/button Enter still belongs to the UI.
            model.editingChanged(true)
            assertTrue(activity.dispatchKeyEvent(KeyEvent(700, 700, KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_ENTER, 0)))
            assertEquals(1, escaped)
        } finally { controller.pause().stop().destroy() }
    }

    private fun type(activity: MainActivity, text: String) {
        text.forEach { char ->
            val key = if (char.isDigit()) KeyEvent.KEYCODE_0 + (char - '0') else KeyEvent.KEYCODE_A + (char.uppercaseChar() - 'A')
            activity.dispatchKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, key))
            activity.dispatchKeyEvent(KeyEvent(KeyEvent.ACTION_UP, key))
        }
    }
}
