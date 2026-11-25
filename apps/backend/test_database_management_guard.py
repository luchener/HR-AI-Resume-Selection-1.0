"""The synthetic runner cannot exec/remove foreign or persistent containers."""
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


class ManagementGuardTests(TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[2] / 'scripts/database-test-management.py'
        spec = importlib.util.spec_from_file_location('management_acceptance_runner', path)
        cls.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner)

    def script(self, mode):
        with patch.object(self.runner.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'{}')) as run:
            self.runner.remote(mode, 'session-for-guard-test')
            return run.call_args.kwargs['input'].decode()

    def execute(self, script, mode, row, *, optimized=False):
        # The wrapper replaces subprocess locally, so the remote script never calls Docker.
        wrapper = '''import json,sys,types
row=json.loads(sys.argv[1]);script=sys.argv[2];mode=sys.argv[3]
calls=[]
def run(args,**kwargs):
 calls.append(args)
 if args[:2]==['docker','inspect']:return types.SimpleNamespace(returncode=0,stdout=json.dumps([row]).encode())
 return types.SimpleNamespace(returncode=0,stdout=b'')
sys.modules['subprocess']=types.SimpleNamespace(run=run)
sys.argv=['remote.py',mode,'session-for-guard-test','']
try:
 exec(compile(script,'remote.py','exec'))
except RuntimeError:
 print(json.dumps({'blocked':True,'calls':calls}));sys.exit(0)
except SystemExit:
 pass
print(json.dumps({'blocked':False,'calls':calls}))
'''
        command = [sys.executable] + (['-O'] if optimized else []) + ['-c', wrapper, json.dumps(row), script, mode]
        result = subprocess.run(command, capture_output=True, check=True)
        return json.loads(result.stdout.decode().splitlines()[-1])

    def fixture(self):
        return {'Id': 'a' * 64, 'Image': 'sha256:' + 'c' * 64,
            'Name': '/resume-db-review-test',
            'Config': {'Image': 'postgres:16-alpine', 'Labels': {
                'purpose': 'resume-account-adapter-review',
                'review-session': 'session-for-guard-test'}},
            'HostConfig': {'NetworkMode': 'bridge', 'Privileged': False,
                'PublishAllPorts': False,
                'Tmpfs': {'/var/lib/postgresql/data': 'rw,size=128m'},
                'PortBindings': {'5432/tcp': [
                    {'HostIp': '127.0.0.1', 'HostPort': '15439'}]}},
            'Mounts': [],
            'NetworkSettings': {'Ports': {'5432/tcp': [
                {'HostIp': '127.0.0.1', 'HostPort': '15439'}]}}}

    def test_wrong_identity_or_session_cannot_remove_container_even_under_optimization(self):
        script = self.script('remove')
        for optimized in (False, True):
            for change in ('name', 'label', 'session'):
                row = self.fixture()
                if change == 'name': row['Name'] = '/production'
                if change == 'label': row['Config']['Labels']['purpose'] = 'production'
                if change == 'session': row['Config']['Labels']['review-session'] = 'other'
                result = self.execute(script, 'remove', row, optimized=optimized)
                self.assertTrue(result['blocked'])
                self.assertEqual(len(result['calls']), 1)

    def test_persistent_mount_or_unsafe_network_prevents_execution(self):
        script = self.script('init')
        for change in ('bind', 'volume', 'host', 'privileged'):
            row = self.fixture()
            if change in ('bind', 'volume'): row['Mounts'] = [{'Type': change}]
            if change == 'host': row['HostConfig']['NetworkMode'] = 'host'
            if change == 'privileged': row['HostConfig']['Privileged'] = True
            result = self.execute(script, 'init', row, optimized=True)
            self.assertTrue(result['blocked'])
            self.assertEqual(len(result['calls']), 1)

    def test_owned_temporary_container_can_be_removed(self):
        result = self.execute(self.script('remove'), 'remove', self.fixture())
        self.assertFalse(result['blocked'])
        self.assertEqual(result['calls'][-1], ['docker', 'rm', '-f', 'resume-db-review-test'])

    def test_identity_requires_tmpfs_and_exact_loopback_binding(self):
        script = self.script('identity')
        self.assertFalse(self.execute(script, 'identity', self.fixture())['blocked'])
        for change in ('persistent', 'target', 'host', 'port'):
            row = self.fixture()
            if change == 'persistent':
                row['Mounts'].append({'Type': 'volume', 'Name': 'persistent'})
            if change == 'target':
                row['HostConfig']['Tmpfs'] = {'/tmp': 'rw,size=128m'}
            if change == 'host':
                row['HostConfig']['PortBindings']['5432/tcp'][0]['HostIp'] = '0.0.0.0'
            if change == 'port':
                row['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'] = '15440'
            self.assertTrue(self.execute(script, 'identity', row)['blocked'])

    def test_identity_rejects_extra_wildcard_ipv6_and_multiple_port_bindings(self):
        script = self.script('identity')
        variants = {}

        row = self.fixture()
        row['HostConfig']['PortBindings']['9999/tcp'] = [
            {'HostIp': '0.0.0.0', 'HostPort': '19999'}]
        row['NetworkSettings']['Ports']['9999/tcp'] = [
            {'HostIp': '0.0.0.0', 'HostPort': '19999'}]
        variants['extra_public_port'] = row

        row = self.fixture()
        row['HostConfig']['PortBindings']['9999/tcp'] = [
            {'HostIp': '127.0.0.1', 'HostPort': '19999'}]
        row['NetworkSettings']['Ports']['9999/tcp'] = [
            {'HostIp': '127.0.0.1', 'HostPort': '19999'}]
        variants['extra_loopback_port'] = row

        for host in ('0.0.0.0', '::'):
            row = self.fixture()
            row['HostConfig']['PortBindings']['5432/tcp'][0]['HostIp'] = host
            row['NetworkSettings']['Ports']['5432/tcp'][0]['HostIp'] = host
            variants['wildcard_' + host.replace(':', 'v')] = row

        row = self.fixture()
        row['HostConfig']['PortBindings']['5432/tcp'].append(
            {'HostIp': '::1', 'HostPort': '15439'})
        variants['multiple_host_bindings'] = row

        row = self.fixture()
        row['NetworkSettings']['Ports']['5432/tcp'].append(
            {'HostIp': '::1', 'HostPort': '15439'})
        variants['multiple_network_bindings'] = row

        row = self.fixture()
        row['HostConfig']['PublishAllPorts'] = True
        variants['publish_all_ports'] = row

        for label, row in variants.items():
            with self.subTest(label=label):
                self.assertTrue(self.execute(script, 'identity', row)['blocked'])

        row = self.fixture()
        row['HostConfig']['PortBindings']['9999/tcp'] = [
            {'HostIp': '0.0.0.0', 'HostPort': '19999'}]
        row['NetworkSettings']['Ports']['9999/tcp'] = [
            {'HostIp': '0.0.0.0', 'HostPort': '19999'}]
        for mode in ('init', 'remove'):
            with self.subTest(guard_mode=mode):
                result = self.execute(self.script(mode), mode, row)
                self.assertTrue(result['blocked'])
                self.assertEqual(result['calls'], [['docker', 'inspect', 'resume-db-review-test']])

    def test_loopback_port_probe_detects_active_listener(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(('127.0.0.1', 0))
            held.listen(1)
            port = held.getsockname()[1]
            self.assertFalse(self.runner.loopback_port_is_free(port, attempts=1))
        self.assertTrue(self.runner.loopback_port_is_free(port, attempts=20))
