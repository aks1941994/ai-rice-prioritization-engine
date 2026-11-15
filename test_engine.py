import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rice_engine import plan_releases, rice_score, validate  # noqa: E402


def item(id_, score, effort, depends_on=()):
    return {"id": id_, "rice_score": score, "effort": effort, "depends_on": list(depends_on)}


class TestScoring(unittest.TestCase):
    def test_rice_formula(self):
        est = {"reach": 1000, "impact": 2, "confidence": 0.8, "effort": 2}
        self.assertEqual(rice_score(est), 800)

    def test_validate_snaps_to_scales(self):
        est, flags = validate({"reach": 10.6, "impact": 1.7, "confidence": 0.85, "effort": 2}, "X")
        self.assertEqual((est["reach"], est["impact"], est["confidence"]), (11, 2, 0.8))
        self.assertEqual(len(flags), 2)

    def test_validate_missing_estimate(self):
        _, flags = validate(None, "X")
        self.assertIn("missing estimate", flags)


class TestPlanning(unittest.TestCase):
    def test_capacity_spills_to_next_release(self):
        ranked = [item("A", 100, 4), item("B", 90, 4)]
        plan_releases(ranked, ["R1", "R2"], capacity=6)
        self.assertEqual([i["release"] for i in ranked], ["R1", "R2"])

    def test_over_capacity_goes_to_backlog(self):
        ranked = [item("A", 100, 10)]
        plan_releases(ranked, ["R1"], capacity=6)
        self.assertEqual(ranked[0]["release"], "Backlog")

    def test_dependency_pulled_forward(self):
        ranked = [item("A", 100, 2, depends_on=["B"]), item("B", 10, 2)]
        plan_releases(ranked, ["R1", "R2"], capacity=6)
        self.assertEqual(ranked[1]["release"], "R1")
        self.assertEqual(ranked[1]["pulled_forward_by"], "A")

    def test_never_before_dependency(self):
        ranked = [item("B", 100, 5), item("C", 95, 2), item("A", 90, 1, depends_on=["C"])]
        plan_releases(ranked, ["R1", "R2"], capacity=6)
        self.assertEqual(ranked[1]["release"], "R2")
        self.assertEqual(ranked[2]["release"], "R2")


if __name__ == "__main__":
    unittest.main()
