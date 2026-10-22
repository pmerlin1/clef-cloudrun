"""Bounded image transport checks without loading the GPU model."""
import base64
import unittest
from io import BytesIO

from media import prepare_images
from PIL import Image


def data_url(color, size=(32, 32)):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


class ImageInputTest(unittest.TestCase):
    def test_one_and_two_images_decode_for_upstream_processor(self):
        red = data_url("red")
        blue = data_url("blue")
        payload = {"state": "classify", "images": [red, blue]}
        prepared = prepare_images(payload)
        self.assertEqual(len(prepared["images"]), 2)
        self.assertEqual(prepared["images"][0].getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(prepared["images"][1].getpixel((0, 0)), (0, 0, 255))
        self.assertEqual(payload["images"], [red, blue])

    def test_urls_files_and_invalid_data_are_rejected(self):
        for value in ("https://example.com/a.png", "file:///etc/passwd", "data:image/png;base64,?",
                      "data:image/png;base64," + base64.b64encode(b"not an image").decode()):
            with self.subTest(value=value[:24]), self.assertRaises(ValueError):
                prepare_images({"images": [value]})

    def test_jpeg_decodes_and_declared_type_must_match_contents(self):
        output = BytesIO()
        Image.new("RGB", (32, 32), "blue").save(output, format="JPEG")
        encoded = base64.b64encode(output.getvalue()).decode()
        prepared = prepare_images({"images": ["data:image/jpeg;base64," + encoded]})
        self.assertEqual(prepared["images"][0].mode, "RGB")
        self.assertEqual(prepared["images"][0].size, (32, 32))
        with self.assertRaisesRegex(ValueError, "declared type"):
            prepare_images({"images": ["data:image/png;base64," + encoded]})

    def test_dimensions_and_image_count_are_bounded(self):
        with self.assertRaises(ValueError):
            prepare_images({"images": [data_url("red", (513, 32))]})
        with self.assertRaises(ValueError):
            prepare_images({"images": [data_url("red")] * 3})
