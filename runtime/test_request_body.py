import unittest

from request_body import RequestTooLarge, read_bounded_body


class RequestBodyTest(unittest.IsolatedAsyncioTestCase):
    async def test_collects_a_body_at_the_limit(self):
        async def stream():
            yield b"a" * 32768
            yield b"b" * 32768
        self.assertEqual(len(await read_bounded_body(stream())), 65536)

    async def test_stops_reading_when_chunked_body_exceeds_limit(self):
        async def stream():
            yield b"a" * 65536
            yield b"b"
            self.fail("Must stop consuming the request after the limit")
        with self.assertRaises(RequestTooLarge):
            await read_bounded_body(stream())
