"""HTTP API routes for upload, conversion, progress tracking, and cleanup."""

import logging
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from flask import Blueprint, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from modules import cache, exif, ffmpeg, state

logger = logging.getLogger(__name__)

api_blueprint = Blueprint("api", __name__)

DEV_SHM_PATH = Path("/dev/shm")  # noqa: S108
DEFAULT_CONTAINER = "mp4"
DEFAULT_MIMETYPE = "video/mp4"


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

# Temporary debug switch to inspect FFmpeg progress/stderr output in app logs.
LOG_RAW_FFMPEG_PROGRESS = False
MAX_CAPTURED_STDERR_LINES = 4000


def _temp_dir() -> str:
    """Return preferred temp directory path."""
    if DEV_SHM_PATH.exists():
        return str(DEV_SHM_PATH)
    return tempfile.gettempdir()


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
    state.set_progress(
        file_id=file_id,
        percent=state.PERCENT_PENDING,
        status=state.STATUS_STARTING,
        duration=total_duration,
        frame=0,
        total_frames=total_frames,
        speed="-",
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
            "Running conversion via /dev/shm (attempt %d, audio_mode=%s): %s",
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
            returncode = process.wait(timeout=7200)
        except subprocess.TimeoutExpired:
            process.kill()
            progress_thread.join(timeout=2)
            _cleanup_after_failed_conversion(file_id, input_path, settings.output_path)
            return jsonify({"error": ERR_CONVERSION_TIMEOUT}), 500

        progress_thread.join(timeout=2)
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
            return jsonify({"error": ERR_CONVERSION_FAILED}), 500

        logger.warning(
            "Audio copy appears incompatible with target container; "
            "retrying with compatible audio encoding.",
        )
        state.set_progress(
            file_id=file_id,
            percent=state.PERCENT_PENDING,
            status=state.STATUS_STARTING,
            duration=total_duration,
            frame=0,
            total_frames=total_frames,
            speed="-",
        )

    state.remove_progress(file_id)

    if not succeeded:
        _cleanup_after_failed_conversion(file_id, input_path, settings.output_path)
        return jsonify({"error": ERR_CONVERSION_FAILED}), 500

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

    return jsonify({"filename": output_id, "size": output_size}), 200


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
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if not file or file.filename == "":
            return jsonify({"error": "No file selected"}), 400

        filename = secure_filename(str(file.filename))
        logger.info("Analyzing video: %s", filename)

        client_size_raw = request.form.get("clientSizeBytes", "")
        client_size_bytes = 0
        if client_size_raw.isdigit():
            client_size_bytes = int(client_size_raw)
        input_ext = Path(filename).suffix or ".mp4"
        with tempfile.NamedTemporaryFile(
            delete=False,
            dir=_temp_dir(),
            suffix=input_ext,
        ) as temp_file:
            temp_filepath = temp_file.name
            file.save(temp_filepath)

        saved_size_bytes = Path(temp_filepath).stat().st_size
        request_content_length = int(request.content_length or 0)
        if client_size_bytes > 0 and saved_size_bytes != client_size_bytes:
            delta = saved_size_bytes - client_size_bytes
            logger.warning(
                "Upload size mismatch for %s: saved-client delta=%d bytes (%.2f MiB)",
                filename,
                delta,
                delta / (1024 * 1024),
            )

        exif.log_exif_dump(temp_filepath, "ORIGINAL VIDEO")

        meta = ffmpeg.get_video_info(temp_filepath, filename)
        if not meta:
            _safe_delete_path(temp_filepath)
            return jsonify({"error": ERR_COULD_NOT_READ_VIDEO}), 500

        file_id = f"{uuid.uuid4().hex}_{filename}"
        cache.put_file(
            file_id=file_id,
            filepath=temp_filepath,
            filename=filename,
            mimetype=file.mimetype or DEFAULT_MIMETYPE,
        )

        logger.info(
            "Cached file %s in /dev/shm (%.2f MB)",
            file_id,
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
        ), 200

    except Exception:
        logger.exception("Unexpected error in analyze endpoint")
        if file_id:
            cache.remove_file_and_delete(file_id)
        elif temp_filepath:
            _safe_delete_path(temp_filepath)
        return jsonify({"error": ERR_INTERNAL}), 500


@api_blueprint.route("/convert", methods=["POST"])
def convert() -> tuple[Response, int]:
    """Convert video file with specified settings using direct file paths."""
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"error": ERR_INVALID_JSON}), 400

        data = cast("dict[str, Any]", data)

        file_id_obj = data.get("fileId")
        if not isinstance(file_id_obj, str) or not file_id_obj:
            return jsonify({"error": "No file ID provided"}), 400

        return _convert_cached_file(file_id=file_id_obj, data=data)

    except MemoryError:
        logger.exception("Out of memory during conversion")
        return jsonify({"error": "Insufficient memory for conversion"}), 413
    except Exception:
        logger.exception("Unexpected error in convert endpoint")
        return jsonify({"error": ERR_INTERNAL}), 500


@api_blueprint.route("/download/<filename>")
def download(filename: str) -> Response | tuple[Response, int]:
    """Serve the converted file directly from its path."""
    cached_file = cache.get_file(filename)
    if not cached_file:
        return jsonify({"error": ERR_FILE_NOT_FOUND}), 404

    try:
        return send_file(
            cached_file["filepath"],
            mimetype=cached_file["mimetype"],
            as_attachment=True,
            download_name=cached_file["filename"],
        )
    except Exception:
        logger.exception("Unexpected error in download endpoint")
        return jsonify({"error": ERR_INTERNAL}), 500


@api_blueprint.route("/cache/status")
def cache_status() -> tuple[Response, int]:
    """Get current /dev/shm memory cache status."""
    return jsonify(cache.get_cache_status()), 200


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
    ), 200


@api_blueprint.route("/progress/<file_id>")
def progress(file_id: str) -> tuple[Response, int]:
    """Get conversion progress for a specific file."""
    progress_data = state.get_progress(file_id)
    if progress_data:
        return jsonify(progress_data), 200

    if cache.has_file(file_id):
        return jsonify(state.pending_payload()), 200

    return jsonify(state.complete_payload()), 200


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
            return jsonify({"error": "No file IDs provided"}), 400

        data = cast("dict[str, Any]", data)

        file_ids_obj = data.get("fileIds", [])
        file_ids = [fid for fid in file_ids_obj if isinstance(fid, str)]
        cleaned = cache.cleanup_files(file_ids)

        for cleaned_file_id in cleaned:
            logger.info("Cleaned up file and freed RAM: %s", cleaned_file_id)

        return jsonify(
            {
                "success": True,
                "cleaned": len(cleaned),
                "files": cleaned,
            },
        ), 200
    except Exception:
        logger.exception("Error in cleanup endpoint")
        return jsonify({"error": "Cleanup failed"}), 500
