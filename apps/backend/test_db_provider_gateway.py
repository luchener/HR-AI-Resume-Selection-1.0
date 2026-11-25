"""No real vendor calls: guarded DNS/HTTP/SMTP contracts and encrypted SQL."""
import json
import os
import socket
import unittest
from unittest.mock import Mock, patch

import httpcore
import httpx

from db_config_resolved import environment_snapshot, private_config_document, public_config_document
from db_provider_gateway import ProviderUnavailable, SQLProviderGateway, provider_adapter_factory
from db_provider_transport import (EndpointError, PinnedBackend, http_client,
                                   resolve_host, resolve_https, smtp_client)


def answer(address='93.184.216.34'):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, 443))


def llm_cfg(**changes):
    return dict(api_key='SyntheticProviderKey', base_url='https://api.example.test/v1',
                model='synthetic-model', timeout=20, enabled=True, **changes)


def mail_cfg(**changes):
    cfg = dict(host='smtp.example.test', port=465, security='ssl', username='sender@example.test',
               password=' Synthetic Mail Password ', from_address='sender@example.test', from_name='Synthetic',
               enabled=True, ready=True)
    return cfg | changes


class ProviderBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.gateway = SQLProviderGateway(Mock(), Mock())
        self.dns = patch('db_provider_transport.socket.getaddrinfo', return_value=[answer()])
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def test_private_resolution_matches_public_source_and_slot_fallback(self):
        env = environment_snapshot({'llm': {'api_key': 'EnvKey', 'base_url': 'https://api.example.test'},
                                    'mail': mail_cfg()})
        for document, current in (
                ('llm', None), ('llm', dict(api_key='SQLKey', base_url='https://ui.example.test')),
                ('llm', dict(api_key='Disabled', enabled=False)),
                ('mail', None), ('mail', {'slots': {'transactional': mail_cfg(password='SQLPassword')}})):
            for slot in ('transactional', 'notification'):
                private = private_config_document(document, current, env, slot=slot)
                public = public_config_document(document, current, env)
                view = public if document == 'llm' else public['slots'][slot]
                self.assertEqual(private['source'], view['source'])
                self.assertEqual(private['ready'], view['ready'])
                self.assertNotIn(private.get('api_key', private.get('password')), str(public))
        self.assertEqual(private_config_document('mail', {'slots': {'transactional': mail_cfg(password='SQLPassword')}},
            env, slot='notification')['password'], 'SQLPassword')

    def test_invalid_and_nonpublic_destinations_are_rejected(self):
        for url in ('http://api.example.test', 'https://user:pass@api.example.test',
                    'https://api.example.test?key=x', 'https://api.example.test#fragment',
                    'https://api.example.test\\other', 'https://api.example.test:0',
                    'https://127.0.0.1', 'https://169.254.169.254', 'https://[::1]',
                    'https://[::ffff:127.0.0.1]', 'https://[64:ff9b::7f00:1]',
                    'https://224.0.0.1', 'https://[ff02::1]'):
            with self.subTest(url=url), self.assertRaises(EndpointError):
                resolve_https(url)
        for address in ('10.0.0.1', '127.0.0.1', '224.0.0.1', '::ffff:224.0.0.1'):
            with patch('db_provider_transport.socket.getaddrinfo', return_value=[answer(), answer(address)]), \
                 self.assertRaises(EndpointError):
                resolve_https('https://api.example.test')

    def test_dns_is_resolved_fresh_and_pinned_backend_rejects_origin_changes(self):
        endpoint = resolve_https('https://api.example.test/v1/')
        self.assertEqual(endpoint.url, 'https://api.example.test/v1')
        ipv6 = resolve_https('https://[2001:4860:4860:0:0:0:0:8888]/v1')
        self.assertEqual(ipv6.host, '2001:4860:4860::8888')
        backend = PinnedBackend(endpoint)
        with patch.object(httpcore.SyncBackend, 'connect_tcp', return_value='socket') as connect:
            self.assertEqual(backend.connect_tcp(endpoint.host, 443, timeout=5), 'socket')
            self.assertEqual(connect.call_args.args[0], '93.184.216.34')
            for host, port in (('other.example.test', 443), (endpoint.host, 444)):
                with self.assertRaises(httpcore.ConnectError):
                    backend.connect_tcp(host, port)
        with patch('db_provider_transport.socket.getaddrinfo', return_value=[answer('127.0.0.1')]), \
             self.assertRaises(EndpointError):
            resolve_https('https://api.example.test')

    def test_http_client_disables_proxy_redirect_and_transport_retries(self):
        endpoint = resolve_https('https://api.example.test')
        with patch.dict(os.environ, HTTPS_PROXY='http://127.0.0.1:9'):
            with http_client(endpoint, timeout=12) as client:
                self.assertFalse(client.trust_env)
                self.assertFalse(client.follow_redirects)
                self.assertEqual(client.timeout.read, 12)
                self.assertIsInstance(client._transport._pool._network_backend, PinnedBackend)

    def test_model_protocol_has_one_request_closes_client_and_caps_timeout(self):
        requests, clients = [], []
        def build(endpoint, *, timeout):
            self.assertEqual(timeout, 300)
            def handle(request):
                requests.append(request)
                self.assertEqual(request.headers['Authorization'], 'Bearer SyntheticProviderKey')
                return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                    'message': {'content': '{"ok":true}'}}]})
            client = httpx.Client(transport=httpx.MockTransport(handle))
            clients.append(client)
            return client
        with patch('db_provider_gateway.http_client', side_effect=build):
            result = self.gateway.complete([{'role': 'user', 'content': 'JSON synthetic'}],
                runtime_config=llm_cfg() | {'timeout': 3600}, expect_json=True)
        self.assertEqual(result, {'ok': True})
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), 'https://api.example.test/v1/chat/completions')
        self.assertEqual(json.loads(requests[0].content)['response_format'], {'type': 'json_object'})
        self.assertTrue(clients[0].is_closed)

    def test_provider_errors_redirects_timeouts_and_truncated_outputs_never_retry_or_leak(self):
        for outcome in ('timeout', 302, 429, 500, 'length', 'bad_json'):
            calls = []
            def handle(request):
                calls.append(request)
                if outcome == 'timeout':
                    raise httpx.ReadTimeout('SyntheticProviderKey recipient@example.test')
                if isinstance(outcome, int):
                    return httpx.Response(outcome, headers={'location': 'http://127.0.0.1'}, text='SyntheticProviderKey')
                return httpx.Response(200, json={'choices': [{'finish_reason': outcome if outcome == 'length' else 'stop',
                    'message': {'content': 'not-json SyntheticProviderKey'}}]})
            with self.subTest(outcome=outcome), patch('db_provider_gateway.http_client',
                    side_effect=lambda *args, **kwargs: httpx.Client(transport=httpx.MockTransport(handle))):
                with self.assertRaises(ProviderUnavailable) as caught:
                    self.gateway.complete([{'role': 'user', 'content': 'synthetic'}], runtime_config=llm_cfg(), expect_json=True)
                self.assertNotIn('SyntheticProviderKey', str(caught.exception))
                self.assertEqual(len(calls), 1)

    def test_only_official_deepseek_flash_screening_disables_thinking(self):
        def complete(method, path, cfg, payload):
            body = payload(cfg['model'])
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(body)}}]}

        for url, model, screening_call, expected in (
            ('https://api.deepseek.com/v1', 'deepseek-v4-flash', ('generating', 2), {'type': 'disabled'}),
            ('https://api.deepseek.com', 'deepseek-flash', ('planning', 1), {'type': 'disabled'}),
            ('https://api.deepseek.com', 'deepseek-v4-pro', ('generating', 2), None),
            ('https://api.deepseek.com', 'deepseek-v4-flash', None, None),
            ('https://api.deepseek.com.example.test', 'deepseek-v4-flash', ('generating', 2), None),
        ):
            with self.subTest(url=url, model=model, screening_call=screening_call), \
                    patch.object(self.gateway, '_request', side_effect=complete):
                result = self.gateway.complete([{'role': 'user', 'content': 'synthetic'}],
                    runtime_config={'base_url': url, 'model': model}, expect_json=True,
                    max_tokens=8192, screening_call=screening_call)
                self.assertEqual(result.get('thinking'), expected)
                self.assertEqual(result['response_format'], {'type': 'json_object'})
                self.assertEqual(result['max_tokens'], 8192)

    def test_improvement_sends_official_flash_thinking_disabled_only(self):
        def complete(method, path, cfg, payload):
            return {'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps(payload(cfg['model']))}}]}

        for host, expected in [('api.deepseek.com', {'type': 'disabled'}), ('example.test', None)]:
            with self.subTest(host=host), patch.object(self.gateway, '_request', side_effect=complete):
                output = self.gateway.complete([{'role': 'user', 'content': 'synthetic'}],
                    runtime_config={'base_url': f'https://{host}/v1', 'model': 'deepseek-v4-flash'},
                    improvement_call=1)
                body = json.loads(output)
                self.assertEqual(body.get('thinking'), expected)
                self.assertNotIn('response_format', body)

    def test_screening_diagnostics_log_truncation_metadata_without_response_text(self):
        from db_provider_gateway import screening_task_id

        task_token = screening_task_id.set('synthetic-task')
        try:
            with patch.object(self.gateway, '_request', return_value={
                'choices': [{'finish_reason': 'length', 'message': {'content': 'private-output'}}],
                'usage': {'completion_tokens': 6500, 'completion_tokens_details': {'reasoning_tokens': 6400}},
            }), self.assertLogs('gunicorn.error', level='INFO') as logs:
                with self.assertRaises(ProviderUnavailable) as caught:
                    self.gateway.complete([{'role': 'user', 'content': 'private-prompt'}],
                        max_tokens=6500, expect_json=True, screening_call=('generating', 2))
            self.assertEqual(caught.exception.kind, 'finish_length')
            self.assertEqual(json.loads(logs.output[0].split('sql_screening_provider ', 1)[1]), {
                'task_id': 'synthetic-task', 'stage': 'generating', 'call': 2, 'max_tokens': 6500,
                'review': False,
                'outcome': 'finish_length', 'finish_reason': 'length', 'completion_tokens': 6500,
                'reasoning_tokens': 6400, 'content_chars': len('private-output'),
            })
        finally:
            screening_task_id.reset(task_token)

    def test_official_flash_review_uses_low_thinking_and_keeps_other_providers_compatible(self):
        def complete(method, path, cfg, payload):
            return {'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps(payload(cfg['model']))}}]}

        for url, expected in (('https://api.deepseek.com/v1', True), ('https://api.example.test/v1', False)):
            with self.subTest(url=url), patch.object(self.gateway, '_request', side_effect=complete):
                result = self.gateway.complete([{'role': 'user', 'content': 'synthetic'}],
                    runtime_config={'base_url': url, 'model': 'deepseek-v4-flash'},
                    expect_json=True, screening_call=('generating', 3), screening_review=True)
                self.assertEqual(result.get('thinking'), {'type': 'enabled'} if expected else None)
                self.assertEqual(result.get('reasoning_effort'), 'low' if expected else None)

    def test_mail_requires_tls_pins_addresses_and_validates_certificate_hostname(self):
        endpoint = resolve_host('smtp.example.test', 465)
        context, raw = Mock(), Mock()
        with patch('db_provider_transport.ssl.create_default_context', return_value=context), \
             patch('db_provider_transport.socket.create_connection', return_value=raw) as connect, \
             patch('smtplib.SMTP.getreply', return_value=(220, b'Synthetic SMTP')), \
             patch('smtplib.SMTP.ehlo', return_value=(250, b'OK')):
            client = smtp_client(endpoint, security='ssl', timeout=15)
            connect.assert_called_once_with(('93.184.216.34', 465), timeout=15)
            context.wrap_socket.assert_called_once_with(raw, server_hostname='smtp.example.test')
            client.close()
        with self.assertRaises(EndpointError):
            smtp_client(endpoint, security='plain', timeout=15)

    def test_mail_login_send_and_cleanup_are_single_and_secret_safe(self):
        client = Mock()
        client.send_message.return_value = {}
        client.close.side_effect = RuntimeError('ambiguous QUIT password')
        with patch('db_provider_gateway.smtp_client', return_value=client):
            self.gateway.send_notification('recipient@example.test', 'Synthetic', 'plain', '<b>HTML</b>',
                [{'image_id': 'id1', 'content': b'png', 'mime_subtype': 'png'}], cfg=mail_cfg())
        client.login.assert_called_once_with('sender@example.test', ' Synthetic Mail Password ')
        client.send_message.assert_called_once()
        message = client.send_message.call_args.args[0]
        self.assertIn('<img-id1>', message.as_string())
        self.assertNotIn('Password', message.as_string())
        self.assertEqual(client.send_message.call_args.kwargs['to_addrs'], ['recipient@example.test'])

    def test_mail_error_or_invalid_header_does_not_retry(self):
        client = Mock()
        client.send_message.side_effect = RuntimeError('PrivateMailPassword')
        with patch('db_provider_gateway.smtp_client', return_value=client) as connect:
            with self.assertRaises(ProviderUnavailable) as caught:
                self.gateway.send_notification('recipient@example.test', 'Synthetic', '', '', cfg=mail_cfg())
            self.assertNotIn('PrivateMailPassword', str(caught.exception))
            self.assertEqual(client.send_message.call_count, 1)
            with self.assertRaises(ProviderUnavailable):
                self.gateway.send_notification('recipient@example.test', 'Subject\r\nBcc: bad@example.test', '', '', cfg=mail_cfg())
            self.assertEqual(connect.call_count, 1)

    def test_all_mail_purposes_and_probe_only_delivery(self):
        with patch.object(self.gateway, 'configuration', return_value=mail_cfg()), \
             patch.object(self.gateway, 'send_notification') as send:
            for purpose in ('register', 'reset', 'unfreeze', 'invite', 'invite_reject'):
                self.gateway.send_auth_mail(email='recipient@example.test', purpose=purpose, code='123456', reason='Synthetic')
            self.assertEqual(send.call_count, 5)
        with patch.object(self.gateway, '_mail') as connect, patch.object(self.gateway, 'send_notification') as send:
            self.assertFalse(self.gateway.mail_probe(mail_cfg(), ''))
            send.assert_not_called()
            connect.assert_called_once()

    def test_invalid_bounds_and_model_settings_fail_before_connecting(self):
        for bounds in ({'timeout_cap': True}, {'timeout_cap': 0}, {'mail_timeout': 61}):
            with self.assertRaises(ValueError):
                SQLProviderGateway(Mock(), Mock(), **bounds)
        for cfg in (llm_cfg() | {'timeout': True}, llm_cfg() | {'enabled': False},
                    llm_cfg() | {'api_key': 'key\r\nX: y'}, llm_cfg() | {'model': ''}):
            with patch('db_provider_gateway.http_client') as connect, self.assertRaises(ProviderUnavailable):
                self.gateway.llm_probe(cfg)
            connect.assert_not_called()

    def test_invalid_application_provider_factory_cleans_engine_before_workers_start(self):
        from db_application import create_application
        from db_original_objects import OriginalObjects
        from test_db_application import settings
        engine = Mock()
        with patch('db_application.build_runtime', return_value=(engine, Mock())), \
             patch('db_application.verify_runtime', return_value=True), \
             patch('db_analysis_workflow.AnalysisWorkflow') as worker, self.assertRaises(TypeError):
            create_application(settings(), lambda *args: object(), Mock(spec=OriginalObjects))
        engine.dispose.assert_called_once()
        worker.assert_not_called()


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class ProviderGatewayLiveTests(unittest.TestCase):
    def setUp(self):
        from test_db_application import ApplicationLiveTests
        ApplicationLiveTests.setUp(self)

    def finish(self):
        from test_db_application import ApplicationLiveTests
        ApplicationLiveTests.finish(self)

    def upload(self):
        from test_db_application import ApplicationLiveTests
        return ApplicationLiveTests.upload(self)

    def gateway(self):
        from db_secrets import ConfigCipher
        return SQLProviderGateway(self.application.sessions,
            ConfigCipher(self.config.config_key, key_id=self.config.config_key_id),
            environment={'llm': llm_cfg(), 'mail': mail_cfg()})

    def test_sql_configuration_refresh_and_corruption_fail_closed(self):
        import sqlalchemy as sa
        from db_models import SystemConfig
        gateway = self.gateway()
        self.assertEqual(gateway.configuration('llm')['api_key'], 'SyntheticProviderKey')
        for key in ('SQLKey1', 'SQLKey2'):
            with gateway.sessions() as session, session.begin():
                from db_secrets import EncryptedConfigRepository
                EncryptedConfigRepository(session, gateway.cipher).put({'system_config/llm.json': llm_cfg() | {'api_key': key}})
            self.assertEqual(gateway.configuration('llm')['api_key'], key)
        with gateway.sessions() as session, session.begin():
            session.execute(sa.update(SystemConfig).where(SystemConfig.key == 'system_config').values(value={'corrupt': True}))
        with self.assertRaises(ProviderUnavailable):
            gateway.configuration('llm')

    def test_provider_factory_binds_owned_sessions_and_runs_analysis_through_gateway(self):
        from dataclasses import replace
        from db_application import create_application
        from db_secrets import EncryptedConfigRepository
        from test_db_content_live import analysis_report
        import sqlalchemy as sa
        captured, requests = [], []
        def business(gateway):
            captured.append(gateway)
            def analyze(*, resume, job):
                return gateway.complete([{'role': 'user', 'content': resume['content'] + job['content']}], expect_json=True)
            return replace(self.callbacks, analyze=analyze)
        self.assertTrue(self.application.close()['closed'])
        self.application = create_application(self.config, provider_adapter_factory(business), self.objects)
        self.client = self.application.app.test_client()
        gateway = captured[0]
        self.assertIs(gateway.sessions, self.application.sessions)
        with gateway.sessions() as session, session.begin():
            EncryptedConfigRepository(session, gateway.cipher).put({'system_config/llm.json': llm_cfg(),
                'system_config/mail.json': {'slots': {'transactional': mail_cfg()}}})
        real_dns = socket.getaddrinfo
        def dns(host, *args, **kwargs):
            return [answer()] if host in {'api.example.test', 'smtp.example.test'} else real_dns(host, *args, **kwargs)
        def build(endpoint, *, timeout):
            # All private snapshots have committed before provider work.
            with gateway.sessions() as session, session.begin():
                idle = session.scalar(sa.text("SELECT count(*) FROM pg_stat_activity WHERE usename='resume_app' AND state='idle in transaction'"))
                self.assertEqual(idle, 0)
            def handle(request):
                requests.append(request)
                if request.url.path.endswith('/models'):
                    return httpx.Response(200, json={'data': [{'id': 'one'}, {'id': 'one'}]})
                return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                    'message': {'content': json.dumps(analysis_report())}}]})
            return httpx.Client(transport=httpx.MockTransport(handle))
        with patch('db_provider_transport.socket.getaddrinfo', side_effect=dns), \
             patch('db_provider_gateway.http_client', side_effect=build):
            resume, job = self.upload()
            response = self.client.post('/api/v1/resumes/hr-analysis', headers={
                **self.headers, 'Idempotency-Key': str(__import__('uuid').uuid4())},
                json={'resume_ids': [resume], 'job_id': job})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['data']['status'], 'completed')
            self.assertEqual(len(requests), 1)
            models = self.client.post('/api/v1/admin/system/llm/models', headers=self.headers, json={})
            self.assertEqual(models.status_code, 200, models.get_json())
            self.assertEqual(models.get_json()['data']['models'], ['one'])
            self.assertNotIn('SyntheticProviderKey', models.get_data(as_text=True))
        smtp = Mock()
        smtp.send_message.return_value = {}
        with patch('db_provider_transport.socket.getaddrinfo', side_effect=dns), \
             patch('db_provider_gateway.smtp_client', return_value=smtp):
            gateway.send_auth_mail(email='recipient@example.test', code='123456', purpose='register')
        self.assertEqual(smtp.send_message.call_count, 1)


if __name__ == '__main__':
    unittest.main()
