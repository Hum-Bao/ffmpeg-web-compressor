"""Flask web application for video conversion using FFmpeg.

This module provides a web interface for uploading, analyzing, and converting
video files with various codec, resolution, and framerate options.

All processing uses stdin/stdout pipes - ZERO disk I/O, entirely in RAM.
No temporary files are ever created.
"""

import argparse
import logging

from flask import Flask

from modules import api, exif

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
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

    # Add ssl_context='adhoc' to generate a temporary HTTPS certificate
    # This is needed because iOS won't allow the share button to show on
    # non-HTTPS websites
    app.run(host="0.0.0.0", port=5000, debug=False, ssl_context="adhoc")
