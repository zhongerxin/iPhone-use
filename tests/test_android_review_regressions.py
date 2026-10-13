"""Regression cases from PR #18: indexed queries, scroll proof and worker stop."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from android_client import AndroidError
from android_controller import AndroidController
from android_use import Runtime, read, save


def node(text='OK', x=10, y=20):
    return {'text': text, 'resource_id': 'app:id/label', 'class_name': 'android.widget.TextView',
            'package': 'app', 'enabled': True, 'password': False, 'rect': [x, y, 50, 30]}


def phone(nodes):
    result = AndroidController(Mock(), '.', Mock())
    result.tree = Mock(return_value=nodes)
    result.context = Mock(return_value={'package': 'app', 'width': 500, 'height': 1000, 'rotation': 0})
    return result


class IndexedSelectors(unittest.TestCase):
    def test_missing_index_does_not_verify_find_wait_or_postcondition(self):
        controller = phone([node()])
        selector = {'text': 'OK', 'index': 5}
        self.assertEqual(controller.find(selector), {'matches': 0, 'nodes': [], 'truncated': False})
        with self.assertRaises(AndroidError) as failure:
            controller.wait(selector, timeout_seconds=0)
        self.assertEqual(failure.exception.code, 'postcondition_failed')
        controller.deadline = 0.01
        with self.assertRaises(AndroidError) as failure:
            controller.finish(expect=selector)
        self.assertTrue(failure.exception.details['action_executed'])

    def test_index_is_applied_before_limit_and_not_applied_twice_on_tap(self):
        controller = phone([node(y=20), node(y=80), node(y=140)])
        selector = {'text': 'OK', 'index': 2}
        found = controller.find(selector, limit=1)
        self.assertEqual(found['matches'], 1)
        self.assertFalse(found['truncated'])
        self.assertEqual(found['nodes'][0]['index'], 2)
        controller.tap(selector=selector)
        controller.client.rpc.assert_called_once_with('click', [35, 155], mutation=True)

    def test_unindexed_query_still_reports_ambiguity(self):
        controller = phone([node(), node(y=80)])
        self.assertEqual(controller.find({'text': 'OK'}, limit=1)['matches'], 2)
        with self.assertRaises(AndroidError) as failure:
            controller.resolve({'text': 'OK'})
        self.assertEqual(failure.exception.code, 'ambiguous_target')

    def test_scroll_find_can_swipe_for_a_missing_index(self):
        controller = phone([])
        controller.tree.side_effect = [[node()], [node(), node(y=80)], [node(), node(y=80)]]
        controller.swipe = Mock()
        with patch('android_controller.time.sleep'):
            result = controller.scroll_find({'text': 'OK', 'index': 1})
        self.assertEqual(result['swipes'], 1)
        self.assertEqual(result['target']['rect'][1], 80)
        controller.swipe.assert_called_once_with('up')

    def test_indexed_rows_and_end_marker_do_not_match_other_rows(self):
        controller = phone([node(), node(y=80)])
        result = controller.collect_list({'text': 'OK', 'index': 1}, max_pages=1,
                                         end_selector={'text': 'OK', 'index': 5})
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['rows'][0]['rect'][1], 80)
        self.assertFalse(result['end_marker_seen'])


class ScrollAnchors(unittest.TestCase):
    def swipe(self, before, after, direction='up', verified=False):
        controller = phone([])
        controller.tree.side_effect = [before, after]
        with patch('android_controller.time.sleep'):
            if verified:
                self.assertTrue(controller.swipe(direction, verify=True)['verified'])
            else:
                with self.assertRaises(AndroidError) as failure:
                    controller.swipe(direction, verify=True)
                self.assertEqual(failure.exception.code, 'no_scroll_progress')
                self.assertTrue(failure.exception.details['action_executed'])
        self.assertEqual(controller.client.rpc.call_count, 1)

    def test_unchanged_duplicate_rows_never_prove_motion(self):
        for direction in ('up', 'down', 'left', 'right'):
            with self.subTest(direction=direction):
                nodes = [node('On', x=20, y=20), node('On', x=100, y=100)]
                self.swipe(nodes, nodes, direction)

    def test_anchor_must_be_unique_in_both_snapshots(self):
        self.swipe([node('On', y=100)], [node('On', y=20), node('On', y=100)])
        self.swipe([node('On', y=20), node('On', y=100)], [node('On', y=20)])

    def test_unique_anchor_proves_correct_direction_only(self):
        for direction, after in (('up', node(y=40)), ('down', node(y=160)),
                                 ('left', node(x=40, y=100)), ('right', node(x=160, y=100))):
            with self.subTest(direction=direction):
                self.swipe([node(x=100, y=100)], [after], direction, verified=True)
        self.swipe([node(y=100)], [node(y=160)], 'up')

    def test_unique_motion_still_verifies_with_duplicate_or_numeric_noise(self):
        self.swipe([node('On', y=20), node('On', y=80), node('Unique', y=200)],
                   [node('On', y=20), node('On', y=80), node('Unique', y=140)], verified=True)
        self.swipe([node('123', y=200)], [node('123', y=140)])


class WorkerStopping(unittest.TestCase):
    def test_stop_then_immediate_start_does_not_reuse_doomed_service(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            runtime.client = Mock(serial='selected')
            path = Path(directory) / 'setup-job.json'
            save(path, {'id': 'owned', 'state': 'complete', 'service': {'ready': True}})
            try:
                stopped = runtime.setup('stop')
                self.assertFalse(stopped['stopped'])
                self.assertEqual(stopped['job']['state'], 'stopping')
                self.assertTrue(read(path)['stop_requested'])
                with patch('android_use.subprocess.Popen') as spawn:
                    started = runtime.setup('start')
                runtime.client.rpc.assert_not_called()
                runtime.client.attach.assert_not_called()
                self.assertEqual(started['job']['state'], 'running')
                self.assertNotEqual(started['job']['id'], 'owned')
                spawn.assert_called_once()
            finally:
                runtime.close()

    def test_status_waits_for_stop_and_only_then_reports_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            runtime.doctor = Mock(return_value={})
            path = Path(directory) / 'setup-job.json'
            save(path, {'id': 'owned', 'state': 'stopping', 'stop_requested': True})
            try:
                def complete_stop(_):
                    save(path, {'id': 'owned', 'state': 'stopped', 'service': {'ready': False}})
                with patch('android_use.time.sleep', side_effect=complete_stop) as sleep:
                    status = runtime.setup('status', job_id='owned', wait_seconds=1)
                sleep.assert_called_once()
                self.assertEqual(status['job']['state'], 'stopped')
                self.assertTrue(runtime.setup('stop')['stopped'])
            finally:
                runtime.close()

    def test_stop_without_owned_worker_does_not_touch_external_service(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            runtime.client = Mock(serial='selected')
            try:
                self.assertTrue(runtime.setup('stop')['stopped'])
                runtime.client.rpc.assert_not_called()
                runtime.client.command.assert_not_called()
            finally:
                runtime.close()


if __name__ == '__main__':
    unittest.main()
