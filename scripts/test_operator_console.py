"""Workbench process ownership and loopback HTTP integration, without cameras."""
import copy
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from operator_config import OperatorConfig
from operator_console import KIT, Server, Workbench


class FakeProcess:
    def __init__(self, command, **kwargs):
        self.command, self.kwargs = command, kwargs
        self.control = Path(command[command.index('--control-dir') + 1])
        self.returncode = None
        self.on_wait = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.on_wait:
            self.on_wait(timeout)
        self.returncode = 0
        (self.control / 'status.json').write_text('{"stage":"complete"}', encoding='utf-8')
        return 0


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tempkit = self.root / 'kit'
        self.tempkit.mkdir()
        (self.tempkit / 'config').mkdir()
        self.original = (KIT / 'config/operator-template.json').read_bytes()
        (self.tempkit / 'config/operator-template.json').write_bytes(self.original)
        self.settings = OperatorConfig(self.tempkit)
        self.settings.initialize()
        self.children = []

        def popen(command, **kwargs):
            process = FakeProcess(command, **kwargs)
            self.children.append(process)
            return process

        self.app = Workbench(self.settings, kit=KIT, work_dir=self.root / 'work', popen=popen)
        self.addCleanup(self.close_log)

    def close_log(self):
        if self.app.log:
            self.app.log.close()

    def test_invalid_configuration_does_not_spawn(self):
        path = self.settings.directory / 'advanced.json'
        advanced = json.loads(path.read_text(encoding='utf-8'))
        advanced['encoder'] = 'auto'
        path.write_text(json.dumps(advanced), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.app.start('record')
        self.assertEqual(self.children, [])
        self.assertFalse(self.app.running())

    def test_running_record_locks_save_start_and_create(self):
        self.app.start('record')
        daily = self.settings.view()['daily']
        original = copy.deepcopy(daily)
        for action in (
            lambda: self.app.save({'daily': dict(daily, duration_hours=2)}),
            lambda: self.app.start('record'),
            lambda: self.app.start('preview'),
            lambda: self.app.create_scheme({'name': '新方案', 'source_id': 'current'}),
            lambda: self.app.save_imaging({'imaging': self.settings.view()['imaging']}),
            self.app.open_official,
        ):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.settings.view()['daily'], original)
        self.assertTrue(self.app.state()['locked'])

    def test_details_restart_preview_after_release_with_new_snapshot(self):
        view = self.settings.create_scheme('细节', 'current')
        self.app.start('preview')
        old = self.children[0]
        snapshot = copy.deepcopy(self.app.profile)
        imaging = dict(view['imaging'], gamma=121)

        def wait(timeout):
            self.assertEqual(timeout, 15)
            self.assertTrue((old.control / 'stop.request').exists())
            self.assertEqual(len(self.children), 1)
            self.assertNotEqual(self.settings.view()['imaging'], imaging)

        old.on_wait = wait
        self.app.save_imaging({'imaging': imaging,
                              'daily': dict(view['daily'], duration_hours=3),
                              'scheme_values': {'exposure_ms': 20, 'gain_multiplier': 2}})
        self.assertEqual(len(self.children), 2)
        self.assertEqual(old.returncode, 0)
        self.assertEqual(self.app.profile['imaging'], imaging)
        self.assertEqual(self.app.profile['exposure_us'], 20000)
        self.assertEqual(self.settings.view()['schemes']['current'], view['schemes']['current'])
        self.assertEqual(snapshot['imaging'], view['imaging'])
        self.assertFalse(self.app.transition)

    def test_invalid_details_dont_stop_preview(self):
        self.app.start('preview')
        before = self.settings.view()
        with self.assertRaises(ValueError):
            self.app.save_imaging({'imaging': dict(before['imaging'], gamma=181)})
        self.assertFalse((self.app.control / 'stop.request').exists())
        self.assertEqual(self.settings.view(), before)
        self.assertEqual(len(self.children), 1)

    def test_details_release_timeout_does_not_save_or_overlap(self):
        self.app.start('preview')
        before = self.settings.view()

        def timeout(seconds):
            raise subprocess.TimeoutExpired('preview', seconds)

        self.children[0].on_wait = timeout
        with self.assertRaises(ValueError):
            self.app.save_imaging({'imaging': dict(before['imaging'], gamma=121)})
        self.assertEqual(self.settings.view(), before)
        self.assertEqual(len(self.children), 1)
        self.assertTrue(self.app.running())
        self.assertFalse(self.app.transition)

    def test_official_release_exact_executable_and_exclusive_owner(self):
        self.app.start('preview')
        old = self.children[0]
        events = []
        executable = KIT / 'runtime/imageview/x64/imageview.exe'
        self.assertTrue(executable.is_file())

        def wait(timeout):
            self.assertTrue((old.control / 'stop.request').exists())
            events.append('released')

        def popen(command, **kwargs):
            self.assertEqual(events, ['released'])
            self.assertEqual(command, [str(executable)])
            self.assertEqual(kwargs['cwd'], executable.parent)
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            events.append('spawned')
            return SimpleNamespace(poll=lambda: None)

        old.on_wait = wait
        self.app.popen = popen
        self.app.open_official()
        self.assertEqual(events, ['released', 'spawned'])
        self.assertTrue(self.app.state()['official_running'])
        self.app.open_official()
        self.assertEqual(events, ['released', 'spawned'])
        for mode in ('preview', 'record'):
            with self.assertRaises(ValueError):
                self.app.start(mode)

    def test_official_release_timeout_does_not_spawn(self):
        self.app.start('preview')

        def timeout(seconds):
            raise subprocess.TimeoutExpired('preview', seconds)

        self.children[0].on_wait = timeout
        with self.assertRaises(ValueError):
            self.app.open_official()
        self.assertEqual(len(self.children), 1)
        self.assertTrue(self.app.running())
        self.assertFalse(self.app.official_running())
        self.assertFalse(self.app.transition)

    def test_official_spawn_failure_leaves_preview_released(self):
        self.app.start('preview')

        def fail(command, **kwargs):
            self.assertEqual(self.children[0].returncode, 0)
            raise OSError('official unavailable')

        self.app.popen = fail
        with self.assertRaises(OSError):
            self.app.open_official()
        self.assertFalse(self.app.running())
        self.assertFalse(self.app.official_running())
        self.assertFalse(self.app.transition)

    def test_preview_releases_camera_before_record_launch(self):
        self.app.start('preview')
        old = self.children[0]
        events = []

        def wait(timeout):
            events.append('wait')
            self.assertEqual(timeout, 15)
            self.assertTrue((old.control / 'stop.request').exists())
            self.assertEqual(len(self.children), 1)
            self.assertTrue(self.app.transition)
            with self.assertRaises(ValueError):
                self.app.save({'daily': self.settings.view()['daily']})

        old.on_wait = wait
        self.app.start('record')
        self.assertEqual(events, ['wait'])
        self.assertEqual(len(self.children), 2)
        self.assertEqual(old.returncode, 0)
        self.assertFalse(self.app.transition)
        self.assertEqual(self.app.mode, 'record')

    def test_preview_release_timeout_does_not_spawn_or_kill(self):
        self.app.start('preview')

        def timeout(seconds):
            raise subprocess.TimeoutExpired('camera', seconds)

        self.children[0].on_wait = timeout
        with self.assertRaises(ValueError):
            self.app.start('record')
        self.assertEqual(len(self.children), 1)
        self.assertTrue(self.app.running())
        self.assertFalse(self.app.transition)

    def test_launch_failure_closes_log(self):
        handles = []

        def fail(command, **kwargs):
            handles.append(kwargs['stdout'])
            raise OSError('simulated missing runtime')

        self.app.popen = fail
        with self.assertRaises(OSError):
            self.app.start('record')
        self.assertTrue(handles[0].closed)
        self.assertIsNone(self.app.process)
        self.assertFalse(self.app.running())

    def test_shutdown_waits_for_preview_handoff_and_stops_final_owner(self):
        self.app.start('preview')
        entered, release = threading.Event(), threading.Event()
        errors = []

        def wait(timeout):
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError('test preview handoff did not release')

        self.children[0].on_wait = wait

        def guarded(action):
            try:
                action()
            except BaseException as exc:
                errors.append(exc)

        starter = threading.Thread(target=guarded, args=(lambda: self.app.start('record'),))
        stopper = threading.Thread(target=guarded, args=(self.app.shutdown,))
        starter.start()
        try:
            self.assertTrue(entered.wait(timeout=3))
            stopper.start()
            deadline = time.monotonic() + 3
            while not self.app.closing and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(self.app.closing)
            self.assertTrue(stopper.is_alive())
        finally:
            release.set()
            starter.join(timeout=3)
            if stopper.ident:
                stopper.join(timeout=3)
        self.assertFalse(starter.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.app.running())
        self.assertTrue((self.children[-1].control / 'stop.request').exists())
        with self.assertRaises(ValueError):
            self.app.start('record')

    def test_short_run_uses_fixed_snapshot_and_preserves_source(self):
        before = self.settings.view()
        self.app.start('record', short=True)
        profile = json.loads((self.app.control / 'profile.json').read_text(encoding='utf-8'))
        self.assertEqual(profile['duration_hours'] * 3600, 60)
        self.assertEqual(profile['segment_seconds'], 20)
        self.assertFalse(profile['preview_enabled'])
        self.assertTrue(profile['status_enabled'])
        self.assertEqual(profile, self.app.profile)
        self.assertEqual(self.settings.view(), before)
        self.assertEqual((self.tempkit / 'config/operator-template.json').read_bytes(), self.original)
        # Later independent configuration edits cannot mutate the captured profile.
        self.settings.save(dict(before['daily'], duration_hours=2))
        self.assertEqual(self.app.profile, profile)
        command = self.children[0].command
        self.assertIn(str(KIT / 'scripts/capture.py'), command)
        self.assertIn('record', command)
        self.assertEqual(self.children[0].kwargs['stdin'], subprocess.DEVNULL)

    def test_nonzero_child_cannot_report_success(self):
        self.app.start('record')
        (self.app.control / 'status.json').write_text('{"stage":"complete"}', encoding='utf-8')
        self.children[0].returncode = 7
        state = self.app.state()
        self.assertEqual(state['stage'], 'incomplete')
        self.assertIn('7', state['error'])
        self.assertFalse(state['running'])
        self.assertIsNone(self.app.log)

    def test_http_auth_and_valid_state_save(self):
        server = Server(('127.0.0.1', 0), self.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            def request(method, path, data=None, headers=None):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                try:
                    body = json.dumps(data) if data is not None else None
                    connection.request(method, path, body=body, headers=headers or {})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()

            valid = {'X-Camera-Token': server.token, 'Content-Type': 'application/json'}
            for headers in ({}, {'X-Camera-Token': 'wrong'},
                            dict(valid, Origin='https://attacker.invalid'),
                            dict(valid, Host='attacker.invalid')):
                self.assertEqual(request('GET', '/api/state', headers=headers)[0], 403)
                self.assertEqual(request('POST', '/api/save', {'daily': self.settings.view()['daily']}, headers)[0], 403)
            status, state = request('GET', '/api/state', headers=valid)
            self.assertEqual(status, 200)
            self.assertEqual(state['config']['daily'], self.settings.view()['daily'])
            daily = dict(state['config']['daily'], duration_hours=2)
            self.assertEqual(request('POST', '/api/save', {'daily': daily}, valid), (200, {'ok': True}))
            self.assertEqual(self.settings.view()['daily'], daily)
            bad = dict(daily, output_dir='relative')
            self.assertEqual(request('POST', '/api/save', {'daily': bad}, valid)[0], 400)
            self.assertEqual(self.settings.view()['daily'], daily)
            self.assertEqual(self.children, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
