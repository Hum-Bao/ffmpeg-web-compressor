"""OS-aware runtime configuration for RAM-backed storage."""

import logging
import os
import tempfile
from pathlib import Path

from modules import api, ramdisk


def _configure_linux_defaults(logger: logging.Logger) -> None:
    """Set Linux-friendly RAM env defaults when available."""
    if os.name != "posix":
        return

    if os.environ.get(api.RAMDISK_ENV_VAR, "").strip():
        return

    if api.DEV_SHM_PATH.exists() and api.DEV_SHM_PATH.is_dir():
        os.environ[api.RAMDISK_ENV_VAR] = str(api.DEV_SHM_PATH)
        logger.info(
            "Linux RAM storage auto-configured: %s=%s",
            api.RAMDISK_ENV_VAR,
            os.environ[api.RAMDISK_ENV_VAR],
        )
        return

    if api.RUN_SHM_PATH.exists() and api.RUN_SHM_PATH.is_dir():
        os.environ[api.RAMDISK_ENV_VAR] = str(api.RUN_SHM_PATH)
        logger.info(
            "Linux RAM storage auto-configured: %s=%s",
            api.RAMDISK_ENV_VAR,
            os.environ[api.RAMDISK_ENV_VAR],
        )


def configure_runtime(logger: logging.Logger) -> str:
    """Detect OS and apply environment setup for RAM-backed runtime storage."""
    os.environ.setdefault(api.REQUIRE_RAM_ENV_VAR, "1")

    if os.name == "nt":
        ramdisk.ensure_windows_ramdisk(logger)
    else:
        _configure_linux_defaults(logger)

    active_temp_dir = api.get_active_temp_dir()
    logger.info("Active temp workspace: %s", active_temp_dir)

    tempfile.tempdir = active_temp_dir
    os.environ["TMPDIR"] = active_temp_dir
    os.environ["TMP"] = active_temp_dir
    os.environ["TEMP"] = active_temp_dir

    exif_dump_dir = str(Path(active_temp_dir) / "exif_dumps")
    os.environ.setdefault("FFMPEG_WEB_EXIF_DUMP_DIR", exif_dump_dir)

    return active_temp_dir
