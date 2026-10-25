"""Native 30 FPS H.264 access units over the authenticated SSH channel."""
from dataclasses import dataclass
import json
import struct

WIDTH, HEIGHT = 1366, 768
FPS = 30
FRAME = struct.Struct("!QI")
MAX_AU = 4 * 1024 * 1024


def read_frame(read_exact, expected_sequence):
    """Return an ordered access unit, or None for EOF between complete frames."""
    header = read_exact(FRAME.size, clean_eof=True)
    if header is None:
        return None
    sequence, length = FRAME.unpack(header)
    if sequence != expected_sequence or not 0 < length <= MAX_AU:
        raise ValueError("invalid H.264 access-unit sequence or length")
    return sequence, read_exact(length)


MAX_CURSOR_DIM = 384
MAX_CURSOR_HEADER = 1024


@dataclass(frozen=True)
class CursorShape:
    width: int
    height: int
    hotspot_x: int
    hotspot_y: int
    pixels: bytes


@dataclass(frozen=True)
class CursorState:
    x: int = 0
    y: int = 0
    shape: CursorShape | None = None


class CursorReader:
    def __init__(self, stream):
        self.stream = stream
        self.shape = None

    def read(self):
        line = self.stream.readline(MAX_CURSOR_HEADER + 1)
        if not line:
            return None
        if len(line) > MAX_CURSOR_HEADER:
            raise ValueError("oversized cursor header")
        if not line.endswith(b"\n"):
            raise EOFError("truncated cursor header")
        header = json.loads(line)
        if not isinstance(header, dict) or type(header.get("visible")) is not bool:
            raise ValueError("invalid cursor visibility")
        length = header.get("bytes")
        if type(length) is not int or not 0 <= length <= MAX_CURSOR_DIM ** 2 * 4:
            raise ValueError("invalid cursor bitmap length")
        if not header["visible"]:
            if set(header) != {"visible", "bytes"} or length:
                raise ValueError("invalid hidden cursor")
            return CursorState()
        fields = {"visible", "x", "y", "bytes"}
        if "shape" in header:
            fields.add("shape")
        if set(header) != fields:
            raise ValueError("invalid cursor fields")
        x, y = header.get("x"), header.get("y")
        if any(type(value) is not int or not -32768 <= value <= 32767 for value in (x, y)):
            raise ValueError("invalid cursor position")
        if "shape" in header:
            shape = header["shape"]
            if not isinstance(shape, list) or len(shape) != 4 or any(type(v) is not int for v in shape):
                raise ValueError("invalid cursor shape")
            width, height, hotspot_x, hotspot_y = shape
            if (not 1 <= width <= MAX_CURSOR_DIM or not 1 <= height <= MAX_CURSOR_DIM or
                    not 0 <= hotspot_x < width or not 0 <= hotspot_y < height or
                    length != width * height * 4):
                raise ValueError("invalid cursor geometry or payload length")
            pixels = self.stream.read(length)
            if len(pixels) != length:
                parts = bytearray(pixels)
                while len(parts) < length:
                    chunk = self.stream.read(length - len(parts))
                    if not chunk:
                        raise EOFError("truncated cursor bitmap")
                    parts.extend(chunk)
                pixels = bytes(parts)
            self.shape = CursorShape(width, height, hotspot_x, hotspot_y, pixels)
        elif length or self.shape is None:
            raise ValueError("cursor motion has no known shape")
        return CursorState(x, y, self.shape)


def encode_cursor(state, previous_shape=None):
    pixels = b""
    header = {"visible": state.shape is not None, "bytes": 0}
    if state.shape is not None:
        header.update(x=state.x, y=state.y)
        if state.shape is not previous_shape:
            shape = state.shape
            pixels = shape.pixels
            header.update(shape=[shape.width, shape.height, shape.hotspot_x, shape.hotspot_y],
                          bytes=len(pixels))
    return json.dumps(header, separators=(",", ":")).encode() + b"\n", pixels
