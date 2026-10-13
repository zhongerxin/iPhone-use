"""A preview child must not consume the MCP server's request pipe."""
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class VideoStdinTest(unittest.TestCase):
    def test_idle_snapshot_is_fresh_and_cannot_publish_a_locked_screen(self):
        import io, threading
        from unittest.mock import Mock
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
        try: from PIL import Image
        except ImportError: self.skipTest('Pillow not installed in this test interpreter')
        from android_video import idle_snapshot
        raw=io.BytesIO();Image.new('RGB',(100,200)).save(raw,format='PNG')
        screen=Mock();screen.client.preview_screenshot.return_value=raw.getvalue()
        screen.client.locked.side_effect=[False,False]
        idle_snapshot(screen,threading.Event())
        screen.client.preview_screenshot.assert_called_once()
        self.assertEqual(screen._publish_frame.call_args.kwargs['viewport'],{'width':100,'height':200})
        screen._publish_frame.reset_mock()
        screen.client.locked.side_effect=[False,True]
        idle_snapshot(screen,threading.Event())
        screen._publish_frame.assert_not_called()
        screen.set_paused.assert_called_once_with(True,reason='device_locked')

    def test_adb_preview_cannot_steal_pending_mcp_input(self):
        server = Path(__file__).resolve().parents[1] / 'server'
        script = textwrap.dedent('''
            import json, socket, subprocess, sys, tempfile, threading, time
            from pathlib import Path
            from unittest.mock import Mock, patch
            from android_video import capture
            from wda_screen import ScreenHub
            original_popen = subprocess.Popen
            with tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / 'child-input'
                def spawn(command, **kwargs):
                    if command[0] == 'fake-adb':
                        program = 'import sys;from pathlib import Path;Path(sys.argv[1]).write_text(sys.stdin.read())'
                        return original_popen([sys.executable, '-c', program, str(marker)], **kwargs)
                    program = 'import time;time.sleep(.5)'
                    return original_popen([sys.executable, '-c', program], **kwargs)
                reader, writer = socket.socketpair()
                writer.send(b'x')
                screen = Mock()
                screen.state_dir = Path(directory)
                screen.client.adb = 'fake-adb'
                screen.client.serial = 'test-device'
                screen.client.locked.return_value = False
                screen.client.command.return_value = '1234'
                deadline = time.monotonic() + 2
                screen._live.side_effect = lambda stop: time.monotonic() < deadline
                screen._terminate = ScreenHub._terminate
                with patch('android_video.dependencies', return_value=('test.jar','1.0','fake-ffmpeg')), \\
                     patch('android_video.socket.create_connection', return_value=reader), \\
                     patch('android_video.subprocess.Popen', side_effect=spawn):
                    capture(screen, threading.Event())
                writer.close()
                print(json.dumps({'child': marker.read_text(), 'remaining': sys.stdin.read()}))
        ''')
        import os
        result = subprocess.run([sys.executable, '-c', script], input='MCP_REQUEST\n', text=True,
                                capture_output=True, timeout=8, env={**os.environ, 'PYTHONPATH': str(server)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'child': '', 'remaining': 'MCP_REQUEST\n'})


if __name__ == '__main__': unittest.main()
