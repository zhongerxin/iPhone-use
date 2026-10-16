import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from wda_visual import _normalize_ocr_items, ocr_available, visual_find


class TestVisualOCR(unittest.TestCase):
    def test_ocr_available_on_darwin_with_binary(self):
        # We can't control the host, but test the function contract.
        result = ocr_available()
        self.assertIsInstance(result, bool)

    def test_normalize_strips_base64_and_json_array(self):
        import base64
        raw_text = base64.b64encode('["发送"]'.encode()).decode()
        document = {'items': [
            {'text': raw_text, 'confidence': 1.0, 'x': 100.0, 'y': 200.0, 'width': 50.0, 'height': 30.0},
            {'text': 'plain', 'confidence': 0.8, 'x': 0.0, 'y': 0.0, 'width': 0.0, 'height': 0.0},
        ]}
        items = _normalize_ocr_items(document)
        self.assertEqual(items[0]['text'], '发送')
        self.assertEqual(items[1]['text'], 'plain')

    def test_normalize_handles_garbage_base64(self):
        document = {'items': [{'text': 'not-base64!', 'confidence': 0.5, 'x': 0, 'y': 0, 'width': 0, 'height': 0}]}
        items = _normalize_ocr_items(document)
        self.assertEqual(items[0]['text'], 'not-base64!')


class TestVisualFind(unittest.TestCase):
    def test_find_returns_logical_coordinates(self):
        ocr_output = '{"imageWidth":1170,"imageHeight":2532,"items":[' \
                     '{"text":"W+WPkemAgV0=","confidence":1.0,"x":1001.0,"y":2326.0,"width":85.0,"height":44.0}' \
                     ']}'
        with mock.patch('wda_visual.subprocess.run') as mock_run, \
             mock.patch('wda_visual.ocr_available', return_value=True), \
             mock.patch.object(Path, 'is_file', return_value=True):
            mock_run.return_value = mock.MagicMock(returncode=0, stdout=ocr_output)
            result = visual_find('/tmp/fake.png', '发送')
        self.assertTrue(result['found'])
        self.assertAlmostEqual(result['x'], 347.8, delta=0.1)
        self.assertAlmostEqual(result['y'], 782.7, delta=0.1)

    def test_find_returns_not_found_when_no_match(self):
        ocr_output = '{"imageWidth":1170,"imageHeight":2532,"items":[' \
                     '{"text":"W+WPkemAgV0=","confidence":1.0,"x":100.0,"y":100.0,"width":85.0,"height":44.0}' \
                     ']}'
        with mock.patch('wda_visual.subprocess.run') as mock_run, \
             mock.patch('wda_visual.ocr_available', return_value=True), \
             mock.patch.object(Path, 'is_file', return_value=True):
            mock_run.return_value = mock.MagicMock(returncode=0, stdout=ocr_output)
            result = visual_find('/tmp/fake.png', '不存在的文字')
        self.assertFalse(result['found'])

    def test_find_multiple_candidates(self):
        ocr_output = '{"imageWidth":1170,"imageHeight":2532,"items":[' \
                     '{"text":"WyJORVctMiJd","confidence":1.0,"x":100.0,"y":300.0,"width":100.0,"height":30.0},' \
                     '{"text":"WyJORVctMyJd","confidence":1.0,"x":400.0,"y":300.0,"width":100.0,"height":30.0}' \
                     ']}'
        with mock.patch('wda_visual.subprocess.run') as mock_run, \
             mock.patch('wda_visual.ocr_available', return_value=True), \
             mock.patch.object(Path, 'is_file', return_value=True):
            mock_run.return_value = mock.MagicMock(returncode=0, stdout=ocr_output)
            result = visual_find('/tmp/fake.png', 'NEW')
        self.assertTrue(result['found'])
        self.assertEqual(result['match_count'], 2)
        self.assertEqual(len(result['candidates']), 2)


if __name__ == '__main__':
    unittest.main()
