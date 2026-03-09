"""Windows RAM-disk auto-setup helpers.

This module attempts to provision and mount a RAM disk via ImDisk on Windows,
then exposes a writable subdirectory for this app's temp workspace.
"""

import atexit
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

RAMDISK_ENV_VAR = "FFMPEG_WEB_RAMDISK_DIR"
REQUIRE_RAM_ENV_VAR = "FFMPEG_WEB_REQUIRE_RAM_STORAGE"
WIN_RAMDISK_SIZE_ENV = "FFMPEG_WEB_WINDOWS_RAMDISK_SIZE"
WIN_RAMDISK_DRIVE_ENV = "FFMPEG_WEB_WINDOWS_RAMDISK_DRIVE"
WIN_RAMDISK_FOLDER_ENV = "FFMPEG_WEB_WINDOWS_RAMDISK_FOLDER"

_DEFAULT_DRIVE = "R:"
_DEFAULT_FOLDER = "ffmpeg-temp"
_DEFAULT_SIZE = "8G"
_MOUNT_READY_TIMEOUT_SECONDS = 8.0
_MOUNT_READY_POLL_SECONDS = 0.2
_WINERR_UNRECOGNIZED_FILESYSTEM = 1005


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


def _detach_imdisk(*, imdisk_bin: str, drive: str) -> None:
    """Detach a mounted ImDisk volume if present."""
    _run_imdisk([imdisk_bin, "-D", "-m", drive])


def _wait_for_mount_root(mount_root: Path) -> bool:
    """Wait briefly for mount root to become available after attachment."""
    deadline = time.monotonic() + _MOUNT_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if mount_root.exists():
            return True
        time.sleep(_MOUNT_READY_POLL_SECONDS)
    return mount_root.exists()


def _create_workspace_dir(mount_root: Path, folder: str) -> Path:
    """Create and return writable workspace directory on mounted volume."""
    ram_dir = mount_root / folder
    ram_dir.mkdir(parents=True, exist_ok=True)
    return ram_dir


def ensure_windows_ramdisk(logger: logging.Logger) -> None:
    """Ensure FFMPEG_WEB_RAMDISK_DIR points to a valid Windows RAM-disk folder."""
    if os.name != "nt":
        return

    existing = os.environ.get(RAMDISK_ENV_VAR, "").strip()
    if existing and Path(existing).exists() and Path(existing).is_dir():
        return

    if not _require_ram_storage():
        return

    imdisk_bin = shutil.which("imdisk")
    if not imdisk_bin:
        msg = (
            "Windows RAM disk auto-setup requires ImDisk CLI in PATH. "
            "Install ImDisk Toolkit or set FFMPEG_WEB_RAMDISK_DIR manually."
        )
        raise RuntimeError(msg)

    drive = _normalize_drive(os.environ.get(WIN_RAMDISK_DRIVE_ENV, _DEFAULT_DRIVE))
    size = os.environ.get(WIN_RAMDISK_SIZE_ENV, _DEFAULT_SIZE).strip() or _DEFAULT_SIZE
    folder = (
        os.environ.get(WIN_RAMDISK_FOLDER_ENV, _DEFAULT_FOLDER).strip()
        or _DEFAULT_FOLDER
    )

    mount_root = Path(f"{drive}\\")
    created_mount = False

    if not mount_root.exists():
        result = _attach_imdisk(imdisk_bin=imdisk_bin, drive=drive, size=size)
        if result.returncode != 0 and not mount_root.exists():
            msg = (
                f"Failed to create Windows RAM disk at {drive} (size {size}). "
                f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
            )
            raise RuntimeError(msg)

        created_mount = True
        logger.info("Created Windows RAM disk at %s (%s)", drive, size)

    if created_mount and not _wait_for_mount_root(mount_root):
        msg = f"Windows RAM disk {drive} did not become available in time."
        raise RuntimeError(msg)

    try:
        ram_dir = _create_workspace_dir(mount_root, folder)
    except OSError as exc:
        # WinError 1005: filesystem not recognized yet (or format failed).
        if (
            created_mount
            and getattr(exc, "winerror", None) == _WINERR_UNRECOGNIZED_FILESYSTEM
        ):
            logger.warning(
                (
                    "RAM disk %s not ready after creation; "
                    "reattaching and formatting once."
                ),
                drive,
            )
            _detach_imdisk(imdisk_bin=imdisk_bin, drive=drive)
            result = _attach_imdisk(imdisk_bin=imdisk_bin, drive=drive, size=size)
            if result.returncode != 0:
                msg = (
                    "Failed to reinitialize Windows RAM disk at "
                    f"{drive} (size {size}). "
                    f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
                )
                raise RuntimeError(msg) from exc

            if not _wait_for_mount_root(mount_root):
                msg = f"Windows RAM disk {drive} did not become available after retry."
                raise RuntimeError(msg) from exc

            ram_dir = _create_workspace_dir(mount_root, folder)
        else:
            raise

    os.environ[RAMDISK_ENV_VAR] = str(ram_dir)
    logger.info("Using Windows RAM workspace: %s", ram_dir)

    if not created_mount:
        return

    def _cleanup_mount() -> None:
        _detach_imdisk(imdisk_bin=imdisk_bin, drive=drive)

    atexit.register(_cleanup_mount)
