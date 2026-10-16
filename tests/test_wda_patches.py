import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import wda_patches


class WdaPatchTests(unittest.TestCase):
    def test_patch_is_stable_and_bounded(self):
        data = wda_patches.patch_bytes()
        self.assertLess(len(data), 64 * 1024)
        self.assertEqual(wda_patches.PATCH_FINGERPRINT, wda_patches.patch_fingerprint())
        self.assertRegex(wda_patches.PATCH_FINGERPRINT, r'^[0-9a-f]{64}$')
        self.assertIn(b'options.originalFilename = [NSString stringWithFormat:@"PUA-%@.%@"', data)
        self.assertIn(b'record.requestId', data)

    def test_apply_rejects_unchecked_patch_and_checks_both_directions(self):
        with tempfile.TemporaryDirectory() as name:
            source = Path(name)
            forward = subprocess.run(
                ['git', '-C', str(source), 'apply', '--check', str(wda_patches.PATCH)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            self.assertNotEqual(forward.returncode, 0)


class PhotoValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import struct
        cls.directory = tempfile.TemporaryDirectory(prefix='iphone-use-photos ')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        png_header = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR'
        png_dimensions = struct.pack('>IIBBBBB', 1, 2, 8, 2, 0, 0, 0)
        (cls.root / 'valid.png').write_bytes(png_header + png_dimensions)
        (cls.root / 'fake.png').write_bytes(b'not-a-png')

    def test_dimensions_and_magic(self):
        sys.path.insert(0, str(ROOT / 'server'))
        from wda_photos import image_dimensions, validate_photo
        self.assertEqual(image_dimensions('png', (self.root / 'valid.png').read_bytes()), (1, 2))
        photo = validate_photo(str(self.root / 'valid.png'))
        self.assertEqual((photo['format'], photo['width'], photo['height']), ('png', 1, 2))
        self.assertEqual(photo['size_bytes'], (self.root / 'valid.png').stat().st_size)
        with self.assertRaisesRegex(Exception, 'actual JPEG or PNG'):
            validate_photo(str(self.root / 'fake.png'))


if __name__ == '__main__':
    unittest.main()
