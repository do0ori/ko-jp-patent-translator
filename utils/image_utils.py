import logging
import os
import shutil
import subprocess
import tempfile
from io import BytesIO
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)


def _try_cli_conversion(image_bytes: bytes, suffix: str = ".wmf") -> Optional[Image.Image]:
    """Attempt converting vector image bytes (WMF/EMF) to PNG using system CLI tools.

    Tries in order:
      1. wmf2gd (libwmf-bin)
      2. magick / convert (ImageMagick)
      3. soffice / libreoffice (LibreOffice)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        output_png = os.path.join(tmpdir, "output.png")

        try:
            with open(input_path, "wb") as f:
                f.write(image_bytes)
        except Exception as e:
            log.warning("Failed to write temporary image file for conversion: %s", e)
            return None

        # 1. Try wmf2gd (libwmf-bin)
        if shutil.which("wmf2gd"):
            try:
                cmd = ["wmf2gd", "-t", "png", "-o", output_png, input_path]
                res = subprocess.run(cmd, capture_output=True, timeout=15)
                if res.returncode == 0 and os.path.exists(output_png) and os.path.getsize(output_png) > 0:
                    with Image.open(output_png) as img:
                        img_copy = img.copy()
                        img_copy.load()
                        log.info("Successfully converted %s to PNG via wmf2gd.", suffix)
                        return img_copy
            except Exception as e:
                log.debug("wmf2gd conversion failed: %s", e)

        # 2. Try ImageMagick (magick or convert)
        magick_bin = shutil.which("magick") or shutil.which("convert")
        if magick_bin:
            try:
                cmd = [magick_bin, input_path, output_png]
                res = subprocess.run(cmd, capture_output=True, timeout=15)
                if res.returncode == 0 and os.path.exists(output_png) and os.path.getsize(output_png) > 0:
                    with Image.open(output_png) as img:
                        img_copy = img.copy()
                        img_copy.load()
                        log.info("Successfully converted %s to PNG via %s.", suffix, magick_bin)
                        return img_copy
            except Exception as e:
                log.debug("ImageMagick conversion failed: %s", e)

        # 3. Try LibreOffice (soffice / libreoffice)
        libreoffice_bin = shutil.which("soffice") or shutil.which("libreoffice")
        if libreoffice_bin:
            try:
                cmd = [
                    libreoffice_bin,
                    "--headless",
                    "--convert-to",
                    "png",
                    "--outdir",
                    tmpdir,
                    input_path,
                ]
                res = subprocess.run(cmd, capture_output=True, timeout=30)
                converted_png = os.path.join(tmpdir, "input.png")
                if res.returncode == 0 and os.path.exists(converted_png) and os.path.getsize(converted_png) > 0:
                    with Image.open(converted_png) as img:
                        img_copy = img.copy()
                        img_copy.load()
                        log.info("Successfully converted %s to PNG via LibreOffice.", suffix)
                        return img_copy
            except Exception as e:
                log.debug("LibreOffice conversion failed: %s", e)

    return None


def load_or_convert_image(image_bytes: bytes) -> Optional[Image.Image]:
    """Safely load an image from bytes into a PIL Image.

    If standard Pillow loading fails (e.g. WMF/EMF on Linux without loader, or
    corrupted data), attempts external CLI conversion tools. If all conversion
    attempts fail, logs a warning and returns None without raising an exception.
    """
    if not image_bytes:
        return None

    # First attempt: standard Pillow open and load
    try:
        image = Image.open(BytesIO(image_bytes))
        # Force loading image raster data to ensure it's not a lazy stub
        image.load()
        return image
    except Exception as exc:
        log.warning("Pillow could not load image directly (%s): %s. Attempting fallback conversion...", type(exc).__name__, exc)

    # Detect header or assume WMF/EMF for fallback
    suffix = ".wmf"
    if len(image_bytes) >= 4 and image_bytes[:4] == b"\x01\x00\x00\x00":
        suffix = ".emf"

    converted = _try_cli_conversion(image_bytes, suffix=suffix)
    if converted is not None:
        return converted

    log.warning("Could not convert or load image (%s). Figure will be skipped gracefully.", suffix)
    return None
