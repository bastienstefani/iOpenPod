"""Read-only Android connection check over virtual Volumes."""

import base64
import json
from pathlib import Path

import pytest

from iOpenPod.android.read_only_check import (
    CheckReport,
    CheckRequest,
    StepStatus,
    UsbDeviceObservation,
    parse_request,
    report_text,
    run_check,
    run_check_json,
)
from iPodDB.iTunesDB.cdb import compress_iTunesCDB
from iPodDB.library import IPodLibrary
from storage import (
    AccessMode,
    FilesystemSession,
    MountedVolume,
    Storage,
    VolumeDisconnectedError,
)
from storage.testing import VirtualStoragePlatform

_GUID = bytes.fromhex("000A270012345678")
_NANO5 = UsbDeviceObservation(
    vendor_id=0x05AC,
    product_id=0x1265,
    product_name="iPod",
    manufacturer="Apple Inc.",
    serial=_GUID.hex().upper(),
)


_FIXTURE = Path(__file__).parents[2] / "fixtures" / "iTunesDB"


def _captured_database() -> bytes:
    return base64.b64decode(
        b"".join((_FIXTURE / "captured-album-index-36.b64").read_bytes().split()),
        validate=True,
    )


def _install_hashinfo(root: Path, uuid: bytes) -> None:
    (root / "iPod_Control" / "Device" / "HashInfo").write_bytes(
        b"HASHv0" + uuid + bytes(range(20, 32)) + bytes(range(16))
    )


def _nano5_volume(tmp_path: Path) -> Path:
    root = tmp_path / "nano"
    device = root / "iPod_Control" / "Device"
    database = root / "iPod_Control" / "iTunes"
    device.mkdir(parents=True)
    database.mkdir(parents=True)
    (device / "SysInfo").write_text(
        f"ModelNumStr: MC027\nFirewireGuid: {_GUID.hex().upper()}\n",
        encoding="utf-8",
    )
    _install_hashinfo(root, _GUID + bytes(12))
    (database / "iTunesCDB").write_bytes(compress_iTunesCDB(_captured_database()))
    return root


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _step(report: CheckReport, name: str) -> tuple[StepStatus, str]:
    matches = [step for step in report.steps if step.name == name]
    assert len(matches) == 1, [step.name for step in report.steps]
    return matches[0].status, matches[0].detail


def _check(
    root: Path,
    storage: Storage,
    *devices: UsbDeviceObservation,
) -> CheckReport:
    return run_check(
        CheckRequest(mount_point=str(root), usb_devices=devices),
        storage,
        mountinfo=lambda: "",
    )


def test_nano5_check_reads_library_and_signing_material(tmp_path: Path) -> None:
    root = _nano5_volume(tmp_path)
    platform = VirtualStoragePlatform()
    platform.add_volume(root, label="Nano")

    report = _check(root, Storage(platform), _NANO5)

    assert report.passed, report_text(report)
    assert _step(report, "Storage inspection")[0] is StepStatus.PASSED
    status, detail = _step(report, "Device Registry")
    assert status is StepStatus.PASSED
    assert "status=exact" in detail
    assert "iPod Nano 5th Gen" in detail
    assert "checksum=hash72" in detail
    status, detail = _step(report, "HASH72 material")
    assert status is StepStatus.PASSED
    assert "000A270012345678" in detail
    assert _step(report, "iTunesCDB")[0] is StepStatus.PASSED
    expected = IPodLibrary.parse(_captured_database()).snapshot.tracks
    assert report.track_count == len(expected) == 60
    assert [
        (row.title, row.artist, row.album, row.length_ms) for row in report.tracks
    ] == [
        (track.title, track.artist, track.album, track.length_ms) for track in expected
    ]


def test_check_never_changes_the_volume(tmp_path: Path) -> None:
    root = _nano5_volume(tmp_path)
    platform = VirtualStoragePlatform()
    platform.add_volume(root, label="Nano")
    before = _tree(root)

    _check(root, Storage(platform), _NANO5)

    assert _tree(root) == before


def test_hashinfo_for_another_guid_is_a_warning(tmp_path: Path) -> None:
    root = _nano5_volume(tmp_path)
    _install_hashinfo(root, bytes(range(20)))
    platform = VirtualStoragePlatform()
    platform.add_volume(root, label="Nano")

    report = _check(root, Storage(platform), _NANO5)

    status, detail = _step(report, "HASH72 material")
    assert status is StepStatus.WARNING
    assert "not bound" in detail
    assert report.track_count == 60


def test_several_apple_devices_are_not_attributed_to_the_volume(
    tmp_path: Path,
) -> None:
    root = _nano5_volume(tmp_path)
    platform = VirtualStoragePlatform()
    platform.add_volume(root, label="Nano")
    other = UsbDeviceObservation(vendor_id=0x05AC, product_id=0x1261, serial="1")

    report = _check(root, Storage(platform), _NANO5, other)

    status, detail = _step(report, "Device Registry")
    assert status is StepStatus.PASSED
    assert "iPod Nano 5th Gen" in detail
    assert "Classic" not in detail
    # SysInfo still supplies the FireWire GUID for HASH72 material.
    assert _step(report, "HASH72 material")[0] is StepStatus.PASSED


def test_storage_failure_falls_back_to_diagnostic_reads(tmp_path: Path) -> None:
    root = _nano5_volume(tmp_path)

    report = _check(root, Storage(VirtualStoragePlatform()), _NANO5)

    assert not report.passed
    assert _step(report, "Storage inspection")[0] is StepStatus.FAILED
    assert _step(report, "Direct read fallback")[0] is StepStatus.WARNING
    assert report.track_count == 60


def test_volume_without_ipod_control_fails(tmp_path: Path) -> None:
    root = tmp_path / "usb"
    root.mkdir()
    platform = VirtualStoragePlatform()
    platform.add_volume(root, label="USB")

    report = _check(root, Storage(platform))

    assert not report.passed
    assert _step(report, "iPod_Control")[0] is StepStatus.FAILED
    assert _step(report, "USB devices")[0] is StepStatus.WARNING
    assert report.track_count == 0


def test_mount_records_name_only_the_volume(tmp_path: Path) -> None:
    root = tmp_path / "1234-ABCD"
    root.mkdir()
    mountinfo = (
        "41 30 0:52 / /storage/1234-ABCD rw - fuse /dev/fuse rw\n"
        "42 30 0:53 / /storage/emulated rw - fuse /dev/fuse rw\n"
    )

    report = run_check(
        CheckRequest(mount_point=str(root)),
        Storage(VirtualStoragePlatform()),
        mountinfo=lambda: mountinfo,
    )

    status, detail = _step(report, "Mount records")
    assert status is StepStatus.PASSED
    assert detail == "41 30 0:52 / /storage/1234-ABCD rw - fuse /dev/fuse rw"


def test_parse_request_reads_android_payload() -> None:
    request = parse_request(
        json.dumps(
            {
                "mount_point": "/storage/1234-ABCD",
                "usb_devices": [
                    {"vendor_id": 1452, "product_id": 4709, "serial": "ABC"}
                ],
                "host": {"sdk": 34, "model": "Pixel"},
                "track_limit": 10,
            }
        )
    )

    assert request == CheckRequest(
        mount_point="/storage/1234-ABCD",
        usb_devices=(
            UsbDeviceObservation(vendor_id=1452, product_id=4709, serial="ABC"),
        ),
        host=(("sdk", "34"), ("model", "Pixel")),
        track_limit=10,
    )


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "{}",
        '{"mount_point": "/x", "usb_devices": {}}',
        '{"mount_point": "/x", "usb_devices": [{"vendor_id": "1"}]}',
        '{"mount_point": "/x", "track_limit": -1}',
    ],
)
def test_parse_request_rejects_malformed_payloads(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_request(payload)


def test_json_entry_point_reports_errors_instead_of_raising() -> None:
    result = json.loads(run_check_json("not json"))

    assert result["passed"] is False
    assert "[FAILED] Request" in result["text"]


def test_report_text_lists_steps_and_tracks(tmp_path: Path) -> None:
    root = _nano5_volume(tmp_path)
    platform = VirtualStoragePlatform()
    platform.add_volume(root, label="Nano")

    report = _check(root, Storage(platform), _NANO5)
    text = report_text(report)

    first = report.tracks[0]
    assert "Result: passed" in text
    assert "[PASSED] iPodDB Library: 60 Tracks" in text
    assert f"   1. {first.title} - {first.artist} - {first.album} (" in text


class _SessionRefusingStorage(Storage):
    def open_session(
        self,
        mounted_volume: MountedVolume,
        *,
        access: AccessMode = AccessMode.READ_ONLY,
    ) -> FilesystemSession:
        raise VolumeDisconnectedError("unplugged during the check")


def test_session_failure_is_reported_once_under_its_own_step(tmp_path: Path) -> None:
    root = _nano5_volume(tmp_path)
    platform = VirtualStoragePlatform()
    platform.add_volume(root, label="Nano")

    report = _check(root, _SessionRefusingStorage(platform), _NANO5)

    assert _step(report, "Storage inspection")[0] is StepStatus.PASSED
    status, detail = _step(report, "Read-only Filesystem Session")
    assert status is StepStatus.FAILED
    assert "unplugged" in detail
    assert report.track_count == 60
