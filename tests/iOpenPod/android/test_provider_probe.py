"""Document-provider write probe over a provider double."""

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from iOpenPod.android.provider_probe import (
    PROBE_PREFIX,
    ScratchViolationError,
    _Scratch,
    probe_text,
    run_probe,
    run_probe_json,
)
from iOpenPod.android.read_only_check import CheckRequest, CheckStep, StepStatus

_FAT_INVALID = re.compile(r'[\x00-\x1f"*/:<>?\\|\x7f]')


class _ProviderDouble:
    """Directory-backed double of Android's external storage document provider.

    Like the platform provider, it replaces FAT-invalid characters, never
    overwrites on create or rename, compares names ignoring case, and deletes
    directories recursively.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str]] = []

    def _file(self, path: str) -> Path:
        return self.root.joinpath(path) if path else self.root

    def _unique(self, parent: Path, name: str) -> str:
        name = _FAT_INVALID.sub("_", name)
        existing = {child.name.casefold() for child in parent.iterdir()}
        stem, dot, extension = name.rpartition(".")
        if not dot:
            stem, extension = name, ""
        candidate, number = name, 0
        while candidate.casefold() in existing:
            number += 1
            candidate = f"{stem} ({number}){dot}{extension}"
        return candidate

    def describe(self) -> str:
        return "content://double/tree/root"

    def exists(self, path: str) -> bool:
        return self._file(path).exists()

    def openRead(self, path: str) -> int:
        self.calls.append(("openRead", path))
        return os.open(self._file(path), os.O_RDONLY)

    def stat(self, path: str) -> str | None:
        target = self._file(path)
        if not target.exists():
            return None
        status = target.stat()
        return json.dumps(
            {
                "name": target.name,
                "size": status.st_size,
                "last_modified": status.st_mtime_ns // 1_000_000,
                "mime_type": "application/octet-stream",
                "flags": 0x2 | 0x4 | 0x40 | 0x100,
            }
        )

    def createDirectory(self, parent: str, name: str) -> str:
        self.calls.append(("createDirectory", f"{parent}/{name}"))
        chosen = self._unique(self._file(parent), name)
        self._file(parent).joinpath(chosen).mkdir()
        return chosen

    def createFile(self, parent: str, name: str) -> str:
        self.calls.append(("createFile", f"{parent}/{name}"))
        chosen = self._unique(self._file(parent), name)
        self._file(parent).joinpath(chosen).touch()
        return chosen

    def openWrite(self, path: str, mode: str) -> int:
        self.calls.append(("openWrite", path))
        flags = {
            "w": os.O_WRONLY | os.O_TRUNC,
            "wt": os.O_WRONLY | os.O_TRUNC,
            "rw": os.O_RDWR,
        }[mode]
        return os.open(self._file(path), flags)

    def rename(self, path: str, name: str) -> str:
        self.calls.append(("rename", path))
        source = self._file(path)
        chosen = self._unique(source.parent, name)
        source.rename(source.parent / chosen)
        return chosen

    def move(self, path: str, parent: str) -> str:
        self.calls.append(("move", path))
        source = self._file(path)
        chosen = self._unique(self._file(parent), source.name)
        source.rename(self._file(parent) / chosen)
        return chosen

    def delete(self, path: str) -> bool:
        self.calls.append(("delete", path))
        target = self._file(path)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return True


def _volume(tmp_path: Path) -> Path:
    root = tmp_path / "volume"
    database = root / "iPod_Control" / "iTunes"
    database.mkdir(parents=True)
    (database / "iTunesCDB").write_bytes(b"database")
    (root / "notes.txt").write_text("user file", encoding="utf-8")
    return root


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes() if path.is_file() else None
        )
        for path in sorted(root.rglob("*"))
    }


def _step(steps: tuple[CheckStep, ...], name: str) -> CheckStep:
    matches = [step for step in steps if step.name == name]
    assert len(matches) == 1, [step.name for step in steps]
    return matches[0]


def _request() -> CheckRequest:
    return CheckRequest(mount_point="/mnt/media_rw/0925-8B88")


def test_probe_measures_provider_and_leaves_the_volume_unchanged(
    tmp_path: Path,
) -> None:
    root = _volume(tmp_path)
    provider = _ProviderDouble(root)
    before = _tree(root)

    steps = run_probe(_request(), provider)

    assert _tree(root) == before
    assert all(step.status is not StepStatus.FAILED for step in steps), probe_text(
        "", steps
    )
    assert (
        _step(
            steps,
            "Read back",
        ).detail
        == "content matches the write"
    )
    assert (
        "mode 'w' truncates" in _step(steps, "Write mode 'w' over a larger file").detail
    )
    assert "'probe (1).bin'" in _step(steps, "Create an existing name").detail
    rename = _step(steps, "Rename onto an existing name")
    assert rename.status is StepStatus.WARNING
    assert "'target (1).bin'" in rename.detail
    assert "target kept" in rename.detail
    assert "'case (1).bin'" in _step(steps, "Names differing only by case").detail
    assert "'a_b_.bin'" in _step(steps, "FAT-invalid characters").detail
    assert _step(steps, "Delete scratch directory").status is StepStatus.PASSED


def test_probe_touches_only_its_scratch_directory(tmp_path: Path) -> None:
    provider = _ProviderDouble(_volume(tmp_path))

    run_probe(_request(), provider, scratch_name=PROBE_PREFIX + "test")

    mutations = [path for call, path in provider.calls if call not in {"openRead"}]
    assert mutations
    assert all(
        path.lstrip("/").startswith(PROBE_PREFIX + "test") for path in mutations
    ), mutations


def test_failed_measurement_still_deletes_the_scratch_directory(
    tmp_path: Path,
) -> None:
    root = _volume(tmp_path)
    before = _tree(root)

    class _BrokenWrites(_ProviderDouble):
        def openWrite(self, path: str, mode: str) -> int:
            raise OSError("provider refused the write")

    steps = run_probe(_request(), _BrokenWrites(root))

    assert _step(steps, "Write and fsync").status is StepStatus.FAILED
    assert _step(steps, "Delete scratch directory").status is StepStatus.PASSED
    assert _tree(root) == before


def test_existing_scratch_name_is_never_reused_or_deleted(tmp_path: Path) -> None:
    root = _volume(tmp_path)
    existing = root / (PROBE_PREFIX + "taken")
    existing.mkdir()
    (existing / "keep.bin").write_bytes(b"keep")

    steps = run_probe(_request(), _ProviderDouble(root), scratch_name=existing.name)

    assert _step(steps, "Create scratch directory").status is StepStatus.FAILED
    assert (existing / "keep.bin").read_bytes() == b"keep"
    assert len(steps) == 4


def test_renamed_scratch_directory_is_reported_and_removed(tmp_path: Path) -> None:
    root = _volume(tmp_path)
    before = _tree(root)

    class _RenamingProvider(_ProviderDouble):
        def createDirectory(self, parent: str, name: str) -> str:
            return super().createDirectory(parent, name + "_")

    steps = run_probe(_request(), _RenamingProvider(root))

    assert _step(steps, "Create scratch directory").status is StepStatus.FAILED
    assert _step(steps, "Delete scratch directory").status is StepStatus.PASSED
    assert _tree(root) == before


@pytest.mark.parametrize(
    "action",
    [
        lambda scratch: scratch.open_write("iPod_Control/iTunes/iTunesCDB", "wt"),
        lambda scratch: scratch.delete_contents("notes.txt"),
        lambda scratch: scratch.rename(PROBE_PREFIX + "guard/../notes.txt", "x"),
        lambda scratch: scratch.create_file("iPod_Control", "x.bin"),
        lambda scratch: scratch.move(PROBE_PREFIX + "guard/a.bin", "iPod_Control"),
    ],
)
def test_guard_refuses_paths_outside_the_scratch_directory(
    tmp_path: Path, action: Callable[[_Scratch], object]
) -> None:
    root = _volume(tmp_path)
    scratch = _Scratch(_ProviderDouble(root), PROBE_PREFIX + "guard")
    scratch.create()
    (root / scratch.root / "a.bin").write_bytes(b"a")
    before = _tree(root)

    with pytest.raises(ScratchViolationError):
        action(scratch)

    assert _tree(root) == before


def test_guard_refuses_mutation_before_the_scratch_directory_exists(
    tmp_path: Path,
) -> None:
    scratch = _Scratch(_ProviderDouble(_volume(tmp_path)), PROBE_PREFIX + "late")

    with pytest.raises(ScratchViolationError):
        scratch.create_file(scratch.root, "early.bin")
    assert scratch.delete_root()


def test_json_entry_point_rejects_bad_requests_without_touching_the_tree(
    tmp_path: Path,
) -> None:
    root = _volume(tmp_path)
    provider = _ProviderDouble(root)

    result = json.loads(run_probe_json("not json", provider))

    assert result["passed"] is False
    assert "[FAILED] Request" in result["text"]
    assert provider.calls == []


def test_json_entry_point_reports_the_probe(tmp_path: Path) -> None:
    provider = _ProviderDouble(_volume(tmp_path))

    result = json.loads(
        run_probe_json(json.dumps({"mount_point": "/mnt/media_rw/X"}), provider)
    )

    assert result["passed"] is True
    assert result["text"].startswith("iOpenPod Android document-provider probe\n")
    assert "[PASSED] Delete scratch directory" in result["text"]
