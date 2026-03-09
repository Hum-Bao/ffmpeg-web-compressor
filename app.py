"""Flask web application for video conversion using FFmpeg.

This module provides a web interface for uploading, analyzing, and converting
video files with various codec, resolution, and framerate options.

All processing uses stdin/stdout pipes - ZERO disk I/O, entirely in RAM.
No temporary files are ever created.
"""

import argparse
import logging
import tempfile

from flask import Flask, Request

from modules import api, configureos, exif


class RamBackedRequest(Request):
    """Request class that keeps upload temp streams inside active temp workspace."""

    def _get_file_stream(  # type: ignore[override]
        self,
        total_content_length: int | None,
        content_type: str | None,
        filename: str | None = None,
        content_length: int | None = None,
    ) -> tempfile.SpooledTemporaryFile[bytes]:
        # Werkzeug passes these by keyword; keep names exactly as expected.
        del total_content_length, content_type, filename, content_length
        return tempfile.SpooledTemporaryFile(
            max_size=8 * 1024 * 1024,
            mode="wb+",
            dir=api.get_active_temp_dir(),
        )


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.request_class = RamBackedRequest
# No file size limit - handle large files
app.config["MAX_CONTENT_LENGTH"] = None

app.register_blueprint(api.api_blueprint)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run ffmpeg-web server")
    parser.add_argument(
        "--enable-exif-logging",
        action="store_true",
        help="Capture EXIF dump logs to files under logs/exif_dumps",
    )
    args = parser.parse_args()

    exif.set_exif_dump_enabled(enabled=args.enable_exif_logging)
    if args.enable_exif_logging:
        logger.info("EXIF dump logging enabled")

    configureos.configure_runtime(logger)

    # Use pre-generated self-signed certificates for HTTPS
    # (adhoc context was causing startup hangs on some systems)
    # This is needed because iOS won't allow the share button to show on
    # non-HTTPS websites
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        ssl_context=("cert.pem", "key.pem"),
    )
