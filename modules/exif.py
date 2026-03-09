"""ExifTool helpers for metadata restoration and debug dumps."""

import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

EXIFTOOL_BIN = shutil.which("exiftool") or "exiftool"

EXIF_DUMP_DIR_ENV_VAR = "FFMPEG_WEB_EXIF_DUMP_DIR"
DEFAULT_EXIF_DUMP_LOG_DIR = Path("logs/exif_dumps")
_EXIF_SETTINGS: dict[str, bool] = {"dump_enabled": False}


def set_exif_dump_enabled(*, enabled: bool) -> None:
    """Enable or disable EXIF dump capture to log files."""
    _EXIF_SETTINGS["dump_enabled"] = enabled


def _sanitize_for_filename(value: str) -> str:
    """Return a filesystem-safe token from arbitrary text."""
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return sanitized[:120] or "unknown"


def _build_exif_dump_filename(video_path: str, title: str) -> str:
    """Build deterministic dump filename with timestamp, title, and source path."""
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_title = _sanitize_for_filename(title)
    safe_video_path = _sanitize_for_filename(video_path)
    return f"{timestamp}__{safe_title}__{safe_video_path}.log"


def _write_exif_dump_file(video_path: str, title: str, dump_text: str) -> None:
    """Write EXIF dump output to a log file."""
    try:
        configured_dir = os.environ.get(EXIF_DUMP_DIR_ENV_VAR, "").strip()
        dump_dir = (
            Path(configured_dir).expanduser()
            if configured_dir
            else DEFAULT_EXIF_DUMP_LOG_DIR
        )

        dump_dir.mkdir(parents=True, exist_ok=True)
        dump_file = dump_dir / _build_exif_dump_filename(video_path, title)
        dump_file.write_text(dump_text, encoding="utf-8")
        logger.info("Wrote EXIF dump log: %s", dump_file)
    except OSError:
        logger.exception("Failed to write EXIF dump log file")


def log_exif_dump(video_path: str, title: str) -> None:
    """Capture exiftool dump to file when enabled."""
    if not _EXIF_SETTINGS["dump_enabled"]:
        return

    debug_cmd = [EXIFTOOL_BIN, "-a", "-G1", "-s", "-D", video_path]
    try:
        debug_result = subprocess.run(  # noqa: S603
            debug_cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        file_dump = (
            f"Title: {title}\n"
            f"Video Path: {video_path}\n"
            f"Command: {' '.join(debug_cmd)}\n"
            f"Return Code: {debug_result.returncode}\n\n"
            f"STDOUT:\n{debug_result.stdout}\n\n"
            f"STDERR:\n{debug_result.stderr}\n"
        )
        _write_exif_dump_file(video_path=video_path, title=title, dump_text=file_dump)

        if debug_result.returncode != 0:
            logger.error(
                "ExifTool dump failed (title=%s, path=%s, code=%d): %s",
                title,
                video_path,
                debug_result.returncode,
                debug_result.stderr.strip(),
            )
    except (OSError, subprocess.SubprocessError):
        logger.exception("Failed to run debug exiftool")


def restore_exif_metadata(input_path: str, output_path: str) -> None:
    """Copy metadata from source file back to converted output."""
    exif_cmd = [
        EXIFTOOL_BIN,
        "-config",
        ".ExifTool_config",
        "-m",  # Ignore minor errors
        "-overwrite_original",
        "-api",
        "QuickTimeUTC=1",
        "-api",
        "LargeFileSupport=1",
        "-tagsFromFile",
        input_path,
        # Standard QuickTime Dates
        "-Time:All",
        # GPS Info
        "-Keys:GPSCoordinates",
        "-Keys:LocationAccuracyHorizontal",  # Optional, for precision
        # Lens Info
        "-VideoKeys:All",
        # Other Info
        "-Keys:Make",
        "-Keys:Model",
        "-Keys:Software",
        "-Keys:CreationDate",
        output_path,
    ]

    try:
        exif_result = subprocess.run(  # noqa: S603
            exif_cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if exif_result.returncode == 0:
            logger.info("ExifTool successfully forced the reverse-DNS Apple tags.")
        else:
            logger.error(
                "ExifTool metadata restore failed (input=%s, output=%s, code=%d): %s",
                input_path,
                output_path,
                exif_result.returncode,
                exif_result.stderr.strip(),
            )
    except (OSError, subprocess.SubprocessError):
        logger.exception("Unexpected ExifTool error")
