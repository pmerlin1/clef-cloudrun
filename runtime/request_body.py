"""Bound request buffering even when Content-Length is missing or misleading."""


class RequestTooLarge(ValueError):
    pass


async def read_bounded_body(stream, limit=65536):
    body = bytearray()
    async for chunk in stream:
        if len(body) + len(chunk) > limit:
            raise RequestTooLarge("Maximum request size is 64 KiB")
        body.extend(chunk)
    return bytes(body)
