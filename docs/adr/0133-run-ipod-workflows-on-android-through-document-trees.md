# ADR-0133: Run iPod workflows on Android through document trees

- Status: Proposed
- Date: 2026-10-09
- Amends, when accepted: ADR-0001, ADR-0060, ADR-0081, ADR-0103, and ADR-0107
  for the Android Host only
- Extends: ADR-0002, ADR-0011, ADR-0029, and ADR-0115

## Context

A user wants to manage an iPod Nano 5th generation from an Android phone through
a USB OTG adapter. The desktop GUI is a PySide6 interface designed for large
windows, and several desktop dependencies have no Android build. iPodDB, Device
Registry, the Storage core, and the main Sync workflows do not depend on Qt.

The investigation, an Android read-only check, and a document-provider write probe
established the following on a Google Pixel with Android 17:

- Chaquopy runs CPython 3.12 on the phone. iPodDB, Device Registry, and Storage
  pass their test suites with the Pillow and pycryptodome versions Chaquopy
  provides once `wasmtime` is imported only for HASHAB.
- Android mounts the iPod's FAT32 Volume where applications cannot reach it by
  path, even with All files access. Only adoptable Volumes are exposed by path;
  USB drives are reachable only through the Storage Access Framework.
- A document-tree grant of the Volume root reads every file iOpenPod needs.
  Device Registry identified the iPod exactly, and `HashInfo` is bound to the USB
  serial number reported by `UsbManager`.
- The document provider writes, `fsync`s, moves, and deletes reliably, and
  `fstatvfs` on its descriptors reports free space. It cannot set modification
  times (`EPERM`), mode `w` does not truncate, creation and renaming never
  replace an existing name but silently choose another name, names compare
  ignoring case, and FAT-invalid characters are silently replaced.

Details are in `docs/research/android-host-feasibility.md`.

## Decision

1. **Architecture.** The Android Host build is a native Kotlin presentation over
   embedded CPython 3.12 (Chaquopy) running the same `iPodDB`, `device_registry`,
   `storage`, and Qt-free `iOpenPod` Application Layer modules from `src`. No iPod
   format, identification, or safety logic is reimplemented in Kotlin. Lower
   boundaries never import Android or Chaquopy modules.
2. **Volume access.** The user grants the Volume root once through the Storage
   Access Framework. The app requests no storage permission. Read access is
   persisted; write access is requested only when a workflow needs it.
3. **Storage owns document-tree access.** Storage gains a document-tree backend
   behind its existing Filesystem Session contract. The Kotlin side supplies only
   provider primitives (resolve, stat, list, open, create, rename, move, delete)
   through an injected protocol and holds no domain logic. Reads and writes
   outside Storage, such as the read-only check's, remain diagnostic.
4. **Document-tree write rules.** The backend:
   - opens files for writing with mode `wt` only;
   - validates FAT32 names itself and rejects a create, rename, or move whose
     provider-chosen name differs from the requested one;
   - publishes replacements as journaled steps (retain the original under a
     temporary name, publish and verify the new file, then remove the original),
     because the provider has no atomic replace; Restore Recovery covers every
     intermediate state;
   - reports modification times as unsettable, so a workflow that must reproduce
     them, currently Backup Snapshot restore and Restore Recovery, fails or
     degrades explicitly instead of silently.
5. **Identity and lifetime.** The USB serial number from `UsbManager` is current
   hardware evidence for the transport serial (FireWire GUID). Volume Identity
   comes from the Volume UUID and its mount record. Android Volume and USB events
   drive Connection Generations; a detached or unmounted Volume invalidates its
   Filesystem Session.
6. **Platform scope.** Android 11 (API 30) or later, `arm64-v8a`, personal
   installation of signed APKs. Android is not a release target; ADR-0107's
   release assets are unchanged.
7. **Tooling.** Gradle builds the Android package and Chaquopy's build step uses
   Python 3.12. UV remains the only tool for the Python environment and checks.
8. **Media tools.** The first Android write scope publishes only media the iPod
   plays without conversion. FFmpeg and fpcalc integration on Android is a later
   decision; ADR-0081 and ADR-0103 do not apply to the Android Host until then.
9. **Safe removal.** Android does not let applications eject a Volume. The app
   flushes its work, invalidates the session, and directs the user to the system
   storage settings, reporting removal only when Android reports the Volume
   unmounted.
10. **Language.** On acceptance, `GLOSSARY.md` defines **Host** as the computer or
    phone running iOpenPod and **iOpenPod** as the product that manages an iPod
    without iTunes on desktop Hosts and, experimentally, on Android.

## Consequences

- iPod format behavior, lossless round trips (ADR-0006), and the single
  definition-driven iPodDB path (ADR-0008) are shared with the desktop.
- Storage must abstract its filesystem operations so that path-based and
  document-tree Volumes satisfy the same Filesystem Session and Storage
  Transaction guarantees, with tests for both.
- Replacement is never atomic on Android. Interrupted publication always relies
  on journaled recovery, which must be proven on the document-tree backend
  before Library saves are enabled.
- Backup Snapshot restore cannot reproduce modification times on Android.
- The Android interface needs its own screens; the desktop GUI is not reused.
- Pulling the cable during a write remains unmeasured and must not be tested on a
  Volume holding user data.
- Physical acceptance of the Nano 5th generation SQLite projection (ADR-0115)
  remains open on every Host and should be closed before Android saves rely on it.
