"""Run from the backend folder:  python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import screening as sc  # noqa: E402

PAGES = [
    (1, sc.normalize_for_search("EDUCATION\nUniversity of Connecticut\nB.S. Computer Science — GPA: 3.86 / 4.0")),
    (2, sc.normalize_for_search("Software Engineer Intern, Acme (Jun 2025 – Aug 2025)\nBuilt dashboards in Python")),
]


class ParseJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(sc.parse_json_object('{"a": 1}'), {"a": 1})

    def test_code_fence_and_chatter(self):
        raw = 'Sure! Here it is:\n```json\n{"summary": "ok"}\n```'
        self.assertEqual(sc.parse_json_object(raw), {"summary": "ok"})

    def test_garbage(self):
        self.assertIsNone(sc.parse_json_object("no json here"))
        self.assertIsNone(sc.parse_json_object("[1, 2]"))
        self.assertIsNone(sc.parse_json_object(None))


class SplitConditions(unittest.TestCase):
    def test_job_related_and_excluded(self):
        data = {"conditions": [
            {"text": "GPA above 3.5", "job_related": True},
            {"text": "under 30", "job_related": False},
            {"text": "Python internship", "job_related": "true"},
        ]}
        checked, excluded = sc.parse_conditions(data, "GPA above 3.5, under 30, Python internship")
        self.assertEqual(checked, ["GPA above 3.5", "Python internship"])
        self.assertEqual(excluded, ["under 30"])

    def test_plain_strings_and_duplicates(self):
        checked, _ = sc.parse_conditions({"conditions": ["Python", "Python", "SQL"]}, "x")
        self.assertEqual(checked, ["Python", "SQL"])

    def test_fallback_to_whole_requirement(self):
        checked, excluded = sc.parse_conditions(None, "GPA above 3.5")
        self.assertEqual((checked, excluded), (["GPA above 3.5"], []))

    def test_cap(self):
        checked, _ = sc.parse_conditions({"conditions": [f"c{i}" for i in range(10)]}, "x")
        self.assertEqual(len(checked), sc.MAX_CONDITIONS)


class Evidence(unittest.TestCase):
    def test_found_despite_spacing_and_case(self):
        self.assertEqual(sc.locate_evidence("gpa: 3.86/4.0", PAGES), 1)
        self.assertEqual(sc.locate_evidence("Built dashboards in Python.", PAGES), 2)

    def test_not_found(self):
        self.assertIsNone(sc.locate_evidence("GPA: 3.9", PAGES))
        self.assertIsNone(sc.locate_evidence("ab", PAGES))


class Verdicts(unittest.TestCase):
    def test_rules(self):
        row = lambda m: {"met": m}  # noqa: E731
        self.assertEqual(sc.compute_verdict([row("yes"), row("yes")]), "match")
        self.assertEqual(sc.compute_verdict([row("yes"), row("unclear")]), "unclear")
        self.assertEqual(sc.compute_verdict([row("unclear"), row("no")]), "no_match")
        self.assertEqual(sc.compute_verdict([]), "unclear")

    def test_normalize_met(self):
        self.assertEqual(sc.normalize_met("YES"), "yes")
        self.assertEqual(sc.normalize_met(False), "no")
        self.assertEqual(sc.normalize_met("满足"), "yes")  # Chinese for "meets"
        self.assertEqual(sc.normalize_met("maybe"), "unclear")


class BuildResult(unittest.TestCase):
    CONDITIONS = ["GPA above 3.5", "Python internship", "Java"]

    def test_full_flow(self):
        data = {
            "results": [
                {"id": 1, "met": "yes", "evidence": "GPA: 3.86 / 4.0", "note": "3.86 > 3.5"},
                {"id": 2, "met": "yes", "evidence": "Built dashboards in Python"},
                {"id": 3, "met": "unclear", "evidence": ""},
            ],
            "summary": "Strong GPA and a Python internship; Java not mentioned.",
        }
        result = sc.build_result(data, self.CONDITIONS, PAGES)
        self.assertEqual(result["verdict"], "unclear")
        rows = result["conditions"]
        self.assertEqual([r["page"] for r in rows], [1, 2, None])
        self.assertEqual([r["verified"] for r in rows], [True, True, None])

    def test_made_up_quote_is_flagged(self):
        data = {"results": [{"id": 1, "met": "yes", "evidence": "GPA: 3.95"}]}
        row = sc.build_result(data, self.CONDITIONS[:1], PAGES)["conditions"][0]
        self.assertFalse(row["verified"])

    def test_missing_ids_fall_back_to_order(self):
        data = {"results": [{"met": "no"}, {"met": "yes"}]}
        rows = sc.build_result(data, self.CONDITIONS[:2], PAGES)["conditions"]
        self.assertEqual([r["met"] for r in rows], ["no", "yes"])

    def test_missing_condition_is_unclear(self):
        data = {"results": [{"id": 1, "met": "yes"}]}
        result = sc.build_result(data, self.CONDITIONS[:2], PAGES)
        self.assertEqual(result["conditions"][1]["met"], "unclear")
        self.assertEqual(result["verdict"], "unclear")

    def test_bad_shape(self):
        result = sc.build_result({"oops": 1}, self.CONDITIONS[:1], PAGES)
        self.assertEqual(result["verdict"], "unclear")


class Tokens(unittest.TestCase):
    def test_estimate(self):
        self.assertEqual(sc.estimate_tokens("abcd" * 10), 10)
        self.assertEqual(sc.estimate_tokens("绩点三点八六"), 6)  # 6 CJK characters -> ~6 tokens


if __name__ == "__main__":
    unittest.main()
