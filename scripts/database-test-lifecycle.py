"""Destructive lifecycle test ONLY for the disposable review_test database."""
import os
import subprocess
from sqlalchemy.engine import make_url
from sqlalchemy import text
from db_security import migration_engine

url = os.environ['DATABASE_TEST_URL']
if make_url(url).database != 'review_test':
    raise SystemExit('Refusing non-test database')
engine = migration_engine(url)
with engine.begin() as conn:
    conn.execute(text('DROP SCHEMA IF EXISTS reporting CASCADE'))
    conn.execute(text('DROP SCHEMA public CASCADE'))
    conn.execute(text('CREATE SCHEMA public'))
engine.dispose()
env = dict(os.environ, DATABASE_URL=url)
for args in [('upgrade', 'head'), ('downgrade', 'base'), ('upgrade', 'head')]:
    subprocess.run([os.sys.executable, '-m', 'alembic', *args], env=env, check=True)
print('PASS: real Alembic upgrade/downgrade/upgrade')
