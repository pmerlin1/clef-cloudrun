"""Turn small inline images into PIL objects for the pinned Clef processor."""
import base64
import binascii
from io import BytesIO
import re

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGES = 2
MAX_IMAGE_SIDE = 512
MAX_IMAGE_PIXELS = 512 * 512
MAX_IMAGE_BYTES = 48 * 1024
_DATA_URL = re.compile(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)\Z")


def prepare_images(payload):
    """Accept inline PNG/JPEG only; never fetch a caller-provided URL or path."""
    if "images" not in payload:
        return payload
    images = payload["images"]
    if not isinstance(images, list) or not 1 <= len(images) <= MAX_IMAGES:
        raise ValueError("Provide one or two inline images")
    prepared = []
    for value in images:
        match = _DATA_URL.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise ValueError("Images must be PNG or JPEG base64 data URLs")
        if len(match[2]) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise ValueError("Image exceeds the 48 KiB decoded limit")
        try:
            source = base64.b64decode(match[2], validate=True)
            if len(source) > MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds the 48 KiB decoded limit")
            with Image.open(BytesIO(source)) as image:
                expected = "PNG" if match[1] == "png" else "JPEG"
                if image.format != expected:
                    raise ValueError("Image contents do not match the declared type")
                width, height = image.size
                if (width < 16 or height < 16 or width > MAX_IMAGE_SIDE
                        or height > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS):
                    raise ValueError("Image dimensions must be 16–512 pixels per side")
                prepared.append(ImageOps.exif_transpose(image).convert("RGB"))
        except (binascii.Error, UnidentifiedImageError, OSError) as exc:
            raise ValueError("Invalid image data") from exc
    return {**payload, "images": prepared}
