# Android Host Feasibility Research

Date: 2026-10-08

## Scope

This note evaluates whether iOpenPod can run on an Android phone that acts as the
Host for an iPod Nano 5th generation connected through a USB OTG cable. It records
the constraints, the reusable source, the candidate approaches, and the evidence
still missing. It is investigation only: it makes no decision, adds no product
behavior, and authorizes no device mutation. A decision belongs in an ADR after the
open questions below are answered.

## Summary

Running the desktop application unchanged on Android is not realistic. Its GUI is a
43k-line PySide6 desktop interface, and several dependencies have no Android build.
The lower boundaries are much more portable:

- iPodDB (about 27k lines) imports only the standard library and Pillow when its
  public `iPodDB.library` package is loaded. `wasmtime` (HASHAB, Nano 6th/7th
  generation) and `pycryptodome` are imported lazily and are not needed to read a
  Nano 5th generation Library.
- Device Registry (about 3k lines) is pure Python.
- Storage (about 12k lines) has a platform-neutral core behind the
  `storage.platform.base.PlatformAdapter` protocol. Only the native adapters are
  desktop-specific.
- The Application Layer workflows that carry the most domain logic, such as
  `sync_plan.py`, `sync_execution.py`, `library_sync_helper.py`, and
  `host_media_library.py`, do not import Qt. Controllers, settings, runtime, and
  status publication do.

The most promising direction is therefore a native Android interface over an
embedded Python runtime that hosts iPodDB, Device Registry, a new Android Storage
adapter, and the Qt-free Application Layer workflows. Feasibility depends on
several facts that can only be confirmed on a physical phone with the iPod
attached; a read-only spike should establish them before any write path is built.

## Target device facts

The Device Registry catalog describes the iPod Nano 5th generation as:

- USB vendor `0x05AC`, product `0x1265`, normal connection mode;
- 240 × 376 display, cover formats 1056, 1078, 1073, and 1074, Photo formats 1087,
  1079, and 1066;
- HASH72 database checksum, database version `0x30`, 14 music directories;
- compressed iTunesCDB plus an SQLite Library Artifact Set, also HASH72-signed;
- video up to 640 × 480 at 2500 kbit/s, gapless playback, subtitles, and captions.

HASH72 output requires retained signing material. The Application Layer reads it
from `iPod_Control/Device/HashInfo`, which is bound to the FireWire GUID, or recovers
it from the retained iTunes-signed database. An iPod Nano 5th generation that has
never been synced by iTunes has neither source, so iOpenPod cannot produce a valid
signed database for it on any Host.

ADR-0115 also notes that the built-in SQLite projection used for Nano 5th
generation saves follows the Original iOpenPod writer and is not yet proof of
physical Nano 5th generation firmware acceptance. That risk exists on every Host
and should be closed on desktop before an Android write path depends on it.

## Android Host constraints

### Filesystem format

Android mounts FAT32 (vfat) and exFAT Volumes from USB mass-storage devices. It does
not mount HFS+. An iPod Nano formatted by iTunes on a Mac is HFS+ and will appear as
an unmounted USB device. Only a Windows-formatted (FAT32) Nano is reachable through
the operating system's mount. Reformatting is a destructive, user-owned step outside
iOpenPod and also discards the HASH72 material held in the existing database.

### Volume access

Android exposes a mounted USB Volume as a public storage Volume, normally at
`/storage/XXXX-XXXX`. An application can reach it in three ways:

| Access path | How it works | Strengths | Limits |
| --- | --- | --- | --- |
| All files access (`MANAGE_EXTERNAL_STORAGE`, Android 11+) | Ordinary POSIX paths through Android's FUSE layer. | Existing path-based Storage code and Device Paths keep working. Rename, `fsync`, and direct reads are available. | Restricted on Google Play to qualifying app categories. Behavior of USB Volumes under FUSE, especially rename-over-existing, `fsync`, and `flock`, must be verified per device. |
| Storage Access Framework tree grant | The user selects the Volume root; the app uses `content://` document URIs. | Play-compliant, per-Volume user consent. | No paths: every Storage operation needs a file-descriptor or URI bridge. No atomic replace; rename and delete are separate IPC calls. Slow for large trees. |
| Userspace USB (USB Host API and a userspace FAT32 driver such as libaums) | The app claims the mass-storage interface and talks SCSI directly. | Raw access, including SCSI INQUIRY VPD pages used for identity evidence. | Detaches the kernel driver, so the system mount disappears. Requires a second, much less proven FAT32 writer, which conflicts with the device-safety goals. |

All files access is the only option that preserves the current Storage design with
modest change. It should be the first hypothesis tested; the Storage Access
Framework remains the fallback if the target phone refuses direct USB paths or if
store distribution becomes a requirement.

Android 10 and earlier do not let ordinary applications write to secondary Volumes
through file paths, so a path-based design implies a minimum of Android 11 (API 30).

### Device identity evidence

The Linux adapter's evidence sources (`/run/udev/data`, `/sys/class/block`, and
`SG_IO` SCSI inquiries) are unavailable to an unprivileged Android application.
Available evidence instead includes:

- USB vendor and product identifiers from `UsbManager`, readable without
  permission; `0x05AC:0x1265` identifies the Nano 5th generation family;
- the USB serial number, after the user grants USB permission;
- `SysInfo` and `SysInfoExtended` read through the Volume, which ADR-0115 already
  treats as optional metadata, plus `HashInfo` for HASH72 material;
- `StorageVolume` UUID and description as Volume Identity.

Whether the USB serial number equals the FireWire GUID that `HashInfo` is bound to
on this model is unverified and must be checked on hardware.

### Connection lifetime and safe removal

`ACTION_USB_DEVICE_ATTACHED`, `ACTION_USB_DEVICE_DETACHED`, `ACTION_MEDIA_MOUNTED`,
and related broadcasts can drive Connection Generations, which is closer to the
event-driven monitoring that desktop builds still lack. An unprivileged application
cannot unmount or eject a Volume. Safe removal must hand the user to the system
storage settings and only report success once Android reports the Volume as
unmounted, consistent with ADR-0060's rule that iOpenPod never claims removal it
did not observe.

### Python runtime and dependencies

Chaquopy (MIT-licensed) embeds CPython in an Android application and supports
Python 3.12, matching ADR-0001. Build-time consequences:

- the Android build requires Gradle and the Android SDK in addition to UV, which
  ADR-0001 currently names as the only command runner;
- native wheels must come from Chaquopy's package index because PyPI publishes no
  Android wheels for Pillow 11.3 through 12.3. Chaquopy's newest builds are Pillow
  11.0.0 and pycryptodome 3.21.0, below the desktop's Pillow 12.3.0 floor; see
  "Read-only check findings" for their test results;
- `librosa`/`numba` (Synesthesia), `wasmtime` (HASHAB), `keyring`, PySide6, and
  `winrt-*`/`pyobjc-*` would be excluded from the Android build;
- pure-Python dependencies such as `mutagen` and `feedparser` should install
  unchanged.

### Media tools

ADR-0081 and ADR-0103 require user-installed FFmpeg, FFprobe, and fpcalc and
install them through WinGet, Homebrew, or Linux package managers. Android has no
equivalent. Options are to bundle executables compiled with the NDK and shipped in
the native library directory, which Android permits to execute, or to transcode
through Android's `MediaCodec` and `MediaMuxer`. The Nano 5th generation plays MP3,
AAC, Apple Lossless, WAV, and AIFF directly, so a first Android scope can publish
compatible files only and defer conversion and Acoustic Fingerprints.

## Reusable source

| Area | Size | Android status |
| --- | --- | --- |
| iPodDB | ~27k lines | Reusable; needs Pillow on Android. |
| Device Registry | ~3k lines | Reusable unchanged. |
| Storage core | ~12k lines | Reusable behind a new Android `PlatformAdapter`; `native_platform_adapter()` would select the Linux adapter under Chaquopy because `sys.platform` is `linux`, so the Android composition root must inject its adapter explicitly. |
| Application Layer workflows | part of ~59k lines | Many are Qt-free and reusable; controllers, settings stores, runtime, status, and update channels are Qt- or desktop-bound. |
| GUI | ~43k lines | Not reusable on a phone. |

## Approaches considered

### A. Run the PySide6 application on Android

Qt for Python provides experimental Android deployment. It would reuse the most code
but carry a desktop interface (multi-pane windows, drag and drop, hover) to a phone,
and it still needs Android builds of every native dependency. The GUI would need a
redesign regardless, so the reuse advantage is mostly illusory.

### B. Native Android interface over embedded Python boundaries

A Kotlin interface owns presentation and Android integration (permissions, USB
broadcasts, Volume enumeration, safe-removal hand-off). Embedded Python runs iPodDB,
Device Registry, Storage with an Android adapter, and the Qt-free workflows.
Dependency direction is preserved: the Android interface is another presentation of
the iOpenPod Application Layer, and lower boundaries still never import it. The
lossless round-trip guarantee (ADR-0006) and the single definition-driven iPodDB
path (ADR-0008) stay intact because the same iPodDB code runs.

### C. Rewrite in Kotlin

Duplicates iPodDB's format knowledge in a second language, conflicting with
ADR-0008, and discards the existing round-trip tests. Not recommended.

## Documentation conflicts to resolve before a decision

- `GLOSSARY.md` defines **Host** as "the computer running iOpenPod" and
  **iOpenPod** as "the desktop product". An Android Host changes both definitions.
- ADR-0001 names UV as the only command runner; an Android build adds Gradle.
- ADR-0081 and ADR-0103 assume user-installed media tools.
- ADR-0060 assumes a native safe-removal service the application can call.
- ADR-0107 limits release assets to four desktop archives; an Android package
  would be a new release asset.

## Suggested increments

1. **Read-only spike.** A minimal Android application that requests All files
   access, lists storage Volumes and USB devices, finds `iPod_Control`, reads the
   iTunesCDB through `IPodLibrary.parse`, and lists Tracks. No device writes.
   It answers: is the Volume visible, are Device Paths readable, does iPodDB run
   under Chaquopy, and what USB serial does the Nano report. Implemented in
   `android/` and `iOpenPod.android.read_only_check`; awaiting a run on hardware.
2. **Filesystem semantics probe.** Against a scratch directory on the iPod Volume,
   measure rename-over-existing, `fsync`, `flock`, and free-space reporting through
   FUSE. Storage Transactions depend on these behaviors.
3. **Decision.** Record the approach, the minimum Android version, the Volume access
   path, the media-tool strategy, and the glossary changes in an ADR.
4. **Android Storage adapter and read-only browsing.** Discovery, Connection
   Generations from Android broadcasts, Active iPod selection, and Library browsing.
5. **Library edits.** Metadata and Playlist changes through Library Drafts and
   Storage Transactions, including HASH72 signing and verification.
6. **Adding compatible music.** Sync Execution limited to media the Nano plays
   without conversion.
7. **Conversion, artwork, Photos, Podcasts** as separate later increments.

## Read-only check findings

Evidence gathered while building the read-only check, before any hardware run:

- The iPodDB, Device Registry, Storage, and read-only check test suites pass
  unchanged with Pillow 11.0.0 and pycryptodome 3.21.0.
- `iPodDB.iTunesDB.writer.signature` imported `wasmtime` at module level, so HASH58
  and HASH72 failed wherever `wasmtime` is unavailable, including Android. The
  import now happens only when HASHAB is computed.
- Chaquopy supports PEP 420 namespace packages and honors source-set include and
  exclude filters, so the APK carries about 2 MB of Python source from `iPodDB`,
  `device_registry`, `storage`, and `iOpenPod/android`, without the desktop GUI.
- Under Chaquopy, `Storage()` selects the Linux adapter. It inspected a real
  tmpfs Mount Point using only `/proc/self/mountinfo` and tolerated the missing
  udev and sysfs data. On Android it will see the FUSE mount (`fuse`), not the
  underlying `vfat` Volume, so it cannot report FAT32 limits such as the 4 GiB
  maximum file size or case-insensitive names. The Android adapter must supply
  them before any write path is enabled.

## Prerequisites on the user's side

- The iPod Nano 5th generation is formatted for Windows (FAT32).
- It has been synced at least once by iTunes, so HASH72 material exists.
- The phone runs Android 11 or later and supports USB OTG mass storage.
- The phone shows the iPod as a USB storage Volume when connected.
