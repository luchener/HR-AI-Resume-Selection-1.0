"""Explicit isolated cloud preview factory. Never imports the JSON application."""
import json
import os
from pathlib import Path
import re
import stat

from sqlalchemy.engine import make_url

from db_application import ApplicationAdapters, ApplicationSettings, create_application
from db_original_objects import OriginalObjects
from db_screening import SQLScreeningAnalyzer, SQLResumeImprover
from db_search_provider import SQLSearchProvider
from db_provider_gateway import provider_adapter_factory
import prompt_safety


def load_configuration(filename):
    """Read one operator-created private file; never create keys or a store."""
    path = Path(filename)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('An absolute private deployment file is required')
    with path.open('rb') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536
                or (os.name != 'nt' and (stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_uid not in {0, os.geteuid()}))):
            raise ValueError('Deployment file must be private and bounded')
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('Duplicate deployment setting')
                value[key] = item
            return value
        data = json.loads(handle.read(65537), object_pairs_hook=unique)
    required = {'scope', 'database_url', 'jwt_secret', 'auth_secret', 'config_key',
                'config_key_id', 'invite_key', 'admin_emails', 'originals_root', 'storage_id'}
    if (type(data) is not dict or not required <= data.keys()
            or data.keys() - required - {'environment', 'captcha_enabled', 'max_workers', 'max_pending',
                                          'search', 'trusted_origins'}
            or data['scope'] != 'isolated-sql-preview'):
        raise ValueError('Explicit isolated deployment settings required')
    url = make_url(data['database_url'])
    if (url.username != 'resume_app' or not url.password or not url.host or url.query
            or url.drivername != 'postgresql+psycopg'
            or not re.fullmatch(r'resume_sql_preview_[a-z0-9_]{1,40}', url.database or '')):
        raise ValueError('Only a dedicated SQL preview database is accepted')
    root = Path(data['originals_root'])
    if not root.is_absolute():
        raise ValueError('An absolute persistent originals directory is required')
    for key in ('auth_secret', 'config_key', 'invite_key'):
        if not isinstance(data[key], str):
            raise ValueError('Explicit encoded deployment key required')
    # 可信外部 Origin 必须显式给出：Next.js rewrite 的 changeOrigin 会把上游 Host 改成
    # 后端地址，不能用 Host 推断同源。优先取部署文件，其次取容器环境变量。
    raw_origins = data.get('trusted_origins')
    if raw_origins is None:
        raw_origins = [item for item in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if item.strip()]
    if isinstance(raw_origins, (str, bytes)) or not isinstance(raw_origins, (list, tuple)):
        raise ValueError('Trusted origins must be an explicit list')
    origins = []
    for entry in raw_origins:
        origin = prompt_safety.normalize_origin(entry)
        if origin is None:
            raise ValueError('Invalid trusted origin')
        origins.append(origin)
    settings = ApplicationSettings(database_url=data['database_url'], jwt_secret=data['jwt_secret'],
        auth_secret=data['auth_secret'].encode('utf-8'), config_key=data['config_key'].encode('ascii'),
        config_key_id=data['config_key_id'], invite_key=data['invite_key'].encode('ascii'),
        admin_emails=data['admin_emails'], captcha_enabled=data.get('captcha_enabled', True),
        max_workers=data.get('max_workers', 1), max_pending=data.get('max_pending', 4),
        trusted_origins=tuple(dict.fromkeys(origins)))
    search = data.get('search', {})
    SQLSearchProvider(search)  # Validate without DNS or external requests.
    return settings, OriginalObjects(root, storage_id=data['storage_id']), data.get('environment', {}), search


def business_adapters(gateway, *, search_config=None):
    from config_rules import merge_llm, merge_mail
    from parser import extract_text_from_file, summarize_job_locally

    def merge(document, current, incoming):
        if document == 'mail':
            result = merge_mail(current, incoming)
            # The SQL provider requires TLS, including when settings are saved.
            if any(slot.get('security') == 'plain' for slot in result.get('slots', {}).values()):
                raise ValueError('TLS mail transport required')
            return result
        if document == 'llm':
            return merge_llm(current, incoming, validate_base_url=gateway.validate_base_url)
        raise ValueError('Unknown configuration document')

    return ApplicationAdapters(extract_text=extract_text_from_file, summarize_job=summarize_job_locally,
        analyze=SQLScreeningAnalyzer(gateway, search_provider=SQLSearchProvider(search_config)), send_auth_mail=gateway.send_auth_mail,
        send_notification=gateway.send_notification,
        config_validator=lambda document, incoming: merge(document, {}, incoming), config_merger=merge,
        validate_base_url=gateway.validate_base_url, llm_probe=gateway.llm_probe,
        models_probe=gateway.models_probe, mail_probe=gateway.mail_probe,
        model_tester=gateway.model_tester, improver=SQLResumeImprover(gateway))


def build(filename):
    settings, originals, environment, search = load_configuration(filename)
    return create_application(settings, provider_adapter_factory(lambda gateway: business_adapters(gateway, search_config=search)), originals,
                              environment=environment)


def wsgi():
    """Gunicorn calls the factory inside each worker, with preload disabled."""
    try:
        filename = os.environ.get('SQL_DEPLOYMENT_FILE', '')
        if not filename:
            raise ValueError('Explicit SQL deployment file required')
        return build(filename).app
    except Exception:
        # No DSN/key/parser/provider exception can escape into startup logs.
        raise RuntimeError('SQL preview startup checks failed') from None
