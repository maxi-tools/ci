#!/usr/bin/env python3
"""Pixel-fidelity tests for the summary PNG downscaler (stdlib only)."""
import importlib.util
import pathlib
import struct
import subprocess
import shutil
import unittest
import zlib

SOURCE = pathlib.Path(__file__).resolve().parents[1] / '.github/actions/moments-gallery/moments_gallery.py'
spec = importlib.util.spec_from_file_location('moments_gallery', SOURCE)
assert spec is not None and spec.loader is not None
gallery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gallery)


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


def fixture(channels, filter_type, interlace=0):
    width, height = 480, 4
    rows = [bytes(((x * 13 + y * 37 + c * 71) % 256 for x in range(width) for c in range(channels))) for y in range(height)]
    previous = bytes(width * channels)
    scanlines = bytearray()
    for row in rows:
        scanlines.append(filter_type)
        for i, value in enumerate(row):
            a = row[i - channels] if i >= channels else 0
            b = previous[i]
            c = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                predictor = a
            elif filter_type == 2:
                predictor = b
            elif filter_type == 3:
                predictor = (a + b) // 2
            elif filter_type == 4:
                p = a + b - c
                predictor = (a, b, c)[(abs(p-a), abs(p-b), abs(p-c)).index(min(abs(p-a), abs(p-b), abs(p-c)))]
            else:
                predictor = 0
            scanlines.append((value - predictor) & 255)
        previous = row
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2 if channels == 3 else 6, 0, 0, interlace)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(scanlines)) + chunk(b'IEND', b''), rows


def pixels(png):
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    width, height, depth, color, _, _, interlace = struct.unpack('>IIBBBBB', png[16:29])
    assert depth == 8 and color == 2 and interlace == 0
    pos, compressed = 8, bytearray()
    while pos < len(png):
        size = struct.unpack('>I', png[pos:pos+4])[0]
        if png[pos+4:pos+8] == b'IDAT':
            compressed.extend(png[pos+8:pos+8+size])
        pos += 12 + size
    raw = zlib.decompress(compressed)
    assert len(raw) == height * (1 + width * 3)
    assert all(raw[y*(1+width*3)] == 0 for y in range(height))
    return width, height, [raw[y*(1+width*3)+1:(y+1)*(1+width*3)] for y in range(height)]


class PngFilterTest(unittest.TestCase):
    def test_all_png_filters_rgb_and_rgba(self):
        for channels in (3, 4):
            for filter_type in range(5):
                with self.subTest(channels=channels, filter=filter_type):
                    source, rows = fixture(channels, filter_type)
                    width, height, got = pixels(gallery._png_thumbnail(source, 240))
                    self.assertEqual((width, height), (240, 2))
                    for y in range(height):
                        sy = y * 4 // height
                        for x in range(width):
                            sx = x * 480 // width
                            original = rows[sy][sx*channels:(sx+1)*channels]
                            expected = original[:3] if channels == 3 else bytes(round(v*original[3]/255) for v in original[:3])
                            self.assertEqual(got[y][x*3:x*3+3], expected, (channels, filter_type, x, y))

    def test_interlace_rejected_explicitly(self):
        source, _ = fixture(3, 1, interlace=1)
        with self.assertRaisesRegex(gallery.GalleryError, 'non-interlaced'):
            gallery._png_thumbnail(source, 240)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not available')
    def test_real_ffmpeg_encoder_fixture(self):
        source, _ = fixture(3, 0)
        # ffmpeg decodes the input and independently encodes a new PNG.
        result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'image2pipe', '-c:v', 'png', '-i', '-', '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', '-'], input=source, capture_output=True, check=True)
        encoded = result.stdout
        self.assertNotEqual(source, encoded)
        width, height, got = pixels(gallery._png_thumbnail(encoded, 240))
        self.assertEqual((width, height), (240, 2))
        _, expected_rows = fixture(3, 0)
        for y in range(height):
            for x in range(width):
                self.assertEqual(got[y][x*3:x*3+3], expected_rows[y*2][x*6:x*6+3])


if __name__ == '__main__':
    unittest.main()
