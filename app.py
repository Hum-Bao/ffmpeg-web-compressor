"""Flask web application for video conversion using FFmpeg.

This module provides a web interface for uploading, analyzing, and converting
video files with various codec, resolution, and framerate options.

All processing uses stdin/stdout pipes - ZERO disk I/O, entirely in RAM.
No temporary files are ever created.
"""

import argparse
import ipaddress
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from flask import Flask, Request

from modules import api, cache, configureos, exif, ramdisk, state

APP_DIR = Path(__file__).resolve().parent
CERT_PATH = APP_DIR / "cert.pem"
KEY_PATH = APP_DIR / "key.pem"


def _ensure_ssl_certificates(logger: logging.Logger) -> tuple[str, str]:
    """Create self-signed HTTPS cert/key on first run and return their paths."""
    if CERT_PATH.exists() and KEY_PATH.exists():
        return str(CERT_PATH), str(KEY_PATH)

    logger.info("Generating self-signed HTTPS certificate for first run")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ffmpeg-web-compressor"),
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ],
    )

    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ],
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    KEY_PATH.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info("Generated HTTPS certificate and key: %s, %s", CERT_PATH, KEY_PATH)
    return str(CERT_PATH), str(KEY_PATH)


def _graceful_shutdown(logger: logging.Logger) -> None:
    """Best-effort cleanup for Ctrl+C and normal process exit."""
    cleaned_files = cache.clear_all_files()
    removed_progress = state.clear_all_progress()
    removed_temp_dir = api.cleanup_runtime_temp_dir()
    detached_ramdisk = ramdisk.cleanup_windows_ramdisk(logger)

    logger.info(
        (
            "Shutdown cleanup complete: files=%d, progress_entries=%d, "
            "temp_dir_removed=%s, ramdisk_detached=%s"
        ),
        len(cleaned_files),
        removed_progress,
        removed_temp_dir,
        detached_ramdisk,
    )


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
    cert_file, key_file = _ensure_ssl_certificates(logger)

    # Use pre-generated self-signed certificates for HTTPS
    # (adhoc context was causing startup hangs on some systems)
    # This is needed because iOS won't allow the share button to show on
    # non-HTTPS websites
    try:
        app.run(
            host="0.0.0.0",
            port=5000,
            debug=False,
            ssl_context=(cert_file, key_file),
        )
    finally:
        _graceful_shutdown(logger)
