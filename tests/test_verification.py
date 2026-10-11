"""Startup must never bypass verification or prepare services after a failure."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import dotagents
import launcher
import verification


class VerificationTests(unittest.TestCase):
    def test_both_invocations_block_before_importing_dashboard_or_saving_discovery(self):
        with patch.object(launcher, 'verify', side_effect=ValueError('incomplete')) as verify, \
                patch('node_discovery.save') as save, patch.dict(sys.modules, {'tui': None}):
            for args in ([], ['--verify']):
                with self.assertRaisesRegex(ValueError, 'incomplete'):
                    dotagents.main(args)
            self.assertEqual(verify.call_args_list, [call(full=False), call(full=True)])
            save.assert_not_called()

    def test_verify_exits_without_starting_or_writing(self):
        with patch.object(launcher, 'verify', return_value=[]) as verify, \
                patch('node_discovery.save') as save, patch.dict(sys.modules, {'tui': None}):
            dotagents.main(['--verify'])
            verify.assert_called_once_with(full=True)
            save.assert_not_called()

    def test_run_verifies_then_publishes_routes_and_opens_dashboard(self):
        events = []
        app = Mock()
        app.run.side_effect = lambda: events.append('dashboard')
        fake_tui = Mock(DotAgents=Mock(return_value=app))
        with patch.object(launcher, 'verify', side_effect=lambda **kwargs: events.append(('verify', kwargs)) or []), \
                patch('node_discovery.save', side_effect=lambda *args: events.append('routes')), \
                patch.object(sys.stdin, 'isatty', return_value=True), \
                patch.object(sys.stdout, 'isatty', return_value=True), patch.dict(sys.modules, tui=fake_tui):
            dotagents.main([])
        self.assertEqual(events, [('verify', {'full': False}), 'routes', 'dashboard'])
        self.assertTrue(app.verified)

    def test_no_extra_verification_or_setup_command(self):
        with patch.object(launcher, 'verify') as verify, patch.object(sys, 'stderr'):
            for args in (['setup'], ['serve'], ['--verify', '--project-only']):
                with self.assertRaises(SystemExit):
                    dotagents.main(args)
            verify.assert_not_called()

    def test_missing_enabled_engine_fails_even_when_no_weights_are_installed(self):
        model = dict(engine='omlx', optional=True, artifacts=[])
        with patch.object(verification.platform, 'machine', return_value='arm64'), \
                patch('omlx_backend.validate', side_effect=ValueError('missing pinned engine')) as validate:
            with self.assertRaisesRegex(ValueError, 'missing pinned engine'):
                verification.engine(Path('/unused'), model)
            validate.assert_called_once_with(model, weights=False)

    def test_model_verification_checks_sizes_without_reading_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weights = root / 'weights.bin'
            model = dict(engine='omlx', artifacts=[{'path': weights.name, 'size': 4}])
            with patch.object(launcher, 'ROOT', root), \
                    patch.object(verification.platform, 'machine', return_value='arm64'), \
                    patch('omlx_backend.validate'):
                with self.assertRaisesRegex(ValueError, 'Missing or incomplete'):
                    verification.engine(root, model)
                weights.write_bytes(b'bad')
                with self.assertRaisesRegex(ValueError, 'Missing or incomplete'):
                    verification.engine(root, model)
                weights.write_bytes(b'good')
                with patch.object(Path, 'open', side_effect=AssertionError('Do not read model contents')):
                    verification.engine(root, model)

    def test_only_enabled_host_models_are_required_and_failures_accumulate(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            (root / 'config').mkdir()
            config = dict(default_model='host', enabled_models=['host'],
                          models={'host': {'name': 'host'}, 'unused': {'name': 'unused'}})
            (root / 'config/models.json').write_text(json.dumps(config))
            for name in ('host', 'dependencies', 'applications', 'project'):
                stack.enter_context(patch.object(verification, name))
            stack.enter_context(patch.object(verification, 'activate'))
            stack.enter_context(patch('control_plane.catalog'))
            stack.enter_context(patch('node_discovery.discover', return_value=[]))
            engine = stack.enter_context(patch.object(verification, 'engine'))
            self.assertEqual(verification.verify(root), [])
            engine.assert_called_once_with(root, config['models']['host'])
            engine.side_effect = ValueError('missing engine')
            verification.applications.side_effect = ValueError('missing images')
            with self.assertRaisesRegex(ValueError, 'Startup is blocked.*applications.*engine'):
                verification.verify(root)
            self.assertEqual(verification.project.call_count, 2)
            verification.project.reset_mock()
            with self.assertRaisesRegex(ValueError, 'Startup is blocked.*applications.*engine'):
                verification.verify(root, full=False)
            verification.project.assert_not_called()
            engine.side_effect = None
            verification.applications.side_effect = None
            self.assertEqual(verification.verify(root, full=False), [])
            verification.project.assert_not_called()

    def test_launcher_preserves_verification_mode(self):
        with patch.object(verification, 'verify', return_value=[]) as verify:
            for full in (False, True):
                self.assertEqual(launcher.verify(full=full), [])
                verify.assert_called_with(launcher.ROOT, full=full)

    def test_full_project_checks_include_enabled_calendar_tests(self):
        root = Path('/fixture')
        for enabled in (False, True):
            with self.subTest(enabled=enabled), \
                    patch.object(verification, 'load', return_value={
                        'integrations': {'calendar': {'enabled': enabled}}}), \
                    patch('calendar_bridge.command', return_value=['/fixture/python']), \
                    patch.object(verification, 'run') as run, \
                    patch.object(verification.shutil, 'which', return_value='/fixture/node'), \
                    patch.object(verification.subprocess, 'run', return_value=Mock(returncode=0)) as suite:
                verification.project(root)
                self.assertEqual(run.call_count, 3 if enabled else 2)
                if enabled:
                    run.assert_called_with(root, ['/fixture/python', '-m', 'pytest', '-q',
                                                 '/fixture/mcp/calendar-mcp/tests'], timeout=60)
                suite.assert_called_once()

    def test_full_verification_requires_node_before_running_tests(self):
        with patch.object(verification.shutil, 'which', return_value=None), \
                patch.object(verification, 'run') as run, \
                patch.object(verification.subprocess, 'run') as suite:
            with self.assertRaisesRegex(ValueError, 'requires Node.js'):
                verification.project(Path('/fixture'))
            run.assert_not_called()
            suite.assert_not_called()

    def test_default_model_must_be_enabled(self):
        with self.assertRaises(ValueError):
            launcher.configured_models(dict(default_model='required', enabled_models=['other'],
                                           models={'required': {}, 'other': {}}))

    def test_enabled_image_application_needs_a_discovered_image_api(self):
        with patch.object(verification, 'load', return_value={'applications': {'images': True}}), \
                patch.dict(verification.os.environ, {'DOTAGENTS_REMOTE_IMAGE_URL': ''}), \
                patch.object(verification, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'No Image API was discovered'):
                verification.applications(Path('/unused'))
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
