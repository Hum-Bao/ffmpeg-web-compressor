"""HTTP API routes for upload, conversion, progress tracking, and cleanup."""

import atexit
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Generator
from dataclasses import replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from flask import Blueprint, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from modules import cache, exif, ffmpeg, state

logger = logging.getLogger(__name__)

api_blueprint = Blueprint("api", __name__)

DEV_SHM_PATH = Path("/dev/shm")  # noqa: S108
RUN_SHM_PATH = Path("/run/shm")
RAMDISK_ENV_VAR = "FFMPEG_WEB_RAMDISK_DIR"
REQUIRE_RAM_ENV_VAR = "FFMPEG_WEB_REQUIRE_RAM_STORAGE"
SESSION_DIR_PREFIX = "ffmpeg-web-compressor-"
DEFAULT_CONTAINER = "mp4"
DEFAULT_MIMETYPE = "video/mp4"
DEFAULT_SPEED = "-"

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_REQUEST_ENTITY_TOO_LARGE = 413
HTTP_INTERNAL_SERVER_ERROR = 500

CONVERSION_TIMEOUT_SECONDS = 7200
THREAD_JOIN_TIMEOUT_SECONDS = 2


def _output_mimetype(container: str) -> str:
    """Return download mimetype for a converted output container."""
    normalized = container.lower()
    if normalized == "m4v":
        return DEFAULT_MIMETYPE
    return f"video/{normalized}"


ERR_INTERNAL = "Internal server error"
ERR_FILE_NOT_FOUND = "File not found"
ERR_FILE_NOT_FOUND_IN_CACHE = "File not found in cache"
ERR_COULD_NOT_READ_VIDEO = "Could not read video file"
ERR_CONVERSION_FAILED = "Conversion failed"
ERR_CONVERSION_TIMEOUT = "Conversion timeout"
ERR_INVALID_JSON = "Invalid JSON data"
ERR_INSUFFICIENT_TEMP_SPACE = "Insufficient temporary storage space"
ERR_NO_FILE_PROVIDED = "No file provided"
ERR_NO_FILE_SELECTED = "No file selected"
ERR_NO_FILE_ID_PROVIDED = "No file ID provided"
ERR_NO_FILE_IDS_PROVIDED = "No file IDs provided"
ERR_NO_VALID_FILE_IDS_PROVIDED = "No valid file IDs provided"
ERR_OUT_OF_MEMORY = "Insufficient memory for conversion"
ERR_CLEANUP_FAILED = "Cleanup failed"

# Temporary debug switch to inspect FFmpeg progress/stderr output in app logs.
LOG_RAW_FFMPEG_PROGRESS = False
MAX_CAPTURED_STDERR_LINES = 4000


def _error_response(message: str, status: int) -> tuple[Response, int]:
    """Return a standardized JSON error payload."""
    return jsonify({"error": message}), status


def _set_starting_progress(
    *,
    file_id: str,
    total_duration: float,
    total_frames: int,
) -> None:
    """Initialize conversion progress before/while conversion begins."""
    state.set_progress(
        file_id=file_id,
        percent=state.PERCENT_PENDING,
        status=state.STATUS_STARTING,
        duration=total_duration,
        frame=0,
        total_frames=total_frames,
        speed=DEFAULT_SPEED,
    )


def _extract_str_items(
    data: dict[str, Any],
    key: str,
    *,
    dedupe: bool = False,
) -> list[str] | None:
    """Return a filtered string list from request JSON field."""
    raw_items = data.get(key)
    if not isinstance(raw_items, list):
        return None

    typed_items = cast("list[Any]", raw_items)
    string_items = [item for item in typed_items if isinstance(item, str)]
    if dedupe:
        return list(dict.fromkeys(string_items))
    return string_items


@lru_cache(maxsize=1)
def _configured_ramdisk_dir() -> str | None:
    """Return configured RAM-disk directory if valid, else None."""
    configured = os.environ.get(RAMDISK_ENV_VAR, "").strip()
    if not configured:
        return None

    configured_path = Path(configured).expanduser()
    if configured_path.exists() and configured_path.is_dir():
        return str(configured_path)

    logger.warning(
        "%s is set to %s but that directory is unavailable.",
        RAMDISK_ENV_VAR,
        configured,
    )
    return None


def _require_ram_storage() -> bool:
    """Return whether RAM-backed storage is required by configuration."""
    raw = os.environ.get(REQUIRE_RAM_ENV_VAR)
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_ram_base_dir() -> str | None:
    """Return a RAM-backed base directory when available."""
    configured = _configured_ramdisk_dir()
    if configured is not None:
        return configured

    if DEV_SHM_PATH.exists() and DEV_SHM_PATH.is_dir():
        return str(DEV_SHM_PATH)
    if RUN_SHM_PATH.exists() and RUN_SHM_PATH.is_dir():
        return str(RUN_SHM_PATH)
    return None


@lru_cache(maxsize=1)
def _session_temp_dir() -> str:
    """Create one temp directory for this process and remove it on exit."""
    ram_base = _resolve_ram_base_dir()
    require_ram_storage = _require_ram_storage()

    if ram_base is None:
        if require_ram_storage:
            msg = (
                "RAM-backed storage is required but unavailable. "
                "Set FFMPEG_WEB_RAMDISK_DIR to a RAM-disk path."
            )
            raise RuntimeError(msg)
        return tempfile.gettempdir()

    session_dir = tempfile.mkdtemp(prefix=SESSION_DIR_PREFIX, dir=ram_base)
    logger.info("Using RAM-backed temp workspace: %s", session_dir)

    def _cleanup_session_dir() -> None:
        shutil.rmtree(session_dir, ignore_errors=True)

    atexit.register(_cleanup_session_dir)
    return session_dir


def _temp_dir() -> str:
    """Return preferred temp directory path.

    Priority:
    1) Explicit RAM-disk path from `FFMPEG_WEB_RAMDISK_DIR`
    2) Linux shared-memory mounts (`/dev/shm`, `/run/shm`)
    3) Platform default temp dir
    """
    return _session_temp_dir()


def get_active_temp_dir() -> str:
    """Expose active temp dir for startup validation and diagnostics."""
    return _temp_dir()


def _build_ffmpeg_settings(
    data: dict[str, Any],
    input_path: str,
    output_path: str,
) -> ffmpeg.BuildSettings:
    """Create normalized FFmpeg conversion settings from request payload."""
    return ffmpeg.BuildSettings(
        target_res=str(data.get("resolution", "original")),
        target_fps=str(data.get("fps", "original")),
        target_codec=str(data.get("codec", "original")),
        container=str(data.get("container", DEFAULT_CONTAINER)),
        input_path=input_path,
        output_path=output_path,
        quality=str(data.get("quality", "balanced")),
        use_hardware=bool(data.get("useHardware", True)),
    )


def _output_temp_file(container: str) -> str:
    """Allocate output temp file path for conversion output."""
    output_ext = f".{container}"
    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=output_ext,
        dir=_temp_dir(),
        delete=False,
    ) as output_temp:
        return output_temp.name


def _bytes_to_mb(value: int) -> float:
    """Return bytes converted to MiB for user-facing messages."""
    return round(value / (1024 * 1024), 2)


def _has_enough_temp_space(required_bytes: int) -> tuple[bool, int]:
    """Check whether temp dir has enough free space for an upload."""
    if required_bytes <= 0:
        return True, 0

    free_bytes = shutil.disk_usage(_temp_dir()).free
    # Keep a small safety margin so conversion output can still be created.
    needed_with_headroom = int(required_bytes * 1.08)
    return free_bytes >= needed_with_headroom, free_bytes


def _safe_delete_path(path: str | None) -> None:
    """Delete file path if present, ignoring failures."""
    if path:
        cache.delete_physical_file(path)


def _monitor_progress(
    process: subprocess.Popen[bytes],
    file_id: str,
    total_duration: float,
    total_frames: int,
    stderr_lines: list[str],
) -> None:
    """Parse FFmpeg stderr and publish conversion progress."""
    stderr_pipe = process.stderr
    if stderr_pipe is None:
        return

    try:
        current_frame = 0
        current_speed = "-"

        for line in iter(stderr_pipe.readline, b""):
            line_str = line.decode("utf-8", errors="ignore")
            if len(stderr_lines) < MAX_CAPTURED_STDERR_LINES:
                stderr_lines.append(line_str)

            if LOG_RAW_FFMPEG_PROGRESS:
                raw = line_str.strip()
                if raw:
                    logger.info("FFmpeg raw [%s]: %s", file_id, raw)

            frame_match = re.search(r"frame=\s*(\d+)", line_str)
            if frame_match:
                current_frame = int(frame_match.group(1))

            speed_match = re.search(r"speed=\s*([0-9.]+x)", line_str)
            if speed_match:
                current_speed = speed_match.group(1)

            current_time = ffmpeg.parse_ffmpeg_progress(line_str)
            if current_time is None or total_duration <= 0:
                continue

            percent = min(state.PERCENT_COMPLETE, (current_time / total_duration) * 100)
            state.set_progress(
                file_id=file_id,
                percent=round(percent, 1),
                status=state.STATUS_CONVERTING,
                duration=total_duration,
                frame=current_frame,
                total_frames=total_frames,
                speed=current_speed,
            )
    except Exception:
        logger.exception("Progress monitoring error")
    finally:
        stderr_pipe.close()


def _cleanup_after_failed_conversion(
    file_id: str,
    input_path: str | None,
    output_path: str | None,
) -> None:
    """Cleanup cache/progress/files after conversion failure paths."""
    state.remove_progress(file_id)
    cache.remove_file(file_id)
    _safe_delete_path(input_path)
    _safe_delete_path(output_path)


def _convert_cached_file(
    file_id: str,
    data: dict[str, Any],
) -> tuple[Response, int]:
    """Run conversion for a cached input and return HTTP response tuple."""
    cached_file = cache.get_file(file_id)
    if not cached_file:
        return jsonify({"error": ERR_FILE_NOT_FOUND_IN_CACHE}), 404

    input_path = cached_file["filepath"]
    original_filename = cached_file["filename"]

    meta = ffmpeg.get_video_info(input_path)
    if not meta:
        cache.remove_file(file_id)
        return jsonify({"error": ERR_COULD_NOT_READ_VIDEO}), 500

    settings = _build_ffmpeg_settings(
        data=data,
        input_path=input_path,
        output_path=_output_temp_file(str(data.get("container", DEFAULT_CONTAINER))),
    )

    total_duration = float(meta.get("duration", 0))
    source_fps = int(meta.get("fps", 0))
    total_frames = (
        int(total_duration * source_fps) if total_duration > 0 and source_fps > 0 else 0
    )
    _set_starting_progress(
        file_id=file_id,
        total_duration=total_duration,
        total_frames=total_frames,
    )

    attempt_settings = [
        settings,
        replace(settings, audio_mode="compat"),
    ]
    succeeded = False
    final_settings = settings

    for attempt_index, attempt in enumerate(attempt_settings, start=1):
        cmd = ffmpeg.build_ffmpeg_command(meta, attempt)
        logger.info(
            "Running conversion via temp workspace %s (attempt %d, audio_mode=%s): %s",
            _temp_dir(),
            attempt_index,
            attempt.audio_mode,
            " ".join(cmd),
        )

        process = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        state.update_status(file_id, state.STATUS_CONVERTING)

        stderr_lines: list[str] = []
        progress_thread = threading.Thread(
            target=_monitor_progress,
            args=(process, file_id, total_duration, total_frames, stderr_lines),
            daemon=True,
        )
        progress_thread.start()

        try:
            returncode = process.wait(timeout=CONVERSION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            progress_thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
            _cleanup_after_failed_conversion(file_id, input_path, settings.output_path)
            return _error_response(ERR_CONVERSION_TIMEOUT, HTTP_INTERNAL_SERVER_ERROR)

        progress_thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        stderr_text = "".join(stderr_lines)

        if returncode == 0:
            succeeded = True
            final_settings = attempt
            break

        logger.error("FFmpeg failed with return code: %d", returncode)
        should_retry = (
            attempt_index == 1
            and attempt.audio_mode == "copy"
            and ffmpeg.should_retry_with_compat_audio(stderr_text)
        )
        if not should_retry:
            _cleanup_after_failed_conversion(file_id, input_path, settings.output_path)
            return _error_response(ERR_CONVERSION_FAILED, HTTP_INTERNAL_SERVER_ERROR)

        logger.warning(
            "Audio copy appears incompatible with target container; "
            "retrying with compatible audio encoding.",
        )
        _set_starting_progress(
            file_id=file_id,
            total_duration=total_duration,
            total_frames=total_frames,
        )

    state.remove_progress(file_id)

    if not succeeded:
        _cleanup_after_failed_conversion(file_id, input_path, settings.output_path)
        return _error_response(ERR_CONVERSION_FAILED, HTTP_INTERNAL_SERVER_ERROR)

    exif.restore_exif_metadata(
        input_path=input_path,
        output_path=final_settings.output_path,
    )
    exif.log_exif_dump(final_settings.output_path, "COMPRESSED VIDEO")

    output_size = Path(final_settings.output_path).stat().st_size
    output_size_mb = output_size / (1024 * 1024)
    logger.info("Conversion complete: %.2f MB (%d bytes)", output_size_mb, output_size)

    output_filename = (
        f"compressed_{Path(original_filename).stem}.{final_settings.container}"
    )
    output_id = f"{uuid.uuid4().hex}_{output_filename}"
    cache.put_file(
        file_id=output_id,
        filepath=final_settings.output_path,
        filename=output_filename,
        mimetype=_output_mimetype(final_settings.container),
    )

    cache.remove_file(file_id)
    _safe_delete_path(input_path)

    return jsonify({"filename": output_id, "size": output_size}), HTTP_OK


@api_blueprint.route("/")
def index() -> str:
    """Render the main page."""
    return render_template("index.html")


@api_blueprint.route("/analyze", methods=["POST"])
def analyze() -> tuple[Response, int]:
    """Upload and analyze video file directly from /dev/shm."""
    file_id: str | None = None
    temp_filepath: str | None = None

    try:
        if "file" not in request.files:
            return _error_response(ERR_NO_FILE_PROVIDED, HTTP_BAD_REQUEST)

        file = request.files["file"]
        if not file or file.filename == "":
            return _error_response(ERR_NO_FILE_SELECTED, HTTP_BAD_REQUEST)

        client_size_raw = request.form.get("clientSizeBytes", "")
        client_size_bytes = int(client_size_raw) if client_size_raw.isdigit() else 0
        content_length = int(request.content_length or 0)
        estimated_upload_bytes = max(client_size_bytes, content_length)
        has_space, free_bytes = _has_enough_temp_space(estimated_upload_bytes)
        if not has_space:
            return (
                jsonify(
                    {
                        "error": ERR_INSUFFICIENT_TEMP_SPACE,
                        "estimated_upload_mb": _bytes_to_mb(estimated_upload_bytes),
                        "free_temp_mb": _bytes_to_mb(free_bytes),
                        "temp_dir": _temp_dir(),
                    },
                ),
                HTTP_REQUEST_ENTITY_TOO_LARGE,
            )

        filename = secure_filename(str(file.filename))
        logger.info("Analyzing video: %s", filename)
        input_ext = Path(filename).suffix or ".mp4"
        try:
            with tempfile.NamedTemporaryFile(
                delete=False,
                dir=_temp_dir(),
                suffix=input_ext,
            ) as temp_file:
                temp_filepath = temp_file.name
                file.save(temp_filepath)
        except OSError:
            _safe_delete_path(temp_filepath)
            return (
                jsonify(
                    {
                        "error": ERR_INSUFFICIENT_TEMP_SPACE,
                        "temp_dir": _temp_dir(),
                    },
                ),
                HTTP_REQUEST_ENTITY_TOO_LARGE,
            )

        exif.log_exif_dump(temp_filepath, "ORIGINAL VIDEO")

        meta = ffmpeg.get_video_info(temp_filepath, filename)
        if not meta:
            _safe_delete_path(temp_filepath)
            return _error_response(ERR_COULD_NOT_READ_VIDEO, HTTP_INTERNAL_SERVER_ERROR)

        file_id = f"{uuid.uuid4().hex}_{filename}"
        cache.put_file(
            file_id=file_id,
            filepath=temp_filepath,
            filename=filename,
            mimetype=file.mimetype or DEFAULT_MIMETYPE,
        )

        logger.info(
            "Cached file %s in temp workspace %s (%.2f MB)",
            file_id,
            _temp_dir(),
            float(meta["size"]) / (1024 * 1024),
        )

        return jsonify(
            {
                "fileId": file_id,
                "filename": filename,
                "width": meta["width"],
                "height": meta["height"],
                "codec": meta["codec"],
                "fps": meta["fps"],
                "size": meta["size"],
                "duration": meta["duration"],
                "bitrate": meta["bitrate"],
                "container": meta["container"],
            },
        ), HTTP_OK

    except Exception:
        logger.exception("Unexpected error in analyze endpoint")
        if file_id:
            cache.remove_file_and_delete(file_id)
        elif temp_filepath:
            _safe_delete_path(temp_filepath)
        return _error_response(ERR_INTERNAL, HTTP_INTERNAL_SERVER_ERROR)


@api_blueprint.route("/convert", methods=["POST"])
def convert() -> tuple[Response, int]:
    """Convert video file with specified settings using direct file paths."""
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return _error_response(ERR_INVALID_JSON, HTTP_BAD_REQUEST)

        data = cast("dict[str, Any]", data)

        file_id_obj = data.get("fileId")
        if not isinstance(file_id_obj, str) or not file_id_obj:
            return _error_response(ERR_NO_FILE_ID_PROVIDED, HTTP_BAD_REQUEST)

        return _convert_cached_file(file_id=file_id_obj, data=data)

    except MemoryError:
        logger.exception("Out of memory during conversion")
        return _error_response(ERR_OUT_OF_MEMORY, HTTP_REQUEST_ENTITY_TOO_LARGE)
    except Exception:
        logger.exception("Unexpected error in convert endpoint")
        return _error_response(ERR_INTERNAL, HTTP_INTERNAL_SERVER_ERROR)


@api_blueprint.route("/download/<filename>")
def download(filename: str) -> Response | tuple[Response, int]:
    """Serve the converted file directly from its path."""
    cached_file = cache.get_file(filename)
    if not cached_file:
        return _error_response(ERR_FILE_NOT_FOUND, HTTP_NOT_FOUND)

    try:
        return send_file(
            cached_file["filepath"],
            mimetype=cached_file["mimetype"],
            as_attachment=True,
            download_name=cached_file["filename"],
        )
    except Exception:
        logger.exception("Unexpected error in download endpoint")
        return _error_response(ERR_INTERNAL, HTTP_INTERNAL_SERVER_ERROR)


def _extract_file_ids_for_batch_download(data: dict[str, Any]) -> list[str] | None:
    """Return a deduplicated list of file IDs from request payload."""
    file_ids = _extract_str_items(data, "fileIds", dedupe=True)
    if not file_ids:
        return []
    return file_ids


def _cached_files_for_batch_ids(file_ids: list[str]) -> list[cache.CachedFile]:
    """Resolve cached files for IDs, skipping missing or deleted paths."""
    selected_files: list[cache.CachedFile] = []
    for file_id in file_ids:
        cached = cache.get_file(file_id)
        if cached and Path(cached["filepath"]).exists():
            selected_files.append(cached)
    return selected_files


@api_blueprint.route("/download-batch", methods=["POST"])
def download_batch() -> Response | tuple[Response, int]:
    """Build and stream a zip containing selected cached files."""
    zip_path: str | None = None
    response: Response

    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return _error_response(ERR_INVALID_JSON, HTTP_BAD_REQUEST)

        data = cast("dict[str, Any]", data)

        file_ids = _extract_file_ids_for_batch_download(data)
        if file_ids is None or not file_ids:
            error = (
                ERR_NO_FILE_IDS_PROVIDED
                if file_ids is None
                else ERR_NO_VALID_FILE_IDS_PROVIDED
            )
            return _error_response(error, HTTP_BAD_REQUEST)

        selected_files = _cached_files_for_batch_ids(file_ids)

        if not selected_files:
            return _error_response(ERR_FILE_NOT_FOUND_IN_CACHE, HTTP_NOT_FOUND)

        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".zip",
            dir=_temp_dir(),
            delete=False,
        ) as temp_zip:
            zip_path = temp_zip.name

        with zipfile.ZipFile(
            zip_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as zf:
            for index, cached in enumerate(selected_files, start=1):
                file_path = cached["filepath"]
                filename = cached["filename"]
                archive_name = filename
                if archive_name in zf.namelist():
                    archive_name = f"{index}_{filename}"
                zf.write(file_path, arcname=archive_name)

        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        response = send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"converted_batch_{timestamp}.zip",
        )
    except Exception:
        logger.exception("Unexpected error in download_batch endpoint")
        _safe_delete_path(zip_path)
        return _error_response(ERR_INTERNAL, HTTP_INTERNAL_SERVER_ERROR)

    def _cleanup_zip() -> None:
        _safe_delete_path(zip_path)

    response.call_on_close(_cleanup_zip)
    return response


@api_blueprint.route("/cache/status")
def cache_status() -> tuple[Response, int]:
    """Get current /dev/shm memory cache status."""
    return jsonify(cache.get_cache_status()), HTTP_OK


@api_blueprint.route("/hardware/status")
def hardware_status() -> tuple[Response, int]:
    """Check if hardware encoders are available."""
    h264_hw = ffmpeg.get_hw_encoder("h264")
    h265_hw = ffmpeg.get_hw_encoder("h265")

    return jsonify(
        {
            "h264_available": h264_hw is not None,
            "h264_encoder": h264_hw,
            "h265_available": h265_hw is not None,
            "h265_encoder": h265_hw,
            "has_hardware": h264_hw is not None or h265_hw is not None,
        },
    ), HTTP_OK


@api_blueprint.route("/progress/<file_id>")
def progress(file_id: str) -> tuple[Response, int]:
    """Get conversion progress for a specific file."""
    progress_data = state.get_progress(file_id)
    if progress_data:
        return jsonify(progress_data), HTTP_OK

    if cache.has_file(file_id):
        return jsonify(state.pending_payload()), HTTP_OK

    return jsonify(state.complete_payload()), HTTP_OK


@api_blueprint.route("/progress/stream/<file_id>")
def progress_stream(file_id: str) -> Response:
    """Stream conversion progress using Server-Sent Events."""

    def generate() -> Generator[str, None, None]:
        logger.info("SSE stream started for file_id: %s", file_id)
        last_percent = -1.0
        wait_count = 0

        try:
            while True:
                progress_data = state.get_progress(file_id)
                if progress_data:
                    percent = float(progress_data.get("percent", state.PERCENT_PENDING))
                    status = str(progress_data.get("status", state.STATUS_CONVERTING))
                    frame = int(progress_data.get("frame", 0))
                    total_frames = int(progress_data.get("total_frames", 0))
                    speed = str(progress_data.get("speed", "-"))

                    if percent != last_percent or wait_count == 0:
                        last_percent = percent
                        logger.debug("SSE sending: %s%% - %s", percent, status)
                        yield state.to_sse(
                            state.payload(percent, status, frame, total_frames, speed),
                        )

                    if percent >= state.PERCENT_COMPLETE:
                        logger.info("SSE stream complete for file_id: %s", file_id)
                        yield state.to_sse(state.complete_payload())
                        break
                else:
                    if not cache.has_file(file_id):
                        logger.info("SSE stream ending, file not in cache: %s", file_id)
                        yield state.to_sse(state.complete_payload())
                        break

                    wait_count += 1
                    if wait_count > state.SSE_MAX_WAIT_CYCLES:
                        logger.warning(
                            "SSE stream timeout waiting for conversion: %s",
                            file_id,
                        )
                        yield state.to_sse(state.timeout_payload())
                        break

                    if wait_count == 1:
                        logger.debug("SSE waiting for conversion to start: %s", file_id)

                time.sleep(state.POLL_INTERVAL_SECONDS)
        except (GeneratorExit, BrokenPipeError, ConnectionResetError):
            # Normal when client refreshes/closes tab while SSE is active.
            logger.info("SSE client disconnected for file_id: %s", file_id)
        except Exception:
            logger.exception("Unexpected SSE stream error for file_id: %s", file_id)

    response = Response(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


@api_blueprint.route("/cleanup", methods=["POST"])
def cleanup() -> tuple[Response, int]:
    """Physically delete files from /dev/shm and remove from cache."""
    try:
        data = request.get_json()
        if not isinstance(data, dict) or "fileIds" not in data:
            return _error_response(ERR_NO_FILE_IDS_PROVIDED, HTTP_BAD_REQUEST)

        data = cast("dict[str, Any]", data)

        file_ids = _extract_str_items(data, "fileIds") or []
        cleaned = cache.cleanup_files(file_ids)

        for cleaned_file_id in cleaned:
            logger.info("Cleaned up file and freed RAM: %s", cleaned_file_id)

        return jsonify(
            {
                "success": True,
                "cleaned": len(cleaned),
                "files": cleaned,
            },
        ), HTTP_OK
    except Exception:
        logger.exception("Error in cleanup endpoint")
        return _error_response(ERR_CLEANUP_FAILED, HTTP_INTERNAL_SERVER_ERROR)
