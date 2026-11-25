"""Frozen 0014 schema, ownership, constraints and loss-preventing downgrade."""
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
from sqlalchemy.exc import IntegrityError

from db_check_schema import metadata
from db_invite_state import requests
from db_models import User
from db_public_auth_state import invites
from db_security import migration_engine
from db_test_guard import review_test_url


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class InviteMigrationLiveTests(TestCase):
    def setUp(self):
        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        self.engine = migration_engine(url.render_as_string(hide_password=False))
        self.addCleanup(self.engine.dispose)
        self.config = Config(str(Path(__file__).parent / 'alembic.ini'))
        self.config.set_main_option('script_location', str(Path(__file__).parent / 'migrations'))

    def test_empty_roundtrip_schema_and_migrator_ownership(self):
        command.downgrade(self.config, '0013_public_auth')
        with self.engine.connect() as conn:
            self.assertNotIn('invite_requests', sa.inspect(conn).get_table_names())
            self.assertNotIn('revoked_at', {c['name'] for c in sa.inspect(conn).get_columns('auth_invites')})
        command.upgrade(self.config, 'head')
        with self.engine.connect() as conn:
            self.assertEqual(compare_metadata(MigrationContext.configure(conn, opts={'compare_type': True}), metadata()), [])
            self.assertEqual(conn.scalar(sa.text("SELECT tableowner FROM pg_tables WHERE schemaname='public' "
                                                "AND tablename='invite_requests'")), 'resume_migrator')

    def test_downgrade_refuses_each_business_state_without_partial_schema_changes(self):
        uid, now = uuid4(), datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            conn.execute(User.__table__.insert().values(id=uid, username='invite-migration-'+uid.hex,
                password_hash='synthetic', password_salt='synthetic'))
        def clean():
            with self.engine.begin() as conn:
                conn.execute(requests.delete())
                conn.execute(invites.delete())
                conn.execute(sa.delete(User).where(User.id == uid))
        self.addCleanup(clean)
        fixtures = [(requests, dict(id=uuid4(), email_hash='a'*64, sealed={}))]
        base = dict(code_hash='b'*64, email_hash='c'*64, created_by=uid, expires_at=now+timedelta(hours=1))
        fixtures += [(invites, base|extra) for extra in [dict(details={}), dict(revoked_at=now),
                     dict(delivery_pending=True), dict(request_id=uuid4())]]
        for table, values in fixtures:
            with self.subTest(state=sorted(values)):
                with self.engine.begin() as conn:
                    conn.execute(table.insert().values(**values))
                try:
                    with self.assertRaisesRegex(ValueError, 'Reconcile invitation workflow'):
                        command.downgrade(self.config, '0013_public_auth')
                    with self.engine.connect() as conn:
                        self.assertEqual(conn.scalar(sa.text('SELECT version_num FROM alembic_version')),
                                         ScriptDirectory.from_config(self.config).get_current_head())
                        self.assertEqual(conn.scalar(sa.select(sa.func.count()).select_from(table)), 1)
                        self.assertIn('delivery_pending', {c['name'] for c in sa.inspect(conn).get_columns('auth_invites')})
                finally:
                    with self.engine.begin() as conn:
                        conn.execute(table.delete())

    def test_database_lifecycle_hash_and_pending_email_constraints(self):
        for extra in [dict(email_hash='bad'), dict(status='invalid'), dict(code_sent=True),
                      dict(status='approved'), dict(status='rejected'), dict(invite_hash='bad')]:
            with self.subTest(extra=extra), self.assertRaises(IntegrityError), self.engine.begin() as conn:
                conn.execute(requests.insert().values(**(dict(id=uuid4(), email_hash='d'*64, sealed={})|extra)))
        try:
            with self.engine.begin() as conn:
                conn.execute(requests.insert().values(id=uuid4(), email_hash='e'*64, sealed={}))
            with self.assertRaises(IntegrityError), self.engine.begin() as conn:
                conn.execute(requests.insert().values(id=uuid4(), email_hash='e'*64, sealed={}))
        finally:
            with self.engine.begin() as conn:
                conn.execute(requests.delete())
