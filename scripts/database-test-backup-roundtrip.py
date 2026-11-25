"""Synthetic-only integration: seed test snapshot, dump/restore, certify, corrupt.

Run after starting the reviewed tmpfs container and SSH tunnel. The reviewed
restore shell script must already be copied to /tmp inside that container.
"""
import os
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps' / 'backend'))
from sqlalchemy import text
from db_security import migration_engine
from test_database_live import LiveDatabaseTests

if __name__ == '__main__':
    from sqlalchemy.engine import make_url
    from db_test_guard import review_test_url
    review_test_url(os.environ['DATABASE_TEST_URL'])
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LiveDatabaseTests)
    if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
        sys.exit(1)
    # Synthetic key stays in this process, never in argv, output, dump or disk.
    ssh_target = os.environ.get('RESUME_QA_SSH_TARGET', 'resume-qa.invalid')
    subprocess.run(['ssh', '-o', 'BatchMode=yes', '--', ssh_target,
                    'sudo docker exec resume-db-review-test sh /tmp/database-test-restore.sh'], check=True)
    verifier = runpy.run_path(str(Path(__file__).with_name('database-test-verify-restore.py')))
    verifier['verify_restore'](os.environ['DATABASE_TEST_URL'], source_report=LiveDatabaseTests.source_report)
    from sqlalchemy.engine import make_url
    url = make_url(os.environ['DATABASE_TEST_URL']).set(database='review_restore')
    engine = migration_engine(url.render_as_string(hide_password=False))
    try:
        with engine.begin() as conn:
            conn.execute(text('UPDATE quota_daily_usage SET used=used+1'))
        try:
            verifier['verify_restore'](os.environ['DATABASE_TEST_URL'], source_report=LiveDatabaseTests.source_report)
        except ValueError:
            print('PASS: corrupted restored usage rejected')
        else:
            raise AssertionError('Corrupted restore was accepted')
    finally:
        engine.dispose()
