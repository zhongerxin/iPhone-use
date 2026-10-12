import base64
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import wda_image
from wda_controller import PhoneController


class CaptureRetentionTests(unittest.TestCase):
    def test_legacy_midscene_files_do_not_delete_new_pua_capture(self):
        with tempfile.TemporaryDirectory() as root:
            artifacts = Path(root) / 'artifacts'
            artifacts.mkdir()
            for i in range(100):
                path = artifacts / f'midscene-{i}.png'
                path.write_bytes(b'old')
                os.utime(path, (1, 1))
            data = wda_image.PNG_SIGNATURE + b'fixture'
            client = Mock()
            client.request.return_value = {'value': base64.b64encode(data).decode()}
            # Use the real PUA capture path, bypassing only image conversion.
            phone = PhoneController(client, root)
            with patch.object(wda_image, 'model_image', side_effect=lambda p: (p, 'image/png', 402, 874)):
                image = phone.capture({'width': 402, 'height': 874})
            self.assertTrue(Path(image['path']).is_file())
            self.assertEqual(len(list(artifacts.glob('*.png'))), 100)

    def test_midscene_only_retention_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            for i in range(105):
                last = wda_image.save_capture(root, wda_image.PNG_SIGNATURE)
                wda_image.prune_captures(root)
            self.assertTrue(last.is_file())
            self.assertEqual(len(list((Path(root) / 'artifacts').glob('*.png'))), 100)
            self.assertEqual(last.stat().st_mode & 0o777, 0o600)


class CalendarAuditTests(unittest.TestCase):
    def test_duplicate_saved_events_fail_even_when_detail_is_correct(self):
        spec = importlib.util.spec_from_file_location('calendar_fixture', ROOT / 'scripts/calendar-benchmark-fixture.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        import xml.etree.ElementTree as ET
        detail = ET.fromstring(f'<root><Button visible="true" name="编辑"/><Button visible="true" name="BackButton"/><Text visible="true" label="{fixture.TITLE}"/><Text visible="true" label="{fixture.TITLE}, 会议室 A, 2026年10月10日 15:00 15:30"/></root>')
        for count in (0, 1, 2):
            day = ET.fromstring('<root><Text visible="true" name="current-day" label="2026年10月10日"/>' + ''.join(f'<Button name="event-shown:{fixture.TITLE}"/>' for _ in range(count)) + '</root>')
            with patch.object(fixture, 'source', side_effect=[detail, day]), patch.object(fixture, 'tap'):
                result = fixture.audit(['15:00', '15:30'])
            self.assertEqual(result['passed'], count == 1)
            self.assertEqual(result['matching_event_count'], count)
