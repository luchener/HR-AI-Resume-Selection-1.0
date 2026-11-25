"""Pure management views with explicitly supplied environment fallback.

No config/app imports, environment access, filesystem, provider calls or logs.
The legacy source names are part of the frontend contract: "ui" means the
operator's stored configuration regardless of its storage backend.
"""
from copy import deepcopy

MASK = '\u2022' * 8
SLOTS = ('transactional', 'notification')


def _text(document, name, default=''):
    value = document.get(name, default)
    if not isinstance(value, str):
        raise ValueError('Invalid configuration text')
    return value


def _flag(document, name, default=True):
    value = document.get(name, default)
    if type(value) is not bool:
        raise ValueError('Invalid configuration flag')
    return value


def _number(document, name, default, maximum):
    value = document.get(name, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError('Invalid configuration number')
    return value


def _object(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('Invalid configuration document')
    return value


def environment_snapshot(environment):
    """Copy only explicit mappings; never inherit ambient os.environ/config."""
    source = _object(environment)
    if set(source) - {'mail', 'llm'}:
        raise ValueError('Only explicit mail/llm environment mappings accepted')
    result = {key: deepcopy(_object(source.get(key))) for key in ('mail', 'llm')}
    # Validate even fallback that is currently hidden by an active stored value.
    _mail(result['mail'])
    _llm(result['llm'], result['llm'])
    return result


def _mail(value):
    current = _object(value)
    username = _text(current, 'username')
    security = _text(current, 'security') or 'ssl'
    if security not in {'ssl', 'starttls', 'plain'}:
        raise ValueError('Invalid mail security')
    return {'host': _text(current, 'host'), 'port': _number(current, 'port', 465, 65535),
            'security': security, 'username': username,
            'password': _text(current, 'password'),
            'from_address': _text(current, 'from_address') or username,
            'from_name': _text(current, 'from_name') or 'AI 简历智选', 'enabled': _flag(current, 'enabled')}


def _mail_ready(cfg):
    return bool(cfg['host'] and cfg['username'] and cfg['password'])


def _llm(current, environment):
    current, environment = _object(current), _object(environment)
    return {'api_key': _text(current, 'api_key'),
            'base_url': _text(current, 'base_url') or _text(environment, 'base_url'),
            'model': _text(current, 'model') or _text(environment, 'model') or 'deepseek-chat',
            'timeout': _number(current, 'timeout', 300, 3600), 'enabled': _flag(current, 'enabled')}


def _llm_ready(cfg):
    return bool(cfg['api_key'] and cfg['base_url'])


def public_config_document(document, current, environment):
    data = _object(current)
    updated = {'updated_at': _text(data, 'updated_at'), 'updated_by': _text(data, 'updated_by')}
    if document == 'mail':
        slots = _object(data.get('slots'))
        env = _mail(environment['mail'])
        views = {}
        for name in SLOTS:
            chosen, source = env, 'env' if _mail_ready(env) else 'none'
            candidates = [(name, 'ui')]
            if name == 'notification':
                candidates.append(('transactional', 'ui.transactional'))
            for key, candidate_source in candidates:
                candidate = _mail(slots.get(key))
                if candidate['enabled'] and _mail_ready(candidate):
                    chosen, source = candidate, candidate_source
                    break
            public = {key: chosen[key] for key in ('host', 'port', 'security', 'username', 'from_address', 'from_name', 'enabled')}
            public.update(slot=name, source=source, ready=_mail_ready(chosen), password_set=bool(chosen['password']),
                          password_mask=MASK if chosen['password'] else '')
            views[name] = public
        return dict(updated, slots=views, env_available=_mail_ready(env))
    if document != 'llm':
        raise ValueError('Unsupported configuration document')
    env = _llm(environment['llm'], environment['llm'])
    stored = _llm(data, environment['llm'])
    if data and stored['enabled'] and _llm_ready(stored):
        chosen, source = stored, 'ui'
    else:
        chosen, source = env, 'env' if _llm_ready(env) else 'none'
    models = data.get('models', [])
    if not isinstance(models, list) or any(not isinstance(item, str) for item in models):
        raise ValueError('Invalid cached model list')
    result = {key: chosen[key] for key in ('base_url', 'model', 'timeout', 'enabled')}
    result.update(updated, source=source, ready=_llm_ready(chosen), env_available=_llm_ready(env),
                  api_key_set=bool(chosen['api_key']), api_key_mask=MASK if chosen['api_key'] else '',
                  models=models[:200], models_fetched_at=_text(data, 'models_fetched_at'))
    return result


def private_config_document(document, current, environment, *, slot='transactional'):
    """Resolve the same source as the public view, including its private secret.

    Trusted provider callbacks only. Never return this object through HTTP or
    logging; storage/decryption errors must be handled before calling here.
    """
    data = _object(current)
    view = public_config_document(document, data, environment)
    if document == 'llm':
        result = {key: view[key] for key in ('base_url', 'model', 'timeout', 'enabled', 'ready', 'source')}
        origin = data if view['source'] == 'ui' else environment['llm']
        result['api_key'] = _text(origin, 'api_key')
        return result
    if slot not in SLOTS:
        raise ValueError('Invalid mail slot')
    public = view['slots'][slot]
    slots = _object(data.get('slots'))
    origin = (_object(slots.get(slot)) if public['source'] == 'ui' else
              _object(slots.get('transactional')) if public['source'] == 'ui.transactional' else environment['mail'])
    result = {key: public[key] for key in ('host', 'port', 'security', 'username', 'from_address',
                                         'from_name', 'enabled', 'ready', 'source', 'slot')}
    result['password'] = _text(origin, 'password')
    return result
