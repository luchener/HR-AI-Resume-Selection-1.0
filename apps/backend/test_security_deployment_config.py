# -*- coding: utf-8 -*-
"""部署前配置检查：真实渲染、容器实际来源与严格 Origin 规则。

R2 复核关注点：
- `env_file` 会被 Compose 插值，自制解析器得到的不是最终值，可能误放行无效 Origin；
- SQL 后端的可信源在挂载的 `deployment.json` 里，不在环境变量里；
- 权威结果必须来自真实 `docker compose config`（不需要 daemon）。
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
KEY = 'CSRF_TRUSTED_ORIGINS'
DOCKER = shutil.which('docker')


def load_preflight():
    path = ROOT / 'scripts' / 'security-deployment-preflight.py'
    spec = importlib.util.spec_from_file_location('security_deployment_preflight', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PF = load_preflight()


def run_main(args):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = PF.main(args)
    return code, buffer.getvalue()


class ComposeSourceGuardTests(unittest.TestCase):
    """compose 文件本身不得用空值覆盖 env_file（回归护栏）。"""

    def compose_service(self, filename, service='backend'):
        yaml = __import__('yaml')
        data = yaml.safe_load((ROOT / filename).read_text(encoding='utf-8'))
        return data['services'][service]

    def test_secure_compose_keeps_value_in_env_file_only(self):
        service = self.compose_service('docker-compose.secure.yml')
        self.assertNotIn(KEY, service.get('environment') or {},
                         'compose environment 会用空值覆盖 env_file，必须只在 .env 配置')
        self.assertTrue(service.get('env_file'), '必须是 env_file 持久来源')

    def test_sql_preview_compose_does_not_interpolate_the_key(self):
        service = self.compose_service('docker-compose.sql-preview.yml')
        self.assertNotIn(KEY, service.get('environment') or {})

    def test_env_sample_documents_the_key(self):
        sample = (ROOT / 'apps' / 'backend' / '.env.sample').read_text(encoding='utf-8')
        self.assertIn(KEY, sample)
        self.assertIn('唯一持久配置来源', sample)


class TempComposeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding='utf-8')
        return path

    def compose(self, name, block):
        return self.write(name, 'services:\n  backend:\n    image: example/backend:test\n' + block)

    def env(self, value):
        return self.write('backend.env', KEY + '=' + value + '\nJWT_SECRET_KEY=synthetic\n')

    @staticmethod
    def env_file_block():
        return '    env_file:\n      - path: backend.env\n        required: true\n'


class AuthoritativeRenderingTests(TempComposeCase):
    """真实 `docker compose config` 是唯一权威结果。"""

    @unittest.skipUnless(DOCKER, 'docker compose CLI required (no daemon needed)')
    def test_env_file_literal_value_is_accepted(self):
        self.env('https://www.luchenstudio.cn')
        code, out = run_main(['--compose', str(self.compose('c1.yml', self.env_file_block()))])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)['rendered_by'], 'docker compose config')

    @unittest.skipUnless(DOCKER, 'docker compose CLI required (no daemon needed)')
    def test_env_file_interpolation_with_outer_value(self):
        """env_file 里的 ${...} 会被 Compose 插值，必须按渲染结果判定。"""
        self.env('https://${PF_R2_PUBLIC_HOST}')
        path = self.compose('c2.yml', self.env_file_block())
        os.environ['PF_R2_PUBLIC_HOST'] = 'www.luchenstudio.cn'
        try:
            code, out = run_main(['--compose', str(path)])
        finally:
            os.environ.pop('PF_R2_PUBLIC_HOST', None)
        self.assertEqual(code, 0, out)

    @unittest.skipUnless(DOCKER, 'docker compose CLI required (no daemon needed)')
    def test_env_file_interpolation_missing_value_is_rejected(self):
        """R2 复现：未展开的变量渲染成 `https://`，必须判为无效而不是放行。"""
        self.env('https://${PF_R2_MISSING_HOST}')
        path = self.compose('c3.yml', self.env_file_block())
        os.environ.pop('PF_R2_MISSING_HOST', None)
        code, out = run_main(['--compose', str(path)])
        self.assertEqual(code, 2, out)
        self.assertIn('invalid trusted origin', out)

    @unittest.skipUnless(DOCKER, 'docker compose CLI required (no daemon needed)')
    def test_quoted_env_file_values_are_rendered(self):
        for quoted in ('"https://www.luchenstudio.cn"', "'https://www.luchenstudio.cn'"):
            self.env(quoted)
            code, out = run_main(['--compose', str(self.compose('c4.yml', self.env_file_block()))])
            self.assertEqual(code, 0, (quoted, out))

    @unittest.skipUnless(DOCKER, 'docker compose CLI required (no daemon needed)')
    def test_environment_override_wins_and_empty_override_is_reported(self):
        self.env('https://www.luchenstudio.cn')
        path = self.compose('c5.yml', self.env_file_block()
                            + '    environment:\n      ' + KEY + ': ${' + KEY + ':-}\n')
        os.environ.pop(KEY, None)
        code, out = run_main(['--compose', str(path)])
        self.assertEqual(code, 2, out)
        self.assertIn('overrides env_file', out)

        os.environ[KEY] = 'https://outer.example'
        try:
            code, out = run_main(['--compose', str(path)])
        finally:
            os.environ.pop(KEY, None)
        self.assertEqual(code, 0, out)

    @unittest.skipUnless(DOCKER, 'docker compose CLI required (no daemon needed)')
    def test_invalid_rendered_value_is_rejected(self):
        for value in ('https://www.luchenstudio.cn/path', 'null', 'www.luchenstudio.cn',
                      'ftp://www.luchenstudio.cn', 'https://${PF_R2_UNSET}'):
            self.env(value)
            code, out = run_main(['--compose', str(self.compose('c6.yml', self.env_file_block()))])
            self.assertEqual(code, 2, (value, out))

    def test_missing_docker_cli_fails_closed(self):
        self.env('https://www.luchenstudio.cn')
        path = self.compose('c7.yml', self.env_file_block())
        original = PF.docker

        def unavailable(args, **kwargs):
            raise PF.ConfigError('docker command could not be executed')

        PF.docker = unavailable
        try:
            code, out = run_main(['--compose', str(path)])
        finally:
            PF.docker = original
        self.assertEqual(code, 2, out)
        self.assertFalse(json.loads(out)['ok'])


class SqlContainerSourceTests(unittest.TestCase):
    """SQL 可信源来自容器实际加载的 deployment.json。"""

    ORIGIN = 'http://127.0.0.1:13010'
    PATH = '/run/secrets/sql-deployment.json'

    def setUp(self):
        self.original = PF.docker
        self.addCleanup(self.restore)
        self.calls = []

    def restore(self):
        PF.docker = self.original

    def fake(self, *, inspect_env=None, exec_stdout=None, fail_exec=None, fail_inspect=False):
        def docker(args, timeout=60, environ=None):
            self.calls.append(list(args))
            if args[0] == 'inspect':
                if fail_inspect:
                    raise PF.ConfigError('container is not readable')
                return json.dumps([{'Config': {'Env': inspect_env if inspect_env is not None else [
                    'SQL_DEPLOYMENT_FILE=' + self.PATH,
                    'OTHER_SECRET=do-not-print-me',
                ]}}])
            if args[0] == 'exec':
                if fail_exec:
                    raise PF.ConfigError(fail_exec)
                return exec_stdout
            raise AssertionError('unexpected docker args: %r' % (args,))
        PF.docker = docker

    def test_reads_trusted_origins_from_mounted_file(self):
        self.fake(exec_stdout=json.dumps({'trusted_origins': [self.ORIGIN]}) + '\n')
        code, out = run_main(['--sql-container', 'preview-backend', '--expect', self.ORIGIN])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)['origins'], [self.ORIGIN])
        self.assertNotIn('do-not-print-me', out)
        exec_args = [call for call in self.calls if call[0] == 'exec'][0]
        self.assertIn(self.PATH, exec_args)

    def test_uses_configured_deployment_path(self):
        self.fake(inspect_env=['SQL_DEPLOYMENT_FILE=/mounted/other.json'],
                  exec_stdout=json.dumps({'trusted_origins': [self.ORIGIN]}))
        self.assertEqual(run_main(['--sql-container', 'preview-backend'])[0], 0)
        exec_args = [call for call in self.calls if call[0] == 'exec'][0]
        self.assertIn('/mounted/other.json', exec_args)

    def test_missing_key_is_rejected(self):
        self.fake(exec_stdout=json.dumps({'trusted_origins': None}))
        self.assertEqual(run_main(['--sql-container', 'preview-backend'])[0], 2)

    def test_string_value_is_rejected(self):
        self.fake(exec_stdout=json.dumps({'trusted_origins': self.ORIGIN}))
        self.assertEqual(run_main(['--sql-container', 'preview-backend'])[0], 2)

    def test_invalid_entry_is_rejected(self):
        self.fake(exec_stdout=json.dumps({'trusted_origins': [self.ORIGIN + '/path']}))
        self.assertEqual(run_main(['--sql-container', 'preview-backend'])[0], 2)

    def test_mismatch_returns_three(self):
        self.fake(exec_stdout=json.dumps({'trusted_origins': [self.ORIGIN]}))
        self.assertEqual(run_main(['--sql-container', 'preview-backend',
                                   '--expect', 'http://127.0.0.1:13011'])[0], 3)

    def test_unreadable_configuration_is_rejected(self):
        self.fake(fail_exec='container configuration is unreadable')
        self.assertEqual(run_main(['--sql-container', 'preview-backend'])[0], 2)

    def test_unreadable_container_is_rejected(self):
        self.fake(fail_inspect=True)
        self.assertEqual(run_main(['--sql-container', 'preview-backend'])[0], 2)


class LegacyContainerSourceTests(unittest.TestCase):
    """JSON 后端仍读环境变量；确认两种来源没有互相混淆。"""

    ORIGIN = 'https://www.luchenstudio.cn'

    def setUp(self):
        self.original = PF.docker
        self.addCleanup(self.restore)

    def restore(self):
        PF.docker = self.original

    def fake(self, env):
        PF.docker = lambda args, timeout=60, environ=None: json.dumps([{'Config': {'Env': env}}])

    def test_reads_environment_variable(self):
        self.fake([KEY + '=' + self.ORIGIN])
        code, out = run_main(['--container', 'backend', '--expect', self.ORIGIN])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)['source'], KEY)

    def test_missing_environment_value_is_rejected(self):
        self.fake(['OTHER=1'])
        self.assertEqual(run_main(['--container', 'backend'])[0], 2)

    def test_mismatch_returns_three(self):
        self.fake([KEY + '=' + self.ORIGIN])
        self.assertEqual(run_main(['--container', 'backend',
                                   '--expect', 'https://other.example'])[0], 3)


class DeploymentJsonTests(TempComposeCase):
    def write_json(self, payload):
        path = self.dir / 'deployment.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_valid_list_passes(self):
        self.assertEqual(run_main(['--deployment-json',
                                   str(self.write_json({'trusted_origins': ['https://www.luchenstudio.cn']}))])[0], 0)

    def test_string_is_rejected(self):
        path = self.write_json({'trusted_origins': 'https://www.luchenstudio.cn'})
        self.assertEqual(run_main(['--deployment-json', str(path)])[0], 2)

    def test_empty_and_missing_are_rejected(self):
        self.assertEqual(run_main(['--deployment-json', str(self.write_json({'trusted_origins': []}))])[0], 2)
        self.assertEqual(run_main(['--deployment-json', str(self.write_json({}))])[0], 2)

    def test_invalid_entry_rejected(self):
        path = self.write_json({'trusted_origins': ['https://www.luchenstudio.cn/sub']})
        self.assertEqual(run_main(['--deployment-json', str(path)])[0], 2)


class OriginValidationTests(unittest.TestCase):
    def test_normalises_and_deduplicates(self):
        values = PF.validate_origins(' HTTPS://WWW.LuchenStudio.CN/ , https://www.luchenstudio.cn ',
                                     source='test')
        self.assertEqual(values, ['https://www.luchenstudio.cn'])

    def test_multiple_origins_keep_order(self):
        values = PF.validate_origins('https://a.example,http://127.0.0.1:13010', source='test')
        self.assertEqual(values, ['https://a.example', 'http://127.0.0.1:13010'])

    def test_unexpanded_variable_is_not_a_valid_origin(self):
        """`https://${HOST}` 不能被当成合法 hostname。"""
        for value in ('https://${PF_HOST}', 'https://{}', 'https://$(hostname)'):
            with self.assertRaises(PF.ConfigError, msg=value):
                PF.validate_origins(value, source='test')

    def test_runtime_normalizer_matches(self):
        from prompt_safety import normalize_origin
        self.assertIsNone(normalize_origin('https://${PF_HOST}'))
        self.assertEqual(normalize_origin('https://www.luchenstudio.cn'), 'https://www.luchenstudio.cn')


if __name__ == '__main__':
    unittest.main()
