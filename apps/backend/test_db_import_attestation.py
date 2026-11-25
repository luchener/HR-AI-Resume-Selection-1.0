"""Live target-import attestation checks on the disposable review database only."""
import os
import unittest
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from db_check_schema import metadata
from db_import_attestation import ATTESTATION_KEY, target_attestation, verify_attestation
from db_security import migration_engine
from db_test_guard import review_test_url


@unittest.skipUnless(os.environ.get("DATABASE_TEST_URL"), "Disposable database not configured")
class ImportAttestationLiveTests(unittest.TestCase):
    """The attestation is a stability certificate, so every mutation is rolled back."""

    @classmethod
    def setUpClass(cls):
        cls.url = review_test_url(os.environ["DATABASE_TEST_URL"])
        cls.engine = migration_engine(cls.url.render_as_string(hide_password=False))
        cls.tables = metadata().tables

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def test_target_attestation_is_stable_and_contains_only_safe_metadata(self):
        with Session(self.engine) as session:
            first = target_attestation(session)
            second = target_attestation(session)
        self.assertEqual(first, second)
        self.assertEqual(first["version"], 1)
        self.assertTrue(first["tables"])
        for value in first["tables"].values():
            self.assertEqual(set(value), {"count", "sha256"})
            self.assertIsInstance(value["count"], int)
            self.assertRegex(value["sha256"], r"^[0-9a-f]{64}$")

    def test_own_attestation_ledger_row_is_excluded(self):
        marker = {"version": 1, "tables": {"synthetic": {"count": 0, "sha256": "0" * 64}}}
        with self.engine.begin() as connection:
            before = target_attestation(Session(bind=connection))
            connection.execute(
                self.tables["legacy_records"].insert(),
                {"root_name": "__migration__", "record_key": ATTESTATION_KEY,
                 "payload": marker},
            )
            after = target_attestation(Session(bind=connection))
            connection.execute(
                sa.delete(self.tables["legacy_records"]).where(
                    sa.and_(self.tables["legacy_records"].c.root_name == "__migration__",
                            self.tables["legacy_records"].c.record_key == ATTESTATION_KEY)
                )
            )
        self.assertEqual(before, after)

    def test_data_change_is_rejected_and_missing_proof_is_rejected(self):
        key = "attestation-test/" + uuid.uuid4().hex
        with self.engine.connect() as connection:
            transaction = connection.begin()
            session = Session(bind=connection)
            try:
                expected = target_attestation(session)
                connection.execute(
                    self.tables["legacy_records"].insert(),
                    {"root_name": "attestation-test", "record_key": key,
                     "payload": {"value": "synthetic-only"}},
                )
                with self.assertRaises(ValueError):
                    verify_attestation(session, expected)
                with self.assertRaises(ValueError):
                    verify_attestation(session, None)
            finally:
                session.close()
                transaction.rollback()


if __name__ == "__main__":
    unittest.main()
