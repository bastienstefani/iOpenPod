"""Document-provider write probe run by the Android Host interface.

Android exposes USB Volumes to applications only through the Storage Access
Framework. Before Storage can perform Storage Transactions there, this probe
measures how the document provider behaves for creation, writes, ``fsync``,
renames, moves, deletion, timestamps, and free space.

Every mutation happens inside one new scratch directory at the Volume root. A
guard refuses any other path, the probe never opens an existing entry for writing,
and the scratch directory is deleted at the end even when a measurement fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import time
from typing import TYPE_CHECKING, Protocol

from iOpenPod.android.read_only_check import (
    CheckStep,
    DocumentTree,
    StepStatus,
    parse_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from iOpenPod.android.read_only_check import CheckRequest

PROBE_PREFIX = "iOpenPod-probe-"
PROBE_SIZE = 1024 * 1024
# 2001-01-01T00:00:00Z: even seconds survive FAT's two-second resolution.
PROBE_MTIME_NS = 978_307_200 * 1_000_000_000

# DocumentsContract.Document flags reported by the provider.
_DOCUMENT_FLAGS = {
    0x2: "write",
    0x4: "delete",
    0x8: "create",
    0x40: "rename",
    0x80: "copy",
    0x100: "move",
    0x200: "remove",
}


class EditableDocumentTree(DocumentTree, Protocol):
    """Provider primitives over a Volume root granted with write access."""

    def stat(self, path: str) -> str | None: ...

    def createDirectory(self, parent: str, name: str) -> str: ...

    def createFile(self, parent: str, name: str) -> str: ...

    def openWrite(self, path: str, mode: str) -> int: ...

    def rename(self, path: str, name: str) -> str: ...

    def move(self, path: str, parent: str) -> str: ...

    def delete(self, path: str) -> bool: ...


class ScratchViolationError(RuntimeError):
    """A probe operation targeted a path outside its scratch directory."""


class _Scratch:
    """Confine every mutation to one scratch directory created by the probe."""

    def __init__(self, tree: EditableDocumentTree, name: str) -> None:
        if "/" in name or not name.startswith(PROBE_PREFIX):
            raise ScratchViolationError(f"Invalid scratch directory name {name!r}")
        self._tree = tree
        self.root = name
        self.created = False

    def create(self) -> str:
        if self._tree.exists(self.root):
            raise ScratchViolationError(f"{self.root} already exists")
        chosen = str(self._tree.createDirectory("", self.root))
        if chosen != self.root:
            self.created = self._tree.exists(chosen)
            self.root = chosen
            raise ScratchViolationError(f"The provider created {chosen!r} instead")
        self.created = True
        return chosen

    def path(self, *names: str) -> str:
        for name in names:
            if not name or "/" in name or name in {".", ".."}:
                raise ScratchViolationError(f"Invalid name {name!r}")
        return "/".join((self.root, *names))

    def _inside(self, path: str) -> str:
        if not self.created or not path.startswith(self.root + "/"):
            raise ScratchViolationError(f"{path!r} is outside the scratch directory")
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise ScratchViolationError(f"Invalid path {path!r}")
        return path

    def _parent(self, path: str) -> str:
        if path == self.root and self.created:
            return path
        return self._inside(path)

    def stat(self, path: str) -> dict[str, object] | None:
        raw = self._tree.stat(self._inside(path))
        if raw is None:
            return None
        value = json.loads(str(raw))
        if not isinstance(value, dict):
            raise ValueError("The provider returned malformed metadata")
        return value

    def exists(self, path: str) -> bool:
        return bool(self._tree.exists(self._inside(path)))

    def create_file(self, parent: str, name: str) -> str:
        return str(self._tree.createFile(self._parent(parent), name))

    def create_directory(self, parent: str, name: str) -> str:
        return str(self._tree.createDirectory(self._parent(parent), name))

    def open_read(self, path: str) -> int:
        return int(self._tree.openRead(self._inside(path)))

    def open_write(self, path: str, mode: str) -> int:
        return int(self._tree.openWrite(self._inside(path), mode))

    def rename(self, path: str, name: str) -> str:
        return str(self._tree.rename(self._inside(path), name))

    def move(self, path: str, parent: str) -> str:
        return str(self._tree.move(self._inside(path), self._parent(parent)))

    def delete_contents(self, path: str) -> bool:
        return bool(self._tree.delete(self._inside(path)))

    def delete_root(self) -> bool:
        if not self.created:
            return True
        deleted = bool(self._tree.delete(self.root))
        return deleted and not self._tree.exists(self.root)


class _Steps:
    def __init__(self) -> None:
        self.items: list[CheckStep] = []

    def add(self, name: str, status: StepStatus, detail: str) -> None:
        self.items.append(CheckStep(name, status, detail))

    def measure(self, name: str, action: Callable[[], tuple[StepStatus, str]]) -> None:
        try:
            status, detail = action()
        except Exception as error:  # Diagnostic boundary: report every failure.
            status, detail = StepStatus.FAILED, f"{type(error).__name__}: {error}"
        self.add(name, status, detail)


def _write(scratch: _Scratch, path: str, mode: str, data: bytes) -> float:
    """Write through a provider descriptor, fsync it, and return the seconds taken."""

    started = time.perf_counter()
    with os.fdopen(scratch.open_write(path, mode), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return time.perf_counter() - started


def _read(scratch: _Scratch, path: str) -> bytes:
    with os.fdopen(scratch.open_read(path), "rb") as stream:
        return stream.read()


def _free_bytes(scratch: _Scratch, path: str) -> int:
    descriptor = scratch.open_read(path)
    try:
        usage = os.fstatvfs(descriptor)
    finally:
        os.close(descriptor)
    return usage.f_bavail * usage.f_frsize


def _flags(value: object) -> str:
    if not isinstance(value, int):
        return "unknown"
    return ",".join(name for bit, name in _DOCUMENT_FLAGS.items() if value & bit)


def run_probe(
    request: CheckRequest,
    tree: EditableDocumentTree,
    *,
    scratch_name: str | None = None,
) -> tuple[CheckStep, ...]:
    """Measure provider behavior inside a new scratch directory, then delete it."""

    steps = _Steps()
    host = " ".join(f"{key}={value}" for key, value in request.host)
    steps.add(
        "Host",
        StepStatus.PASSED,
        f"{host} python={platform.python_version()}".strip(),
    )
    steps.add("Volume access", StepStatus.PASSED, str(tree.describe()))
    scratch = _Scratch(tree, scratch_name or PROBE_PREFIX + secrets.token_hex(4))
    try:
        steps.measure(
            "Create scratch directory",
            lambda: (StepStatus.PASSED, scratch.create()),
        )
        if scratch.created and steps.items[-1].status is StepStatus.PASSED:
            _measure_provider(scratch, steps)
    finally:
        steps.measure(
            "Delete scratch directory",
            lambda: (
                (StepStatus.PASSED, f"{scratch.root} removed")
                if scratch.delete_root()
                else (
                    StepStatus.FAILED,
                    f"{scratch.root} remains; delete it from a file manager",
                )
            ),
        )
    return tuple(steps.items)


def _measure_provider(scratch: _Scratch, steps: _Steps) -> None:
    payload = os.urandom(PROBE_SIZE)
    probe = scratch.path("probe.bin")

    def create_file() -> tuple[StepStatus, str]:
        chosen = scratch.create_file(scratch.root, "probe.bin")
        if chosen != "probe.bin":
            raise ValueError(f"the provider chose {chosen!r} for 'probe.bin'")
        return StepStatus.PASSED, "probe.bin created with the requested name"

    steps.measure("Create file", create_file)
    if steps.items[-1].status is not StepStatus.PASSED:
        return
    free_before = _free_bytes(scratch, probe)

    def write() -> tuple[StepStatus, str]:
        seconds = _write(scratch, probe, "w", payload)
        rate = PROBE_SIZE / max(seconds, 1e-9) / (1024 * 1024)
        return (
            StepStatus.PASSED,
            f"1 MiB written and fsynced in {seconds:.3f} s ({rate:.1f} MiB/s)",
        )

    steps.measure("Write and fsync", write)

    def read_back() -> tuple[StepStatus, str]:
        data = _read(scratch, probe)
        if hashlib.sha256(data).digest() != hashlib.sha256(payload).digest():
            return (
                StepStatus.FAILED,
                f"read {len(data)} bytes that differ from the write",
            )
        return StepStatus.PASSED, "content matches the write"

    steps.measure("Read back", read_back)

    def free_space() -> tuple[StepStatus, str]:
        free_after = _free_bytes(scratch, probe)
        return (
            StepStatus.PASSED,
            f"fstatvfs reports {free_before} bytes free before and {free_after} "
            f"after the write ({free_before - free_after} bytes used)",
        )

    steps.measure("Free space", free_space)

    def metadata() -> tuple[StepStatus, str]:
        provider = scratch.stat(probe) or {}
        descriptor = scratch.open_read(probe)
        try:
            stat = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        size_matches = provider.get("size") == stat.st_size == PROBE_SIZE
        return (
            StepStatus.PASSED if size_matches else StepStatus.WARNING,
            f"provider size={provider.get('size')} "
            f"last_modified_ms={provider.get('last_modified')} "
            f"flags={_flags(provider.get('flags'))}; "
            f"fstat size={stat.st_size} mtime_ns={stat.st_mtime_ns}",
        )

    steps.measure("Metadata", metadata)

    def set_mtime() -> tuple[StepStatus, str]:
        descriptor = scratch.open_write(probe, "rw")
        try:
            if os.utime not in os.supports_fd:
                return StepStatus.WARNING, "os.utime does not accept descriptors"
            os.utime(descriptor, ns=(PROBE_MTIME_NS, PROBE_MTIME_NS))
            observed = os.fstat(descriptor).st_mtime_ns
        finally:
            os.close(descriptor)
        provider = scratch.stat(probe) or {}
        matches = abs(observed - PROBE_MTIME_NS) < 2_000_000_000
        return (
            StepStatus.PASSED if matches else StepStatus.WARNING,
            f"requested {PROBE_MTIME_NS}, fstat {observed}, "
            f"provider last_modified_ms={provider.get('last_modified')}",
        )

    steps.measure("Set modification time", set_mtime)

    def truncate_mode(mode: str, size: int) -> Callable[[], tuple[StepStatus, str]]:
        def action() -> tuple[StepStatus, str]:
            _write(scratch, probe, "wt", payload)
            _write(scratch, probe, mode, payload[:size])
            data = _read(scratch, probe)
            if data == payload[:size]:
                return StepStatus.PASSED, f"mode {mode!r} truncates"
            return (
                StepStatus.WARNING,
                f"mode {mode!r} left {len(data)} bytes after writing {size}",
            )

        return action

    steps.measure("Write mode 'w' over a larger file", truncate_mode("w", 16))
    steps.measure("Write mode 'wt' over a larger file", truncate_mode("wt", 8))

    def create_existing() -> tuple[StepStatus, str]:
        before = _read(scratch, probe)
        chosen = scratch.create_file(scratch.root, "probe.bin")
        preserved = _read(scratch, probe) == before
        return (
            StepStatus.PASSED if preserved else StepStatus.FAILED,
            f"the provider chose {chosen!r}; existing content "
            f"{'preserved' if preserved else 'changed'}",
        )

    steps.measure("Create an existing name", create_existing)

    def rename_onto_existing() -> tuple[StepStatus, str]:
        target = scratch.path("target.bin")
        if scratch.create_file(scratch.root, "target.bin") != "target.bin":
            raise ValueError("could not create target.bin")
        _write(scratch, target, "wt", b"target")
        _write(scratch, probe, "wt", b"source")
        try:
            chosen = scratch.rename(probe, "target.bin")
        except Exception as error:  # Diagnostic boundary: report the refusal.
            return (
                StepStatus.PASSED,
                f"refused ({type(error).__name__}); target unchanged: "
                f"{_read(scratch, target) == b'target'}",
            )
        replaced = _read(scratch, target) == b"source"
        return (
            StepStatus.WARNING,
            f"the provider chose {chosen!r}; target "
            f"{'replaced' if replaced else 'kept'}",
        )

    steps.measure("Rename onto an existing name", rename_onto_existing)

    def case_collision() -> tuple[StepStatus, str]:
        first = scratch.create_file(scratch.root, "Case.bin")
        second = scratch.create_file(scratch.root, "case.bin")
        return StepStatus.PASSED, f"created {first!r}, then {second!r} for 'case.bin'"

    steps.measure("Names differing only by case", case_collision)

    def invalid_name() -> tuple[StepStatus, str]:
        chosen = scratch.create_file(scratch.root, "a:b?.bin")
        return StepStatus.PASSED, f"the provider chose {chosen!r} for 'a:b?.bin'"

    steps.measure("FAT-invalid characters", invalid_name)

    def move() -> tuple[StepStatus, str]:
        directory = scratch.create_directory(scratch.root, "sub")
        source = scratch.create_file(scratch.root, "moved.bin")
        _write(scratch, scratch.path(source), "wt", b"moved")
        chosen = scratch.move(scratch.path(source), scratch.path(directory))
        moved = _read(scratch, scratch.path(directory, chosen)) == b"moved"
        return (
            StepStatus.PASSED if moved else StepStatus.FAILED,
            f"moved into {directory!r} as {chosen!r}; content "
            f"{'intact' if moved else 'changed'}",
        )

    steps.measure("Move between directories", move)

    def delete() -> tuple[StepStatus, str]:
        name = scratch.create_file(scratch.root, "delete.bin")
        path = scratch.path(name)
        _write(scratch, path, "wt", b"delete")
        deleted = scratch.delete_contents(path)
        gone = not scratch.exists(path)
        return (
            StepStatus.PASSED if deleted and gone else StepStatus.FAILED,
            f"delete returned {deleted}; {name!r} {'gone' if gone else 'remains'}",
        )

    steps.measure("Delete file", delete)


def probe_text(mount_point: str, steps: tuple[CheckStep, ...]) -> str:
    passed = all(step.status is not StepStatus.FAILED for step in steps)
    lines = [
        "iOpenPod Android document-provider probe",
        f"Mount Point: {mount_point}",
        f"Result: {'passed' if passed else 'failed'}",
        "",
    ]
    for step in steps:
        detail = step.detail.splitlines() or [""]
        lines.append(f"[{step.status.value.upper()}] {step.name}: {detail[0]}")
        lines.extend(f"    {line}" for line in detail[1:])
    return "\n".join(lines) + "\n"


def run_probe_json(request_json: str, tree: EditableDocumentTree) -> str:
    """Chaquopy entry point: never raises, always returns a JSON report."""

    try:
        request = parse_request(request_json)
    except (ValueError, json.JSONDecodeError) as error:
        steps: tuple[CheckStep, ...] = (
            CheckStep("Request", StepStatus.FAILED, str(error)),
        )
        mount_point = ""
    else:
        mount_point = request.mount_point
        try:
            steps = run_probe(request, tree)
        except Exception as error:  # Diagnostic boundary: report every failure.
            steps = (
                CheckStep(
                    "Probe", StepStatus.FAILED, f"{type(error).__name__}: {error}"
                ),
            )
    return json.dumps(
        {
            "passed": all(step.status is not StepStatus.FAILED for step in steps),
            "text": probe_text(mount_point, steps),
        }
    )
