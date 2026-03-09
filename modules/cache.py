"""In-memory cache helpers for uploaded and converted media files."""

from pathlib import Path
from typing import TypedDict


class CachedFile(TypedDict):
    """Structure for cached file data."""

    filepath: str  # CHANGED: Store the path in /dev/shm, not the raw bytes
    filename: str
    mimetype: str


# In-memory storage for uploaded and converted files
file_cache: dict[str, CachedFile] = {}


def put_file(file_id: str, filepath: str, filename: str, mimetype: str) -> None:
    """Store a file reference in the in-memory cache."""
    file_cache[file_id] = CachedFile(
        filepath=filepath,
        filename=filename,
        mimetype=mimetype,
    )


def has_file(file_id: str) -> bool:
    """Return whether a file ID exists in cache."""
    return file_id in file_cache


def get_file(file_id: str) -> CachedFile | None:
    """Fetch a cached file record by ID."""
    return file_cache.get(file_id)


def remove_file(file_id: str) -> CachedFile | None:
    """Remove and return a cached file record if it exists."""
    return file_cache.pop(file_id, None)


def delete_physical_file(filepath: str) -> bool:
    """Delete a physical file path and return whether deletion succeeded."""
    try:
        Path(filepath).unlink()
        return True
    except OSError:
        return False


def remove_file_and_delete(file_id: str) -> bool:
    """Remove a file from cache and delete its physical file if present."""
    cached = remove_file(file_id)
    if not cached:
        return False

    delete_physical_file(cached["filepath"])
    return True


def cleanup_files(file_ids: list[str]) -> list[str]:
    """Delete physical files and remove matching entries from cache."""
    cleaned: list[str] = []
    for file_id in file_ids:
        if remove_file_and_delete(file_id):
            cleaned.append(file_id)
    return cleaned


def clear_all_files() -> list[str]:
    """Delete every cached file and clear in-memory cache entries."""
    file_ids = list(file_cache.keys())
    return cleanup_files(file_ids)


def get_cache_status() -> dict[str, int | float | list[dict[str, int | float | str]]]:
    """Build cache status payload with file-level and total size info."""
    files_info: list[dict[str, int | float | str]] = []
    total_size = 0

    for file_id, cached in file_cache.items():
        filepath = cached["filepath"]
        try:
            size_bytes = Path(filepath).stat().st_size
        except OSError:
            size_bytes = 0

        size_mb = round(size_bytes / (1024 * 1024), 2)
        total_size += size_bytes
        files_info.append(
            {
                "id": file_id,
                "filename": cached["filename"],
                "size_mb": size_mb,
            },
        )

    return {
        "files_cached": len(file_cache),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "files": files_info,
    }
