"""Hermetic scoring tests: import only the pure module, never app/config/data."""
import itertools
import json
import random
import unittest
from copy import deepcopy

from evidence_scoring import DIMENSION_LIMITS, score_evidence


def assessment(id="skill", **changes):
    result = {"id": id, "text": "熟练使用 Python", "category": "skill",
              "logic": "required", "hard": False, "status": "met",
              "source_quote": "使用 Python 开发服务三年", "resume_basis": "项目经历",
              "evidence_kind": "direct"}
    result.update(changes)
    return result


class EvidenceScoringTests(unittest.TestCase):
    def test_empty_and_all_unknown_score_zero_incomplete_not_elimination(self):
        for items in ([], [assessment(status="manual_review", source_quote="")],
                      [assessment(status="not_mentioned", source_quote="")]):
            result = score_evidence(items)
            meta = result["scoring_meta"]
            self.assertEqual(result["job_fit_score"], 0)
            self.assertEqual(meta["score_range"], {"min": 0, "max": 0})
            self.assertTrue(meta["incomplete"])
            self.assertFalse(meta["decision_ready"])
            self.assertEqual(meta["hard_gate_deduction"], 0)
            self.assertIsNone(meta["gate_cap_applied"])
            self.assertEqual(meta["confirmed_noncompliance"], [])

    def test_missing_dimensions_excluded_and_raw_budget_preserved(self):
        result = score_evidence([assessment()])
        self.assertEqual(result["job_fit_score"], 100)
        self.assertEqual(result["score_breakdown"], {
            "hard_requirements": 0, "responsibility_overlap": 0,
            "skills_projects": 25, "industry_background": 0, "evidence_bonus": 0})
        meta = result["scoring_meta"]
        self.assertEqual(meta["applicable_max"], 25)
        self.assertEqual(meta["raw_points"], 25)
        self.assertEqual(meta["normalized_breakdown"]["skills_projects"], 100)
        self.assertEqual(meta["supported_score"], 100)
        self.assertEqual(meta["score_range"], {"min": 100, "max": 100})
        self.assertFalse(meta["dimensions"]["industry_background"]["applicable"])
        self.assertEqual(meta["dimensions"]["industry_background"]["unknown_ratio"], 0)

    def test_met_without_real_quote_never_earns_supported_points(self):
        for quote in (None, "", "未提供", "简历未体现", "N/A", "...", 99):
            item = assessment(source_quote=quote, resume_basis="熟练掌握 Python")
            result = score_evidence([item])
            self.assertEqual(result["scoring_meta"]["supported_score"], 0)
            self.assertEqual(result["scoring_meta"]["items"][0]["effective_status"], "manual_review")

    def test_quote_list_and_alias_can_support_match(self):
        item = assessment(source_quote="", evidence_kind="alias",
                          evidence=[{"quote": "使用 PostgreSQL 开发", "polarity": "supports"}])
        self.assertEqual(score_evidence([item])["scoring_meta"]["supported_score"], 100)
        item["evidence"] = ["使用 PostgreSQL 开发"]
        self.assertEqual(score_evidence([item])["scoring_meta"]["supported_score"], 100)

    def test_related_clue_does_not_prove_proficiency(self):
        result = score_evidence([assessment(evidence_kind="related", confidence=1)])
        meta = result["scoring_meta"]
        self.assertEqual(meta["supported_score"], 0)
        self.assertEqual(result["job_fit_score"], 0)
        self.assertEqual(meta["items"][0]["reason"], "related_is_not_proficiency")

    def test_not_met_requires_explicit_direct_counterevidence(self):
        for quote in ("", "未提供", "参与项目", "Python 开发三年"):
            result = score_evidence([assessment(status="not_met", source_quote=quote)])
            self.assertEqual(result["job_fit_score"], 0)
            self.assertEqual(result["scoring_meta"]["confirmed_noncompliance"], [])
        direct = assessment(status="not_met", source_quote="没有 Python 使用经验")
        result = score_evidence([direct])
        self.assertEqual(result["job_fit_score"], 0)
        self.assertEqual(result["scoring_meta"]["confirmed_noncompliance"], ["skill"])
        direct.update(source_quote="", evidence=[{"quote": "最高学历本科", "polarity": "contradicts"}])
        result = score_evidence([direct])
        self.assertEqual(result["job_fit_score"], 0)
        self.assertEqual(result["scoring_meta"]["confirmed_noncompliance"], [])
        direct.update(evidence=[{"quote": "没有 Python 使用经验", "polarity": "contradicts"}])
        direct["evidence_kind"] = "related"
        self.assertEqual(score_evidence([direct])["scoring_meta"]["confirmed_noncompliance"], [])

    def test_unknown_hard_requirement_never_creates_blocker(self):
        for status in ("manual_review", "not_mentioned"):
            result = score_evidence([assessment(category="education", hard=True, status=status)])
            self.assertEqual(result["job_fit_score"], 0)
            self.assertIsNone(result["scoring_meta"]["gate_cap_applied"])
            self.assertEqual(result["scoring_meta"]["hard_gate_deduction"], 0)

    def test_preferred_not_met_cannot_deduct_or_be_hard(self):
        missing = assessment(logic="preferred", hard=True, status="not_mentioned", source_quote="")
        failed = assessment(logic="preferred", hard=True, status="not_met", source_quote="没有使用经验")
        self.assertEqual(score_evidence([missing])["job_fit_score"], 0)
        result = score_evidence([failed])
        self.assertEqual(result["job_fit_score"], 0)
        self.assertEqual(result["scoring_meta"]["supported_score"], 0)
        self.assertEqual(result["scoring_meta"]["confirmed_noncompliance"], [])
        self.assertFalse(result["scoring_meta"]["items"][0]["hard"])
        self.assertEqual(score_evidence([assessment(logic="preferred")])["job_fit_score"], 100)

    def test_equal_mean_known_facts_reduce_unknown_coverage_and_change_score(self):
        unknowns = [assessment(id=str(i), text="独立要求 " + str(i),
                              status="not_mentioned", source_quote="") for i in range(4)]
        before = score_evidence(unknowns)
        partly_known = deepcopy(unknowns)
        partly_known[0].update(status="met", source_quote="使用 Python 开发服务")
        after = score_evidence(partly_known)
        self.assertGreater(after["job_fit_score"], before["job_fit_score"])
        self.assertGreater(after["scoring_meta"]["supported_score"], 0)
        self.assertLess(after["scoring_meta"]["unknown_ratio"], before["scoring_meta"]["unknown_ratio"])
        self.assertEqual(after["scoring_meta"]["dimensions"]["skills_projects"]["unknown_ratio"], .75)
        duplicate = partly_known + [deepcopy(partly_known[0])] * 20
        self.assertEqual(score_evidence(duplicate), after)
        partly_known[1].update(status="not_met", source_quote="没有使用经验")
        negative = score_evidence(partly_known)
        self.assertEqual(negative["scoring_meta"]["score_range"], after["scoring_meta"]["score_range"])
        self.assertEqual(negative["job_fit_score"], after["job_fit_score"])

    def test_both_lanes_retain_80_20_but_single_lane_uses_full_budget(self):
        required = assessment()
        preferred = assessment(id="bonus", text="加分技能", logic="preferred", status="not_mentioned", source_quote="")
        meta = score_evidence([required, preferred])["scoring_meta"]
        dim = meta["dimensions"]["skills_projects"]
        self.assertEqual(dim["required_weight"], 20)
        self.assertEqual(dim["preferred_weight"], 5)
        self.assertEqual(dim["score_range"], {"min": 20, "max": 20})
        single = score_evidence([required])["scoring_meta"]["dimensions"]["skills_projects"]
        self.assertEqual(single["score_range"], {"min": 25, "max": 25})
        self.assertEqual(single["required_weight"], 25)

    def test_all_supported_preferences_share_fixed_lane_budget(self):
        passed = [assessment(id=str(i), text="独立偏好 " + str(i), logic="preferred") for i in range(30)]
        self.assertEqual(score_evidence(passed)["score_breakdown"],
                         score_evidence(passed[:1])["score_breakdown"])

    def test_alternatives_require_explicit_group_not_shared_category(self):
        failed = assessment(id="a", text="技能 A", logic="alternative", status="not_met", source_quote="没有经验")
        passed = assessment(id="b", text="技能 B", logic="alternative")
        self.assertEqual(score_evidence([failed, passed])["scoring_meta"]["supported_score"], 50)
        failed["alternative_group"] = passed["alternative_group"] = "choose_one"
        passed["category"] = "other"
        self.assertEqual(score_evidence([failed, passed])["scoring_meta"]["supported_score"], 100)
        passed["alternative_group"] = "different_group"
        self.assertEqual(score_evidence([failed, passed])["scoring_meta"]["supported_score"], 50)

    def test_order_duplicate_ids_and_normalized_text_do_not_affect_output(self):
        first = assessment()
        second = assessment(id="degree", text="本科及以上", category="education", hard=True)
        expected = score_evidence([first, second])
        for rows in itertools.permutations([first, second, deepcopy(first)]):
            self.assertEqual(score_evidence(rows), expected)
        alias = dict(first, id="other-id")
        aliased = score_evidence([alias, first, second])
        self.assertEqual(aliased["score_breakdown"], expected["score_breakdown"])
        self.assertEqual(aliased["job_fit_score"], expected["job_fit_score"])
        self.assertEqual(len(aliased["scoring_meta"]["items"]), 2)
        normalized = dict(first, text="  熟练使用 Python  ")
        self.assertEqual(score_evidence([normalized, first, second]), expected)

    def test_conflicting_duplicates_are_conservative_and_order_independent(self):
        positive = assessment()
        negative = assessment(status="not_met", source_quote="没有经验")
        a = score_evidence([positive, negative])
        self.assertEqual(a, score_evidence([negative, positive]))
        self.assertEqual(a["scoring_meta"]["supported_score"], 0)
        self.assertEqual(a["scoring_meta"]["items"][0]["reason"], "conflicting_duplicates")

    def test_llm_scores_confidence_and_input_mutation_cannot_change_arithmetic(self):
        item = assessment()
        snapshot = deepcopy(item)
        expected = score_evidence([item])
        self.assertEqual(item, snapshot)
        item.update(score=100000, job_fit_score=100, confidence=-100, weight=9999)
        self.assertEqual(score_evidence([item]), expected)
        item["confidence"] = float("nan")
        self.assertEqual(score_evidence([item]), expected)

    def test_all_dimensions_confirmed_has_full_100_integer_score(self):
        items = []
        for dimension in DIMENSION_LIMITS:
            for logic in ("required", "preferred"):
                items.append(assessment(id=dimension + logic, text=dimension + logic,
                                        dimension=dimension, logic=logic))
        result = score_evidence(items)
        self.assertEqual(result["score_breakdown"], DIMENSION_LIMITS)
        self.assertEqual(result["job_fit_score"], 100)
        self.assertEqual(result["scoring_meta"]["supported_score"], 100)
        self.assertFalse(result["scoring_meta"]["incomplete"])

    def test_randomized_bounds_reconciliation_serialization_and_permutation(self):
        rng = random.Random(714)
        for _ in range(100):
            items = [assessment(id=str(i), text="requirement " + str(i),
                                dimension=rng.choice(list(DIMENSION_LIMITS)),
                                logic=rng.choice(["required", "preferred", "alternative"]),
                                status=rng.choice(["met", "not_met", "manual_review", "not_mentioned"]),
                                source_quote=rng.choice(["使用三年", "没有使用经验", "未提供"]))
                     for i in range(rng.randrange(20))]
            result = score_evidence(items)
            rng.shuffle(items)
            self.assertEqual(result, score_evidence(items))
            json.dumps(result, allow_nan=False)
            self.assertEqual(sum(result["score_breakdown"].values()), result["scoring_meta"]["raw_points"])
            self.assertEqual(sum(result["scoring_meta"]["normalized_breakdown"].values()), result["job_fit_score"])
            self.assertLessEqual(result["scoring_meta"]["raw_points"], result["scoring_meta"]["applicable_max"])
            self.assertLessEqual(result["scoring_meta"]["score_range"]["min"], result["job_fit_score"])
            self.assertGreaterEqual(result["scoring_meta"]["score_range"]["max"], result["job_fit_score"])
            for dim, score in result["score_breakdown"].items():
                self.assertIs(type(score), int)
                self.assertLessEqual(0, score)
                self.assertLessEqual(score, DIMENSION_LIMITS[dim])

    def test_missing_dimensions_do_not_make_confirmed_jd_provisional(self):
        result = score_evidence([assessment()])
        meta = result["scoring_meta"]
        self.assertFalse(meta["incomplete"])
        self.assertFalse(meta["provisional"])
        self.assertTrue(meta["decision_ready"])
        self.assertEqual(meta["score_range"], {"min": 100, "max": 100})
        items = [assessment(id=dim, text=dim, dimension=dim) for dim in DIMENSION_LIMITS]
        meta = score_evidence(items)["scoring_meta"]
        self.assertEqual(meta["score_range"], {"min": 100, "max": 100})
        self.assertFalse(meta["incomplete"])
        self.assertFalse(meta["provisional"])
        self.assertTrue(meta["decision_ready"])
        for dim in DIMENSION_LIMITS:
            self.assertEqual(meta["dimensions"][dim]["required_weight"], DIMENSION_LIMITS[dim])
            self.assertEqual(meta["dimensions"][dim]["preferred_weight"], 0)

    def test_mixed_known_unknown_is_provisional_not_analysis_incomplete(self):
        items = [assessment(), assessment(id="industry", text="行业经历",
                 category="industry", status="not_mentioned", source_quote="")]
        meta = score_evidence(items)["scoring_meta"]
        self.assertFalse(meta["incomplete"])
        self.assertTrue(meta["provisional"])
        self.assertFalse(meta["decision_ready"])
        self.assertEqual(meta["evidence_coverage"], 0.5)

    def test_manual_practice_components_score_supported_half_without_inferred_points(self):
        item = assessment(status="manual_review", source_quote="",
                          required_components=[{"id": "practice", "text": "Python 开发实践"},
                                               {"id": "proficiency", "text": "熟练掌握 Python"}])
        before = score_evidence([item])
        item["supported_components"] = [{"id": "practice", "text": "Python 开发实践",
                                         "source_quote": "使用 Flask 开发 API", "kind": "alias"}]
        after = score_evidence([item])
        meta = after["scoring_meta"]
        self.assertEqual(meta["supported_score"], 50)
        self.assertEqual(after["job_fit_score"], 50)
        self.assertEqual(meta["provisional_points"], 0)
        self.assertFalse(meta["analysis_incomplete"])
        self.assertEqual(meta["score_range"], {"min": 50, "max": 50})
        self.assertGreater(meta["supported_score"], before["scoring_meta"]["supported_score"])
        self.assertLess(meta["unknown_ratio"], before["scoring_meta"]["unknown_ratio"])
        self.assertEqual(meta["items"][0]["effective_status"], "manual_review")
        self.assertTrue(meta["provisional"])
        self.assertFalse(meta["decision_ready"])
        self.assertEqual(meta["score_semantics"], "resume_jd_evidence_match")
        item["supported_components"] *= 20
        self.assertEqual(score_evidence([item] * 20), after)

    def test_related_component_only_supports_practice(self):
        components = [{"id": "practice", "text": "项目实践"},
                      {"id": "proficiency", "text": "熟练掌握"}]
        item = assessment(status="manual_review", required_components=components,
                          supported_components=[{"id": c["id"], "text": "项目实践",
                                                 "source_quote": "参与后端服务开发", "kind": "related"}
                                                for c in components])
        result = score_evidence([item])
        self.assertEqual(result["scoring_meta"]["supported_score"], 50)
        self.assertEqual([c["id"] for c in result["scoring_meta"]["items"][0]["supported_components"]], ["practice"])
        item["required_components"][0]["text"] = "熟练使用Python项目实践"
        self.assertEqual(score_evidence([item])["scoring_meta"]["supported_score"], 0)

    def test_component_support_needs_matching_id_real_quote_and_valid_kind(self):
        for support in ({"id": "wrong", "source_quote": "使用 Python", "kind": "direct"},
                        {"id": "practice", "source_quote": "未提供", "kind": "direct"},
                        {"id": "practice", "source_quote": "使用 Python", "kind": "guessed"}):
            item = assessment(status="manual_review", required_components=[{"id": "practice", "text": "开发实践"}],
                              supported_components=[support])
            self.assertEqual(score_evidence([item])["scoring_meta"]["supported_score"], 0)
        item = assessment(status="met", required_components=[{"id": "practice", "text": "开发实践"}])
        self.assertEqual(score_evidence([item])["scoring_meta"]["items"][0]["effective_status"], "manual_review")

    def test_duplicate_components_do_not_increase_weight_or_support(self):
        required = [{"id": "practice", "text": "开发实践"}, {"id": "proficiency", "text": "熟练程度"}]
        item = assessment(status="manual_review", required_components=required,
                          supported_components=[{"id": "practice", "text": "开发实践",
                                                 "source_quote": "使用 Python 开发", "kind": "direct"}])
        expected = score_evidence([item])
        item["required_components"] *= 10
        item["supported_components"] *= 10
        self.assertEqual(score_evidence([item]), expected)

    def test_partial_components_merge_across_identical_requirement_duplicates(self):
        item = assessment(status="manual_review", required_components=[{"id": "practice", "text": "开发实践"},
                                                                       {"id": "proficiency", "text": "熟练程度"}])
        first = dict(item, supported_components=[{"id": "practice", "text": "实践", "source_quote": "开发 API", "kind": "direct"}])
        second = dict(item, supported_components=[{"id": "proficiency", "text": "熟练", "source_quote": "熟练使用 Python", "kind": "direct"}])
        result = score_evidence([first, second])
        self.assertEqual(result["scoring_meta"]["supported_score"], 100)
        self.assertEqual(result, score_evidence([second, first]))
        self.assertEqual(result["scoring_meta"]["items"][0]["effective_status"], "manual_review")
        self.assertFalse(result["scoring_meta"]["incomplete"])

    def test_preferred_support_does_not_fill_other_unknown_preferences(self):
        passed = assessment(id="a", text="偏好 A", logic="preferred")
        missing = assessment(id="b", text="偏好 B", logic="preferred",
                             status="not_mentioned", source_quote="")
        self.assertEqual(score_evidence([passed, missing])["job_fit_score"], 50)
        failed = dict(missing, status="not_met", source_quote="没有相关经验")
        self.assertEqual(score_evidence([passed, failed])["job_fit_score"], 50)
        required = assessment(id="req", text="必需技能", status="not_mentioned", source_quote="")
        result = score_evidence([required, passed, missing])
        self.assertEqual(result["job_fit_score"], 10)
        passed["alternative_group"] = missing["alternative_group"] = "one_preference"
        self.assertEqual(score_evidence([passed, missing])["job_fit_score"], 100)

    def test_partial_preference_cannot_fill_other_unknown_bonus(self):
        partial = assessment(id="p", text="偏好实践与熟练", logic="preferred", status="manual_review",
                             required_components=[{"id": "practice", "text": "开发实践"},
                                                  {"id": "proficiency", "text": "熟练程度"}],
                             supported_components=[{"id": "practice", "source_quote": "开发 API", "kind": "direct"}])
        missing = assessment(id="u", text="其它加分要求", logic="preferred",
                             status="not_mentioned", source_quote="")
        result = score_evidence([partial, missing])
        self.assertEqual(result["job_fit_score"], 25)
        self.assertEqual(result["scoring_meta"]["provisional_points"], 0)
        self.assertFalse(result["scoring_meta"]["analysis_incomplete"])

    def test_current_scores_have_no_inferred_points_or_ability_range(self):
        for items in ([], [assessment(status="not_mentioned", source_quote="")],
                      [assessment(), assessment(id="other", text="另一要求", status="not_mentioned")]):
            result = score_evidence(items)
            meta = result["scoring_meta"]
            score = result["job_fit_score"]
            self.assertEqual(score, meta["base_score"])
            self.assertEqual(score, meta["supported_score"])
            self.assertEqual(meta["score_range"], {"min": score, "max": score})
            self.assertEqual(meta["raw_score_range"], {"min": meta["raw_points"], "max": meta["raw_points"]})
            self.assertEqual(meta["raw_points"], meta["raw_supported_points"])
            self.assertEqual(meta["provisional_points"], 0)
            self.assertEqual(meta["inferred_points"], 0)
            self.assertEqual(meta["score_semantics"], "resume_jd_evidence_match")
            self.assertFalse(meta["ability_claim"])
            self.assertFalse(meta["authenticity_claim"])

    def test_unknown_cannot_become_decision_ready_from_same_future_band(self):
        items = [assessment(id=str(i), text="独立要求 " + str(i)) for i in range(9)]
        items.append(assessment(id="unknown", text="未覆盖要求", status="not_mentioned", source_quote=""))
        result = score_evidence(items)
        self.assertEqual(result["job_fit_score"], 90)
        self.assertFalse(result["scoring_meta"]["decision_ready"])
        self.assertFalse(result["scoring_meta"]["analysis_incomplete"])

    def test_invalid_top_level_input_rejected(self):
        for value in ("text", {"status": "met"}):
            with self.assertRaises(TypeError):
                score_evidence(value)
        self.assertEqual(score_evidence(None), score_evidence([]))


if __name__ == "__main__":
    unittest.main()
