# iOpenPod for Android: read-only check

This directory holds the first Android increment described in
[Android Host feasibility research](../docs/research/android-host-feasibility.md).
[Android port status](../docs/android.md) lists what is verified, the limits, and
the remaining work.
The app checks that an Android phone can read an iPod connected through a USB OTG
adapter. **It never writes to the iPod.**

Android does not let applications reach USB Volumes by path, so the user grants
read access to the iPod's root once in the system file picker. A Kotlin activity
owns that grant, Volume enumeration, and USB observations. Chaquopy embeds Python
3.12 and runs `iOpenPod.android.read_only_check`, which reads the Volume through
the grant, identifies the iPod through Device Registry, and parses the Library
with iPodDB.
The Python packages ship from the repository's `src` directory; the desktop GUI
is not included.

## iPod prerequisites

- The iPod is formatted for Windows (FAT32). Android cannot mount a Mac-formatted
  (HFS+) iPod.
- An iPod Nano 5th generation has been synced at least once by iTunes, so HASH72
  signing material exists. The check reports whether it was found.

## Phone prerequisites

- Android 11 (API 30) or later, with USB OTG mass-storage support.

## Build

Requirements:

- JDK 17 or later.
- Android SDK with platform 36, located through `ANDROID_HOME` or
  `android/local.properties` (`sdk.dir=...`).
- Python 3.12 for Chaquopy's build step. Chaquopy requires the same minor
  version as the app. The repository's UV environment provides one.
- Network access to Google Maven, Maven Central, and `chaquo.com`.

```shell
cd android
./gradlew assembleDebug -Piopenpod.buildPython="$(uv python find 3.12)"
```

The APK is written to `app/build/outputs/apk/debug/app-debug.apk`. It contains the
`arm64-v8a` runtime only, which covers current phones.

Debug builds are signed with the building machine's debug key. Installing a build
from another machine over an existing installation fails until the old one is
uninstalled.

## Install and use

1. Copy the APK to the phone and allow installation from that source, or run
   `adb install -r app/build/outputs/apk/debug/app-debug.apk`.
2. Connect the iPod with the OTG adapter. If Android asks which app should open
   the iPod, choose iOpenPod: this also grants USB access, which lets the check
   read the USB serial number. **Allow USB access** requests it otherwise.
3. Choose **Allow access to the iPod**. In the file picker, stay at the iPod's
   root and confirm. iOpenPod keeps read access only, and only to that Volume.
4. Choose **Run read-only check**.
5. Use **Share report** or **Copy report** to send the result.

Eject the iPod from Android's storage settings before unplugging it.
