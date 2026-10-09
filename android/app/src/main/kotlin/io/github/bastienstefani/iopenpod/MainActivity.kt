package io.github.bastienstefani.iopenpod

import android.app.Activity
import android.app.AlertDialog
import android.app.PendingIntent
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
import android.provider.DocumentsContract
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import org.json.JSONArray
import org.json.JSONObject

/**
 * Read-only check that a connected iPod is reachable from this phone.
 *
 * Android does not expose USB Volumes to applications by path, so the user grants
 * read-only access to the iPod's root through the Storage Access Framework. The
 * Python Application Layer module `iOpenPod.android.read_only_check` reads the
 * Volume through that grant and never writes to it.
 */
class MainActivity : Activity() {
    private val executor: ExecutorService = Executors.newSingleThreadExecutor()
    private lateinit var storageManager: StorageManager
    private lateinit var usbManager: UsbManager

    private lateinit var grantTree: Button
    private lateinit var grantUsb: Button
    private lateinit var connection: TextView
    private lateinit var runCheck: Button
    private lateinit var runProbe: Button
    private lateinit var shareReport: Button
    private lateinit var copyReport: Button
    private lateinit var report: TextView

    private var checkRunning = false
    private var lastReport = ""
    private var requestedVolumeUuid: String? = null

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

        connection = TextView(this).apply {
            setPadding(0, padding, 0, padding)
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
        }
        content.addView(connection)
        grantTree = button(R.string.grant_tree) { requestTreeAccess() }
        content.addView(grantTree)
        grantUsb = button(R.string.grant_usb) { requestUsbPermission() }
        content.addView(grantUsb)
        runCheck = button(R.string.run_check) { startCheck() }
        content.addView(runCheck)
        runProbe = button(R.string.run_probe) { confirmProbe() }
        content.addView(runProbe)
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

    private fun refresh() {
        val volumes = removableVolumes()
        val mounted = volumes.filter { it.state == Environment.MEDIA_MOUNTED }
        grantTree.visibility =
            if (mounted.any { it.uuid != null && treeFor(it) == null }) View.VISIBLE else View.GONE
        val devices = usbManager.deviceList.values.sortedBy { it.deviceName }
        val apple = devices.filter { it.vendorId == APPLE_USB_VENDOR_ID }
        grantUsb.visibility =
            if (apple.any { !usbManager.hasPermission(it) }) View.VISIBLE else View.GONE

        val lines = mutableListOf(getString(R.string.volumes_heading) + ":")
        if (volumes.isEmpty()) lines += "  " + getString(R.string.none)
        for (volume in volumes) {
            val access =
                getString(if (treeFor(volume) != null) R.string.tree_granted else R.string.tree_missing)
            lines +=
                "  ${volume.getDescription(this)} uuid=${volume.uuid ?: "-"} " +
                "state=${volume.state} [$access]"
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
        val hint =
            when {
                mounted.isEmpty() && apple.isEmpty() -> R.string.hint_no_device
                mounted.isEmpty() -> R.string.hint_no_volume
                mounted.any { treeFor(it) == null } -> R.string.hint_grant_tree
                else -> null
            }
        if (hint != null) {
            lines += ""
            lines += getString(hint)
        }
        connection.text = lines.joinToString("\n")
        runCheck.isEnabled = !checkRunning
        runProbe.isEnabled = !checkRunning && mounted.any { it.uuid != null }
        shareReport.isEnabled = lastReport.isNotEmpty()
        copyReport.isEnabled = lastReport.isNotEmpty()
    }

    private fun removableVolumes(): List<StorageVolume> =
        storageManager.storageVolumes.filter { !it.isPrimary }

    /** The persisted read grant whose tree is exactly this Volume's root, if any. */
    private fun treeFor(volume: StorageVolume): Uri? {
        val rootId = "${volume.uuid ?: return null}:"
        return contentResolver.persistedUriPermissions
            .filter { it.isReadPermission }
            .map { it.uri }
            .firstOrNull { uri ->
                DocumentsContract.isTreeUri(uri) &&
                    DocumentsContract.getTreeDocumentId(uri) == rootId
            }
    }

    private fun requestTreeAccess() {
        val volume =
            removableVolumes().firstOrNull {
                it.state == Environment.MEDIA_MOUNTED && it.uuid != null && treeFor(it) == null
            } ?: return
        requestedVolumeUuid = volume.uuid
        startActivityForResult(volume.createOpenDocumentTreeIntent(), REQUEST_TREE)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_TREE && requestCode != REQUEST_WRITE_TREE) return
        val uri = data?.data
        if (resultCode != RESULT_OK || uri == null) return
        val expected = "${requestedVolumeUuid ?: return}:"
        if (DocumentsContract.getTreeDocumentId(uri) != expected) {
            Toast.makeText(this, R.string.tree_not_root, Toast.LENGTH_LONG).show()
            return
        }
        if (requestCode == REQUEST_WRITE_TREE) {
            // Use the temporary grant for this probe only; never persist write access.
            if (data.flags and Intent.FLAG_GRANT_WRITE_URI_PERMISSION == 0) {
                Toast.makeText(this, R.string.probe_needs_write, Toast.LENGTH_LONG).show()
                return
            }
            startProbe(uri)
            return
        }
        // Persist read access only: this check never writes to the iPod.
        contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        Toast.makeText(this, R.string.tree_saved, Toast.LENGTH_SHORT).show()
        refresh()
    }

    private fun confirmProbe() {
        AlertDialog.Builder(this)
            .setTitle(R.string.probe_title)
            .setMessage(R.string.probe_message)
            .setPositiveButton(R.string.probe_continue) { _, _ -> requestWriteTree() }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    private fun requestWriteTree() {
        val volume =
            removableVolumes().firstOrNull {
                it.state == Environment.MEDIA_MOUNTED && it.uuid != null
            } ?: return
        requestedVolumeUuid = volume.uuid
        startActivityForResult(volume.createOpenDocumentTreeIntent(), REQUEST_WRITE_TREE)
    }

    private fun startProbe(treeUri: Uri) {
        val mountPoint =
            removableVolumes().firstOrNull { it.uuid == requestedVolumeUuid }?.directory?.path
                ?: "/storage/$requestedVolumeUuid"
        val request = JSONObject().put("mount_point", mountPoint).put("host", hostDescription())
        val editor = DocumentTreeEditor(contentResolver, treeUri)
        checkRunning = true
        report.setText(R.string.probing)
        refresh()
        executor.execute {
            val text =
                try {
                    if (!Python.isStarted()) Python.start(AndroidPlatform(applicationContext))
                    val result =
                        Python.getInstance()
                            .getModule(PROBE_MODULE)
                            .callAttr("run_probe_json", request.toString(), editor)
                    JSONObject(result.toString()).getString("text")
                } catch (error: Throwable) {
                    getString(R.string.python_error, error.toString())
                }
            runOnUiThread {
                checkRunning = false
                showReport(text)
            }
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
        val volumes =
            removableVolumes()
                .filter { it.state == Environment.MEDIA_MOUNTED }
                .map { volume ->
                    val mountPoint = volume.directory?.path ?: "/storage/${volume.uuid}"
                    mountPoint to treeFor(volume)?.let { DocumentTreeReader(contentResolver, it) }
                }
        if (volumes.isEmpty()) {
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
                    volumes.joinToString("\n") { (mountPoint, tree) ->
                        val request =
                            JSONObject()
                                .put("mount_point", mountPoint)
                                .put("usb_devices", usbDevices)
                                .put("host", host)
                                .put("track_limit", TRACK_LIMIT)
                        val result = module.callAttr("run_check_json", request.toString(), tree)
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
        const val PROBE_MODULE = "iOpenPod.android.provider_probe"
        const val REQUEST_TREE = 1
        const val REQUEST_WRITE_TREE = 2
    }
}
