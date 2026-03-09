# FFmpeg Web Video Compressor

Web application for analyzing and compressing video files in the browser using Flask and FFmpeg.

The app is currently configured and tested primarily for this workflow:

- Hosted on a PC -> Accessed from Safari/browser on iOS over the local network

## Requirements

### Python

- Python 3.10+
- Packages in `requirements.txt`

Install Python dependencies:

```bash
pip install -r requirements.txt
```

### Non-Python System Dependencies

These must be installed on the host machine and available in `PATH`:

- `ffmpeg` (video conversion)
- `ffprobe` (video metadata analysis)
- `exiftool` (metadata copy/restore)

Linux (Debian/Ubuntu):

```bash
sudo apt update
sudo apt install -y ffmpeg libimage-exiftool-perl
```

Verify binaries:

```bash
ffmpeg -version
ffprobe -version
exiftool -ver
```

Notes:

- `ffprobe` is usually installed with `ffmpeg`.
- `exiftool` is required by the metadata restore flow.
- The `ffmpeg` and `ffprobe` version should not matter much, but `exiftool` requires version `>=13.13`, as it added the ability to properly write lens info for iOS metadata

## Run

Start the app:

```bash
python3 app.py
```

Optional EXIF dump logging:

```bash
python3 app.py --enable-exif-logging
```

Default bind address:

- `0.0.0.0:5000`

Access it at:

- `https://<host-ip>:5000`

## RAM Storage On Linux And Windows

The app writes uploads and converted outputs to a temp directory selected by the backend.
Startup uses `modules/configureos.py` to detect the OS and auto-apply runtime env setup.

By default, RAM-backed storage is required (`FFMPEG_WEB_REQUIRE_RAM_STORAGE=1`).
If RAM-backed storage cannot be found, the app exits at startup.

Priority order:

- `FFMPEG_WEB_RAMDISK_DIR` (if set)
- `/dev/shm` (Linux)
- `/run/shm` (Linux)
- OS default temp folder

Note:

- The OS default temp folder is only used if RAM requirement is disabled (`FFMPEG_WEB_REQUIRE_RAM_STORAGE=0`).
- The app creates a per-run temp workspace directory and automatically removes it on process exit.
- Flask upload spool files are also forced into the same RAM-backed workspace.
- The app sets `TMPDIR`/`TMP`/`TEMP` to the active RAM workspace at startup.

### Linux

No extra setup is needed if `/dev/shm` is available.
The app auto-sets `FFMPEG_WEB_RAMDISK_DIR` to `/dev/shm` (or `/run/shm` fallback) when unset.

### Windows (RAM-backed mode)

Windows does not provide `/dev/shm`, so the app can auto-create a RAM disk if ImDisk is installed.

1. Install ImDisk Toolkit (must expose `imdisk` command in `PATH`).
1. Run the app normally.

The app will auto-create and mount a RAM disk on `R:` (default), create `R:\ffmpeg-temp`, and use it as `FFMPEG_WEB_RAMDISK_DIR`.
If the app created the RAM disk, it will detach it on process exit.

Optional Windows tuning:

```powershell
setx FFMPEG_WEB_WINDOWS_RAMDISK_SIZE "8G"
setx FFMPEG_WEB_WINDOWS_RAMDISK_DRIVE "R:"
setx FFMPEG_WEB_WINDOWS_RAMDISK_FOLDER "ffmpeg-temp"
```

You can still override directly:

```powershell
setx FFMPEG_WEB_RAMDISK_DIR "R:\\ffmpeg-temp"
```

If `FFMPEG_WEB_RAMDISK_DIR` is not set (or points to a missing folder), startup fails in default RAM-only mode.

Optional fallback mode (allows disk-backed temp):

```powershell
setx FFMPEG_WEB_REQUIRE_RAM_STORAGE "0"
```

## Minimal Cross-Platform Setup

Linux:

1. Install dependencies from this README.
1. Ensure `/dev/shm` exists (default on most distros).
1. Run `python3 app.py`.

Windows:

1. Install dependencies (`ffmpeg`, `ffprobe`, `exiftool`).
1. Install ImDisk Toolkit (for automatic RAM-disk creation).
1. Run `python app.py`.

## API Endpoints

- `GET /` main page
- `POST /analyze` upload and analyze a video
- `POST /convert` convert uploaded video
- `GET /download/<filename>` download converted video
- `GET /cache/status` cache status
- `GET /hardware/status` hardware encoder status
- `GET /progress/<file_id>` conversion progress snapshot
- `GET /progress/stream/<file_id>` conversion progress SSE stream
- `POST /cleanup` delete cached files
