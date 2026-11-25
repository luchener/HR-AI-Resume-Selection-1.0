"""Frozen-copy gates: real registry, private synthetic files, no Docker/SSH."""
import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from uuid import uuid4

from source_drain import Coordinator


def _review_temp():
    root = os.environ.get('DB_REVIEW_OBJECTS_ROOT')
    if not root:
        return tempfile.TemporaryDirectory()
    path = Path(root) / ('drain-fixture-' + uuid4().hex)
    path.mkdir(parents=False, exist_ok=False)
    return type('FixtureDirectory', (), {
        'name': str(path),
        'cleanup': lambda instance: shutil.rmtree(path, ignore_errors=True),
    })()


class FrozenCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=_review_temp()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/'source';self.source.mkdir()
        self.output=self.root/'output';self.output.mkdir()
        self.file=self.source/'synthetic.json';self.file.write_text('{"synthetic":true}')
        self.registry=self.root/'control.sqlite'
        self.control=Coordinator.initialize(self.registry)
        token=self.control.process_started('master');self.control.process_exited(token)
        self.control.begin()
        tree=ast.parse((Path(__file__).resolve().parents[2]/'scripts/database-production-drain.py').read_text(encoding='utf-8'))
        self.code=next(ast.literal_eval(node.value) for node in tree.body
                       if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name)
                       and node.targets[0].id=='COPY_SOURCE')
        self.code=self.code.replace("Path('/source')",'Path('+repr(str(self.source))+')')
        self.code=self.code.replace("Path('/output/snapshot')",'Path('+repr(str(self.output/'snapshot'))+')')
        self.code=self.code.replace("Path('/output/source-manifest.json')",'Path('+repr(str(self.output/'source-manifest.json'))+')')
        self.code=self.code.replace("Coordinator('/app/logs/source-drain/control.sqlite')",'Coordinator('+repr(str(self.registry))+')')

    def execute(self):
        output=io.StringIO()
        with redirect_stdout(output):exec(compile(self.code,'frozen-copy','exec'),{})
        return json.loads(output.getvalue())

    def test_copy_matches_source_and_keeps_same_closed_epoch(self):
        summary=self.execute()
        self.assertTrue(summary['source_before_after_and_copy_match'])
        self.assertEqual(summary['epoch'],self.control.status()['epoch'])
        self.assertEqual((self.output/'snapshot/synthetic.json').read_bytes(),self.file.read_bytes())
        self.assertTrue((self.output/'source-manifest.json').is_file())

    def test_active_worker_blocks_certificate(self):
        self.control.resume();self.control.enter('workers');self.control.begin()
        with self.assertRaises(AssertionError):self.execute()
        self.assertFalse((self.output/'source-manifest.json').exists())

    def test_source_changed_during_copy_is_not_certified(self):
        copy=shutil.copyfileobj
        def changed(*args,**kwargs):
            result=copy(*args,**kwargs);self.file.write_text('{"synthetic":"changed"}');return result
        with patch('shutil.copyfileobj',side_effect=changed),self.assertRaises(AssertionError):self.execute()
        self.assertFalse((self.output/'source-manifest.json').exists())

    def test_reopened_admission_blocks_certificate(self):
        copy=shutil.copyfileobj
        def resumed(*args,**kwargs):
            result=copy(*args,**kwargs);self.control.resume();return result
        with patch('shutil.copyfileobj',side_effect=resumed),self.assertRaises(AssertionError):self.execute()
        self.assertFalse((self.output/'source-manifest.json').exists())

    def test_existing_destination_is_preserved(self):
        destination=self.output/'snapshot';destination.mkdir()
        marker=destination/'preserve.txt';marker.write_text('Existing work')
        with self.assertRaises(FileExistsError):self.execute()
        self.assertEqual(marker.read_text(),'Existing work')


class ExistingMaintenanceGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=_review_temp();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.flag=self.root/'maintenance'
        self.flag.write_text('Existing maintenance')
        self.tree=ast.parse((Path(__file__).resolve().parents[2]/'scripts/database-production-drain.py').read_text(encoding='utf-8'))
        self.command=Mock()

    def action(self,name):
        node=next(n for n in self.tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
        namespace={'STATE':self.root,'FLAG':self.flag,'json':json,'OperationFailed':RuntimeError,
                   'inspect':lambda:{'Image':'original','State':{'Running':True}},'run':self.command}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'maintenance-guard','exec'),namespace)
        return namespace[name]

    def test_bootstrap_preserves_preexisting_gate(self):
        (self.root/'prepared.json').write_text(json.dumps({'baseline_image':'original'}))
        with self.assertRaisesRegex(RuntimeError,'existing maintenance gate'):self.action('bootstrap')()
        self.assertEqual(self.flag.read_text(),'Existing maintenance');self.command.assert_not_called()

    def test_snapshot_preserves_preexisting_gate(self):
        (self.root/'bootstrapped.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'existing maintenance gate'):self.action('snapshot')()
        self.assertEqual(self.flag.read_text(),'Existing maintenance');self.command.assert_not_called()


if __name__=='__main__':
    unittest.main()
