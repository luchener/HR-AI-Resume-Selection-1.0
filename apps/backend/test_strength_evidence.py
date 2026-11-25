import unittest

from hr_normalization import strength_rows_from_ledger, weakness_rows_from_ledger


class StrengthEvidenceTests(unittest.TestCase):
    def test_strength_rows_separate_status_and_quote(self):
        ledger = [
            {"id": "r1", "text": "具备 3 年结构设计经验", "status": "met",
             "source_quote": "2021-2024 负责结构设计"},
            {"id": "r2", "text": "熟悉 SolidWorks", "status": "manual_review",
             "supported_components": [{"source_quote": "熟练使用 SolidWorks"}]},
            {"id": "r3", "text": "持有 PMP 证书", "status": "not_met", "reason": "简历写明无 PMP"},
        ]
        rows = strength_rows_from_ledger(ledger)
        self.assertEqual([row["status"] for row in rows], ["met", "partial"])
        self.assertEqual(rows[0]["quote"], "2021-2024 负责结构设计")
        self.assertEqual(rows[1]["quote"], "熟练使用 SolidWorks")
        weaknesses = weakness_rows_from_ledger(ledger)
        self.assertEqual([row["status"] for row in weaknesses], ["manual_review", "not_met"])
        self.assertEqual(weaknesses[1]["reason"], "简历写明无 PMP")


if __name__ == '__main__':
    unittest.main()
