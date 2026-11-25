"""SQL-backed provider operations with explicit configuration and no retries.

Screening diagnostics log allowlisted metadata only. Business analysis/scoring
remains an explicitly supplied consumer of complete().
"""
from contextvars import ContextVar
from dataclasses import replace
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
import json
import hashlib
import logging
import re
from urllib.parse import urlsplit
from uuid import uuid4

from db_config_resolved import environment_snapshot, private_config_document
from db_provider_transport import http_client, resolve_host, resolve_https, smtp_client
from db_secrets import EncryptedConfigRepository


screening_task_id = ContextVar('screening_task_id', default=None)
SCREENING_THINKING_POLICY = 'official-deepseek-flash-fast-with-low-review-v2'
_logger = logging.getLogger('gunicorn.error')
# This deployment configures the root logger at WARNING; screening diagnostics are
# INFO and must stay visible in container logs.
_logger.setLevel(logging.INFO)


class ProviderUnavailable(ValueError):
    """Provider failure carrying a coarse, secret-free kind.

    `kind` lets callers make bounded decisions (e.g. retry once on output-length
    truncation) without ever inspecting model text. Unknown causes stay 'unknown'
    and must not be retried blindly.
    """

    KINDS = frozenset({'unknown', 'transport', 'http_status', 'timeout', 'config',
                       'finish_length', 'finish_other', 'empty_content', 'invalid_format',
                       'unexpected'})

    def __init__(self, kind='unknown'):
        self.kind = kind if kind in self.KINDS else 'unknown'
        super().__init__('Provider unavailable or configuration invalid')


def _screening_json_source(content):
    # Unwrap a complete JSON fence only. Never salvage a fragment of a report.
    fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", content.strip(), re.IGNORECASE | re.DOTALL)
    return fence.group(1) if fence else content


def _text(value, maximum=4096, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or (not empty and not value.strip())):
        raise ProviderUnavailable()
    return value.strip()


def _mailbox(value):
    value = _text(value, 320)
    if parseaddr(value)[1] != value or value.count('@') != 1:
        raise ProviderUnavailable()
    return value


class SQLProviderGateway:
    def __init__(self, sessions, cipher, *, environment=None, timeout_cap=300, mail_timeout=15):
        if (not callable(sessions) or type(timeout_cap) is not int or not 1 <= timeout_cap <= 3600
                or type(mail_timeout) is not int or not 1 <= mail_timeout <= 60):
            raise ValueError('Explicit sessions and bounded provider timeouts required')
        self.sessions, self.cipher = sessions, cipher
        self.environment = environment_snapshot(environment)
        self.timeout_cap, self.mail_timeout = timeout_cap, mail_timeout
        self._environment_generation = uuid4().hex

    def model_snapshot(self):
        """Return private model settings and a secret-free source revision.

        SQL revisions hash the ciphertext envelope. Explicit fallback generations
        are process-local opaque identifiers, never hashes of plaintext keys.
        """
        try:
            from sqlalchemy import select
            from db_models import SystemConfig
            with self.sessions() as session, session.begin():
                envelope = session.scalar(select(SystemConfig.value).where(SystemConfig.key == 'system_config'))
                documents = self.cipher.decrypt(envelope) if envelope is not None else {}
                current = (documents or {}).get('system_config/llm.json')
                cfg = private_config_document('llm', current, self.environment)
                uses_fallback = cfg['source'] != 'ui' or any(
                    not (current or {}).get(key) and self.environment['llm'].get(key)
                    for key in ('base_url', 'model'))
                material = {'envelope': envelope if cfg['source'] == 'ui' else None,
                            'fallback': self._environment_generation if uses_fallback else None,
                            'source': cfg['source']}
                revision = hashlib.sha256(json.dumps(material, sort_keys=True,
                    separators=(',', ':')).encode()).hexdigest()
            return cfg, revision
        except Exception:
            raise ProviderUnavailable() from None

    def configuration(self, document, *, slot='transactional'):
        """Read/decrypt afresh; DB/decryption failure never falls back to ENV."""
        try:
            with self.sessions() as session, session.begin():
                documents = EncryptedConfigRepository(session, self.cipher).get() or {}
                cfg = private_config_document(document, documents.get('system_config/' + document + '.json'),
                                              self.environment, slot=slot)
            return cfg
        except Exception:
            raise ProviderUnavailable() from None

    @staticmethod
    def validate_base_url(value):
        return resolve_https(value).url

    def _model_configuration(self, runtime_config):
        cfg = self.configuration('llm') if runtime_config is None else dict(runtime_config)
        key = _text(cfg.get('api_key') or cfg.get('apiKey'))
        url = _text(cfg.get('base_url') or cfg.get('baseUrl'), 500)
        model = _text(cfg.get('model'), 200)
        timeout = cfg.get('timeout', 300)
        if type(timeout) is not int or not 1 <= timeout <= 3600 or cfg.get('enabled', True) is not True:
            raise ProviderUnavailable()
        return key, resolve_https(url), model, min(timeout, self.timeout_cap)

    def _request(self, method, path, cfg, payload=None):
        try:
            key, endpoint, model, timeout = self._model_configuration(cfg)
            # Use a fresh pinned client per operation: no redirects, proxies,
            # global singleton, SDK retry, or transaction during network I/O.
            with http_client(endpoint, timeout=timeout) as client:
                response = client.request(method, endpoint.url + path,
                    headers={'Authorization': 'Bearer ' + key}, json=payload(model) if payload else None)
                response.raise_for_status()
                if not 200 <= response.status_code < 300:
                    raise ProviderUnavailable('http_status')
                return response.json()
        except ProviderUnavailable:
            raise
        except Exception as error:
            name = type(error).__name__
            kind = ('timeout' if 'Timeout' in name else
                    'http_status' if name == 'HTTPStatusError' else 'transport')
            raise ProviderUnavailable(kind) from None

    def complete(self, messages, *, runtime_config=None, max_tokens=4096, expect_json=False,
                 screening_call=None, screening_review=False, improvement_call=None):
        outcome = 'ok'
        reason = None
        completion_tokens = reasoning_tokens = content_chars = None
        try:
            if (type(max_tokens) is not int or not 1 <= max_tokens <= 32768 or type(expect_json) is not bool
                    or not isinstance(messages, list) or not 1 <= len(messages) <= 64
                    or type(screening_review) is not bool or (screening_review and screening_call is None)
                    or (improvement_call is not None and
                        (type(improvement_call) is not int or improvement_call not in (1, 2)
                         or screening_call is not None or expect_json))):
                raise ProviderUnavailable()
            for item in messages:
                if (type(item) is not dict or set(item) != {'role', 'content'}
                        or item['role'] not in {'system', 'user', 'assistant', 'developer'}
                        or not isinstance(item['content'], str)):
                    raise ProviderUnavailable()
            if len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) > 4 * 1024 * 1024:
                raise ProviderUnavailable()
            cfg = runtime_config or {}
            def payload(model):
                body = {'model': model, 'messages': messages, 'max_tokens': max_tokens, 'stream': False}
                if screening_call is not None:
                    # 筛选/复核是评分依据，固定温度 0，避免同一份简历重复分析分数漂移。
                    body['temperature'] = 0
                if expect_json:
                    body['response_format'] = {'type': 'json_object'}
                if ((screening_call is not None or improvement_call is not None)
                        and model in {'deepseek-v4-flash', 'deepseek-flash'}
                        and urlsplit(cfg.get('base_url') or cfg.get('baseUrl') or '').hostname == 'api.deepseek.com'):
                    body['thinking'] = {'type': 'enabled' if screening_review else 'disabled'}
                    if screening_review:
                        body['reasoning_effort'] = 'low'
                return body
            result = self._request('POST', '/chat/completions', runtime_config, payload)
            choice = result['choices'][0]
            reason = choice.get('finish_reason')
            if screening_call is not None or improvement_call is not None:
                usage = result.get('usage')
                if isinstance(usage, dict):
                    completion_tokens = usage.get('completion_tokens')
                    details = usage.get('completion_tokens_details')
                    if isinstance(details, dict):
                        reasoning_tokens = details.get('reasoning_tokens')
                message = choice.get('message')
                if isinstance(message, dict) and isinstance(message.get('content'), str):
                    content_chars = len(message['content'])
            if reason != 'stop':
                raise ProviderUnavailable('finish_length' if reason == 'length' else 'finish_other')
            content = choice['message']['content']
            if not isinstance(content, str) or not content:
                raise ProviderUnavailable('empty_content')
            if expect_json:
                try:
                    source = _screening_json_source(content) if screening_call is not None else content
                    value = json.loads(source, strict=screening_call is None)
                except (TypeError, ValueError):
                    raise ProviderUnavailable('invalid_format') from None
                if type(value) is not dict:
                    raise ProviderUnavailable('invalid_format')
                return value
            return content
        except ProviderUnavailable as error:
            outcome = error.kind
            raise
        except Exception:
            outcome = 'unexpected'
            raise ProviderUnavailable('unexpected') from None
        finally:
            if screening_call is not None or improvement_call is not None:
                stage, ordinal = screening_call if screening_call is not None else ('improvement', improvement_call)
                event = 'sql_screening_provider' if screening_call is not None else 'sql_improvement_provider'
                _logger.info('%s %s', event, json.dumps({
                    'task_id': str(screening_task_id.get()), 'stage': stage, 'call': ordinal,
                    'review': screening_review,
                    'max_tokens': max_tokens, 'outcome': outcome,
                    'finish_reason': reason if reason in ('stop', 'length') else
                        ('other' if reason is not None else None),
                    'completion_tokens': completion_tokens if type(completion_tokens) is int else None,
                    'reasoning_tokens': reasoning_tokens if type(reasoning_tokens) is int else None,
                    'content_chars': content_chars,
                }))

    def llm_probe(self, cfg):
        reply = self.complete([{'role': 'user', 'content': 'Reply with OK.'}],
                              runtime_config=cfg, max_tokens=16)
        return {'reply_chars': len(reply)}

    def models_probe(self, cfg):
        try:
            data = self._request('GET', '/models', cfg)['data']
            if not isinstance(data, list) or len(data) > 10000:
                raise ProviderUnavailable()
            models = [_text(item['id'], 200) for item in data]
            return list(dict.fromkeys(models))[:200]
        except Exception:
            raise ProviderUnavailable() from None

    def model_tester(self, *, runtime_config):
        self.llm_probe(runtime_config)
        return {}

    def _mail(self, cfg, message=None, recipient=None):
        client = None
        try:
            if cfg.get('enabled') is not True or cfg.get('ready') is not True:
                raise ProviderUnavailable()
            username = _text(cfg.get('username'), 320)
            _text(cfg.get('password'))
            password = cfg['password']
            sender = _mailbox(cfg.get('from_address') or username)
            if recipient is not None:
                recipient = _mailbox(recipient)
            endpoint = resolve_host(cfg.get('host'), cfg.get('port'))
            client = smtp_client(endpoint, security=cfg.get('security'), timeout=self.mail_timeout)
            client.login(username, password)
            if message is not None:
                refused = client.send_message(message, from_addr=sender, to_addrs=[recipient])
                if refused:
                    raise ProviderUnavailable()
        except Exception:
            raise ProviderUnavailable() from None
        finally:
            if client is not None:
                # A QUIT failure after accepted DATA must not mark delivery as
                # failed or invite a second send. No automatic send retry.
                try:
                    client.close()
                except Exception:
                    pass

    def send_notification(self, email, subject, plain, html, images=None, *, cfg=None):
        try:
            cfg = self.configuration('mail', slot='notification') if cfg is None else dict(cfg)
            sender = _mailbox(cfg.get('from_address') or cfg.get('username'))
            recipient = _mailbox(email)
            message = EmailMessage()
            message['Subject'] = _text(subject, 200)
            message['From'] = formataddr((_text(cfg.get('from_name', ''), 100, empty=True), sender))
            message['To'] = recipient
            if not isinstance(plain, str) or not isinstance(html, str):
                raise ProviderUnavailable()
            message.set_content(plain)
            message.add_alternative(html, subtype='html')
            if images:
                part = message.get_payload()[-1]
                for image in images:
                    identity = _text(image['image_id'], 128)
                    subtype = image['mime_subtype']
                    if subtype not in {'png', 'jpeg', 'gif', 'webp'} or type(image['content']) is not bytes:
                        raise ProviderUnavailable()
                    part.add_related(image['content'], maintype='image', subtype=subtype,
                                     cid='<img-' + identity + '>')
            self._mail(cfg, message, recipient)
        except Exception:
            raise ProviderUnavailable() from None

    def send_auth_mail(self, *, email, purpose, code='', reason='', expires_hours=24):
        subjects = {'register': '注册验证码', 'reset': '重置密码验证码', 'unfreeze': '账号解冻验证码',
                    'invite': '注册邀请码', 'invite_reject': '注册申请结果'}
        if purpose not in subjects:
            raise ProviderUnavailable()
        # Plain text is sufficient for transactional delivery; never interpolate
        # user-controlled reason/code into an HTML template.
        if purpose == 'invite_reject':
            text = '您的注册申请未通过。\n' + _text(reason, 2000)
        else:
            text = subjects[purpose] + '：' + _text(code, 128)
            if purpose == 'invite':
                if type(expires_hours) is not int or not 1 <= expires_hours <= 720:
                    raise ProviderUnavailable()
                text += '\n有效期：' + str(expires_hours) + ' 小时。'
        cfg = self.configuration('mail', slot='transactional')
        self.send_notification(email, subjects[purpose], text, '', cfg=cfg)

    def mail_probe(self, cfg, recipient):
        if not recipient:
            self._mail(cfg)
            return False
        self.send_notification(recipient, '邮件服务连接测试', '邮件服务连接成功。', '', cfg=cfg)
        return True


def provider_adapter_factory(business_builder, *, timeout_cap=300, mail_timeout=15):
    """Bind existing business callbacks to SQL providers in the owned session.

    business_builder(gateway) returns ApplicationAdapters. It supplies the local
    parsers, configuration merge rules and the unchanged analysis/scoring code;
    the latter calls gateway.complete() instead of the legacy global LLM client.
    """
    if not callable(business_builder):
        raise TypeError('Explicit business adapter builder required')
    def build(sessions, cipher, environment):
        from db_application import ApplicationAdapters
        gateway = SQLProviderGateway(sessions, cipher, environment=environment,
                                     timeout_cap=timeout_cap, mail_timeout=mail_timeout)
        business = business_builder(gateway)
        if not isinstance(business, ApplicationAdapters):
            raise TypeError('Business builder must return ApplicationAdapters')
        return replace(business, send_auth_mail=gateway.send_auth_mail,
            send_notification=gateway.send_notification, validate_base_url=gateway.validate_base_url,
            llm_probe=gateway.llm_probe, models_probe=gateway.models_probe,
            mail_probe=gateway.mail_probe, model_tester=gateway.model_tester)
    return build
