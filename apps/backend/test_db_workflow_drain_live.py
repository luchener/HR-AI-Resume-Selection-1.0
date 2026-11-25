"""Execute the synthetic long-request/SSE/worker drain rehearsal on guarded PG."""
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import runpy
import unittest


@unittest.skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class WorkflowDrainLiveTests(unittest.TestCase):
    def test_long_request_sse_workers_and_durable_state_drain(self):
        script = Path(__file__).resolve().parents[1].parent / 'scripts' / 'database-test-workflow-drain.py'
        namespace = runpy.run_path(str(script), run_name='drain-rehearsal')
        output = io.StringIO()
        with redirect_stdout(output):
            namespace['main']()
        report = json.loads(output.getvalue())
        self.assertFalse(report['production_acceptance'])
        self.assertEqual(report['new_request_after_drain_http_status'], 503)
        self.assertTrue(report['sse_completion_matches_committed_results'])
        self.assertTrue(report['checkpoints'][-1]['complete'])
        active = report['checkpoints'][0]
        self.assertEqual((active['requests'], active['streams'], active['workers']), (1, 1, 2))
        self.assertFalse(report['checkpoints'][-2]['complete'])


if __name__ == '__main__':
    unittest.main()
