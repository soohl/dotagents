"""One YAML file owns runtime choices; private credentials do not override them."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import runtime_config


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = dict(models=dict(default='host', enabled=['host']),
                           network=dict(gateway_port=8888),
                           applications=dict(images=False, remote_browser=False))

    def write(self, config):
        (self.root / 'config.yaml').write_text(json.dumps(config))

    def test_host_only_defaults_override_old_compose_environment(self):
        self.write(self.config)
        with patch.dict(os.environ, COMPOSE_FILE='old-private-stack', COMPOSE_PROFILES='extra'):
            runtime_config.activate(self.root)
            self.assertEqual(os.environ['COMPOSE_FILE'], 'config/compose.harness.yaml')
            self.assertEqual(os.environ['COMPOSE_PROFILES'], '')
            self.assertEqual(os.environ['COMPOSE_PROJECT_NAME'], 'dotagents')

    def test_optional_services_are_selected_only_in_yaml(self):
        self.config['applications'].update(images=True, remote_browser=True)
        self.write(self.config)
        with patch.dict(os.environ):
            runtime_config.activate(self.root)
            self.assertEqual(os.environ['COMPOSE_FILE'].split(':'),
                             ['config/compose.harness.yaml', 'config/compose.harness-remote.yaml',
                              'config/compose.images.yaml'])

    def test_invalid_switches_models_and_typographical_errors_are_rejected(self):
        bad = []
        for key, value in [('gateway_port', True), ('gateway_port', -1), ('gateway_port', 65536), ('gateway_port', 8100)]:
            config = copy.deepcopy(self.config)
            config['network'][key] = value
            bad.append(config)
        for section, key, value in [('applications', 'images', 'false'),
                                    ('models', 'enabled', ['host', 'host']),
                                    ('models', 'default', 'missing'),
                                    ('network', 'gateway_prt', 8888)]:
            config = copy.deepcopy(self.config)
            config[section][key] = value
            bad.append(config)
        for config in bad:
            self.write(config)
            with self.subTest(config=config), self.assertRaises(ValueError):
                runtime_config.load(self.root)

    def test_runtime_selection_overlays_pins_without_modifying_them(self):
        self.write(self.config)
        (self.root / 'config').mkdir()
        path = self.root / 'config/models.json'
        pins = dict(models={'host': {'revision': 'pinned'}, 'unused': {'revision': 'other'}})
        path.write_text(json.dumps(pins))
        result = runtime_config.model_profiles(self.root)
        self.assertEqual(result['default_model'], 'host')
        self.assertEqual(result['enabled_models'], ['host'])
        self.assertEqual(json.loads(path.read_text()), pins)

    def test_calendar_defaults_off_and_uses_yaml_not_environment(self):
        self.write(self.config)
        self.assertFalse(runtime_config.load(self.root)['integrations']['calendar']['enabled'])
        self.config['integrations'] = {'calendar': {'enabled': True, 'calendar_ids': ['fixture']}}
        self.write(self.config)
        with patch.dict(os.environ, DOTAGENTS_CALENDAR_ENABLED='false'):
            runtime_config.activate(self.root)
            self.assertEqual(os.environ['DOTAGENTS_CALENDAR_ENABLED'], 'true')
        for change in ({'enabled': 'true'}, {'calendar_ids': ['x', 'x']},
                       {'calendar_ids': 'all'}, {'calendar_ids': ['\0']}, {'write': True}):
            config = copy.deepcopy(self.config)
            config['integrations']['calendar'].update(change)
            self.write(config)
            with self.subTest(change=change), self.assertRaises(ValueError):
                runtime_config.load(self.root)


if __name__ == '__main__':
    unittest.main()
