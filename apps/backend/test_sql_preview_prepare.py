# -*- coding: utf-8 -*-
"""`sql-preview-cloud.py prepare` 的失败原子性：配置校验必须在任何持久写入之前。

R2 复核关注点：必填 Origin 的校验曾经排在创建 PRIVATE、originals 标识与 postgres.env
之后，漏配一次会留下半套私有状态，补齐后再跑会因为"状态已存在"被拒绝。

这里用临时目录 + 桩替代 Docker/socket，验证：
- 漏配、非法配置不产生任何目录或文件；
- 纠正配置后可以直接重试并成功；
- 写入阶段失败会清理本次创建的目录，可重试；
- 已存在的私有状态不会被触碰。
"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent


def load_prepare_module():
    path = ROOT / 'scripts' / 'sql-preview-cloud.py'
    spec = importlib.util.spec_from_file_location('sql_preview_cloud', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = load_prepare_module()
ORIGINAL_GUARD = M.guard_existing_state
ORIGINAL_CHECK_PORTS = M.check_ports


class PrepareAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.private = Path(self.temp.name) / 'private'
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(M, 'PRIVATE', self.private).start()
        mock.patch.object(M, 'configuration', lambda: ('abcdef123456', {})).start()
        mock.patch.object(M, 'guard_existing_state', lambda: None).start()
        mock.patch.object(M.os, 'chown', lambda *args: None, create=True).start()
        mock.patch.object(M, 'save', self.fake_save).start()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop('SQL_PREVIEW_TRUSTED_ORIGINS', None)

    @staticmethod
    def fake_save(path, value, *, owner=0):
        path = Path(path)
        text = value if isinstance(value, str) else json.dumps(value, indent=2)
        path.write_text(text, encoding='utf-8')

    def run_prepare(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            M.prepare()
        return buffer.getvalue()

    def test_missing_origin_leaves_no_state(self):
        with self.assertRaises(ValueError) as raised:
            self.run_prepare()
        self.assertIn('SQL_PREVIEW_TRUSTED_ORIGINS', str(raised.exception))
        self.assertFalse(self.private.exists())

    def test_invalid_origin_leaves_no_state(self):
        for value in ('https://www.luchenstudio.cn/path', '${PF_PREPARE_HOST}', 'null',
                      'https://${PF_PREPARE_HOST}'):
            os.environ['SQL_PREVIEW_TRUSTED_ORIGINS'] = value
            with self.assertRaises(ValueError, msg=value):
                self.run_prepare()
            self.assertFalse(self.private.exists(), value)

    def test_retry_after_fixing_configuration_succeeds(self):
        with self.assertRaises(ValueError):
            self.run_prepare()
        self.assertFalse(self.private.exists())

        os.environ['SQL_PREVIEW_TRUSTED_ORIGINS'] = ' HTTPS://WWW.LuchenStudio.CN/ ,http://127.0.0.1:13010 '
        output = self.run_prepare()
        self.assertIn('"prepared": true', output)
        self.assertTrue(self.private.is_dir())
        deployment = json.loads((self.private / 'deployment.json').read_text(encoding='utf-8'))
        self.assertEqual(deployment['trusted_origins'],
                         ['https://www.luchenstudio.cn', 'http://127.0.0.1:13010'])
        self.assertFalse((self.private / 'initialized.json').exists())

    def test_failure_during_write_cleans_up_created_directory(self):
        os.environ['SQL_PREVIEW_TRUSTED_ORIGINS'] = 'https://www.luchenstudio.cn'
        calls = {'count': 0}
        real_save = self.fake_save

        def failing_save(path, value, *, owner=0):
            calls['count'] += 1
            if calls['count'] == 2:
                raise OSError('disk full (simulated)')
            return real_save(path, value, owner=owner)

        with mock.patch.object(M, 'save', failing_save):
            with self.assertRaises(OSError):
                self.run_prepare()
        self.assertFalse(self.private.exists(), '本次创建的部分状态必须被清理')

        # 重试（不再失败）应当成功
        self.assertTrue(self.private.is_dir() is False)
        self.run_prepare()
        self.assertTrue(self.private.is_dir())

    def test_existing_state_is_never_touched(self):
        self.private.mkdir(parents=True)
        marker = self.private / 'keep.txt'
        marker.write_text('do not touch', encoding='utf-8')
        os.environ['SQL_PREVIEW_TRUSTED_ORIGINS'] = 'https://www.luchenstudio.cn'

        # 直接验证真实 guard：已存在状态必须拒绝，且不修改任何内容
        with mock.patch.object(M, 'guard_existing_state', ORIGINAL_GUARD), \
             mock.patch.object(M, 'check_ports', lambda: None), \
             mock.patch.object(M, 'run', lambda *args, **kwargs: ''):
            with self.assertRaises(ValueError) as raised:
                M.guard_existing_state()
        self.assertIn('Existing preview state refused', str(raised.exception))
        self.assertEqual(marker.read_text(encoding='utf-8'), 'do not touch')


class GuardExistingStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.private = Path(self.temp.name) / 'private'
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(M, 'PRIVATE', self.private).start()
        mock.patch.object(M, 'check_ports', lambda: None).start()

    def test_clean_state_passes(self):
        with mock.patch.object(M, 'run', lambda *args, **kwargs: ''):
            M.guard_existing_state()

    def test_existing_volumes_refused(self):
        with mock.patch.object(M, 'run',
                               lambda command, **kwargs: 'resume-sql-preview_sql-preview-postgres\n'):
            with self.assertRaisesRegex(ValueError, 'volumes refused'):
                M.guard_existing_state()

    def test_existing_containers_refused(self):
        def run(command, **kwargs):
            return '' if command[1] == 'volume' else 'abc123\n'
        with mock.patch.object(M, 'run', run):
            with self.assertRaisesRegex(ValueError, 'containers refused'):
                M.guard_existing_state()

    def test_port_check_binds_loopback(self):
        bound = []

        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def bind(self, address):
                bound.append(address)

        with mock.patch.object(M, 'socket') as fake_socket:
            fake_socket.socket.return_value = FakeSocket()
            ORIGINAL_CHECK_PORTS()
        self.assertEqual([address[1] for address in bound], [15442, 18010, 13010])
        self.assertTrue(all(address[0] == '127.0.0.1' for address in bound))


if __name__ == '__main__':
    unittest.main()
