"""Cache regressions; process tests load only analysis_cache with a fake config."""
import os
import unittest
from unittest.mock import patch

import analysis_cache
import app as backend
import auth
from testsupport import isolated_data_dirs


def valid_result():
    return {
        "job_fit_score": 76,
        "score_breakdown": {"hard_requirements": 20, "responsibility_overlap": 20, "skills_projects": 20, "industry_background": 10, "evidence_bonus": 6},
        "agent_meta": {"validation_passed": True},
        "agent_validation": {"passed": True},
    }


# Executed with spawn: importing this test module would load real app config.
# Use a subprocess script below instead, injecting config before cache import.
_PROCESS_SCRIPT = '''
import sys, types, time, os
sys.path.insert(0, sys.argv[1])
sys.modules['config'] = types.SimpleNamespace(DATA_DIR=sys.argv[2])
import analysis_cache
key = ('same-user', 'same-content')
with analysis_cache.singleflight(key):
    value = analysis_cache.get(key)
    if value is None:
        with open(os.path.join(sys.argv[2], 'calls'), 'a') as f: f.write('call\\n')
        time.sleep(.25)
        value = {"score_breakdown": {"hard_requirements":20,"responsibility_overlap":20,"skills_projects":20,"industry_background":10,"evidence_bonus":6},"agent_meta":{"validation_passed":True},"agent_validation":{"passed":True}}
        analysis_cache.put(key, value)
assert value['score_breakdown']['hard_requirements'] == 20
'''


class AnalysisCacheTests(unittest.TestCase):
    def setUp(self):
        self.dirs = isolated_data_dirs("analysis_cache")
        self.addCleanup(self.dirs.restore)
        self.user, _ = auth.create_user("cache-tester", "password123")
        self.uid = self.user["user_id"]

    def sources(self, content="same resume", job="same job", uid=None):
        uid = uid or self.uid
        rid = backend.store.save_resume(content, {}, uid)
        jid = backend.store.save_job(rid, job, {}, uid)
        return rid, jid

    def test_same_content_new_ids_reuses_persistent_result(self):
        first, second = self.sources(), self.sources()
        with patch.object(backend.screening_agent, "run_screening_agent", return_value=valid_result()) as run:
            a = backend._run_hr_analysis(*first, self.uid)
            backend._HR_ANALYSIS_CACHE.clear()
            b = backend._run_hr_analysis(*second, self.uid)
        self.assertEqual(a, b)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len([n for n in os.listdir(analysis_cache.directory()) if n.endswith('.json')]), 1)

    def test_deleted_or_foreign_sources_never_read_cache_single_or_batch(self):
        rid, jid = self.sources()
        with patch.object(backend.screening_agent, "run_screening_agent", return_value=valid_result()):
            backend._run_hr_analysis(rid, jid, self.uid)
        with patch.object(backend, "_hr_cache_get", side_effect=AssertionError("unauthorized cache read")):
            with self.assertRaises(backend.ApiError):
                backend._run_hr_analysis(rid, jid, "different-user")
            backend.store.delete_resume(rid, user_id=self.uid)
            with self.assertRaises(backend.ApiError):
                backend._run_hr_analysis(rid, jid, self.uid)
            results, failures = backend._run_hr_batch_analysis([rid], jid, self.uid)
            self.assertEqual(results, {})
            self.assertEqual(len(failures), 1)
        rid2, jid2 = self.sources()
        backend.store.delete_job(jid2, user_id=self.uid)
        with patch.object(backend, "_hr_cache_get", side_effect=AssertionError("deleted job cache read")):
            with self.assertRaises(backend.ApiError):
                backend._run_hr_analysis(rid2, jid2, self.uid)

    def test_invalid_unreviewed_and_incomplete_results_are_not_cached(self):
        cases = []
        for field, value in [('validation_passed', False), ('recheck_required', True)]:
            raw = valid_result()
            raw['agent_meta'][field] = value
            cases.append(raw)
        raw = valid_result(); raw['agent_validation']['passed'] = False; cases.append(raw)
        raw = valid_result(); raw.pop('agent_validation'); cases.append(raw)
        raw = valid_result(); raw['score_breakdown'].pop('evidence_bonus'); cases.append(raw)
        for index, raw in enumerate(cases):
            with self.subTest(index=index):
                ids = self.sources(content=str(index))
                with patch.object(backend.screening_agent, 'run_screening_agent', return_value=raw) as run:
                    backend._run_hr_analysis(*ids, self.uid)
                    backend._run_hr_analysis(*ids, self.uid)
                self.assertEqual(run.call_count, 2)
        value = valid_result(); value['analysis_incomplete'] = True
        self.assertFalse(analysis_cache.cacheable(value))

    def test_key_includes_all_semantic_inputs_and_is_user_private(self):
        resume, job = {'content':'resume'}, {'content':'job'}
        key = backend._hr_content_cache_key(resume, job, self.uid, None, None)
        self.assertNotEqual(key, backend._hr_content_cache_key(resume, job, 'other', None, None))
        self.assertNotEqual(key, backend._hr_content_cache_key({'content':'changed'}, job, self.uid, None, None))
        for attr, value in [('_HR_ANALYSIS_VERSION', 'new')]:
            with patch.object(backend, attr, value):
                self.assertNotEqual(key, backend._hr_content_cache_key(resume, job, self.uid, None, None))
        with patch.object(backend.config, 'DECEPTION_CHECK_MODE', 'changed'):
            self.assertNotEqual(key, backend._hr_content_cache_key(resume, job, self.uid, None, None))
        with patch.object(backend.llm, 'model_config_fingerprint', return_value='changed'):
            self.assertNotEqual(key, backend._hr_content_cache_key(resume, job, self.uid, None, None))
        with patch.object(backend, '_agent_config_fingerprint', return_value='changed'):
            self.assertNotEqual(key, backend._hr_content_cache_key(resume, job, self.uid, None, None))
        with patch.object(backend, 'datetime') as date:
            date.now.return_value.strftime.return_value = '2099-01'
            self.assertNotEqual(key, backend._hr_content_cache_key(resume, job, self.uid, None, None))

    def test_ttl_capacity_corruption_and_no_sliding_expiration(self):
        with patch.object(analysis_cache, 'MAX_ENTRIES', 2), patch.object(analysis_cache.time, 'time', side_effect=[1000, 1001, 1002, 1003]):
            for i in range(3): analysis_cache.put((i,), valid_result())
            self.assertEqual(len([n for n in os.listdir(analysis_cache.directory()) if n.endswith('.json')]), 2)
            self.assertIsNone(analysis_cache.get((0,)))
        with patch.object(analysis_cache.time, 'time', return_value=1002 + analysis_cache.TTL_SECONDS):
            self.assertIsNone(analysis_cache.get((2,)))
            self.assertFalse(any(n.endswith('.json') for n in os.listdir(analysis_cache.directory())))
        path = os.path.join(analysis_cache.directory(), analysis_cache.key_digest(('bad',)) + '.json')
        with open(path, 'w') as handle: handle.write('{')
        self.assertIsNone(analysis_cache.get(('bad',)))
        self.assertFalse(os.path.exists(path))

    def test_two_processes_singleflight_calls_once_and_restart_reuses(self):
        import subprocess
        import sys
        args = [sys.executable, '-c', _PROCESS_SCRIPT, os.path.dirname(__file__), self.dirs.root]
        processes = [subprocess.Popen(args, stdout=subprocess.DEVNULL) for _ in range(2)]
        for proc in processes:
            self.assertEqual(proc.wait(timeout=30), 0)
        self.assertEqual(subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode, 0)
        with open(os.path.join(self.dirs.root, 'calls')) as handle:
            self.assertEqual(handle.read().splitlines(), ['call'])
