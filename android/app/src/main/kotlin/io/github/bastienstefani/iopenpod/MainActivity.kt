package io.github.bastienstefani.iopenpod

import android.app.Activity
import android.app.PendingIntent
import android.content.ActivityNotFoundException
import android.content.BroadcastReceiver
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Typeface
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.storage.StorageManager
import android.os.storage.StorageVolume
import android.provider.Settings
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import org.json.JSONArray
import org.json.JSONObject

/**
 * Read-only check that a connected iPod is reachable from this phone.
 *
 * Android owns permissions, Volume enumeration and USB observations; the Python
 * Application Layer module `iOpenPod.android.read_only_check` reads the Volume
 * through Storage and never writes to it.
 */
class MainActivity : Activity() {
    private val executor: ExecutorService = Executors.newSingleThreadExecutor()
    private lateinit var storageManager: StorageManager
    private lateinit var usbManager: UsbManager

    private lateinit var accessStatus: TextView
    private lateinit var grantAccess: Button
    private lateinit var grantUsb: Button
    private lateinit var connection: TextView
    private lateinit var runCheck: Button
    private lateinit var shareReport: Button
    private lateinit var copyReport: Button
    private lateinit var report: TextView

    private var checkRunning = false
    private var lastReport = ""

    private val volumeCallback =
        object : StorageManager.StorageVolumeCallback() {
            override fun onStateChanged(volume: StorageVolume) = refresh()
        }

    private val usbReceiver =
        object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) = refresh()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        storageManager = getSystemService(StorageManager::class.java)
        usbManager = getSystemService(UsbManager::class.java)
        setContentView(buildLayout())

        storageManager.registerStorageVolumeCallback(mainExecutor, volumeCallback)
        val filter =
            IntentFilter().apply {
                addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED)
                addAction(UsbManager.ACTION_USB_DEVICE_DETACHED)
                addAction(ACTION_USB_PERMISSION)
            }
        // Android 12 and earlier ignore the flag; the receiver only refreshes the screen.
        registerReceiver(usbReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        refresh()
    }

    override fun onDestroy() {
        storageManager.unregisterStorageVolumeCallback(volumeCallback)
        unregisterReceiver(usbReceiver)
        executor.shutdown()
        super.onDestroy()
    }

    private fun buildLayout(): View {
        val padding = dp(16)
        val content =
            LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(padding, padding, padding, padding)
            }
        content.addView(
            TextView(this).apply {
                setText(R.string.title)
                textSize = 22f
                setTypeface(typeface, Typeface.BOLD)
            },
        )
        content.addView(TextView(this).apply { setText(R.string.summary) })

        accessStatus = TextView(this).apply { setPadding(0, padding, 0, 0) }
        content.addView(accessStatus)
        grantAccess = button(R.string.grant_access) { openAllFilesAccessSettings() }
        content.addView(grantAccess)

        connection = TextView(this).apply {
            setPadding(0, padding, 0, padding)
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
        }
        content.addView(connection)
        grantUsb = button(R.string.grant_usb) { requestUsbPermission() }
        content.addView(grantUsb)
        runCheck = button(R.string.run_check) { startCheck() }
        content.addView(runCheck)
        shareReport = button(R.string.share_report) { share() }
        content.addView(shareReport)
        copyReport = button(R.string.copy_report) { copy() }
        content.addView(copyReport)

        report = TextView(this).apply {
            setPadding(0, padding, 0, 0)
            typeface = Typeface.MONOSPACE
            textSize = 12f
            setTextIsSelectable(true)
        }
        content.addView(report)

        return ScrollView(this).apply {
            addView(content)
            setOnApplyWindowInsetsListener { view, insets ->
                val bars = insets.getInsets(WindowInsets.Type.systemBars())
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
                insets
            }
        }
    }

    private fun button(label: Int, action: () -> Unit): Button =
        Button(this).apply {
            setText(label)
            setOnClickListener { action() }
        }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun hasAllFilesAccess(): Boolean = Environment.isExternalStorageManager()

    private fun refresh() {
        val access = hasAllFilesAccess()
        accessStatus.setText(if (access) R.string.access_granted else R.string.access_missing)
        grantAccess.visibility = if (access) View.GONE else View.VISIBLE

        val volumes = removableVolumes()
        val devices = usbManager.deviceList.values.sortedBy { it.deviceName }
        val apple = devices.filter { it.vendorId == APPLE_USB_VENDOR_ID }
        grantUsb.visibility =
            if (apple.any { !usbManager.hasPermission(it) }) View.VISIBLE else View.GONE

        val lines = mutableListOf(getString(R.string.volumes_heading) + ":")
        if (volumes.isEmpty()) lines += "  " + getString(R.string.none)
        for (volume in volumes) {
            val directory = volume.directory
            val marker =
                if (directory != null && File(directory, "iPod_Control").isDirectory) {
                    "  [" + getString(R.string.ipod_volume) + "]"
                } else {
                    ""
                }
            lines +=
                "  ${volume.getDescription(this)} ${directory?.path ?: "-"} " +
                "uuid=${volume.uuid ?: "-"} state=${volume.state}$marker"
        }
        lines += ""
        lines += getString(R.string.usb_heading) + ":"
        if (devices.isEmpty()) lines += "  " + getString(R.string.none)
        for (device in devices) {
            lines +=
                "  %04x:%04x %s %s".format(
                    device.vendorId,
                    device.productId,
                    device.manufacturerName ?: "",
                    device.productName ?: "",
                )
        }
        val ipodVisible =
            volumes.any { volume ->
                volume.directory?.let { File(it, "iPod_Control").isDirectory } == true
            }
        if (access && !ipodVisible) {
            lines += ""
            lines +=
                getString(if (apple.isEmpty()) R.string.hint_no_device else R.string.hint_no_volume)
        }
        connection.text = lines.joinToString("\n")
        runCheck.isEnabled = !checkRunning
        shareReport.isEnabled = lastReport.isNotEmpty()
        copyReport.isEnabled = lastReport.isNotEmpty()
    }

    private fun removableVolumes(): List<StorageVolume> =
        storageManager.storageVolumes.filter { !it.isPrimary }

    private fun openAllFilesAccessSettings() {
        val appSettings =
            Intent(
                Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                Uri.parse("package:$packageName"),
            )
        try {
            startActivity(appSettings)
        } catch (ignored: ActivityNotFoundException) {
            startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
        }
    }

    private fun requestUsbPermission() {
        val pending =
            PendingIntent.getBroadcast(
                this,
                0,
                Intent(ACTION_USB_PERMISSION).setPackage(packageName),
                // UsbManager adds the device and result extras to this intent.
                PendingIntent.FLAG_MUTABLE,
            )
        usbManager.deviceList.values
            .filter { it.vendorId == APPLE_USB_VENDOR_ID && !usbManager.hasPermission(it) }
            .forEach { usbManager.requestPermission(it, pending) }
    }

    private fun startCheck() {
        val mountPoints =
            removableVolumes()
                .filter { it.state == Environment.MEDIA_MOUNTED }
                .mapNotNull { it.directory?.path }
        if (mountPoints.isEmpty()) {
            showReport(getString(R.string.no_volume_to_check))
            return
        }
        val usbDevices = usbObservations()
        val host = hostDescription()
        checkRunning = true
        report.setText(R.string.running)
        refresh()
        executor.execute {
            val text =
                try {
                    if (!Python.isStarted()) Python.start(AndroidPlatform(applicationContext))
                    val module = Python.getInstance().getModule(CHECK_MODULE)
                    mountPoints.joinToString("\n") { mountPoint ->
                        val request =
                            JSONObject()
                                .put("mount_point", mountPoint)
                                .put("usb_devices", usbDevices)
                                .put("host", host)
                                .put("track_limit", TRACK_LIMIT)
                        val result = module.callAttr("run_check_json", request.toString())
                        JSONObject(result.toString()).getString("text")
                    }
                } catch (error: Throwable) {
                    getString(R.string.python_error, error.toString())
                }
            runOnUiThread {
                checkRunning = false
                showReport(text)
            }
        }
    }

    private fun usbObservations(): JSONArray {
        val devices = JSONArray()
        for (device in usbManager.deviceList.values) {
            devices.put(
                JSONObject()
                    .put("vendor_id", device.vendorId)
                    .put("product_id", device.productId)
                    .put("manufacturer", device.manufacturerName ?: "")
                    .put("product_name", device.productName ?: "")
                    .put("serial", serialNumber(device)),
            )
        }
        return devices
    }

    private fun serialNumber(device: UsbDevice): String {
        if (!usbManager.hasPermission(device)) return ""
        return try {
            device.serialNumber ?: ""
        } catch (ignored: SecurityException) {
            ""
        }
    }

    private fun hostDescription(): JSONObject =
        JSONObject()
            .put("android", Build.VERSION.RELEASE)
            .put("sdk", Build.VERSION.SDK_INT)
            .put("manufacturer", Build.MANUFACTURER)
            .put("model", Build.MODEL)
            .put("all_files_access", hasAllFilesAccess())

    private fun showReport(text: String) {
        lastReport = text
        report.text = text
        refresh()
    }

    private fun share() {
        val send =
            Intent(Intent.ACTION_SEND)
                .setType("text/plain")
                .putExtra(Intent.EXTRA_TEXT, lastReport)
        startActivity(Intent.createChooser(send, getString(R.string.share_report)))
    }

    private fun copy() {
        val clipboard = getSystemService(ClipboardManager::class.java)
        clipboard.setPrimaryClip(ClipData.newPlainText(getString(R.string.app_name), lastReport))
        Toast.makeText(this, R.string.copied, Toast.LENGTH_SHORT).show()
    }

    private companion object {
        const val APPLE_USB_VENDOR_ID = 0x05AC
        const val ACTION_USB_PERMISSION = "io.github.bastienstefani.iopenpod.USB_PERMISSION"
        const val CHECK_MODULE = "iOpenPod.android.read_only_check"
        const val TRACK_LIMIT = 500
    }
}
