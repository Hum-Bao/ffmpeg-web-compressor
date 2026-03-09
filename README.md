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
