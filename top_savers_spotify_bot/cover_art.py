from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image


MAX_THUMBNAIL_BYTES = 200 * 1024
MAX_THUMBNAIL_SIZE = (320, 320)


def prepare_audio_thumbnail(image_bytes: bytes, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size <= MAX_THUMBNAIL_BYTES:
        return destination

    with Image.open(BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        image.thumbnail(MAX_THUMBNAIL_SIZE)

        quality = 88
        while quality >= 45:
            output = BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
            if output.tell() <= MAX_THUMBNAIL_BYTES:
                destination.write_bytes(output.getvalue())
                return destination
            quality -= 8

        output = BytesIO()
        image.save(output, format="JPEG", quality=40, optimize=True)
        destination.write_bytes(output.getvalue())
        return destination
