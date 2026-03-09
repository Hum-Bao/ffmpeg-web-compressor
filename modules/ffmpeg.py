"""FFmpeg/ffprobe helpers for video analysis and conversion command building."""

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

# Resolve executables once; fallback keeps behavior on uncommon setups.
FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE_BIN = shutil.which("ffprobe") or "ffprobe"

# Map UI values to FFmpeg libraries
CODECS = {"h264": "libx264", "h265": "libx265"}

# Map codec to hardware encoder options (in preference order)
HW_ENCODERS = {
    "h264": ["h264_nvenc", "h264_qsv", "h264_vaapi"],
    "h265": ["hevc_nvenc", "hevc_qsv", "hevc_vaapi"],
}

SUPPORTED_CONTAINERS = {"mov", "mp4", "m4v", "avi", "mkv", "webm", "flv", "wmv"}

# FFmpeg's `m4v` muxer is raw MPEG-4 video, not the MP4 container used by .m4v files.
# We still write a `.m4v` filename, but force MP4 muxing for compatibility.
OUTPUT_MUXER_BY_CONTAINER = {
    "m4v": "mp4",
}

IOS_HEVC_TAG_CONTAINERS = {"mp4", "m4v", "mov"}
AUDIO_COMPAT_ARGS_BY_CONTAINER = {
    "mp4": ["-c:a", "aac", "-b:a", "160k"],
    "m4v": ["-c:a", "aac", "-b:a", "160k"],
    "mov": ["-c:a", "aac", "-b:a", "160k"],
    "mkv": ["-c:a", "aac", "-b:a", "160k"],
    "avi": ["-c:a", "aac", "-b:a", "160k"],
    "webm": ["-c:a", "libopus", "-b:a", "128k"],
    "wmv": ["-c:a", "wmav2", "-b:a", "160k"],
    "flv": ["-c:a", "aac", "-b:a", "128k"],
}
DEFAULT_AUDIO_COMPAT_ARGS = ["-c:a", "aac", "-b:a", "160k"]

QUALITY_MAP = {
    "less_space": {
        "h264_crf": "28",
        "h265_crf": "28",
        "nvenc_cq": "28",
        "qsv_q": "28",
        "preset": "veryfast",
    },
    "less_space_plus": {
        "h264_crf": "26",
        "h265_crf": "26",
        "nvenc_cq": "26",
        "qsv_q": "26",
        "preset": "faster",
    },
    "balanced": {
        "h264_crf": "23",
        "h265_crf": "23",
        "nvenc_cq": "23",
        "qsv_q": "23",
        "preset": "medium",
    },
    "balanced_plus": {
        "h264_crf": "21",
        "h265_crf": "21",
        "nvenc_cq": "21",
        "qsv_q": "21",
        "preset": "medium",
    },
    "better_quality": {
        "h264_crf": "19",
        "h265_crf": "19",
        "nvenc_cq": "19",
        "qsv_q": "19",
        "preset": "slow",
    },
}

METADATA_ARGS = [
    # "-map_metadata",
    # "0",
    # "-map_metadata:s:v",
    # "0:s:v",
    # "-map_metadata:s:a",
    # "0:s:a",
    # "-movflags",
    # "use_metadata_tags",
    "-write_tmcd",
    "1",
]


@dataclass(frozen=True)
class BuildSettings:
    """Inputs required to build the FFmpeg conversion command."""

    target_res: str
    target_fps: str
    target_codec: str
    container: str
    input_path: str
    output_path: str
    quality: str = "balanced"
    use_hardware: bool = True
    audio_mode: str = "copy"


# Cache for hardware encoder availability
_hw_encoders_cache: dict[str, str | None] = {}


def _detect_hw_encoder_for_codec(codec: str) -> str | None:
    """Probe supported hardware encoders for one codec and return first match."""
    if codec not in HW_ENCODERS:
        return None

    for encoder in HW_ENCODERS[codec]:
        if _probe_encoder_available(encoder):
            logger.info("Hardware encoder available and working: %s", encoder)
            return encoder

    logger.info("No working hardware encoder found for %s, will use software", codec)
    return None


def _initialize_hw_encoder_cache() -> None:
    """Detect hardware encoders once at process startup."""
    for codec in HW_ENCODERS:
        _hw_encoders_cache[codec] = _detect_hw_encoder_for_codec(codec)


def _to_int(value: object, default: int = 0) -> int:
    """Best-effort int conversion for ffprobe values."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default

    return default


def _to_float(value: object, default: float = 0.0) -> float:
    """Best-effort float conversion for ffprobe values."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default

    return default


def _as_object_dict(value: object) -> dict[str, object] | None:
    """Return a string-keyed object dict when possible."""
    if not isinstance(value, dict):
        return None

    raw_dict = cast("dict[object, object]", value)
    normalized: dict[str, object] = {}
    for key, dict_value in raw_dict.items():
        if not isinstance(key, str):
            return None
        normalized[key] = dict_value
    return normalized


def _as_object_list(value: object) -> list[object] | None:
    """Return a list[object] when possible."""
    if isinstance(value, list):
        return cast("list[object]", value)
    return None


def _first_object_dict(items: list[object]) -> dict[str, object] | None:
    """Return first item as string-keyed object dict when possible."""
    if not items:
        return None

    return _as_object_dict(items[0])


def _probe_encoder_available(encoder: str) -> bool:
    """Return True if a hardware encoder can successfully encode a test frame."""
    try:
        result = subprocess.run(  # noqa: S603
            [
                FFMPEG_BIN,
                "-f",
                "lavfi",
                "-i",
                "nullsrc=s=256x256:d=0.1",
                "-c:v",
                encoder,
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Error testing encoder %s: %s", encoder, exc)
        return False

    if result.returncode == 0:
        return True

    logger.debug(
        "Hardware encoder %s listed but not functional: %s",
        encoder,
        result.stderr.decode()[:200],
    )
    return False


def _detect_container(filename: str, format_name: str) -> str:
    """Infer output container from filename extension or ffprobe format name."""
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext in SUPPORTED_CONTAINERS:
            return ext

    format_list = format_name.split(",") if format_name else ["mp4"]
    return format_list[0]


def _parse_fps(stream: dict[str, object]) -> int:
    """Parse integer FPS from ffprobe r_frame_rate."""
    r_frame_rate = stream.get("r_frame_rate")
    if not isinstance(r_frame_rate, str):
        return 0

    try:
        num, den = map(int, r_frame_rate.split("/"))
        if den > 0:
            return round(num / den)
    except (ValueError, ZeroDivisionError):
        logger.warning("Could not parse frame rate")

    return 0


def _extract_display_dimensions(stream: dict[str, object]) -> tuple[int, int, int]:
    """Return rotation-aware display width/height and the rotation value."""
    display_width = _to_int(stream.get("width", 0))
    display_height = _to_int(stream.get("height", 0))
    rotation = 0

    side_data_list = stream.get("side_data_list")
    side_data_items = _as_object_list(side_data_list)
    if side_data_items is not None:
        for side_data in side_data_items:
            side_data_dict = _as_object_dict(side_data)
            if side_data_dict is None:
                continue
            raw_rotation = side_data_dict.get("rotation")
            if raw_rotation is None:
                continue

            rotation = _to_int(raw_rotation)
            if abs(rotation) in (90, 270):
                display_width, display_height = display_height, display_width
            break

    return display_width, display_height, rotation


def _normalize_codec(stream: dict[str, object]) -> str:
    """Normalize codec naming for H.264/HEVC detection."""
    codec = str(stream.get("codec_name", "unknown"))
    codec_tag = str(stream.get("codec_tag_string", "")).lower()

    if codec in ("h264", "hevc"):
        return codec
    if codec_tag in ("hvc1", "hev1"):
        return "hevc"
    return codec


def _base_ffmpeg_command(input_path: str) -> list[str]:
    """Build the static FFmpeg command prefix for this app."""
    return [
        FFMPEG_BIN,
        "-y",
        "-progress",
        "pipe:2",
        "-i",
        input_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
    ]


def _build_scale_filter(
    meta: dict[str, int | float | str],
    target_res: str,
) -> tuple[str | None, bool]:
    """Return scale filter and whether scaling requires encoding."""
    if target_res == "original":
        return None, False

    target_res_val = int(target_res)
    input_h = int(meta.get("display_height", meta.get("height", 0)))
    input_w = int(meta.get("display_width", meta.get("width", 0)))

    is_portrait = input_w < input_h
    if is_portrait and target_res_val < input_w:
        return f"scale={target_res_val}:-2", True
    if not is_portrait and target_res_val < input_h:
        return f"scale=-2:{target_res_val}", True

    return None, False


def _source_codec_name(meta: dict[str, int | float | str]) -> str:
    """Map source stream codec to h264/h265 labels used by this app."""
    codec = str(meta.get("codec", ""))
    if "hevc" in codec or "h265" in codec:
        return "h265"
    return "h264"


def _select_encoder(codec_name: str, *, use_hardware: bool) -> str:
    """Select hardware or software encoder and emit consistent logging."""
    hw_encoder = get_hw_encoder(codec_name) if use_hardware else None
    if hw_encoder:
        logger.info("Using hardware encoder: %s", hw_encoder)
        return hw_encoder

    sw_encoder = CODECS[codec_name]
    logger.info("Using software encoder: %s", sw_encoder)
    return sw_encoder


def _get_usable_cores() -> str:
    """Return CPU cores visible to this process/container for FFmpeg threading."""
    try:
        return str(len(os.sched_getaffinity(0)))
    except AttributeError:
        return str(os.cpu_count() or 4)


def _is_hw_encoder(encoder: str) -> bool:
    return any(name in encoder for name in ("nvenc", "qsv", "vaapi"))


def _is_h264_codec(codec: str) -> bool:
    return "264" in codec or "h264" in codec


def _is_h265_codec(codec: str) -> bool:
    return "265" in codec or "hevc" in codec


def _is_hevc_encoder(encoder: str, target_codec: str) -> bool:
    return _is_h265_codec(target_codec) or "hevc" in encoder


def _add_encoder_quality_args(
    cmd: list[str],
    selected_codec: str,
    quality_settings: dict[str, str],
    target_codec: str,
    usable_cores: str,
) -> None:
    """Append encoder-specific quality/performance tuning arguments."""
    if _is_hw_encoder(selected_codec):
        if "nvenc" in selected_codec:
            cmd.extend(
                [
                    "-preset",
                    "p4",
                    "-rc",
                    "vbr",
                    "-cq",
                    quality_settings["nvenc_cq"],
                ],
            )
        elif "qsv" in selected_codec:
            cmd.extend(
                [
                    "-preset",
                    "faster",
                    "-global_quality",
                    quality_settings["qsv_q"],
                ],
            )
        return

    cmd.extend(["-preset", quality_settings.get("preset", "faster")])
    if _is_hevc_encoder(selected_codec, target_codec):
        x265_params = ["asm=auto", f"pools={usable_cores}", "lookahead-slices=0"]
        cmd.extend(
            [
                "-crf",
                quality_settings["h265_crf"],
                "-x265-params",
                ":".join(x265_params),
            ],
        )
    else:
        cmd.extend(["-crf", quality_settings["h264_crf"], "-x264-params", "asm=auto"])


def _add_ios_codec_compatibility_args(
    cmd: list[str],
    selected_codec: str,
    container: str,
) -> None:
    """Append iOS playback compatibility args for selected video codec."""
    if _is_h264_codec(selected_codec):
        if "nvenc" in selected_codec:
            cmd.extend(["-profile:v", "main", "-level:v", "4.1"])
        else:
            cmd.extend(["-profile:v", "main", "-level", "4.1"])
        return

    if _is_h265_codec(selected_codec):
        if "nvenc" in selected_codec:
            cmd.extend(["-profile:v", "main", "-level:v", "5.1"])
        else:
            cmd.extend(["-profile:v", "main", "-level", "5.1"])

        if container.lower() in IOS_HEVC_TAG_CONTAINERS:
            cmd.extend(["-tag:v", "hvc1"])


def _compat_audio_args_for_container(container: str) -> list[str]:
    """Return fallback audio codec args compatible with the target container."""
    normalized = container.lower()
    return AUDIO_COMPAT_ARGS_BY_CONTAINER.get(normalized, DEFAULT_AUDIO_COMPAT_ARGS)


def _add_audio_args(cmd: list[str], container: str, audio_mode: str) -> None:
    """Append audio args with copy-first behavior and optional compat fallback."""
    if audio_mode == "copy":
        cmd.extend(["-c:a", "copy"])
        return

    if audio_mode == "compat":
        cmd.extend(_compat_audio_args_for_container(container))
        return

    cmd.extend(["-c:a", "copy"])


def should_retry_with_compat_audio(stderr_text: str) -> bool:
    """Return whether FFmpeg stderr indicates audio-copy/container incompatibility."""
    lowered = stderr_text.lower()

    # Require at least one hard incompatibility marker to avoid retrying unrelated failures.
    hard_markers = [
        "could not find tag for codec",
        "codec not currently supported in container",
        "error initializing output stream 0:1",
        "could not write header",
    ]
    has_hard_marker = any(marker in lowered for marker in hard_markers)
    has_audio_context = "audio" in lowered or "0:1" in lowered
    return has_hard_marker and has_audio_context


def get_hw_encoder(codec: str) -> str | None:
    """Return startup-detected hardware encoder for the given codec."""
    return _hw_encoders_cache.get(codec)


def get_video_info(
    filepath: str,
    filename: str = "",
) -> dict[str, int | float | str] | None:
    """Probe a video file and return normalized stream metadata."""
    cmd = [
        FFPROBE_BIN,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        "-select_streams",
        "v:0",
        "-i",
        filepath,
    ]

    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.exception("ffprobe timeout")
        return None
    except (OSError, subprocess.SubprocessError):
        logger.exception("ffprobe execution error")
        return None

    if result.returncode != 0:
        logger.error("ffprobe failed with stderr: %s", result.stderr.decode())
        return None

    video_info: dict[str, int | float | str] | None = None
    try:
        data_obj = json.loads(result.stdout.decode())
        data = _as_object_dict(data_obj)
        if data is None:
            logger.error("Invalid ffprobe JSON payload")
        else:
            streams_obj = _as_object_list(data.get("streams"))
            stream = _first_object_dict(streams_obj or []) if streams_obj else None
            if stream is None:
                logger.error("No video stream found in file")
            else:
                format_info_obj = _as_object_dict(data.get("format", {})) or {}

                container = _detect_container(
                    filename,
                    str(format_info_obj.get("format_name", "mp4")),
                )
                fps = _parse_fps(stream)
                display_width, display_height, rotation = _extract_display_dimensions(
                    stream,
                )
                codec = _normalize_codec(stream)

                video_info = {
                    "width": _to_int(stream.get("width", 0)),
                    "height": _to_int(stream.get("height", 0)),
                    "display_width": display_width,
                    "display_height": display_height,
                    "rotation": rotation,
                    "codec": codec,
                    "pix_fmt": str(stream.get("pix_fmt", "yuv420p")),
                    "fps": fps,
                    "size": Path(filepath).stat().st_size,
                    "duration": _to_float(format_info_obj.get("duration", 0)),
                    "bitrate": _to_int(format_info_obj.get("bit_rate", 0)),
                    "container": container,
                }
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.exception("Error parsing video info")
    return video_info


def build_ffmpeg_command(
    meta: dict[str, int | float | str],
    settings: BuildSettings,
) -> list[str]:
    """Build FFmpeg command for file-based conversion."""
    cmd = _base_ffmpeg_command(settings.input_path)

    should_encode = False
    scale_filter, scaled = _build_scale_filter(meta, settings.target_res)
    should_encode = should_encode or scaled

    if settings.target_fps != "original":
        cmd.extend(["-r", settings.target_fps])
        should_encode = True

    selected_codec: str | None = None
    if settings.target_codec != "original":
        selected_codec = _select_encoder(
            settings.target_codec,
            use_hardware=settings.use_hardware,
        )
        cmd.extend(["-c:v", selected_codec])
        should_encode = True
    elif should_encode:
        selected_codec = _select_encoder(
            _source_codec_name(meta),
            use_hardware=settings.use_hardware,
        )
        cmd.extend(["-c:v", selected_codec])
    else:
        cmd.extend(["-c:v", "copy"])

    if selected_codec is not None:
        quality_settings = QUALITY_MAP.get(settings.quality, QUALITY_MAP["balanced"])
        usable_cores = _get_usable_cores()
        cmd.extend(["-threads", usable_cores])
        _add_encoder_quality_args(
            cmd,
            selected_codec,
            quality_settings,
            settings.target_codec,
            usable_cores,
        )
        cmd.extend(["-pix_fmt", "yuv420p"])

    if scale_filter:
        cmd.extend(["-vf", scale_filter])

    _add_audio_args(cmd, settings.container, settings.audio_mode)

    if selected_codec:
        _add_ios_codec_compatibility_args(cmd, selected_codec, settings.container)

    output_muxer = OUTPUT_MUXER_BY_CONTAINER.get(
        settings.container.lower(),
        settings.container,
    )

    cmd.extend(METADATA_ARGS)
    cmd.extend(["-f", output_muxer, settings.output_path])
    return cmd


def _parse_hms_time(value: str) -> float | None:
    """Parse HH:MM:SS(.sss) time string into elapsed seconds."""
    match = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value)
    if not match:
        return None

    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


MICROSECONDS_PER_SECOND = 1_000_000.0
MILLISECONDS_PER_SECOND = 1_000.0
NANOSECONDS_PER_SECOND = 1_000_000_000.0
OUT_TIME_MS_MICROSECOND_THRESHOLD = 1_000_000


def _parse_progress_key_value(key: str, value: str) -> float | None:
    """Parse known FFmpeg -progress key/value fields into seconds."""
    match key:
        case "out_time_us":
            return int(value) / MICROSECONDS_PER_SECOND if value.isdigit() else None
        case "out_time_ms":
            if not value.isdigit():
                return None
            raw_value = int(value)
            # Some builds label this *_ms but emit microseconds.
            if raw_value >= OUT_TIME_MS_MICROSECOND_THRESHOLD:
                return raw_value / MICROSECONDS_PER_SECOND
            return raw_value / MILLISECONDS_PER_SECOND
        case "out_time_ns":
            return int(value) / NANOSECONDS_PER_SECOND if value.isdigit() else None
        case "out_time":
            return _parse_hms_time(value)
        case _:
            return None


def parse_ffmpeg_progress(stderr_line: str) -> float | None:
    """Parse FFmpeg stderr for time progress and return elapsed seconds."""
    line = stderr_line.strip()

    # Fast-path FFmpeg progress key/value lines (out_time_*=...)
    key, sep, value = line.partition("=")
    if sep:
        parsed = _parse_progress_key_value(key, value.strip())
        if parsed is not None:
            return parsed

    # Fallback for classic status lines like: ... time=00:00:12.34 ...
    status_match = re.search(r"\btime=(\d+:\d+:\d+(?:\.\d+)?)", line)
    if not status_match:
        return None

    return _parse_hms_time(status_match.group(1))


_initialize_hw_encoder_cache()
