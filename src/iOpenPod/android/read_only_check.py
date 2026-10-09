"""Read-only connection check run by the Android Host interface.

The Android interface supplies the Mount Point of one mounted Volume, the USB
devices it can see, a description of the Host and, when the user granted one, a
read-only Storage Access Framework document tree for the Volume. The check reads
identity metadata and the iTunesDB or iTunesCDB, identifies the iPod through
Device Registry, and projects the Library through iPodDB. It never writes to the
Volume.

Android does not expose USB Volumes to applications by path, so the document tree
is the expected route. Without one, the check opens a read-only Filesystem Session
through Storage; if Storage cannot inspect the Mount Point, it records that failure
and continues with direct reads below the Mount Point. Document-tree and direct
reads are diagnostic evidence only: no workflow may build on them until Storage
owns document-tree access.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from device_registry import (
    DEFAULT_DEVICE_REGISTRY,
    DatabaseChecksum,
    DeviceEvidence,
    DeviceIdentifier,
    EvidenceAuthority,
    IdentificationResult,
    UsbIdentifier,
    parse_sysinfo,
    parse_sysinfo_extended,
)
from iPodDB.library import IPodLibrary, parse_hash72_info, recover_hash72_material
from storage import AccessMode, DevicePath, Storage

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from storage import FilesystemSession

APPLE_USB_VENDOR_ID = 0x05AC
DEFAULT_TRACK_LIMIT = 500
MOUNTINFO_PATH = Path("/proc/self/mountinfo")

_METADATA_LIMIT = 1024 * 1024
_DATABASE_LIMIT = 256 * 1024 * 1024
_USB_SOURCE = "Android UsbManager"

_IPOD_CONTROL = DevicePath("iPod_Control")
_SYSINFO = DevicePath("iPod_Control/Device/SysInfo")
_SYSINFO_EXTENDED = DevicePath("iPod_Control/Device/SysInfoExtended")
_HASHINFO = DevicePath("iPod_Control/Device/HashInfo")
_ITUNESCDB = DevicePath("iPod_Control/iTunes/iTunesCDB")
_ITUNESDB = DevicePath("iPod_Control/iTunes/iTunesDB")


class StepStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UsbDeviceObservation:
    """One USB device as reported by Android, without iPod interpretation."""

    vendor_id: int
    product_id: int
    product_name: str = ""
    manufacturer: str = ""
    serial: str = ""


@dataclass(frozen=True, slots=True)
class CheckRequest:
    mount_point: str
    usb_devices: tuple[UsbDeviceObservation, ...] = ()
    host: tuple[tuple[str, str], ...] = ()
    track_limit: int = DEFAULT_TRACK_LIMIT


@dataclass(frozen=True, slots=True)
class CheckStep:
    name: str
    status: StepStatus
    detail: str


@dataclass(frozen=True, slots=True)
class TrackRow:
    title: str
    artist: str
    album: str
    length_ms: int


@dataclass(frozen=True, slots=True)
class CheckReport:
    mount_point: str
    steps: tuple[CheckStep, ...]
    device_name: str = ""
    track_count: int = 0
    playlist_count: int = 0
    tracks: tuple[TrackRow, ...] = ()

    @property
    def passed(self) -> bool:
        return all(step.status is not StepStatus.FAILED for step in self.steps)


class DocumentTree(Protocol):
    """Read access to one Volume root granted through the Storage Access Framework.

    The Android interface implements it. Paths are Device Paths in POSIX form.
    """

    def describe(self) -> str: ...

    def exists(self, path: str) -> bool: ...

    def openRead(self, path: str) -> int:
        """Return a detached file descriptor that the caller must close."""
        ...


class _DeviceReader(Protocol):
    def exists(self, path: DevicePath) -> bool: ...

    def read(self, path: DevicePath, *, max_bytes: int) -> bytes: ...


class _SessionReader:
    """Read Device Paths through a read-only Filesystem Session."""

    def __init__(self, session: FilesystemSession) -> None:
        self._session = session

    def exists(self, path: DevicePath) -> bool:
        return self._session.exists(path)

    def read(self, path: DevicePath, *, max_bytes: int) -> bytes:
        return self._session.read(path, max_bytes=max_bytes)


class _DirectReader:
    """Diagnostic-only reads below a Mount Point when Storage is unavailable."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def exists(self, path: DevicePath) -> bool:
        return self._root.joinpath(*path.parts).exists()

    def read(self, path: DevicePath, *, max_bytes: int) -> bytes:
        target = self._root.joinpath(*path.parts)
        with target.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"{path} exceeds the {max_bytes}-byte read limit")
        return data


class _DocumentTreeReader:
    """Diagnostic-only reads through a user-granted Android document tree."""

    def __init__(self, tree: DocumentTree) -> None:
        self._tree = tree

    def exists(self, path: DevicePath) -> bool:
        return bool(self._tree.exists(str(path)))

    def read(self, path: DevicePath, *, max_bytes: int) -> bytes:
        with os.fdopen(int(self._tree.openRead(str(path))), "rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"{path} exceeds the {max_bytes}-byte read limit")
        return data


class _Steps:
    def __init__(self) -> None:
        self._items: list[CheckStep] = []

    def add(self, name: str, status: StepStatus, detail: str) -> None:
        self._items.append(CheckStep(name, status, detail))

    def items(self) -> tuple[CheckStep, ...]:
        return tuple(self._items)


def parse_request(text: str) -> CheckRequest:
    """Validate the JSON request sent by the Android interface."""

    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("The check request must be a JSON object")
    mount_point = payload.get("mount_point")
    if not isinstance(mount_point, str) or not mount_point:
        raise ValueError("The check request requires a mount_point")
    devices = payload.get("usb_devices", [])
    if not isinstance(devices, list):
        raise ValueError("usb_devices must be a list")
    host = payload.get("host", {})
    if not isinstance(host, dict):
        raise ValueError("host must be an object")
    track_limit = payload.get("track_limit", DEFAULT_TRACK_LIMIT)
    if not isinstance(track_limit, int) or track_limit < 0:
        raise ValueError("track_limit must be a non-negative integer")
    return CheckRequest(
        mount_point=mount_point,
        usb_devices=tuple(_usb_device(item) for item in devices),
        host=tuple((str(key), str(value)) for key, value in host.items()),
        track_limit=track_limit,
    )


def _usb_device(item: object) -> UsbDeviceObservation:
    if not isinstance(item, dict):
        raise ValueError("Each USB device must be an object")
    vendor_id = item.get("vendor_id")
    product_id = item.get("product_id")
    if not isinstance(vendor_id, int) or not isinstance(product_id, int):
        raise ValueError("USB devices require integer vendor_id and product_id")
    return UsbDeviceObservation(
        vendor_id=vendor_id,
        product_id=product_id,
        product_name=_text(item, "product_name"),
        manufacturer=_text(item, "manufacturer"),
        serial=_text(item, "serial"),
    )


def _text(item: Mapping[object, object], key: str) -> str:
    value = item.get(key)
    return value if isinstance(value, str) else ""


def run_check(
    request: CheckRequest,
    storage: Storage,
    *,
    mountinfo: Callable[[], str] | None = None,
    document_tree: DocumentTree | None = None,
) -> CheckReport:
    """Inspect one Volume without writing to it."""

    steps = _Steps()
    root = Path(request.mount_point)
    steps.add("Host", StepStatus.PASSED, _host_summary(request))
    _record_mount_records(steps, root, mountinfo or _read_mountinfo)
    _record_usb_devices(steps, request.usb_devices)

    if document_tree is not None:
        steps.add(
            "Volume access",
            StepStatus.PASSED,
            f"read-only document tree {document_tree.describe()}",
        )
        return _inspect_device(request, _DocumentTreeReader(document_tree), steps)
    steps.add(
        "Volume access",
        StepStatus.WARNING,
        "no document tree granted; trying the Mount Point path",
    )

    session: FilesystemSession | None = None
    reader: _DeviceReader
    stage = "Storage inspection"
    try:
        mounted = storage.inspect(root)
        volume = mounted.volume
        capabilities = volume.capabilities
        steps.add(
            "Storage inspection",
            StepStatus.PASSED,
            f"filesystem={volume.filesystem_type or 'unknown'} "
            f"label={volume.label!r} volume={volume.id.value} "
            f"readable={capabilities.readable} "
            f"safe_for_writes={capabilities.safe_for_writes} "
            f"case_sensitive={capabilities.case_sensitive} "
            f"max_file_size={capabilities.max_file_size_bytes} "
            f"total={_size(volume.total_bytes)} "
            f"available={_size(volume.available_bytes)}"
            + (
                f" unsafe_write_reasons={list(capabilities.unsafe_write_reasons)}"
                if capabilities.unsafe_write_reasons
                else ""
            ),
        )
        stage = "Read-only Filesystem Session"
        session = storage.open_session(mounted, access=AccessMode.READ_ONLY)
        steps.add(stage, StepStatus.PASSED, "opened")
        reader = _SessionReader(session)
    except Exception as error:  # Diagnostic boundary: report every failure.
        steps.add(stage, StepStatus.FAILED, _describe(error))
        steps.add(
            "Direct read fallback",
            StepStatus.WARNING,
            "continuing with diagnostic-only reads below the Mount Point",
        )
        reader = _DirectReader(root)

    try:
        return _inspect_device(request, reader, steps)
    finally:
        if session is not None:
            session.close()


def _inspect_device(
    request: CheckRequest,
    reader: _DeviceReader,
    steps: _Steps,
) -> CheckReport:
    mount_point = request.mount_point
    try:
        has_control = reader.exists(_IPOD_CONTROL)
    except Exception as error:  # Diagnostic boundary: report every failure.
        steps.add("iPod_Control", StepStatus.FAILED, _describe(error))
        return CheckReport(mount_point, steps.items())
    if not has_control:
        steps.add("iPod_Control", StepStatus.FAILED, "not found at the Volume root")
        return CheckReport(mount_point, steps.items())
    steps.add("iPod_Control", StepStatus.PASSED, "present")

    evidence = _usb_evidence(request.usb_devices)
    for path, parser in (
        (_SYSINFO, parse_sysinfo),
        (_SYSINFO_EXTENDED, parse_sysinfo_extended),
    ):
        payload = _read_optional(reader, path, steps)
        if payload:
            try:
                evidence = evidence.merged_with(parser(payload, source=path.name))
            except ValueError as error:
                steps.add(path.name, StepStatus.WARNING, _describe(error))
    identification = DEFAULT_DEVICE_REGISTRY.identify(evidence)
    steps.add(
        "Device Registry",
        (
            StepStatus.PASSED
            if identification.profile is not None or identification.candidates
            else StepStatus.WARNING
        ),
        _identification_summary(identification),
    )

    database_path = _ITUNESDB
    try:
        if reader.exists(_ITUNESCDB):
            database_path = _ITUNESCDB
        database = reader.read(database_path, max_bytes=_DATABASE_LIMIT)
    except Exception as error:  # Diagnostic boundary: report every failure.
        steps.add(database_path.name, StepStatus.FAILED, _describe(error))
        return CheckReport(mount_point, steps.items())
    if not database:
        steps.add(database_path.name, StepStatus.FAILED, "the database file is empty")
        return CheckReport(mount_point, steps.items())
    steps.add(database_path.name, StepStatus.PASSED, f"{len(database)} bytes read")

    _record_hash72(reader, database, evidence, identification, steps)

    try:
        snapshot = IPodLibrary.parse(database).snapshot
    except Exception as error:  # Diagnostic boundary: report every failure.
        steps.add("iPodDB Library", StepStatus.FAILED, _describe(error))
        return CheckReport(mount_point, steps.items())
    steps.add(
        "iPodDB Library",
        StepStatus.PASSED,
        f"{len(snapshot.tracks)} Tracks, {len(snapshot.playlists)} Playlists, "
        f"device name {snapshot.device_name!r}",
    )
    return CheckReport(
        mount_point=mount_point,
        steps=steps.items(),
        device_name=snapshot.device_name,
        track_count=len(snapshot.tracks),
        playlist_count=len(snapshot.playlists),
        tracks=tuple(
            TrackRow(track.title, track.artist, track.album, track.length_ms)
            for track in snapshot.tracks[: request.track_limit]
        ),
    )


def _read_optional(reader: _DeviceReader, path: DevicePath, steps: _Steps) -> bytes:
    try:
        if not reader.exists(path):
            steps.add(path.name, StepStatus.WARNING, "absent")
            return b""
        payload = reader.read(path, max_bytes=_METADATA_LIMIT)
    except Exception as error:  # Diagnostic boundary: report every failure.
        steps.add(path.name, StepStatus.WARNING, _describe(error))
        return b""
    steps.add(path.name, StepStatus.PASSED, f"{len(payload)} bytes read")
    return payload


def _usb_evidence(devices: tuple[UsbDeviceObservation, ...]) -> DeviceEvidence:
    """Attribute USB facts only when exactly one Apple device is attached.

    Android does not link a UsbDevice to a StorageVolume, so several Apple
    devices would make the attribution a guess.
    """

    apple = [device for device in devices if device.vendor_id == APPLE_USB_VENDOR_ID]
    if len(apple) != 1:
        return DeviceEvidence()
    device = apple[0]
    serial = _transport_serial(device.serial)
    return DeviceEvidence(
        usb_identifiers=(
            DeviceIdentifier(
                value=UsbIdentifier(device.vendor_id, device.product_id),
                source=_USB_SOURCE,
                authority=EvidenceAuthority.CURRENT_HARDWARE,
            ),
        ),
        transport_serials=(
            (
                DeviceIdentifier(
                    value=serial,
                    source=_USB_SOURCE,
                    authority=EvidenceAuthority.CURRENT_HARDWARE,
                ),
            )
            if serial
            else ()
        ),
    )


def _transport_serial(value: str) -> str:
    normalized = "".join(
        character
        for character in value.upper().removeprefix("0X")
        if character.isalnum()
    )
    return "" if not normalized or set(normalized) == {"0"} else normalized


def _firewire_guid(evidence: DeviceEvidence) -> bytes:
    """Return the single highest-authority 8-byte transport serial, if any."""

    if not evidence.transport_serials:
        return b""
    authority = max(item.authority for item in evidence.transport_serials)
    candidates = {
        item.value.lower()
        for item in evidence.transport_serials
        if item.authority == authority
    }
    if len(candidates) != 1:
        return b""
    try:
        guid = bytes.fromhex(next(iter(candidates)))
    except ValueError:
        return b""
    return guid if len(guid) == 8 else b""


def _record_hash72(
    reader: _DeviceReader,
    database: bytes,
    evidence: DeviceEvidence,
    identification: IdentificationResult,
    steps: _Steps,
) -> None:
    """Report whether HASH72 signing material is available, without signing."""

    profiles = (
        (identification.profile,)
        if identification.profile is not None
        else identification.candidates
    )
    if not profiles or not any(
        DatabaseChecksum.HASH72
        in (
            profile.capabilities.database.checksum,
            profile.capabilities.database.sqlite_checksum,
        )
        for profile in profiles
    ):
        return
    guid = _firewire_guid(evidence)
    guid_text = guid.hex().upper() if guid else "unknown"
    try:
        if reader.exists(_HASHINFO):
            material = parse_hash72_info(
                reader.read(_HASHINFO, max_bytes=_METADATA_LIMIT)
            )
            if guid and material.belongs_to_guid(guid):
                steps.add(
                    "HASH72 material",
                    StepStatus.PASSED,
                    f"HashInfo matches FireWire GUID {guid_text}",
                )
            else:
                steps.add(
                    "HASH72 material",
                    StepStatus.WARNING,
                    f"HashInfo present but not bound to FireWire GUID {guid_text}",
                )
            return
        recover_hash72_material(database)
    except Exception as error:  # Diagnostic boundary: report every failure.
        steps.add(
            "HASH72 material",
            StepStatus.WARNING,
            f"not recoverable from HashInfo or the database: {_describe(error)}",
        )
        return
    steps.add(
        "HASH72 material",
        StepStatus.PASSED,
        f"recovered from the retained database; FireWire GUID {guid_text}",
    )


def _identification_summary(result: IdentificationResult) -> str:
    parts = [f"status={result.status.value}"]
    if result.profile is not None:
        profile = result.profile
        database = profile.capabilities.database
        parts.append(
            f"profile={profile.display_name} {profile.advertised_capacity} "
            f"{profile.finish} ({profile.model_number})"
        )
        parts.append(
            f"checksum={database.checksum.value} "
            f"compressed={database.supports_compressed_database} "
            f"sqlite={database.uses_sqlite_database}"
        )
    elif result.candidates:
        names = sorted({profile.display_name for profile in result.candidates})
        parts.append(f"{len(result.candidates)} candidates: {', '.join(names)}")
    parts.extend(issue.code.value for issue in result.issues)
    return " ".join(parts)


def _host_summary(request: CheckRequest) -> str:
    host = " ".join(f"{key}={value}" for key, value in request.host)
    runtime = (
        f"python={platform.python_version()} sys.platform={sys.platform} "
        f"machine={platform.machine()}"
    )
    return f"{host} {runtime}".strip()


def _record_mount_records(
    steps: _Steps,
    root: Path,
    mountinfo: Callable[[], str],
) -> None:
    """Record raw mount-table lines naming the Volume for adapter design."""

    try:
        lines = [line for line in mountinfo().splitlines() if root.name in line]
    except OSError as error:
        steps.add("Mount records", StepStatus.WARNING, _describe(error))
        return
    steps.add(
        "Mount records",
        StepStatus.PASSED if lines else StepStatus.WARNING,
        "\n".join(lines) if lines else f"no mount-table line names {root.name!r}",
    )


def _read_mountinfo() -> str:
    return MOUNTINFO_PATH.read_text(encoding="utf-8", errors="replace")


def _record_usb_devices(
    steps: _Steps,
    devices: tuple[UsbDeviceObservation, ...],
) -> None:
    if not devices:
        steps.add("USB devices", StepStatus.WARNING, "none reported by Android")
        return
    steps.add(
        "USB devices",
        StepStatus.PASSED,
        "\n".join(
            f"{device.vendor_id:04x}:{device.product_id:04x} "
            f"{device.manufacturer!r} {device.product_name!r} "
            f"serial={device.serial or 'unavailable'}"
            for device in devices
        ),
    )


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _size(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value / (1024 * 1024):.1f} MiB"


def report_text(report: CheckReport) -> str:
    """Render the report for display and sharing from the phone."""

    lines = [
        "iOpenPod Android read-only check",
        f"Mount Point: {report.mount_point}",
        f"Result: {'passed' if report.passed else 'failed'}",
        "",
    ]
    for step in report.steps:
        detail_lines = step.detail.splitlines() or [""]
        lines.append(f"[{step.status.value.upper()}] {step.name}: {detail_lines[0]}")
        lines.extend(f"    {line}" for line in detail_lines[1:])
    if report.track_count:
        lines.extend(
            (
                "",
                f"Tracks ({report.track_count}, showing {len(report.tracks)}):",
            )
        )
        lines.extend(
            f"{index:4d}. {track.title} - {track.artist} - {track.album} "
            f"({_duration(track.length_ms)})"
            for index, track in enumerate(report.tracks, start=1)
        )
    return "\n".join(lines) + "\n"


def _duration(length_ms: int) -> str:
    seconds = max(length_ms, 0) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def run_check_json(
    request_json: str,
    document_tree: DocumentTree | None = None,
) -> str:
    """Chaquopy entry point: never raises, always returns a JSON report."""

    try:
        request = parse_request(request_json)
    except (ValueError, json.JSONDecodeError) as error:
        report = CheckReport("", (CheckStep("Request", StepStatus.FAILED, str(error)),))
    else:
        try:
            report = run_check(request, Storage(), document_tree=document_tree)
        except Exception as error:  # Diagnostic boundary: report every failure.
            report = CheckReport(
                request.mount_point,
                (CheckStep("Check", StepStatus.FAILED, _describe(error)),),
            )
    return json.dumps(
        {
            "passed": report.passed,
            "track_count": report.track_count,
            "text": report_text(report),
        }
    )
