"""Bounded raster validation. Pixels and ancillary strings are data, not code.

Pillow.open is lazy: verify the container, reopen it and decode pixels before
recording an analyzed payload. Never execute source or follow embedded URLs.
"""
from __future__ import annotations

from io import BytesIO
import struct
import warnings
import zlib

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
MAX_IMAGE_DIMENSION = 8192
MAX_ANCILLARY_BYTES = 1024 * 1024
IMAGE_MIME_TYPES = {"png": frozenset({"image/png"}), "jpeg": frozenset({"image/jpeg"})}
IMAGE_EXTENSIONS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}


def _png_container(data: bytes) -> None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Not PNG")
    offset, chunks, ancillary = 8, 0, 0
    while offset < len(data):
        chunks += 1
        if chunks > 2048 or offset + 12 > len(data):
            raise ValueError("PNG chunk bounds")
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("PNG truncated chunk")
        chunk = data[offset + 8:end - 4]
        if zlib.crc32(kind + chunk) != struct.unpack_from(">I", data, end - 4)[0]:
            raise ValueError("PNG checksum")
        if kind == b"acTL":
            raise ValueError("Animated PNG requires frame analysis")
        if kind[0] & 32:
            expanded, packed = chunk, None
            if kind in {b"zTXt", b"iCCP"}:
                rest = chunk.split(b"\0", 1)[1]
                if not rest or rest[0] != 0:
                    raise ValueError("PNG compression method")
                packed = rest[1:]
            elif kind == b"iTXt":
                rest = chunk.split(b"\0", 1)[1]
                if len(rest) < 2 or rest[0] not in {0, 1} or rest[1] != 0:
                    raise ValueError("PNG text compression")
                packed = rest[2:].split(b"\0", 2)[2] if rest[0] else None
            else:
                packed = None
            if packed is not None:
                decoder = zlib.decompressobj()
                expanded = decoder.decompress(packed, MAX_ANCILLARY_BYTES + 1)
                if not decoder.eof or decoder.unused_data or len(expanded) > MAX_ANCILLARY_BYTES:
                    raise ValueError("PNG ancillary expansion limit")
            ancillary += max(len(chunk), len(expanded))
            if ancillary > MAX_ANCILLARY_BYTES:
                raise ValueError("PNG ancillary aggregate limit")
        elif kind not in {b"IHDR", b"PLTE", b"IDAT", b"IEND"}:
            raise ValueError("Unknown critical PNG chunk")
        offset = end
        if kind == b"IEND":
            if length or offset != len(data):
                raise ValueError("PNG trailing data")
            return
    raise ValueError("PNG missing IEND")


def parse_image_payload(facts) -> None:
    facts.level = "partial"
    facts.issue("asset_payload_descriptor_unverified")
    data = facts.source.binary_content
    if not data:
        facts.issue("binary_content_not_parsed", metadata_type=facts.source.metadata_type)
        return
    if len(data) > MAX_IMAGE_BYTES:
        facts.issue("asset_payload_size_limit", max_bytes=MAX_IMAGE_BYTES)
        return
    if facts.source.content_sha and facts.source.content_sha != facts.source_sha:
        facts.issue("asset_payload_hash_mismatch")
        return
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        expected = "PNG"
    elif data.startswith(b"\xff\xd8"):
        expected = "JPEG"
    else:
        facts.issue("asset_payload_format_unsupported")
        return
    try:
        from PIL import Image, ImageFile
    except ImportError:
        facts.issue("asset_image_validator_unavailable")
        return
    try:
        if ImageFile.LOAD_TRUNCATED_IMAGES:
            raise ValueError("Permissive global decoder settings")
        if expected == "PNG":
            _png_container(data)
        elif not data.endswith(b"\xff\xd9"):
            raise ValueError("JPEG missing terminal EOI or trailing data")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=[expected]) as image:
                width, height = image.size
                if (image.format != expected or getattr(image, "n_frames", 1) != 1
                        or not 0 < width <= MAX_IMAGE_DIMENSION or not 0 < height <= MAX_IMAGE_DIMENSION
                        or width * height > MAX_IMAGE_PIXELS):
                    raise ValueError("Raster limits or unsupported image subtype")
                image.verify()
            with Image.open(BytesIO(data), formats=[expected]) as image:
                image.load()
                if image.size != (width, height):
                    raise ValueError("Inconsistent image dimensions")
        facts.asset_payload = {"format": expected.lower(), "width": width, "height": height,
                               "bytes": len(data), "frames": 1,
                               "source_file": facts.source.path, "source_sha": facts.source_sha}
    except (OSError, ValueError, SyntaxError, IndexError, KeyError, zlib.error,
            Image.DecompressionBombWarning, Image.DecompressionBombError):
        facts.issue("asset_image_invalid_or_unsupported")
