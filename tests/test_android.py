"""Failure semantics and Android-native contracts, without touching a phone."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from android_client import AndroidClient,AndroidError
from android_controller import AndroidController,parse_nodes
from android_use import Runtime,SCHEMAS,semantics,validate,tool_result

XML='''<hierarchy><node text="蓝牙" resource-id="a:id/title" class="android.widget.TextView" package="a" bounds="[10,20][100,90]" enabled="true"/><node text="secret" password="true" class="android.widget.EditText" bounds="[0,0][10,10]" enabled="true"/></hierarchy>'''

class AndroidTests(unittest.TestCase):
    def test_device_switch_waits_for_old_preview_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            r=Runtime(directory);r.client.serial='old'
            r.client.close=Mock()
            try:
                with patch.object(r.screen,'shutdown',return_value=False):
                    with self.assertRaises(AndroidError) as caught:r.select_device('new')
                    self.assertEqual(caught.exception.code,'preview_stopping')
                    self.assertEqual(r.client.serial,'old');r.client.close.assert_not_called()
                with patch.object(r.screen,'shutdown',return_value=True):
                    r.select_device('new')
                    self.assertEqual(r.client.serial,'new');r.client.close.assert_called_once()
            finally:r.close()

    def test_service_worker_waits_for_previous_owner(self):
        # A real process owns the lock: the replacement must not even enter
        # run_worker until the previous owner has completed its cleanup.
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory)/'service.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                script = """import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import android_use
root=Path(sys.argv[2])
android_use.run_worker=lambda *args:(root/'entered').touch()
(root/'waiting').touch()
android_use.worker(root,'replacement')
"""
                child=subprocess.Popen([sys.executable,'-c',script,str(Path(__file__).resolve().parents[1]/'server'),directory],stdin=subprocess.DEVNULL)
                try:
                    import time
                    deadline=time.monotonic()+5
                    while not (Path(directory)/'waiting').exists() and time.monotonic()<deadline:time.sleep(.01)
                    self.assertTrue((Path(directory)/'waiting').exists())
                    with self.assertRaises(subprocess.TimeoutExpired):child.wait(timeout=.15)
                    self.assertFalse((Path(directory)/'entered').exists())
                    fcntl.flock(lock,fcntl.LOCK_UN)
                    self.assertEqual(child.wait(timeout=5),0)
                    self.assertTrue((Path(directory)/'entered').exists())
                finally:
                    if child.poll() is None:child.kill();child.wait()

    def test_ready_recovers_read_channel_without_replaying_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            r=Runtime(directory);r.client.locked=Mock(return_value=False)
            r.phone.observe=Mock(side_effect=AndroidError('automation_unreachable','offline'))
            r.setup=Mock(return_value={'job':{'state':'running','id':'one'}})
            try:
                self.assertEqual(r.ready(screenshot=False)['state'],'recovering')
                r.ready(screenshot=False)
                r.setup.assert_called_once_with('start')
                self.assertEqual(r.client.mutations,0)
            finally:r.close()

    def test_ready_recover_false_and_authentication_never_start_service(self):
        with tempfile.TemporaryDirectory() as directory:
            r=Runtime(directory);r.client.locked=Mock(return_value=False);r.setup=Mock()
            r.phone.observe=Mock(side_effect=AndroidError('automation_unreachable','offline'))
            try:
                self.assertEqual(r.ready(recover=False)['state'],'recovery_required')
                r.screen.set_paused(True)
                self.assertEqual(r.ready()['state'],'authentication_paused')
                r.setup.assert_not_called()
            finally:r.close()

    def test_widget_session_reuses_live_runtime_but_not_restarted_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            a=Runtime(directory);b=Runtime(directory)
            try:
                first=tool_result(a,{'name':'pua_screen','arguments':{}})['_meta']['openai/widgetSessionId']
                self.assertEqual(first,tool_result(a,{'name':'pua_screen','arguments':{}})['_meta']['openai/widgetSessionId'])
                self.assertNotEqual(first,tool_result(b,{'name':'pua_screen','arguments':{}})['_meta']['openai/widgetSessionId'])
            finally:a.close();b.close()

    def test_start_rebinds_usb_before_replacing_healthy_service_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            r=Runtime(directory);r.client=Mock();r.client.serial='device'
            r.client.rpc.return_value={}
            try:
                with patch('android_use.subprocess.Popen') as spawn:
                    result=r.setup('start')
                    self.assertTrue(result['reused']);spawn.assert_not_called()
                self.assertEqual([call[0] for call in r.client.method_calls][:3],['close','attach','rpc'])
            finally:r.close()

    def test_refresh_reports_success_when_transport_rebind_recovers_service(self):
        with tempfile.TemporaryDirectory() as directory:
            r=Runtime(directory);r.client.locked=Mock(return_value=False)
            r.client.rpc=Mock(side_effect=[AndroidError('automation_unreachable','offline'),{}])
            r.recover_service=Mock(return_value={'ready':False,'state':'recheck_required'})
            try:
                result=r.call('pua_screen_action',{'action':'refresh'})
                self.assertTrue(result['service_ready']);self.assertFalse(result['service_recovering'])
                self.assertEqual(r.client.mutations,0)
            finally:r.close()

    def test_secure_tree_redaction(self):
        nodes=parse_nodes(XML)
        self.assertEqual(nodes[0]['text'],'蓝牙');self.assertEqual(nodes[0]['rect'],[10,20,90,70])
        self.assertEqual(nodes[1]['text'],'[secure]')

    def test_adb_timeout_marks_mutation_uncertain(self):
        client=AndroidClient('serial',adb='/fake/adb')
        with patch('android_client.subprocess.run',side_effect=subprocess.TimeoutExpired('adb',1)) as run:
            with self.assertRaises(AndroidError) as caught:client.command(['shell','input','tap','1','2'],mutation=True)
        self.assertTrue(caught.exception.uncertain);self.assertEqual(run.call_count,1)

    def test_rpc_disconnect_never_replays(self):
        client=AndroidClient('serial',adb='/fake/adb');client.port=1234
        connection=Mock();connection.getresponse.side_effect=ConnectionResetError()
        with patch('android_client.http.client.HTTPConnection',return_value=connection):
            with self.assertRaises(AndroidError) as caught:client.rpc('click',[1,2],mutation=True)
        self.assertTrue(caught.exception.uncertain);self.assertEqual(connection.request.call_count,1)

    def test_samsung_stale_derived_keyguard_flag(self):
        c=AndroidClient();c.command=Mock(return_value='isStatusBarKeyguard=false\n showingAndNotOccluded=true\n mIsShowing=false')
        self.assertFalse(c.locked())
        c.command.return_value='isStatusBarKeyguard=true\n mIsShowing=true';self.assertTrue(c.locked())
        c.command.return_value='unknown';self.assertRaises(AndroidError,c.locked)

    def test_ambiguous_tap_sends_no_action(self):
        c=Mock();c.rpc.return_value={'currentPackageName':'a','displayRotation':0,'displayWidth':100,'displayHeight':200}
        p=AndroidController(c,'.',Mock());p.tree=Mock(return_value=parse_nodes(XML)+parse_nodes(XML))
        with self.assertRaises(AndroidError) as caught:p.tap(selector={'text':'蓝牙'})
        self.assertEqual(caught.exception.code,'ambiguous_target')
        self.assertFalse(any(call.args[0]=='click' for call in c.rpc.call_args_list))

    def test_rotation_invalidates_coordinates(self):
        p=AndroidController(Mock(),'.',Mock());p.context=Mock(return_value={'rotation':1})
        import time
        p.snapshots['x']=({'rotation':0},time.monotonic())
        self.assertRaises(AndroidError,p.check_context,'x')

    def test_all_batch_arguments_validate_before_action(self):
        args={'steps':[{'op':'press_button','args':{'name':'home'}},{'op':'tap','args':{'x':2}}]}
        validate(args,SCHEMAS['batch'])
        self.assertRaises(AndroidError,semantics,'batch',args)

    def test_batch_stops_on_uncertain_action(self):
        with tempfile.TemporaryDirectory() as directory:
            r=Runtime(directory);r.guard=Mock();r.phone.tap=Mock(side_effect=AndroidError('timeout','unknown',uncertain=True));r.phone.press_button=Mock()
            result=r.batch([{'op':'tap','args':{'x':1,'y':1}},{'op':'press_button','args':{'name':'home'}}])
            self.assertEqual(result['stopped_at'],0);self.assertTrue(result['error']['uncertain']);r.phone.press_button.assert_not_called();r.close()

    def test_newline_requires_intent(self):
        self.assertRaises(AndroidError,semantics,'type_text',{'text':'a\nb'})
        semantics('type_text',{'text':'a\nb','allow_newlines':True})

    def test_preview_pause_blocks_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            r=Runtime(directory);r.screen.set_paused(True);r.client.locked=Mock(return_value=False);r.phone.tap=Mock()
            result=tool_result(r,{'name':'pua_tap','arguments':{'x':1,'y':1}})
            self.assertTrue(result['isError']);r.phone.tap.assert_not_called();r.close()


class AndroidTransportContract(unittest.TestCase):
    def test_chinese_request_uses_json_unicode_escapes(self):
        c=AndroidClient('serial',adb='/fake/adb');c.port=1234
        connection=Mock();response=connection.getresponse.return_value
        response.status=200;response.read.return_value=b'{"result":true}'
        with patch('android_client.http.client.HTTPConnection',return_value=connection):
            c.rpc('setText',[{},'蓝牙🙂'],mutation=True)
        payload=connection.request.call_args.args[2]
        self.assertTrue(payload.isascii());self.assertEqual(json.loads(payload)['params'][1],'蓝牙🙂')
        self.assertEqual(c.mutations,1)

    def test_backend_false_is_not_action_success(self):
        c=AndroidClient('serial',adb='/fake/adb');c.port=1234
        connection=Mock();response=connection.getresponse.return_value
        response.status=200;response.read.return_value=b'{"result":false}'
        with patch('android_client.http.client.HTTPConnection',return_value=connection):
            with self.assertRaises(AndroidError):c.rpc('click',[1,2],mutation=True)
        self.assertEqual(connection.request.call_count,1)

    def test_published_schemas_have_resolvable_selector_refs(self):
        from android_use import TOOLS
        for tool in TOOLS:
            schema=tool['inputSchema']
            self.assertFalse(schema['additionalProperties'])
            if '#/$defs/selector' in json.dumps(schema):self.assertIn('selector',schema['$defs'])
        self.assertLess(len(json.dumps(next(t for t in TOOLS if t['name']=='pua_batch')['inputSchema'])),8500)

class AndroidAdaptivePreview(unittest.TestCase):
    def test_preview_jpeg_validates_transport_without_recompression(self):
        import base64
        client=AndroidClient()
        raw=b'\xff\xd8payload\xff\xd9'
        client.rpc=Mock(return_value=base64.b64encode(raw).decode()+'\n')
        self.assertEqual(client.preview_screenshot(),raw)
        client.rpc.assert_called_once_with('takeScreenshot',[1,75],timeout=3)
        for invalid in (None,'bad base64',base64.b64encode(b'not jpeg').decode()):
            client.rpc.return_value=invalid
            with self.assertRaises(AndroidError):client.preview_screenshot()

    def test_lock_during_capture_drops_frame(self):
        import threading
        from PIL import Image
        from android_screen import AndroidScreen
        raw=io.BytesIO();Image.new('RGB',(10,20)).save(raw,format='JPEG')
        client=Mock();client.locked.side_effect=[False,True]
        client.preview_screenshot.return_value=raw.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            screen=AndroidScreen(directory,client)
            screen._live=Mock(return_value=True)
            try:
                with patch('android_video.capture',return_value=False):screen._run(threading.Event())
                self.assertIsNone(screen._frame)
                self.assertTrue(screen._paused())
            finally:screen.close()

    def test_video_dependency_failure_falls_back_without_touching_device(self):
        import threading
        from android_video import capture
        screen=Mock()
        with patch('android_video.dependencies',side_effect=OSError('missing')):
            self.assertFalse(capture(screen,threading.Event()))
        screen.client.command.assert_not_called()

    def test_capture_dimensions_update_viewport_without_observation(self):
        import threading
        from android_screen import AndroidScreen
        with tempfile.TemporaryDirectory() as directory:
            screen=AndroidScreen(directory,Mock());stop=threading.Event()
            try:
                for width,height in [(1440,2960),(2960,1440),(2208,1840),(1800,1800)]:
                    screen._publish_frame(str((width,height)).encode(),width,height,stop)
                    wire=screen._wire(screen._read_state())
                    self.assertEqual(wire['viewport'],{'width':width,'height':height})
                    self.assertEqual((wire['frame']['width'],wire['frame']['height']),(width,height))
                screen.set_paused(True)
                screen._publish_frame(b'late frame',100,200,stop)
                self.assertIsNone(screen._frame)
            finally:screen.close()

    def test_scaled_preview_preserves_native_gesture_coordinates(self):
        import threading
        from android_screen import AndroidScreen
        with tempfile.TemporaryDirectory() as directory:
            screen=AndroidScreen(directory,Mock());stop=threading.Event()
            try:
                for width,height,native in [(778,1600,{'width':1440,'height':2960}),
                                             (1600,778,{'width':2960,'height':1440})]:
                    screen._publish_frame(str((width,height)).encode(),width,height,stop,viewport=native)
                    wire=screen._wire(screen._read_state())
                    self.assertEqual(wire['viewport'],native)
                    self.assertEqual((wire['frame']['width'],wire['frame']['height']),(width,height))
            finally:screen.close()

    def test_old_capture_is_never_reported_live(self):
        import threading,time
        from android_screen import AndroidScreen
        with tempfile.TemporaryDirectory() as directory:
            screen=AndroidScreen(directory,Mock())
            try:
                screen._publish_frame(b'frame',1440,2960,threading.Event())
                self.assertTrue(screen._wire(screen._read_state())['frame_available'])
                screen._last_capture=time.monotonic()-4
                stale=screen._wire(screen._read_state())
                self.assertFalse(stale['frame_available']);self.assertIsNone(stale['frame'])
                self.assertGreaterEqual(stale['capture_age_ms'],4000)
            finally:screen.shutdown()

    def test_android_markup_has_platform_marker_and_no_screen_crop(self):
        from android_widget import android_html
        html=android_html('<head></head><main id="app">iPhone</main>')
        self.assertIn('data-platform="android"',html)
        self.assertIn('#screen{border-radius:0}',html)
        self.assertIn('aspect-ratio:auto',html)
        self.assertIn('.dynamic-island,.side-button,.receiver{display:none!important}',html)
        self.assertEqual(android_html(html),html)

if __name__=='__main__':unittest.main()
