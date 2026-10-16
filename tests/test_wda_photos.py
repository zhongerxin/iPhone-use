import struct
import sys
import tempfile
from unittest import mock
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from wda_client import WDAError
from wda_photos import PhotoImportManager


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response or {'value': {'status': 'succeeded', 'assetId': 'ASSET'}}
        self.error = error
        self.calls = []

    def request(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, payload, timeout))
        if self.error:
            raise self.error
        return self.response


class PhotoImportManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='iphone-use-photo-state ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.photo = self.root / 'photo.png'
        self.photo.write_bytes(self.png_bytes(0))
        self.manager = PhotoImportManager(self.root, FakeClient())

    @staticmethod
    def png_bytes(fill):
        header = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR'
        return header + struct.pack('>IIBBBBB', 1, 2, 8, 2, 0, 0, 0) + bytes([fill]) * 4

    def test_request_ids_are_safe(self):
        for value in ('short', '../escape', 'id/with/slash', 'id\\with\\slash', '.'):
            with self.assertRaises(WDAError):
                self.manager.path_for(value)
        self.assertEqual(self.manager.path_for('import.001:test_id').name, 'import.001:test_id.json')

    def test_oversize_is_rejected_before_reading_photo_bytes(self):
        from wda_photos import MAX_PHOTO_BYTES, validate_photo
        oversized = self.root / 'oversize.png'
        oversized.write_bytes(self.png_bytes(0) + b'0' * (MAX_PHOTO_BYTES - 31 + 1))
        with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('must not read')) as read:
            with self.assertRaisesRegex(WDAError, '20 MiB'):
                validate_photo(str(oversized))
        read.assert_not_called()

    def test_same_request_and_photo_returns_existing_result_without_upload(self):
        first = self.manager.upload(str(self.photo), 'request-0001')
        self.assertEqual(first['status'], 'succeeded')
        second = self.manager.upload(str(self.photo), 'request-0001')
        self.assertEqual(second['status'], 'succeeded')
        self.assertEqual(len(self.manager.client.calls), 1)

    def test_same_request_and_different_photo_conflicts_before_upload(self):
        self.manager.upload(str(self.photo), 'request-0001')
        other = self.root / 'other.png'
        other.write_bytes(self.png_bytes(1))
        with self.assertRaisesRegex(WDAError, 'different photo'):
            self.manager.upload(str(other), 'request-0001')
        self.assertEqual(len(self.manager.client.calls), 1)

    def test_timeout_after_transport_is_uncertain_and_retry_only_queries(self):
        client = FakeClient(error=WDAError('action_uncertain', 'lost response', uncertain=True))
        manager = PhotoImportManager(self.root, client)
        result = manager.upload(str(self.photo), 'request-0001')
        self.assertEqual(result['status'], 'uncertain')
        client.error = None
        client.response = {'value': {'status': 'succeeded', 'assetId': 'ASSET'}}
        result = manager.upload(str(self.photo), 'request-0001')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([(method, path) for method, path, _, _ in client.calls], [('POST', '/wda/photos/import'), ('GET', '/wda/photos/import/request-0001')])

    def test_remote_missing_after_transport_is_uncertain(self):
        client = FakeClient()
        manager = PhotoImportManager(self.root, client)
        manager.upload(str(self.photo), 'request-0001')
        client.error = WDAError('photo_import_not_found', 'missing')
        result = manager.status('request-0001')
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['error']['code'], 'remote_record_missing')


class ResultContentTests(unittest.TestCase):
    def test_successful_photo_result_with_null_error_is_not_an_mcp_error(self):
        sys.path.insert(0, str(ROOT / 'server'))
        from iphone_use import result_content
        result = result_content({'request_id': 'request-0001', 'status': 'succeeded', 'error': None})
        self.assertFalse(result['isError'])


if __name__ == '__main__':
    unittest.main()
