"""Shared in-memory application state helpers."""

import json
from typing import TypedDict


class ConversionProgress(TypedDict):
    """Structure for conversion progress tracking per file."""

    percent: float
    status: str
    duration: float
    frame: int
    total_frames: int
    speed: str


# Conversion progress tracking (file_id -> progress data)
conversion_progress: dict[str, ConversionProgress] = {}

STATUS_STARTING = "starting"
STATUS_CONVERTING = "converting"
STATUS_PENDING = "pending"
STATUS_COMPLETE = "complete"
STATUS_TIMEOUT = "timeout"

PERCENT_PENDING = 0.0
PERCENT_COMPLETE = 100.0
POLL_INTERVAL_SECONDS = 0.5
SSE_MAX_WAIT_CYCLES = 120


class ProgressPayload(TypedDict):
    """Lightweight payload used by progress APIs and SSE."""

    percent: float
    status: str
    frame: int
    total_frames: int
    speed: str


def set_progress(
    file_id: str,
    percent: float,
    status: str,
    duration: float,
    frame: int = 0,
    total_frames: int = 0,
    speed: str = "-",
) -> None:
    """Set conversion progress for a file ID."""
    conversion_progress[file_id] = ConversionProgress(
        percent=percent,
        status=status,
        duration=duration,
        frame=frame,
        total_frames=total_frames,
        speed=speed,
    )


def has_progress(file_id: str) -> bool:
    """Return whether conversion progress exists for a file ID."""
    return file_id in conversion_progress


def get_progress(file_id: str) -> ConversionProgress | None:
    """Fetch conversion progress for a file ID."""
    return conversion_progress.get(file_id)


def update_status(file_id: str, status: str) -> None:
    """Update only conversion status while preserving other fields."""
    current = get_progress(file_id)
    if not current:
        return

    conversion_progress[file_id] = ConversionProgress(
        percent=current["percent"],
        status=status,
        duration=current["duration"],
        frame=current["frame"],
        total_frames=current["total_frames"],
        speed=current["speed"],
    )


def remove_progress(file_id: str) -> None:
    """Remove conversion progress entry for a file ID if present."""
    conversion_progress.pop(file_id, None)


def payload(
    percent: float,
    status: str,
    frame: int = 0,
    total_frames: int = 0,
    speed: str = "-",
) -> ProgressPayload:
    """Build a progress payload dictionary."""
    return ProgressPayload(
        percent=percent,
        status=status,
        frame=frame,
        total_frames=total_frames,
        speed=speed,
    )


def pending_payload() -> ProgressPayload:
    """Build a pending progress payload."""
    return payload(PERCENT_PENDING, STATUS_PENDING)


def complete_payload() -> ProgressPayload:
    """Build a complete progress payload."""
    return payload(PERCENT_COMPLETE, STATUS_COMPLETE)


def timeout_payload() -> ProgressPayload:
    """Build a timeout progress payload."""
    return payload(PERCENT_PENDING, STATUS_TIMEOUT)


def to_sse(payload_obj: ProgressPayload) -> str:
    """Format a progress payload as an SSE data line."""
    return f"data: {json.dumps(payload_obj)}\\n\\n"
