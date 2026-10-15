"""Load the single user configuration; pins and secrets stay separate."""
import json
import os

WORKER_PORT = 8100  # Internal loopback listener; never an application endpoint.


def gateway_port(root):
    return load(root)['network']['gateway_port']


def load(root):
    try:
        import yaml
    except ImportError:
        raise ValueError('Install the pinned requirements from config/tui-requirements.txt.') from None
    try:
        config = yaml.safe_load((root / 'config.yaml').read_text())
    except yaml.YAMLError:
        raise ValueError('Invalid YAML in config.yaml.') from None
    if isinstance(config, dict):
        config.setdefault('integrations', {'calendar': {'enabled': False, 'calendar_ids': []}})
        if isinstance(config.get('models'), dict):
            config['models'].setdefault('max_context_tokens', 262144)
        if isinstance(config.get('applications'), dict):
            config['applications'].setdefault('lan_browser', False)
    shape = {'models': {'default', 'enabled', 'max_context_tokens'}, 'network': {'gateway_port'},
             'integrations': {'calendar'},
             'applications': {'images', 'remote_browser', 'lan_browser'}}
    if (not isinstance(config, dict) or set(config) != set(shape)
            or any(not isinstance(config[key], dict) or set(config[key]) != fields
                   for key, fields in shape.items())):
        raise ValueError('config.yaml must define models, network, and applications with their supported fields.')
    models = config['models']
    if type(models['max_context_tokens']) is not int or not 2 <= models['max_context_tokens'] <= 262144:
        raise ValueError('config.yaml models.max_context_tokens must be an integer from 2 to 262144.')
    if (not isinstance(models['enabled'], list) or not models['enabled']
            or any(not isinstance(key, str) or not key for key in models['enabled'])
            or len(set(models['enabled'])) != len(models['enabled'])
            or not isinstance(models['default'], str) or models['default'] not in models['enabled']):
        raise ValueError('config.yaml models.enabled must include models.default without duplicates.')
    port = config['network']['gateway_port']
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('config.yaml network.gateway_port must be an integer from 1 to 65535.')
    if port == WORKER_PORT:
        raise ValueError(f'config.yaml network.gateway_port cannot use the internal worker port {WORKER_PORT}.')
    if any(type(value) is not bool for value in config['applications'].values()):
        raise ValueError('config.yaml application switches must be true or false.')
    calendar = config['integrations']['calendar']
    if (not isinstance(calendar, dict) or set(calendar) != {'enabled', 'calendar_ids'}
            or type(calendar['enabled']) is not bool
            or not isinstance(calendar['calendar_ids'], list)
            or len(calendar['calendar_ids']) > 100
            or any(not isinstance(key, str) or not 1 <= len(key) <= 512 or '\0' in key
                   for key in calendar['calendar_ids'])
            or len(set(calendar['calendar_ids'])) != len(calendar['calendar_ids'])):
        raise ValueError('config.yaml integrations.calendar needs enabled and a unique calendar_ids list.')
    return config


def model_profiles(root):
    catalog = json.loads((root / 'config/models.json').read_text())
    if (root / 'config.yaml').is_file():
        models = load(root)['models']
        catalog.update(default_model=models['default'], enabled_models=models['enabled'])
        for model in catalog['models'].values():
            if 'context' in model:
                model['context'] = min(model['context'], models['max_context_tokens'])
    return catalog


def activate(root):
    config = load(root)
    applications = config['applications']
    files = ['config/compose.harness.yaml']
    if applications['remote_browser']:
        files.append('config/compose.harness-remote.yaml')
    if applications['images']:
        files.append('config/compose.images.yaml')
    # Compose inherits these choices in all dashboard subprocesses. The private
    # .env supplies credentials, not a second source of runtime choices.
    os.environ['COMPOSE_FILE'] = ':'.join(files)
    os.environ['COMPOSE_PROJECT_NAME'] = 'dotagents'
    os.environ['COMPOSE_PROFILES'] = ''
    os.environ['DOTAGENTS_CALENDAR_ENABLED'] = str(config['integrations']['calendar']['enabled']).lower()
    os.environ['DOTAGENTS_LAN_BROWSER'] = str(applications['lan_browser']).lower()
    os.environ['DOTAGENTS_BROWSER_BIND'] = '0.0.0.0' if applications['lan_browser'] else '127.0.0.1'
