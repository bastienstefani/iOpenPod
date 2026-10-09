package io.github.bastienstefani.iopenpod

import android.content.ContentResolver
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import java.io.FileNotFoundException

/**
 * Read-only access to one Volume root granted through the Storage Access Framework.
 *
 * Implements the `DocumentTree` protocol of `iOpenPod.android.read_only_check`.
 * Paths are Device Paths in POSIX form. Each name is resolved by listing its parent:
 * an exact match wins, otherwise a match ignoring case, as on FAT32.
 */
class DocumentTreeReader(
    private val resolver: ContentResolver,
    private val treeUri: Uri,
) {
    private val rootId: String = DocumentsContract.getTreeDocumentId(treeUri)
    private val resolved = HashMap<String, String>()

    fun describe(): String = treeUri.toString()

    fun exists(path: String): Boolean = documentId(path) != null

    /** Returns a detached file descriptor; the caller owns and closes it. */
    fun openRead(path: String): Int {
        val id = documentId(path) ?: throw FileNotFoundException(path)
        val uri = DocumentsContract.buildDocumentUriUsingTree(treeUri, id)
        val descriptor = resolver.openFileDescriptor(uri, "r") ?: throw FileNotFoundException(path)
        return descriptor.detachFd()
    }

    private fun documentId(path: String): String? {
        var current = rootId
        var prefix = ""
        for (name in path.split('/')) {
            prefix = if (prefix.isEmpty()) name else "$prefix/$name"
            current = resolved[prefix] ?: child(current, name)?.also { resolved[prefix] = it } ?: return null
        }
        return current
    }

    private fun child(parentId: String, name: String): String? {
        val children = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentId)
        val projection = arrayOf(Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME)
        resolver.query(children, projection, null, null, null)?.use { cursor ->
            var ignoringCase: String? = null
            while (cursor.moveToNext()) {
                val displayName = cursor.getString(1)
                if (displayName == name) return cursor.getString(0)
                if (ignoringCase == null && displayName.equals(name, ignoreCase = true)) {
                    ignoringCase = cursor.getString(0)
                }
            }
            return ignoringCase
        }
        return null
    }
}
