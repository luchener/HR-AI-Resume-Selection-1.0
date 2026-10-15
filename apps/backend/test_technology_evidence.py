"""Synthetic-only regression tests; never load application config or real data."""
import unittest

from technology_evidence import match_requirement


class TechnologyEvidenceTests(unittest.TestCase):
    def test_attached_versions_do_not_hide_technology(self):
        for text in ['React19', 'React18.2', 'Next.js15']:
            target='Next.js' if text.startswith('Next') else 'React'
            self.assertTrue(match_requirement(target, '使用'+text+'开发界面')['matched'])
        self.assertFalse(match_requirement('Java', 'JavaScript开发')['matched'])

    def test_frameworks_are_generic_directed_practice_clues(self):
        for requirement, framework in (
            ("Python", "Flask"), ("Python", "FastAPI"), ("Python", "Django"),
            ("Java", "Spring"), ("Java", "Spring Boot"),
            ("Ruby", "Rails"), ("PHP", "Laravel"), ("Go", "Gin"),
            ("SQL", "PostgreSQL"), ("SQL", "MySQL"), ("SQL", "SQLite"),
        ):
            with self.subTest(requirement=requirement, framework=framework):
                result = match_requirement("精通" + requirement, f"项目使用{framework}构建服务")
                self.assertTrue(result["matched"])
                self.assertEqual(result["matches"][0]["match_type"], "related")
                self.assertEqual(result["matches"][0]["strength"], "practice")
                self.assertEqual(result["evidence_scope"], "technology_practice_clues_only")
                # No skill-level or satisfaction-of-JD claim is emitted.
                self.assertNotIn("proficiency", result)

    def test_aliases_across_categories(self):
        for canonical, alias in (
            ("JavaScript", "JS"), ("TypeScript", "TS"), ("C#", "CSharp"),
            ("C++", "CPP"), ("Go", "Golang"), ("PostgreSQL", "Postgres"),
            ("Kubernetes", "K8s"), ("Alibaba Cloud", "阿里云"),
            ("Agent", "智能体"), ("RAG", "检索增强生成"),
            ("RAG", "检索增强"), ("RAG", "Retrieval-Augmented Generation"),
            ("LLM", "大语言模型"),
        ):
            with self.subTest(canonical=canonical):
                result = match_requirement(canonical, "参与" + alias + "项目")
                self.assertTrue(result["matched"])
                self.assertEqual(result["concepts"], [canonical])
                self.assertEqual(result["matches"][0]["match_type"], "alias")
                self.assertEqual(match_requirement(alias, canonical)["concepts"], [canonical])

    def test_citations_are_exact_original_offsets_including_unicode_crlf(self):
        resume = "张某😀\r\n经历：使用 FastAPI 开发服务；使用Postgres存储数据。"
        result = match_requirement("Python和SQL", resume)
        self.assertTrue(result["matched"])
        for item in result["matches"]:
            self.assertEqual(resume[item["start"]:item["end"]], item["quote"])
            self.assertEqual(resume[item["context_start"]:item["context_end"]], item["context"])
        self.assertEqual(result["matches"][0]["start"], resume.index("FastAPI"))

    def test_context_clues_do_not_fill_gaps(self):
        for requirement, resume in (
            ("TypeScript", "NestJS"), ("JavaScript", "Nest.js"),
            ("DevOps", "Docker"), ("DevOps", "K8s"),
            ("Agent", "LangGraph"), ("RAG", "LlamaIndex"),
        ):
            with self.subTest(requirement=requirement, resume=resume):
                result = match_requirement(requirement, resume)
                self.assertFalse(result["matched"])
                self.assertEqual(result["missing_concepts"], result["concepts"])
                self.assertTrue(result["matches"])
                self.assertFalse(any(m["satisfies"] for m in result["matches"]))

    def test_no_reverse_transitive_or_unjustified_inference(self):
        for requirement, resume in (
            ("Flask", "Python"), ("Spring", "Java"), ("PostgreSQL", "SQL"),
            ("TypeScript", "React"), ("C#", ".NET"), ("C#", "ASP.NET"),
            ("TypeScript", "Next.js"), ("DevOps", "AWS"),
        ):
            with self.subTest(requirement=requirement, resume=resume):
                self.assertFalse(match_requirement(requirement, resume)["matched"])
        # Even when a new chain is configured, the engine cannot upgrade it.
        from unittest.mock import patch
        from technology_evidence import DIRECTED_RELATIONS, TechnologyRelation
        with patch("technology_evidence.DIRECTED_RELATIONS", DIRECTED_RELATIONS + (
            TechnologyRelation("Python", "Agent", "practice", "test_chain"),
        )):
            self.assertFalse(match_requirement("Agent", "Flask")["matched"])

    def test_unknown_requirement_tokens_are_not_dictionary_gated(self):
        for technology in ("ZetaDB", "OrbitSDK", "Widget-X", "qxlib", "NewTech9"):
            with self.subTest(technology=technology):
                result = match_requirement("熟悉" + technology, "使用" + technology + "交付项目")
                self.assertTrue(result["matched"])
                self.assertEqual(result["matches"][0]["match_type"], "direct")
                self.assertFalse(match_requirement(technology, "Other" + technology)["matched"])
                self.assertFalse(match_requirement(technology, "没有使用" + technology)["matched"])

    def test_short_token_boundaries_and_overlaps(self):
        for requirement, resume in (
            ("C", "C++ C# CPP CSharp"), ("C++", "C C#"), ("C#", "C C++"),
            ("Java", "JavaScript"), ("JS", "JSX"), ("R", "Redis Rust"),
            ("Go", "Google Django MongoDB"), ("SQL", "NoSQL MySQL"),
        ):
            with self.subTest(requirement=requirement, resume=resume):
                result = match_requirement(requirement, resume)
                # MySQL -> SQL is a documented relation, not a SQL substring.
                if requirement == "SQL":
                    self.assertEqual([m["quote"] for m in result["matches"]], ["MySQL"])
                    self.assertEqual(result["matches"][0]["match_type"], "related")
                else:
                    self.assertFalse(result["matched"])
        result = match_requirement("C/C++/C#", "C++")
        self.assertTrue(result["matched"])
        self.assertEqual([m["quote"] for m in result["matches"]], ["C++"])
        self.assertTrue(match_requirement("Go和R和C", "掌握Go、R、C语言")["matched"])

    def test_compound_and_requires_every_concept(self):
        for requirement in ("Python和SQL", "Python、SQL", "Python and SQL", "Python, SQL"):
            with self.subTest(requirement=requirement):
                result = match_requirement(requirement, "使用Django")
                self.assertFalse(result["matched"])
                self.assertEqual(result["missing_concepts"], ["SQL"])
                self.assertTrue(match_requirement(requirement, "Django PostgreSQL")["matched"])

    def test_or_requires_one_alternative_without_false_missing(self):
        for requirement in ("Python或Java", "Python/Java", "Python or Java", "Python|Java"):
            with self.subTest(requirement=requirement):
                result = match_requirement(requirement, "使用Spring")
                self.assertTrue(result["matched"])
                self.assertEqual(result["missing_concepts"], [])
        result = match_requirement("Python/Java和SQL", "Spring")
        self.assertFalse(result["matched"])
        self.assertEqual(result["missing_concepts"], ["SQL"])
        self.assertTrue(match_requirement("Python/Java和SQL", "Spring MySQL")["matched"])

    def test_flat_list_any_one_markers(self):
        for requirement in ("Python、Java任一种", "Python和Java至少一种", "one of Python, Java"):
            with self.subTest(requirement=requirement):
                self.assertTrue(match_requirement(requirement, "Spring")["matched"])
                self.assertEqual(match_requirement(requirement, "Spring")["missing_concepts"], [])

    def test_more_negation_phrasings_and_unknown_technical_tokens(self):
        for resume in ("未体现Python经验", "不使用Python", "未学过Python", "Python经验不足"):
            with self.subTest(resume=resume):
                result = match_requirement("Python", resume)
                self.assertFalse(result["matched"])
                self.assertEqual(len(result["explicit_negative"]), 1)
        self.assertEqual(match_requirement(".NET Core", "DotNet")["concepts"], [".NET"])
        self.assertFalse(match_requirement("C#", ".NET Core")["matched"])

    def test_parenthesized_alternative_bundles(self):
        requirement = "(Python和SQL)或(Java和Redis)"
        self.assertTrue(match_requirement(requirement, "Spring Redis")["matched"])
        self.assertTrue(match_requirement(requirement, "Flask MySQL")["matched"])
        self.assertFalse(match_requirement(requirement, "Python Redis")["matched"])
        self.assertFalse(match_requirement(requirement, "Python Java")["matched"])

    def test_negation_scopes_preserve_unrelated_positive_clauses(self):
        for resume in (
            "没有Python经验，熟悉Java", "不会Python但使用Spring", "未接触Python；使用Java",
            "No Python experience, used Java", "not familiar with Python but familiar with Java",
        ):
            with self.subTest(resume=resume):
                result = match_requirement("Python和Java", resume)
                self.assertEqual(result["missing_concepts"], ["Python"])
                self.assertEqual(len(result["explicit_negative"]), 1)
                self.assertTrue(any(m["concept"] == "Java" and m["satisfies"] for m in result["matches"]))
                self.assertFalse(result["matched"])

    def test_negated_lists_and_related_frameworks_do_not_satisfy(self):
        for resume in ("不会Python和Java", "未接触Python、Java", "no Python or Java experience"):
            with self.subTest(resume=resume):
                result = match_requirement("Python和Java", resume)
                self.assertEqual(len(result["explicit_negative"]), 2)
                self.assertFalse(result["matches"])
        for requirement, resume in (("Python", "未使用Flask"), ("SQL", "没有Postgres经验")):
            result = match_requirement(requirement, resume)
            self.assertFalse(result["matched"])
            self.assertFalse(result["matches"])
            # Negative framework does not prove negative language/database.
            self.assertEqual(result["explicit_negative"], [])

    def test_postfix_negation_and_positive_reset(self):
        result = match_requirement("Python和Java", "Python不熟悉，Java熟悉")
        self.assertEqual(result["missing_concepts"], ["Python"])
        self.assertTrue(match_requirement("Java", "不会Python而熟悉Java")["matched"])

    def test_pseudo_negation_is_positive(self):
        for resume in ("不仅熟悉Python，还熟悉Java", "不但用过Python也用过Java", "not only Python but Java"):
            with self.subTest(resume=resume):
                self.assertTrue(match_requirement("Python和Java", resume)["matched"])

    def test_contradictions_block_automatic_satisfaction(self):
        result = match_requirement("Python", "精通Python；从未使用Python")
        self.assertFalse(result["matched"])
        self.assertEqual(result["conflicting_concepts"], ["Python"])
        self.assertTrue(result["matches"])
        self.assertFalse(result["matches"][0]["satisfies"])
        self.assertEqual(result["explicit_negative"][0]["quote"], "Python")

    def test_known_terms_do_not_swallow_more_specific_ones(self):
        self.assertEqual(match_requirement("Spring Boot", "SpringBoot")["concepts"], ["Spring Boot"])
        self.assertEqual(match_requirement("ASP.NET Core", "ASP.NET")["concepts"], ["ASP.NET"])
        self.assertEqual(match_requirement("AI Agent", "智能体")["concepts"], ["Agent"])
        self.assertFalse(match_requirement("Spring Boot", "Spring")["matched"])

    def test_empty_nontechnical_text_is_not_vacuously_satisfied(self):
        for requirement in ("", "责任心强，沟通良好", "strong communication skills", None):
            with self.subTest(requirement=requirement):
                result = match_requirement(requirement, "Python")
                self.assertFalse(result["matched"])
                self.assertFalse(result["has_technology"])
        self.assertFalse(match_requirement("Python", None)["matched"])

    def test_no_level_upgrade_or_keyword_only_proficiency_claim(self):
        for requirement in ("了解Python", "熟练Python", "精通Python且有五年经验"):
            result = match_requirement(requirement, "技能：Python")
            self.assertTrue(result["matched"])
            self.assertEqual(result["matches"][0]["reason"], "literal_mention")
            self.assertEqual(result["matches"][0]["strength"], "direct")


if __name__ == "__main__":
    unittest.main()
