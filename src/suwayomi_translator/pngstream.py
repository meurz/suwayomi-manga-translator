"""PNG ancillary chunks keep downstream idle-read timers alive during inference."""

import io
import struct
import zlib

from PIL import Image

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def heartbeat() -> bytes:
    # A legal ancillary PNG chunk, ignored by image decoders (not image pixel data).
    return chunk(b"tEXt", b"Comment\0Translation in progress. " + b" " * 16384)


def prefix(original: bytes) -> tuple[bytes, tuple[int, int]]:
    with Image.open(io.BytesIO(original)) as image:
        size = image.size
    ihdr = struct.pack(">IIBBBBB", *size, 8, 6, 0, 0, 0)
    return SIGNATURE + chunk(b"IHDR", ihdr) + heartbeat(), size


def finish(data: bytes, size: tuple[int, int]) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        if image.size != size:
            raise ValueError("Worker changed image dimensions")
        out = io.BytesIO()
        image.convert("RGBA").save(out, "PNG")
    encoded = out.getvalue()
    # IHDR was sent once in prefix(). Encoding is always RGBA8, non-interlaced.
    assert encoded[:8] == SIGNATURE and encoded[12:16] == b"IHDR"
    return encoded[33:]
