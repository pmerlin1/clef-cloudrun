"""Exercise HTTP adapter validation with a fake model; never load weights/CUDA."""
import asyncio
import base64
from io import BytesIO
import json
from pathlib import Path
import sys

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import app
from media import prepare_images
import preload


class Request:
    def __init__(self, *chunks):
        self.chunks = chunks

    async def stream(self):
        for chunk in self.chunks:
            yield chunk


async def main():
    # Calling the handler directly intentionally avoids the model lifespan hook.
    def fake_inference(payload):
        prepared = prepare_images(payload)
        return {"accepted": prepared["state"],
                "image_modes": [image.mode for image in prepared.get("images", [])]}

    app.inference = fake_inference
    payload = {"model": app.MODEL_NAME, "state": "test", "questions": {"check": {"type": "noul"}}}
    assert await app.systemone(Request(json.dumps(payload).encode())) == {"accepted": "test", "image_modes": []}
    picture = BytesIO()
    Image.new("RGB", (32, 32), "blue").save(picture, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(picture.getvalue()).decode()
    image_payload = {**payload, "images": [data_url]}
    assert await app.systemone(Request(json.dumps(image_payload).encode())) == {"accepted": "test", "image_modes": ["RGB"]}
    assert prepare_images(image_payload)["images"][0].getpixel((0, 0)) == (0, 0, 255)
    assert preload.worker_budget() == 20
    for request, expected_status in (
        (Request(b"a" * 65536, b"b"), 413),
        (Request(b"{invalid json"), 400),
        (Request(b'{"model":"clef","questions":{}}'), 400),
    ):
        try:
            await app.systemone(request)
        except app.HTTPException as exc:
            assert exc.status_code == expected_status, (exc.status_code, expected_status)
        else:
            raise AssertionError("Invalid input unexpectedly accepted")
    for invalid_image in ("https://example.com/untrusted.png", "file:///etc/passwd"):
        try:
            await app.systemone(Request(json.dumps({**payload, "images": [invalid_image]}).encode()))
        except app.HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("External image reference unexpectedly accepted")
    assert not any(getattr(route, "path", "").startswith("/admin/") for route in app.app.routes)
    print("Adapter checks passed: text, inline PNG, blocked external image references, bounded body, invalid schema; no admin route.")


asyncio.run(main())
