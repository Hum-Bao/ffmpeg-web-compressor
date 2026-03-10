"""Windows RAM-disk auto-setup helpers.

This module attempts to provision and mount a RAM disk via ImDisk on Windows,
then exposes a writable subdirectory for this app's temp workspace.
"""

import atexit
import contextlib
import ctypes
import logging
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

RAMDISK_ENV_VAR = "FFMPEG_WEB_RAMDISK_DIR"
REQUIRE_RAM_ENV_VAR = "FFMPEG_WEB_REQUIRE_RAM_STORAGE"
WIN_RAMDISK_SIZE_ENV = "FFMPEG_WEB_WINDOWS_RAMDISK_SIZE"
WIN_RAMDISK_DRIVE_ENV = "FFMPEG_WEB_WINDOWS_RAMDISK_DRIVE"
WIN_RAMDISK_FOLDER_ENV = "FFMPEG_WEB_WINDOWS_RAMDISK_FOLDER"

_DEFAULT_DRIVE = "R:"
_DEFAULT_FOLDER = "ffmpeg-temp"
_DEFAULT_SIZE = "8G"
_MOUNT_READY_TIMEOUT_SECONDS = 25.0
_MOUNT_READY_POLL_SECONDS = 0.2
_ATTACH_RETRIES = 2
_WINERR_UNRECOGNIZED_FILESYSTEM = 1005
_WINERR_DEVICE_NOT_READY = 21
_WINERR_PATH_NOT_FOUND = 3
_IMDISK_PERMISSION_MARKERS = (
    "access is denied",
    "permission denied",
    "requires elevation",
    "administrator",
    "privilege",
)

_runtime_state: dict[str, bool | str | None] = {
    "created_mount_by_app": False,
    "active_imdisk_bin": None,
    "active_drive": None,
}


@dataclass(frozen=True)
class _RamdiskMountSpec:
    drive: str
    size: str
    folder: str
    mount_root: Path
    imdisk_bin: str


def _is_windows_runtime() -> bool:
    """Return True when running on Windows.

    Kept as a helper so static analyzers do not fold platform checks and mark
    Windows-specific code paths as structurally unreachable on Linux/macOS.
    """
    return platform.system().lower() == "windows"


def _is_windows_admin() -> bool:
    """Best-effort check for Windows administrator privileges."""
    if not _is_windows_runtime():
        return False

    with contextlib.suppress(AttributeError, OSError, ValueError):
        windll = cast("Any", getattr(ctypes, "windll", None))
        if windll is None:
            return False
        shell32 = cast("Any", getattr(windll, "shell32", None))
        if shell32 is None:
            return False
        return bool(shell32.IsUserAnAdmin())
    return False


def _resolve_windows_ramdisk_target() -> tuple[str, str, str, Path]:
    """Read RAM-disk target settings from environment."""
    drive = _normalize_drive(os.environ.get(WIN_RAMDISK_DRIVE_ENV, _DEFAULT_DRIVE))
    size = os.environ.get(WIN_RAMDISK_SIZE_ENV, _DEFAULT_SIZE).strip() or _DEFAULT_SIZE
    folder = (
        os.environ.get(WIN_RAMDISK_FOLDER_ENV, _DEFAULT_FOLDER).strip()
        or _DEFAULT_FOLDER
    )
    mount_root = Path(f"{drive}\\")
    return drive, size, folder, mount_root


def _ensure_admin_for_new_windows_ramdisk_mount(mount_root: Path) -> None:
    """Fail fast when creating a new Windows RAM-disk requires elevation."""
    if mount_root.exists() or _is_windows_admin():
        return

    msg = (
        "Windows RAM disk creation requires Administrator privileges. "
        "Re-run this app as Administrator, set "
        "FFMPEG_WEB_REQUIRE_RAM_STORAGE=0 to allow disk-backed temp storage, "
        "or set FFMPEG_WEB_RAMDISK_DIR manually."
    )
    raise RuntimeError(msg)


def _recover_or_raise_on_mount_ready_failure(
    *,
    logger: logging.Logger,
    spec: _RamdiskMountSpec,
    created_mount: bool,
    cause: RuntimeError,
) -> Path:
    """Recover once from a fresh mount that did not become writable in time."""
    if not created_mount:
        msg = (
            f"Windows RAM disk {spec.drive} is unavailable. "
            "Set FFMPEG_WEB_REQUIRE_RAM_STORAGE=0 to allow disk-backed temp "
            "storage, or set FFMPEG_WEB_RAMDISK_DIR manually."
        )
        raise RuntimeError(msg) from cause

    logger.warning(
        "RAM disk %s not ready after creation; reattaching and formatting once.",
        spec.drive,
    )
    _detach_imdisk(imdisk_bin=spec.imdisk_bin, drive=spec.drive)
    result = _attach_imdisk(
        imdisk_bin=spec.imdisk_bin,
        drive=spec.drive,
        size=spec.size,
    )
    if result.returncode != 0:
        _detach_imdisk(imdisk_bin=spec.imdisk_bin, drive=spec.drive)
        msg = (
            "Failed to reinitialize Windows RAM disk at "
            f"{spec.drive} (size {spec.size}). "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
        raise RuntimeError(msg) from cause

    return _create_workspace_dir_with_retry(
        mount_root=spec.mount_root,
        folder=spec.folder,
        timeout_seconds=_MOUNT_READY_TIMEOUT_SECONDS,
    )


def _require_ram_storage() -> bool:
    raw = os.environ.get(REQUIRE_RAM_ENV_VAR)
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_drive(value: str) -> str:
    token = value.strip().upper().replace("\\", "").replace("/", "")
    if not token:
        return _DEFAULT_DRIVE
    if token.endswith(":"):
        return token
    if len(token) == 1 and token.isalpha():
        return f"{token}:"
    return _DEFAULT_DRIVE


def _run_imdisk(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _attach_imdisk(
    *,
    imdisk_bin: str,
    drive: str,
    size: str,
) -> subprocess.CompletedProcess[str]:
    """Attach and format a RAM disk using ImDisk."""
    cmd = [
        imdisk_bin,
        "-a",
        "-s",
        size,
        "-m",
        drive,
        "-p",
        "/fs:NTFS /q /y",
    ]
    return _run_imdisk(cmd)


def _imdisk_permission_error(result: subprocess.CompletedProcess[str]) -> bool:
    """Return True when ImDisk output indicates missing administrator rights."""
    combined = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in combined for marker in _IMDISK_PERMISSION_MARKERS)


def _detach_imdisk(*, imdisk_bin: str, drive: str) -> None:
    """Detach a mounted ImDisk volume if present."""
    with contextlib.suppress(
        KeyboardInterrupt,
        OSError,
        subprocess.SubprocessError,
        subprocess.TimeoutExpired,
    ):
        _run_imdisk([imdisk_bin, "-D", "-m", drive])


def _attach_imdisk_with_retry(
    *,
    logger: logging.Logger,
    imdisk_bin: str,
    drive: str,
    size: str,
) -> None:
    """Attach ImDisk RAM disk, retrying with cleanup if initial attach fails."""
    last_result: subprocess.CompletedProcess[str] | None = None

    for attempt in range(1, _ATTACH_RETRIES + 1):
        # Detach first in case a previous failed run left a stale mapping.
        _detach_imdisk(imdisk_bin=imdisk_bin, drive=drive)

        result = _attach_imdisk(imdisk_bin=imdisk_bin, drive=drive, size=size)
        if result.returncode == 0:
            return

        if _imdisk_permission_error(result):
            msg = (
                "ImDisk failed due to insufficient privileges while creating "
                f"RAM disk {drive}. Re-run this app as Administrator on Windows, "
                "or set FFMPEG_WEB_RAMDISK_DIR manually. "
                f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
            )
            raise RuntimeError(msg)

        last_result = result
        logger.warning(
            "ImDisk attach attempt %d/%d failed for %s: %s",
            attempt,
            _ATTACH_RETRIES,
            drive,
            result.stderr.strip() or result.stdout.strip(),
        )

    # Ensure partial device mappings are cleaned up before surfacing error.
    _detach_imdisk(imdisk_bin=imdisk_bin, drive=drive)
    details = "unknown ImDisk error"
    if last_result is not None:
        details = (
            f"stdout={last_result.stdout.strip()} stderr={last_result.stderr.strip()}"
        )
    msg = f"Failed to create Windows RAM disk at {drive} (size {size}). {details}"
    raise RuntimeError(msg)


def _create_workspace_dir(mount_root: Path, folder: str) -> Path:
    """Create and return writable workspace directory on mounted volume."""
    ram_dir = mount_root / folder
    ram_dir.mkdir(parents=True, exist_ok=True)
    return ram_dir


def _create_workspace_dir_when_ready(
    mount_root: Path,
    folder: str,
) -> tuple[Path | None, OSError | None]:
    """Attempt workspace creation once and classify transient mount errors."""
    try:
        return _create_workspace_dir(mount_root, folder), None
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror in {
            _WINERR_UNRECOGNIZED_FILESYSTEM,
            _WINERR_DEVICE_NOT_READY,
            _WINERR_PATH_NOT_FOUND,
        }:
            return None, exc
        raise


def _create_workspace_dir_with_retry(
    *,
    mount_root: Path,
    folder: str,
    timeout_seconds: float,
) -> Path:
    """Create workspace directory, retrying while Windows mount is settling."""
    deadline = time.monotonic() + timeout_seconds
    last_exc: OSError | None = None

    while time.monotonic() < deadline:
        workspace_dir, transient_exc = _create_workspace_dir_when_ready(
            mount_root,
            folder,
        )
        if workspace_dir is not None:
            return workspace_dir

        last_exc = transient_exc
        time.sleep(_MOUNT_READY_POLL_SECONDS)

    msg = (
        f"Windows RAM disk {mount_root} did not become writable in time "
        f"({timeout_seconds:.0f}s)."
    )
    if last_exc is not None:
        raise RuntimeError(msg) from last_exc
    raise RuntimeError(msg)


def ensure_windows_ramdisk(logger: logging.Logger) -> None:
    """Ensure FFMPEG_WEB_RAMDISK_DIR points to a valid Windows RAM-disk folder."""
    if not _is_windows_runtime():
        return

    existing = os.environ.get(RAMDISK_ENV_VAR, "").strip()
    if existing and Path(existing).exists() and Path(existing).is_dir():
        return

    if not _require_ram_storage():
        return

    drive, size, folder, mount_root = _resolve_windows_ramdisk_target()
    _ensure_admin_for_new_windows_ramdisk_mount(mount_root)

    imdisk_bin = shutil.which("imdisk")
    if not imdisk_bin:
        msg = (
            "Windows RAM disk auto-setup requires ImDisk CLI in PATH. "
            "Install ImDisk Toolkit or set FFMPEG_WEB_RAMDISK_DIR manually."
        )
        raise RuntimeError(msg)
    created_mount = False
    spec = _RamdiskMountSpec(
        drive=drive,
        size=size,
        folder=folder,
        mount_root=mount_root,
        imdisk_bin=imdisk_bin,
    )

    if not mount_root.exists():
        _attach_imdisk_with_retry(
            logger=logger,
            imdisk_bin=imdisk_bin,
            drive=drive,
            size=size,
        )

        created_mount = True
        _runtime_state["created_mount_by_app"] = True
        _runtime_state["active_imdisk_bin"] = imdisk_bin
        _runtime_state["active_drive"] = drive

    try:
        ram_dir = _create_workspace_dir_with_retry(
            mount_root=mount_root,
            folder=folder,
            timeout_seconds=_MOUNT_READY_TIMEOUT_SECONDS,
        )
    except RuntimeError as exc:
        ram_dir = _recover_or_raise_on_mount_ready_failure(
            logger=logger,
            spec=spec,
            created_mount=created_mount,
            cause=exc,
        )

    os.environ[RAMDISK_ENV_VAR] = str(ram_dir)
    if created_mount:
        logger.info("Created Windows RAM disk at %s (%s)", drive, size)
    logger.info("Using Windows RAM workspace: %s", ram_dir)

    # Track values for explicit shutdown cleanup.
    _runtime_state["active_imdisk_bin"] = imdisk_bin
    _runtime_state["active_drive"] = drive

    if not created_mount:
        return

    def _cleanup_mount() -> None:
        cleanup_windows_ramdisk(logger)

    atexit.register(_cleanup_mount)


def cleanup_windows_ramdisk(logger: logging.Logger | None = None) -> bool:
    """Detach app-created Windows RAM disk immediately when present."""
    if not _is_windows_runtime():
        return False

    created_mount_by_app = bool(_runtime_state.get("created_mount_by_app"))
    if not created_mount_by_app:
        return False

    imdisk_bin = _runtime_state.get("active_imdisk_bin")
    drive = _runtime_state.get("active_drive")
    if not isinstance(imdisk_bin, str) or not isinstance(drive, str):
        return False

    _detach_imdisk(imdisk_bin=imdisk_bin, drive=drive)
    _runtime_state["created_mount_by_app"] = False

    if logger is not None:
        logger.info("Detached Windows RAM disk: %s", drive)

    return True
