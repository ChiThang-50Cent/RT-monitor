import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tested_cursor_protocol', ROOT / 'surface-gud/surface_protocol.py')
protocol = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = protocol
spec.loader.exec_module(protocol)


def record(header, pixels=b''):
    return json.dumps(header, separators=(',', ':')).encode() + b'\n' + pixels


class CursorProtocolTests(unittest.TestCase):
    def test_motion_and_reentry_retain_shape_and_hotspot(self):
        pixels = bytes([0, 0, 255, 255, 255, 255, 255, 255])
        stream = io.BytesIO(
            record({'visible': True, 'x': 100, 'y': 200, 'shape': [2, 1, 1, 0], 'bytes': 8}, pixels) +
            record({'visible': True, 'x': -1, 'y': 767, 'bytes': 0}) +
            record({'visible': False, 'bytes': 0}) +
            record({'visible': True, 'x': 1366, 'y': 10, 'bytes': 0}))
        reader = protocol.CursorReader(stream)
        entered = reader.read()
        moved = reader.read()
        hidden = reader.read()
        returned = reader.read()
        self.assertEqual((entered.x, entered.y), (100, 200))
        self.assertEqual(entered.shape.pixels, pixels)
        self.assertEqual((entered.shape.hotspot_x, entered.shape.hotspot_y), (1, 0))
        self.assertEqual((moved.x, moved.y), (-1, 767))
        self.assertIs(moved.shape, entered.shape)
        self.assertIsNone(hidden.shape)
        self.assertEqual((returned.x, returned.y), (1366, 10))
        self.assertIs(returned.shape, entered.shape)
        self.assertIsNone(reader.read())

    def test_shape_change_replaces_pixels_and_hotspot(self):
        reader = protocol.CursorReader(io.BytesIO(
            record({'visible': True, 'x': 5, 'y': 6, 'shape': [1, 1, 0, 0], 'bytes': 4}, b'\0\0\0\xff') +
            record({'visible': True, 'x': 7, 'y': 8, 'shape': [1, 2, 0, 1], 'bytes': 8}, b'\xff' * 8)))
        old = reader.read()
        new = reader.read()
        self.assertEqual((new.shape.width, new.shape.height, new.shape.hotspot_y), (1, 2, 1))
        self.assertEqual(new.shape.pixels, b'\xff' * 8)
        self.assertEqual(old.shape.pixels, b'\0\0\0\xff')

    def test_new_connection_requires_shape_before_visible_motion(self):
        reader = protocol.CursorReader(io.BytesIO(record({'visible': True, 'x': 1, 'y': 2, 'bytes': 0})))
        with self.assertRaises(ValueError):
            reader.read()

    def test_rejects_invalid_geometry_before_reading_body(self):
        for shape, length in (([0, 1, 0, 0], 0), ([385, 1, 0, 0], 1540),
                              ([2, 2, 2, 0], 16), ([2, 2, 0, -1], 16),
                              ([2, 2, 0, 0], 12), ([True, 1, 0, 0], 4)):
            with self.subTest(shape=shape, length=length):
                reader = protocol.CursorReader(io.BytesIO(record(
                    {'visible': True, 'x': 1, 'y': 2, 'shape': shape, 'bytes': length})))
                with self.assertRaises(ValueError):
                    reader.read()

    def test_accepts_producer_maximum_shape(self):
        pixels = b'\0' * (384 * 384 * 4)
        reader = protocol.CursorReader(io.BytesIO(record(
            {'visible': True, 'x': 0, 'y': 0, 'shape': [384, 384, 383, 383], 'bytes': len(pixels)}, pixels)))
        state = reader.read()
        self.assertEqual(state.shape.pixels, pixels)
        self.assertEqual(state.shape.hotspot_x, 383)

    def test_rejects_invalid_headers(self):
        for header in ({'visible': 1, 'bytes': 0}, {'visible': False, 'bytes': 4},
                       {'visible': False, 'bytes': 0, 'x': 1},
                       {'visible': True, 'x': True, 'y': 0, 'bytes': 0},
                       {'visible': True, 'x': 32768, 'y': 0, 'bytes': 0},
                       {'visible': True, 'x': 0, 'y': 0, 'bytes': -1}, []):
            with self.subTest(header=header):
                with self.assertRaises(ValueError):
                    protocol.CursorReader(io.BytesIO(record(header))).read()

    def test_truncated_record_is_not_clean_eof(self):
        for data in (b'{"visible":false', record(
                {'visible': True, 'x': 1, 'y': 2, 'shape': [1, 1, 0, 0], 'bytes': 4}, b'\0\0')):
            with self.subTest(data=data):
                with self.assertRaises(EOFError):
                    protocol.CursorReader(io.BytesIO(data)).read()

    def test_rejects_oversized_header(self):
        with self.assertRaises(ValueError):
            protocol.CursorReader(io.BytesIO(b' ' * 1025 + b'\n')).read()

    def test_fragmented_bitmap_does_not_consume_next_record(self):
        class Fragmented(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 3))

        reader = protocol.CursorReader(Fragmented(
            record({'visible': True, 'x': 1, 'y': 2, 'shape': [1, 2, 0, 1], 'bytes': 8}, b'\xff' * 8) +
            record({'visible': False, 'bytes': 0})))
        self.assertEqual(reader.read().shape.pixels, b'\xff' * 8)
        self.assertIsNone(reader.read().shape)

    def test_reconnection_recovers_current_sprite_without_new_shape_event(self):
        shape = protocol.CursorShape(1, 1, 0, 0, b'\0\0\xff\xff')
        initial = protocol.CursorState(10, 20, shape)
        moved = protocol.CursorState(11, 21, shape)
        first = b''.join(protocol.encode_cursor(initial))
        motion = b''.join(protocol.encode_cursor(moved, shape))
        reader = protocol.CursorReader(io.BytesIO(first + motion))
        self.assertEqual(reader.read(), initial)
        self.assertEqual(reader.read(), moved)
        new_reader = protocol.CursorReader(io.BytesIO(b''.join(protocol.encode_cursor(moved))))
        self.assertEqual(new_reader.read(), moved)


if __name__ == '__main__':
    unittest.main()
