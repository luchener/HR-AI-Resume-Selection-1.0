"""Pure quota conversion tests. No production imports or database required."""
import copy
from datetime import date
import unittest
from uuid import UUID

from db_import_auxiliary import AuxiliaryConversionError, convert_auxiliary


ALICE = "00000000-0000-4000-8000-000000000001"
ADMIN = "00000000-0000-4000-8000-000000000002"
USERS = [{"user_id": ALICE, "username": "Alice"},
         {"user_id": ADMIN, "username": "Admin", "is_admin": True}]


class AuxiliaryConversionTest(unittest.TestCase):
    def convert(self, **kwargs):
        return convert_auxiliary(users=USERS, **kwargs)

    def test_global_defaults_and_no_invented_user_policies(self):
        plan = self.convert()
        self.assertEqual(plan["quota_policies"], [{
            "scope_key": "global", "user_id": None, "mode": "unlimited",
            "daily_limit": None, "enabled": True, "quota_enabled": False,
            "email_notify": False,
        }])
        self.assertEqual(plan["quota_daily_usage"], [])

    def test_limit_modes_and_scope_keys(self):
        for raw, mode, expected in [(None, "inherit", None), (-1, "unlimited", None),
                                     (-5, "unlimited", None), (0, "limited", 0),
                                     (12, "limited", 12), ("3", "limited", 3)]:
            with self.subTest(raw=raw):
                rows = self.convert(limits={"quota_enabled": True, "default_daily": 7,
                                           "users": {"Alice": {"daily_limit": raw}}})["quota_policies"]
                self.assertEqual(rows[0]["mode"], "limited")
                self.assertEqual(rows[0]["daily_limit"], 7)
                self.assertEqual(rows[1]["mode"], mode)
                self.assertEqual(rows[1]["daily_limit"], expected)
                self.assertEqual(rows[1]["scope_key"], ALICE)
                self.assertEqual(rows[1]["user_id"], UUID(ALICE))
                self.assertIsNone(rows[1]["quota_enabled"])
                self.assertTrue(rows[1]["email_notify"])

    def test_admin_inheritance_is_not_materialized(self):
        for config, mode in [({}, "inherit"), ({"daily_limit": None}, "inherit"),
                             ({"daily_limit": 2}, "limited"), ({"daily_limit": -1}, "unlimited")]:
            rows = self.convert(limits={"default_daily": 7, "quota_enabled": True,
                                       "users": {"Admin": config}})["quota_policies"]
            self.assertEqual(rows[1]["mode"], mode)
            # db_quota.resolve_policy combines this row with User.is_admin.
            self.assertNotIn("is_admin", rows[1])

    def test_analysis_gate_and_notification_switches_are_independent(self):
        for on in (False, True):
            rows = self.convert(limits={"quota_enabled": on, "email_notify_enabled": True,
                "users": {"Admin": {"analysis_enabled": False, "email_notify": False}}})["quota_policies"]
            self.assertEqual(rows[0]["quota_enabled"], on)
            self.assertTrue(rows[0]["email_notify"])
            self.assertFalse(rows[1]["enabled"])
            self.assertFalse(rows[1]["email_notify"])

    def test_target_resolver_preserves_full_precedence_matrix(self):
        from db_quota import resolve_policy

        for admin in (False, True):
            for quota_on in (False, True):
                for enabled in (False, True):
                    for raw in (None, -1, 0, 2):
                        with self.subTest(admin=admin, quota_on=quota_on, enabled=enabled, raw=raw):
                            name = "Admin" if admin else "Alice"
                            rows = self.convert(limits={"quota_enabled": quota_on, "default_daily": 7,
                                "email_notify_enabled": True,
                                "users": {name: {"daily_limit": raw, "analysis_enabled": enabled}}}
                            )["quota_policies"]
                            effective = resolve_policy(rows[0], rows[1], is_admin=admin)
                            expected = None if raw == -1 or (raw is None and admin) else (7 if raw is None else raw)
                            self.assertEqual(effective.daily_limit, expected if quota_on else None)
                            self.assertEqual(effective.analysis_enabled, enabled)
                            self.assertTrue(effective.email_notify)

    def test_exact_username_mapping_no_fallback(self):
        for name in ("alice", " Alice", ALICE, "missing"):
            with self.subTest(name=name), self.assertRaises(AuxiliaryConversionError):
                self.convert(limits={"users": {name: {"daily_limit": 1}}})

    def test_uuid_normalization_and_duplicates(self):
        uid = "abcdefab-cdef-4000-8000-abcdefabcdef"
        result = convert_auxiliary(users=[{"user_id": uid.upper(), "username": "x"}],
            limits={"users": {"x": {}}}, usage_by_user_id={uid: {"2026-01-01": {"analysis": 1}}})
        self.assertEqual(result["quota_policies"][1]["scope_key"], uid)
        with self.assertRaises(AuxiliaryConversionError):
            convert_auxiliary(users=[{"user_id": uid, "username": "x"}],
                             usage_by_user_id={uid: {}, uid.upper(): {}})
        for users in ([USERS[0], USERS[0]], [{"username": "x", "user_id": "bad"}],
                      [USERS[0], {"user_id": ADMIN, "username": "Alice"}]):
            with self.assertRaises(AuxiliaryConversionError):
                convert_auxiliary(users=users)

    def test_all_utc_days_analysis_only_and_nonnegative(self):
        usage = {ALICE: {"2026-01-02": {"analysis": "4", "login": 100},
                         "2025-12-31": {"analysis": -2}, "2026-01-01": {"login": 9}}}
        rows = self.convert(usage_by_user_id=usage)["quota_daily_usage"]
        self.assertEqual(rows, [
            {"user_id": UUID(ALICE), "date": date(2025, 12, 31), "used": 0, "reserved": 0},
            {"user_id": UUID(ALICE), "date": date(2026, 1, 1), "used": 0, "reserved": 0},
            {"user_id": UUID(ALICE), "date": date(2026, 1, 2), "used": 4, "reserved": 0},
        ])

    def test_reject_orphans_invalid_dates_counts_and_shapes(self):
        for usage in ({"unknown": {}}, {"00000000-0000-4000-8000-000000000009": {}},
                      {ALICE: []}, {ALICE: {"2026-02-30": {}}},
                      {ALICE: {"20260101": {}}}, {ALICE: {"2026-1-1": {}}},
                      {ALICE: {"2026-01-01T23:00:00-08:00": {}}},
                      {ALICE: {"2026-01-01": []}},
                      {ALICE: {"2026-01-01": {"analysis": "bad"}}},
                      {ALICE: {"2026-01-01": {"analysis": 2**31}}}):
            with self.subTest(usage=usage), self.assertRaises(AuxiliaryConversionError):
                self.convert(usage_by_user_id=usage)
        for limits in ([], {"users": []}, {"users": {"Alice": []}},
                       {"default_daily": "bad"}, {"default_daily": 2**31}):
            with self.assertRaises(AuxiliaryConversionError):
                self.convert(limits=limits)

    def test_quarantine_and_defer_never_copy_source_values(self):
        secret = "do-not-copy-secret"
        configs = {"mail.json": {"slots": {"transactional": {"password": secret}}},
                   "llm.json": {"api_key": secret}}
        audits = {"timestamp_source-id.json": {"detail": secret, "request_id": secret}}
        result = self.convert(system_configs=configs, admin_ops=audits)
        self.assertEqual(result["system_configs"], [])
        self.assertEqual(result["audit_logs"], [])
        self.assertEqual(result["quarantine"], {"system_config": True, "system_config_records": 2})
        self.assertEqual(result["deferred"], {"admin_ops": True, "admin_ops_records": 1})
        self.assertNotIn(secret, repr(result))
        with self.assertRaises(AuxiliaryConversionError) as caught:
            self.convert(limits={"default_daily": secret})
        self.assertNotIn(secret, str(caught.exception))

    def test_deterministic_and_does_not_mutate_inputs(self):
        inputs = {"users": copy.deepcopy(USERS), "limits": {"users": {"Admin": {}, "Alice": {}}},
                  "usage_by_user_id": {ADMIN: {"2026-01-01": {"analysis": 2}}}}
        before = copy.deepcopy(inputs)
        first = convert_auxiliary(**inputs)
        self.assertEqual(inputs, before)
        self.assertEqual(first, convert_auxiliary(**inputs))
        first["quota_policies"][1]["mode"] = "changed"
        self.assertEqual(inputs, before)


if __name__ == "__main__":
    unittest.main()
