"""0011 schema/rollback and frozen-deleted source import into an EMPTY review DB."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from db_accounts import AccountService
from db_auth import hash_password
from db_check_schema import metadata
from db_migrate_json import apply_import, inventory
from db_models import User
from db_reconcile import reconcile
from db_runtime import build_runtime
from db_security import migration_engine
from db_test_guard import review_test_url


@skipUnless(os.environ.get('DATABASE_TEST_URL'), 'Disposable database not configured')
class AccountMigrationLiveTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url=review_test_url(os.environ['DATABASE_TEST_URL'])
        cls.migrator_url=cls.url.set(username='resume_migrator',password='review-migrator-only')
        cls.engine=migration_engine(cls.url.render_as_string(hide_password=False))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def test_01_upgrade_downgrade_upgrade_preserves_roles_and_schema(self):
        # The runner performs this module first, before any account fixture writes.
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(text('count(*)')).select_from(User.__table__)),0)
        config=Config(str(Path(__file__).parent/'alembic.ini'))
        config.set_main_option('script_location',str(Path(__file__).parent/'migrations'))
        migrator=self.url.set(username='resume_migrator',password='review-migrator-only')
        with patch.dict(os.environ,DATABASE_URL=migrator.render_as_string(hide_password=False)):
            command.downgrade(config,'0010_task_inputs')
            command.upgrade(config,'head')
        with self.engine.connect() as conn:
            self.assertEqual(compare_metadata(MigrationContext.configure(conn,opts={'compare_type':True}),metadata()),[])
            owner=conn.scalar(text("SELECT tableowner FROM pg_tables WHERE schemaname='public' AND tablename='users'"))
            self.assertEqual(owner,'resume_migrator')
        for changes in ({'frozen_by':'unknown'}, {'deletion_previous_status':'deleted'},
                        {'login_consecutive_failures':-1}, {'login_window_failures':-1}):
            with self.assertRaises(IntegrityError), self.engine.begin() as conn:
                conn.execute(User.__table__.insert().values(id=uuid4(),username=uuid4().hex,
                    password_hash='synthetic',password_salt='synthetic',**changes))

    def test_02_import_replay_reconcile_and_restore_keep_source_freeze(self):
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(text('count(*)')).select_from(User.__table__)),0)
        owner_id,member_id=uuid4(),uuid4()
        password,salt=hash_password('SyntheticPassword9')
        now=datetime.now(timezone.utc)
        owner={'user_id':str(owner_id),'username':'migration-owner','email':'migration-owner@example.test',
               'password_hash':password,'password_salt':salt}
        member={'user_id':str(member_id),'username':'migration-member','password_hash':password,
                'password_salt':salt,'deleted_at':now.isoformat(),'deleted_by':str(owner_id)}
        freeze={'frozen':True,'frozen_at':now.timestamp(),'frozen_by':'admin','frozen_reason':'synthetic prior freeze',
                'consecutive_failures':3,'failures':[{}, {}, {}]}
        with TemporaryDirectory() as directory, patch.dict(os.environ,
                MIGRATION_ADMIN_EMAILS=owner['email'],MIGRATION_AUTO_UNFREEZE_HOURS='0'):
            root=Path(directory)
            for folder,name,data in [('users','owner',owner),('users','member',member),
                    ('login_failures',sha256(member['username'].encode()).hexdigest(),freeze)]:
                target=root/folder/(name+'.json')
                target.parent.mkdir(exist_ok=True)
                target.write_text(json.dumps(data),encoding='utf-8')
            report=inventory(root)
            result=apply_import(report,self.migrator_url.render_as_string(hide_password=False))
            self.assertEqual(result['users'],2)
            self.assertEqual(apply_import(inventory(root),self.migrator_url.render_as_string(hide_password=False)),{'unchanged_snapshot':1})
            with Session(self.engine) as session, session.begin():
                comparison=reconcile(session,report,admin_emails=[owner['email']])
                self.assertEqual(comparison['mismatch_count'],0)
                session.execute(update(User).where(User.id==member_id).values(frozen_reason='drift'))
                self.assertEqual(reconcile(session,report,admin_emails=[owner['email']])['mismatches'],{'users.frozen_reason':1})
                session.rollback()
            engine,sessions=build_runtime(self.url.set(username='resume_app',password='review-app-only').render_as_string(hide_password=False))
            try:
                service=AccountService(sessions,admin_emails=[owner['email']])
                service.restore_account(owner_id,member['username'],password_version=1,request_id='migration-restore')
                user=service.find_user_by_id(member_id)
                self.assertEqual(user.status,'locked')
                self.assertFalse(user.can_authenticate)
                items=service.list_lifecycle(owner_id,'frozen',password_version=1)['items']
                self.assertEqual(items[0]['consecutive_failures'],3)
                self.assertEqual(items[0]['window_failures'],3)
            finally:
                engine.dispose()
