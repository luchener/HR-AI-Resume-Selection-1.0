"""0013 roundtrip and refusal to silently discard runtime security state."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from unittest import TestCase, skipUnless
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
import sqlalchemy as sa

from db_check_schema import metadata
from db_models import User
from db_public_auth_state import challenges, invites, limits, login_state
from db_security import migration_engine
from db_test_guard import review_test_url


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class PublicAuthMigrationLiveTests(TestCase):
    def test_roundtrip_schema_ownership_and_nonempty_downgrade_guard(self):
        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        engine = migration_engine(url.render_as_string(hide_password=False))
        self.addCleanup(engine.dispose)
        config = Config(str(Path(__file__).parent / 'alembic.ini'))
        config.set_main_option('script_location', str(Path(__file__).parent / 'migrations'))
        # DATABASE_URL is set by the guarded runner to resume_migrator.
        command.downgrade(config, '0012_original_gc')
        command.upgrade(config, 'head')
        with engine.connect() as conn:
            self.assertEqual(compare_metadata(MigrationContext.configure(conn, opts={'compare_type': True}), metadata()), [])
            self.assertEqual(conn.scalar(sa.text("SELECT count(*) FROM pg_tables WHERE schemaname='public' "
                "AND tablename LIKE 'auth_%' AND tableowner='resume_migrator'")), 4)
        uid, now = uuid4(), datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(User.__table__.insert().values(id=uid, username='migration-' + uid.hex,
                password_hash='synthetic', password_salt='synthetic'))
        fixtures = [(limits, dict(key='a'*64, count=1, expires_at=now+timedelta(minutes=1))),
            (challenges, dict(purpose='reset', subject='b'*64, id=uuid4(), code_hash='c'*64,
                status='pending', expires_at=now+timedelta(minutes=1))),
            (invites, dict(code_hash='d'*64, email_hash='e'*64, created_by=uid, expires_at=now+timedelta(minutes=1))),
            (login_state, dict(user_id=uid, failure_times=[now.timestamp()], last_failure_at=now))]
        try:
            for table, values in fixtures:
                with self.subTest(table=table.name):
                    with engine.begin() as conn:
                        conn.execute(table.insert().values(**values))
                    try:
                        with self.assertRaisesRegex(ValueError, 'Reconcile runtime auth state'):
                            command.downgrade(config, '0012_original_gc')
                        with engine.connect() as conn:
                            self.assertEqual(conn.scalar(sa.text('SELECT version_num FROM alembic_version')),
                                             ScriptDirectory.from_config(config).get_current_head())
                            self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(table)), 1)
                    finally:
                        with engine.begin() as conn:
                            conn.execute(table.delete())
        finally:
            with engine.begin() as conn:
                conn.execute(sa.delete(User).where(User.id == uid))
