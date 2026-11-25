"""0016 schema and frozen announcement projection in guarded disposable PG."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from db_announcement_import import apply_announcement_snapshot, project_announcement_snapshot
from db_announcement_state import announcements, dismissals
from db_announcements import AnnouncementService
from db_check_schema import metadata
from db_migrate_json import inventory
from db_models import LegacyRecord, User
from db_security import migration_engine
from db_test_guard import review_test_url
from test_db_announcements import NOW, snapshot_fixture


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class AnnouncementMigrationTests(TestCase):
    def setUp(self):
        url = review_test_url(os.environ['DATABASE_TEST_URL'])
        self.bootstrap = migration_engine(url.render_as_string(hide_password=False))
        self.migrator = migration_engine(url.set(username='resume_migrator', password='review-migrator-only')
                                         .render_as_string(hide_password=False))
        self.sessions = sessionmaker(bind=self.migrator, expire_on_commit=False)
        self.addCleanup(self.bootstrap.dispose)
        self.addCleanup(self.migrator.dispose)
        self.config = Config(str(Path(__file__).parent / 'alembic.ini'))
        self.config.set_main_option('script_location', str(Path(__file__).parent / 'migrations'))

    def counts(self):
        with self.bootstrap.connect() as conn:
            return [conn.scalar(sa.select(sa.func.count()).select_from(table)) for table in (announcements, dismissals)]

    def test_empty_roundtrip_and_full_schema_parity_and_ownership(self):
        command.downgrade(self.config, '0015_change_log')
        with self.bootstrap.connect() as conn:
            self.assertNotIn('announcements', sa.inspect(conn).get_table_names())
            self.assertNotIn('announcement_dismissals', sa.inspect(conn).get_table_names())
        command.upgrade(self.config, 'head')
        with self.bootstrap.connect() as conn:
            self.assertEqual(compare_metadata(MigrationContext.configure(conn, opts={'compare_type': True}), metadata()), [])
            owners = conn.execute(sa.text("SELECT tablename, tableowner FROM pg_tables WHERE schemaname='public' "
                                          "AND tablename IN ('announcements','announcement_dismissals')")).all()
            self.assertEqual(len(owners), 2)
            self.assertTrue(all(owner == 'resume_migrator' for _name, owner in owners))

    def fixture(self, root):
        uid, aid, record = snapshot_fixture(root)
        with self.bootstrap.begin() as conn:
            conn.execute(User.__table__.insert().values(id=UUID(uid), username='migration-' + UUID(uid).hex,
                password_hash='synthetic', password_salt='synthetic'))
        def clean():
            with self.bootstrap.begin() as conn:
                conn.execute(dismissals.delete().where(dismissals.c.announcement_id == UUID(aid)))
                conn.execute(announcements.delete().where(announcements.c.id == UUID(aid)))
                conn.execute(sa.delete(LegacyRecord).where(LegacyRecord.root_name == '__announcement_migration__'))
                conn.execute(sa.delete(User).where(User.id == UUID(uid)))
        self.addCleanup(clean)
        return uid, aid, record

    def test_database_constraints_and_nonempty_downgrade_protect_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            uid, aid, _row = self.fixture(Path(tmp))
            row = project_announcement_snapshot(inventory(Path(tmp)))['announcements'][0]
            for change in [dict(title=''), dict(content='x' * 5001), dict(status='expired'), dict(status='cancelled'),
                           dict(start_mode='unknown'), dict(end_at=NOW), dict(created_by_id=uuid4())]:
                with self.subTest(fields=list(change)), self.assertRaises(IntegrityError), self.bootstrap.begin() as conn:
                    conn.execute(announcements.insert().values(**(row | change)))
            with self.bootstrap.begin() as conn:
                conn.execute(announcements.insert().values(**row))
                conn.execute(dismissals.insert().values(user_id=UUID(uid), announcement_id=UUID(aid), dismissed_at=NOW))
            with self.assertRaisesRegex(ValueError, 'Reconcile announcements and dismissals'):
                command.downgrade(self.config, '0015_change_log')
            self.assertEqual(self.counts(), [1, 1])
            with self.bootstrap.connect() as conn:
                self.assertEqual(conn.scalar(sa.text('SELECT version_num FROM alembic_version')),
                                 ScriptDirectory.from_config(self.config).get_current_head())
            with self.assertRaises(IntegrityError), self.bootstrap.begin() as conn:
                conn.execute(dismissals.insert().values(user_id=UUID(uid), announcement_id=UUID(aid), dismissed_at=NOW))

    def test_frozen_projection_rolls_back_on_late_failure_then_replays_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uid, aid, _row = self.fixture(root)
            report = inventory(root)
            def fail_ledger(conn, cursor, statement, parameters, context, executemany):
                if statement.startswith('INSERT INTO legacy_records'):
                    raise RuntimeError('synthetic ledger failure')
            sa.event.listen(self.migrator, 'before_cursor_execute', fail_ledger)
            try:
                with self.assertRaisesRegex(RuntimeError, 'synthetic ledger failure'), self.sessions() as session, session.begin():
                    apply_announcement_snapshot(session, report)
            finally:
                sa.event.remove(self.migrator, 'before_cursor_execute', fail_ledger)
            self.assertEqual(self.counts(), [0, 0])
            with self.sessions() as session, session.begin():
                result = apply_announcement_snapshot(session, report)
            self.assertEqual(result, {'announcements': 1, 'dismissals': 1, 'replayed': False})
            with self.sessions() as session, session.begin():
                self.assertEqual(apply_announcement_snapshot(session, report)['replayed'], True)
            self.assertEqual(self.counts(), [1, 1])
            # Reopening the source after capture is not part of the apply path.
            source = root / 'announcements' / (aid + '.json')
            mutated = json.loads(source.read_text(encoding='utf-8')) | {'content': 'changed source'}
            source.write_text(json.dumps(mutated), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Conflicting announcement snapshot'), self.sessions() as session, session.begin():
                apply_announcement_snapshot(session, inventory(root))
            self.assertEqual(self.counts(), [1, 1])
            with self.bootstrap.connect() as conn:
                original = conn.execute(sa.select(announcements).where(announcements.c.id == UUID(aid))).mappings().one()
                self.assertEqual(original['content'], '冻结正文')
            # Verify imported closure suppresses this user's popup through the
            # same real read service used by runtime HTTP adapters.
            service = AnnouncementService(self.sessions, clock=lambda: NOW)
            self.assertEqual(service.active_for_user(uid, password_version=1), {'items': []})

    def test_projection_refuses_dirty_target_and_requires_explicit_transaction_and_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _uid, _aid, _row = self.fixture(root)
            report = inventory(root)
            row = project_announcement_snapshot(report)['announcements'][0]
            with self.bootstrap.begin() as conn:
                conn.execute(announcements.insert().values(**row))
            with self.assertRaisesRegex(ValueError, 'empty target tables'), self.sessions() as session, session.begin():
                apply_announcement_snapshot(session, report)
            with self.assertRaisesRegex(ValueError, 'caller-owned transaction'), self.sessions() as session:
                apply_announcement_snapshot(session, report)
            with self.assertRaisesRegex(ValueError, 'migration role'), sessionmaker(bind=self.bootstrap)() as session, session.begin():
                apply_announcement_snapshot(session, report)
            self.assertEqual(self.counts(), [1, 0])
