# Android Port Status

Date: 2026-10-09

This page tracks the effort to run iOpenPod on an Android phone acting as the Host
for an iPod connected through a USB OTG adapter. The first target device is an iPod
Nano 5th generation. Background, constraints, the approaches considered, and
hardware findings are in
[Android Host feasibility research](research/android-host-feasibility.md); build
and installation steps are in [`android/README.md`](../android/README.md).

Android is not a release target. The work is for personal installation and no
architectural decision has been recorded yet.

## Summary

The chosen direction is a native Kotlin interface over an embedded Python 3.12
runtime (Chaquopy) that runs the existing iPodDB, Device Registry, and Storage
code. The first increment is a **read-only check**: it shows whether the phone can
read the iPod, identifies it, reports whether HASH72 signing material is
available, and lists its Tracks. It never writes to the iPod.

The first run on a phone showed that Android does not expose USB Volumes to
applications by path. The check now reads the iPod through a read-only Storage
Access Framework grant of its root, and the second run passed: the iPod was
identified exactly, its HASH72 material was found, and its Library was parsed.

## What exists

| Part | Location | State |
| --- | --- | --- |
| Feasibility research | `docs/research/android-host-feasibility.md` | Includes desktop and hardware findings. |
| Read-only check (Python) | `src/iOpenPod/android/read_only_check.py` | Implemented and tested. |
| Document-provider write probe (Python) | `src/iOpenPod/android/provider_probe.py` | Implemented and tested; not yet run on a phone. |
| Tests | `tests/iOpenPod/android/` | 34 tests, passing. |
| Android app (Kotlin, Gradle) | `android/` | Builds a 27 MB debug APK; the document-tree version passes on a phone. |
| HASH72 without `wasmtime` | `src/iPodDB/iTunesDB/writer/signature.py` | Fixed and tested; also affects the desktop. |

### Read-only check

The Kotlin activity:

- lists removable storage Volumes, whether access to each was granted, and USB
  devices, refreshing when they change;
- asks the user to grant the iPod's root through the system file picker
  (`StorageVolume.createOpenDocumentTreeIntent()`), rejects a folder inside it,
  and persists **read** access only;
- explains when an Apple USB device is attached without a mounted Volume (usually
  a Mac-formatted iPod);
- requests USB permission so the iPod's serial number can be read;
- runs the Python check in the background and offers **Share report** and
  **Copy report**.

The app requests no storage permission. `DocumentTreeReader` resolves Device
Paths by listing each parent document, matching names exactly and then ignoring
case as FAT32 does, and hands Python a detached, read-only file descriptor.

The Python check, for each mounted removable Volume:

1. records the Host description, the mount-table lines naming the Volume, and the
   USB devices;
2. reads through the granted document tree, or without one opens a read-only
   Filesystem Session through Storage, falling back to diagnostic direct reads;
3. reads `SysInfo` and `SysInfoExtended`, builds Device Evidence with the USB
   identifiers, and asks Device Registry to identify the iPod;
4. reads the iTunesCDB (or iTunesDB);
5. for HASH72 models, checks `HashInfo` against the FireWire GUID or recovers the
   material from the retained database, without signing anything;
6. parses the Library with iPodDB and lists up to 500 Tracks.

Document-tree and direct reads bypass Storage. They are diagnostic only: no
workflow may build on them until Storage owns document-tree access.

### Document-provider write probe

**Test writing (temporary folder)** measures how the document provider behaves
for the operations Storage Transactions would need. After a confirmation that
states exactly what will happen, the user chooses the iPod's root again; the
probe uses that temporary write grant and never persists write access.

`DocumentTreeEditor` exposes one call per provider operation: create a file or
directory, open a descriptor for writing, rename, move, delete, and query
metadata. Each call reports what the provider did, such as the name it chose.

The Python probe creates a new `iOpenPod-probe-<random>` directory at the Volume
root, refusing to reuse an existing name. A guard rejects every mutation outside
that directory, and no existing entry is ever opened for writing. Inside it, the
probe measures:

- creating a file and whether the requested name is kept;
- writing 1 MiB, `fsync`, reading it back, and the throughput;
- free space reported by `fstatvfs` before and after the write;
- provider and descriptor metadata, including document flags;
- setting a modification time through the descriptor;
- whether modes `w` and `wt` truncate a larger file;
- creating a name that already exists, renaming onto an existing name, names
  differing only by case, and FAT-invalid characters;
- moving between directories and deleting a file.

The scratch directory is deleted at the end, also after a failed measurement,
and the report says so or names the directory left behind.

The APK contains only `iPodDB`, `device_registry`, `storage`, and
`iOpenPod/android` from `src`, with Chaquopy's Pillow 11.0.0 and pycryptodome
3.21.0. The desktop GUI and its dependencies are excluded.

## Verification

Verified in the development environment:

- the read-only check tests pass, including tests that compare every file on the
  Volume before and after the check and that confirm every document-tree
  descriptor is closed;
- the iPodDB, Device Registry, Storage, and check test suites pass with Pillow
  11.0.0 and pycryptodome 3.21.0, the versions Chaquopy provides;
- the compiled Python extracted from the APK reads a simulated Nano 5th
  generation through a document-tree double, identifies it exactly, finds HASH72
  material, and lists 60 Tracks;
- the compiled probe runs against a double of the external storage provider
  (unique names on create and rename, FAT character replacement, recursive
  delete), removes its scratch directory, and leaves the rest of the Volume
  unchanged; tests also cover the guard and cleanup after failures;
- HASH58 and HASH72 work without `wasmtime`; HASHAB still requires it. A test
  covers both, and it fails without the fix;
- `./gradlew assembleDebug` builds the APK with Android Gradle plugin 8.13.2,
  Kotlin 2.2.21, Chaquopy 17.0.0, and either the system Python 3.12 or the UV
  environment's interpreter. The APK targets API 36, requires API 30, contains
  only `arm64-v8a` code, and declares no permission;
- `./gradlew lintDebug` reports no errors. Its remaining warnings are a missing
  application icon, a newer Gradle patch release, no x86_64 build for ChromeOS,
  the deprecated `allowBackup` attribute, and a flag Android 12 and earlier
  ignore.

Verified on hardware (Google Pixel, Android 17, iPod Nano 5th generation):

- the app starts and Chaquopy runs Python 3.12.12;
- the iPod is mounted as FAT32 (`vfat`) at `/mnt/media_rw/<uuid>`, readable only
  by the `media_rw` group, with no mount under `/storage`; reads by path fail
  with `EACCES` even with All files access;
- through the read-only document-tree grant, `DocumentTreeReader` reads
  `SysInfo`, `SysInfoExtended`, `HashInfo`, and the iTunesCDB;
- Device Registry identifies the iPod exactly as an 8 GB Nano 5th generation;
- `HashInfo` is bound to the USB serial number, which is therefore the FireWire
  GUID;
- iPodDB parses the physical iTunesCDB: no Tracks, six Playlists, and the device
  name.

Not verified:

- the write probe on a phone, and therefore any write through the real document
  provider;
- parsing a physical Library that contains Tracks.

## Limits of the current increment

- **No Library writes.** Nothing in the iPod Library can be added, edited, or
  deleted. The only writes are the probe's, inside its own scratch directory.
- **Storage bypassed.** Reads through the document tree happen in the check
  module, outside Storage. Storage has no document-tree backend yet.
- **FAT32 only.** Android cannot mount a Mac-formatted (HFS+) iPod.
- **HASH72 prerequisite.** An iPod Nano 5th generation that iTunes has never
  synced has no signing material, so no Host can write a valid database for it.
- **One grant per Volume.** The user must choose the iPod's root in the file
  picker once; the grant is tied to the Volume UUID.
- **Android 11 or later**, `arm64-v8a` phones only.
- **USB attribution.** Android does not link a USB device to a storage Volume. The
  check uses USB identifiers only when exactly one Apple device is attached.
- **No safe removal from the app.** Android does not let applications eject a
  Volume; the user ejects the iPod from the system storage settings.
- **Debug signing.** Each build machine signs with its own debug key, so
  installing a build from another machine requires uninstalling the previous one.

## Remaining work

Steps 1 to 7 each depend on the previous one.

1. **Run the document-tree check on the phone.** Done; see Verification.
2. **Probe document-provider behavior.** Implemented as the write probe and
   awaiting a run on the phone. Behavior when the cable is pulled during a write
   is not measured: it risks the FAT32 Volume and needs its own decision.
3. **Record the decision.** An ADR covering the Kotlin and Chaquopy approach, the
   minimum Android version, document-tree Volume access, the media-tool strategy,
   and the related updates to `GLOSSARY.md` (**Host** and **iOpenPod** currently
   mean a desktop computer and a desktop product) and to ADR-0001, ADR-0060,
   ADR-0081, ADR-0103, and ADR-0107.
4. **Storage document-tree backend.** Storage resolves Device Paths in a granted
   tree and performs its reads, verified writes, and Storage Transactions there,
   with Volume Identity from the Volume UUID, FAT32 limits from the mount record,
   and Connection Generations driven by attach, detach, and mount events. The
   check then reads through Storage instead of bypassing it.
5. **Browse the Library.** Active iPod selection and a phone interface for Albums,
   Artists, Tracks, and Playlists, reusing the Application Layer workflows that do
   not depend on Qt.
6. **Edit the Library.** Metadata and Playlist changes through Library Drafts and
   Storage Transactions, with HASH72 signing and verification. ADR-0115 notes that
   physical Nano 5th generation acceptance of the SQLite projection is not yet
   proven on any Host; that should be checked first.
7. **Add music.** Sync Execution limited to formats the Nano plays without
   conversion: MP3, AAC, Apple Lossless, WAV, and AIFF.
8. **Later increments.** Conversion through bundled FFmpeg or Android's media
   codecs, artwork, Photos, Podcasts, playback, and other iPod models.

## Repository state when the check was added

These failures were present before the Android work and are unrelated to it:

- 17 tests fail in the full suite: translation catalogs, Synesthesia, media-tool
  setup, the Python update helper, Windows system media, Volume presentation, and
  `test_library_boundary.py`. All of them fail the same way on the commit the
  Android work started from;
- `uv run mypy` reports 37 errors in five Windows- and update-related files;
- `uv run ruff format --check .` reports one file that needs formatting.
