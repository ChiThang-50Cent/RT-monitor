"""Native 30 FPS H.264 access units over the authenticated SSH channel."""
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
