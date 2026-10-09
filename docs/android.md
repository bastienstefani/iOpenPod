# Android Port Status

Date: 2026-10-08

This page tracks the effort to run iOpenPod on an Android phone acting as the Host
for an iPod connected through a USB OTG adapter. The first target device is an iPod
Nano 5th generation. Background, constraints, and the approaches considered are in
[Android Host feasibility research](research/android-host-feasibility.md); build
and installation steps are in [`android/README.md`](../android/README.md).

Android is not a release target. The work is for personal installation and no
architectural decision has been recorded yet.

## Summary

The chosen direction is a native Kotlin interface over an embedded Python 3.12
runtime (Chaquopy) that runs the existing iPodDB, Device Registry, and Storage
code. The first increment is a **read-only check**: it shows whether the phone can
see the iPod, identifies it, reports whether HASH72 signing material is available,
and lists its Tracks. It never writes to the iPod.

The debug APK builds and passes Android Lint without errors. The check has not yet
run on a phone.

## What exists

| Part | Location | State |
| --- | --- | --- |
| Feasibility research | `docs/research/android-host-feasibility.md` | Written; includes findings from building the check. |
| Read-only check (Python) | `src/iOpenPod/android/read_only_check.py` | Implemented and tested. |
| Tests | `tests/iOpenPod/android/test_read_only_check.py` | 16 tests, passing. |
| Android app (Kotlin, Gradle) | `android/` | Builds a 27 MB debug APK; not yet run on a phone. |
| HASH72 without `wasmtime` | `src/iPodDB/iTunesDB/writer/signature.py` | Fixed and tested; also affects the desktop. |

### Read-only check

The Kotlin activity:

- asks for All files access and opens the matching settings page;
- lists removable storage Volumes and USB devices, refreshing when they change;
- marks a Volume containing `iPod_Control`, and explains when an Apple USB device
  is attached without a readable Volume (usually a Mac-formatted iPod);
- requests USB permission so the iPod's serial number can be read;
- runs the Python check in the background and offers **Share report** and
  **Copy report**.

The Python check, for each mounted removable Volume:

1. records the Host description, the mount-table lines naming the Volume, and the
   USB devices;
2. inspects the Volume through Storage and opens a **read-only** Filesystem
   Session;
3. reads `SysInfo` and `SysInfoExtended`, builds Device Evidence with the USB
   identifiers, and asks Device Registry to identify the iPod;
4. reads the iTunesCDB (or iTunesDB);
5. for HASH72 models, checks `HashInfo` against the FireWire GUID or recovers the
   material from the retained database, without signing anything;
6. parses the Library with iPodDB and lists up to 500 Tracks.

If Storage cannot inspect the Volume, the check reports the failure and continues
with diagnostic-only direct reads, so a Storage problem can be told apart from an
iPodDB problem. No workflow may build on those direct reads.

The APK contains only `iPodDB`, `device_registry`, `storage`, and
`iOpenPod/android` from `src`, with Chaquopy's Pillow 11.0.0 and pycryptodome
3.21.0. The desktop GUI and its dependencies are excluded.

## Verification

Verified in the development environment:

- the read-only check tests pass, including one that compares every file on the
  Volume before and after the check;
- the iPodDB, Device Registry, Storage, and check test suites (1678 tests) pass
  with Pillow 11.0.0 and pycryptodome 3.21.0, the versions Chaquopy provides;
- with only the packaged Python sources and those two libraries, the check reads a
  simulated Nano 5th generation on a real tmpfs Mount Point through the native
  Storage adapter, identifies it exactly, finds HASH72 material, and lists 60
  Tracks;
- `MainActivity.kt` compiles without warnings against the Android 16 (API 36)
  framework classes and the Chaquopy 17 Java API;
- HASH58 and HASH72 work without `wasmtime`; HASHAB still requires it. A new test
  covers both, and it fails without the fix;
- `./gradlew assembleDebug` builds the APK with Android Gradle plugin 8.13.2,
  Kotlin 2.2.21, Chaquopy 17.0.0, and either the system Python 3.12 or the UV
  environment's interpreter. The APK targets API 36, requires API 30, and contains
  only `arm64-v8a` code;
- the APK's Python payload holds 278 files from `device_registry`, `iPodDB`,
  `storage`, and `iOpenPod/android`, with no desktop GUI module, plus Pillow
  11.0.0 and pycryptodome 3.21.0;
- `./gradlew lintDebug` reports no errors. Its remaining warnings are a missing
  application icon, a newer Gradle patch release, no x86_64 build for ChromeOS,
  the deprecated `allowBackup` attribute, and a flag Android 12 and earlier
  ignore.

Not verified:

- any run of the app. The development environment has no hardware
  virtualization, so the Android emulator cannot run there;
- any run with a physical iPod.

## Limits of the current increment

- **Read-only.** Nothing can be added, edited, or deleted on the iPod.
- **FAT32 only.** Android cannot mount a Mac-formatted (HFS+) iPod.
- **HASH72 prerequisite.** An iPod Nano 5th generation that iTunes has never
  synced has no signing material, so no Host can write a valid database for it.
- **All files access.** Required for path-based access to the Volume. It suits
  personal installation but is restricted on Google Play.
- **Android 11 or later**, `arm64-v8a` phones only.
- **Volume type hidden by FUSE.** Android exposes the Volume as `fuse`, so Storage
  cannot see the FAT32 limits (4 GiB maximum file size, case-insensitive names).
  This does not matter for reading but must be solved before writing.
- **USB attribution.** Android does not link a USB device to a storage Volume. The
  check uses USB identifiers only when exactly one Apple device is attached.
- **No safe removal from the app.** Android does not let applications eject a
  Volume; the user ejects the iPod from the system storage settings.
- **Debug signing.** Each build machine signs with its own debug key, so
  installing a build from another machine requires uninstalling the previous one.
- **Temporary Storage path.** Under Chaquopy, Storage selects its Linux adapter
  because Python reports the platform as `linux`. It works for reading but is not
  the intended Android adapter.

## Remaining work

Steps 1 to 7 each depend on the previous one.

1. **Run the check on the phone.** The report should confirm that the Volume is visible and readable, the
   filesystem actually used, the USB serial number, and whether that serial
   matches the FireWire GUID bound to `HashInfo`.
2. **Probe filesystem behavior.** On a scratch directory of the iPod Volume,
   measure what Storage Transactions rely on: rename over an existing file,
   `fsync`, `flock`, free space, file timestamps, and behavior when the cable is
   pulled. This is the first step that writes, and only to a scratch directory.
3. **Record the decision.** An ADR covering the Kotlin and Chaquopy approach, the
   minimum Android version, the Volume access method, the media-tool strategy, and
   the related updates to `GLOSSARY.md` (**Host** and **iOpenPod** currently mean a
   desktop computer and a desktop product) and to ADR-0001, ADR-0060, ADR-0081,
   ADR-0103, and ADR-0107.
4. **Android Storage adapter.** A dedicated `PlatformAdapter` fed by Android's
   Volume and USB observations: the real filesystem type and limits, Volume
   Identity from the Volume UUID, and Connection Generations driven by attach,
   detach, and mount events. Injected explicitly by the Android entry point
   instead of the Linux adapter.
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
