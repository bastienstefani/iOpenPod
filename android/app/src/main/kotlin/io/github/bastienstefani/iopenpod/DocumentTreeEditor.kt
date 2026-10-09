package io.github.bastienstefani.iopenpod

import android.content.ContentResolver
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import java.io.FileNotFoundException
import org.json.JSONObject

/**
 * Document-provider primitives for the write probe, over a tree with write access.
 *
 * Implements the `EditableDocumentTree` protocol of `iOpenPod.android.provider_probe`.
 * Each call maps to one provider operation and reports what the provider did,
 * such as the display name it actually chose, without retrying or repairing.
 */
class DocumentTreeEditor(
    resolver: ContentResolver,
    treeUri: Uri,
) : DocumentTreeReader(resolver, treeUri) {
    /** Metadata as JSON, or null when the path does not resolve. */
    fun stat(path: String): String? {
        if (documentId(path) == null) return null
        val projection =
            arrayOf(
                Document.COLUMN_DISPLAY_NAME,
                Document.COLUMN_SIZE,
                Document.COLUMN_LAST_MODIFIED,
                Document.COLUMN_MIME_TYPE,
                Document.COLUMN_FLAGS,
            )
        resolver.query(documentUri(path), projection, null, null, null)?.use { cursor ->
            if (!cursor.moveToFirst()) return null
            return JSONObject()
                .put("name", cursor.getString(0))
                .put("size", if (cursor.isNull(1)) JSONObject.NULL else cursor.getLong(1))
                .put("last_modified", if (cursor.isNull(2)) JSONObject.NULL else cursor.getLong(2))
                .put("mime_type", cursor.getString(3))
                .put("flags", cursor.getInt(4))
                .toString()
        }
        return null
    }

    /** Creates a directory and returns the display name the provider chose. */
    fun createDirectory(parent: String, name: String): String = create(parent, name, Document.MIME_TYPE_DIR)

    /** Creates an empty file and returns the display name the provider chose. */
    fun createFile(parent: String, name: String): String = create(parent, name, OCTET_STREAM)

    /** Returns a detached descriptor opened with a ContentResolver mode such as "w" or "wt". */
    fun openWrite(path: String, mode: String): Int = open(path, mode)

    /** Renames in place and returns the display name the provider chose. */
    fun rename(path: String, name: String): String {
        val renamed =
            DocumentsContract.renameDocument(resolver, documentUri(path), name)
                ?: throw FileNotFoundException(path)
        forgetResolvedNames()
        return displayName(renamed)
    }

    /** Moves into another directory and returns the display name the provider chose. */
    fun move(path: String, parent: String): String {
        val sourceParent = path.substringBeforeLast('/', "")
        val moved =
            DocumentsContract.moveDocument(
                resolver,
                documentUri(path),
                documentUri(sourceParent),
                documentUri(parent),
            ) ?: throw FileNotFoundException(path)
        forgetResolvedNames()
        return displayName(moved)
    }

    /** Deletes a file, or a directory with its contents. */
    fun delete(path: String): Boolean {
        val deleted = DocumentsContract.deleteDocument(resolver, documentUri(path))
        forgetResolvedNames()
        return deleted
    }

    private fun create(parent: String, name: String, mimeType: String): String {
        val created =
            DocumentsContract.createDocument(resolver, documentUri(parent), mimeType, name)
                ?: throw FileNotFoundException("$parent/$name")
        forgetResolvedNames()
        return displayName(created)
    }

    private fun displayName(document: Uri): String {
        resolver.query(document, arrayOf(Document.COLUMN_DISPLAY_NAME), null, null, null)?.use {
            if (it.moveToFirst()) return it.getString(0)
        }
        throw FileNotFoundException(document.toString())
    }

    private companion object {
        const val OCTET_STREAM = "application/octet-stream"
    }
}
