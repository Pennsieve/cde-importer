import json
import pathlib
import unittest

from cde_importer import coordinate as C

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "nlm_cdes.json").read_text())
BY_TINY = {c["tinyId"]: c for c in FIXTURE}


def tree(steward, elements):
    return {"stewardOrg": {"name": steward}, "elements": elements}


def node(name, *children):
    return {"name": name, "elements": list(children)}


class WalkTreeTest(unittest.TestCase):
    def test_disease_axis_with_tier_population_domain(self):
        cls = [tree("NINDS", [
            node("Disease",
                 node("Epilepsy",
                      node("Classification", node("Core"), node("Supplemental")),
                      node("Domain", node("Outcomes")),
                      node("Population", node("Pediatric")))),
        ])]
        rows = C.walk_tree(cls)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["tier"] for r in rows}, {"Core", "Supplemental"})
        r = rows[0]
        self.assertEqual((r["steward"], r["context"], r["sub_context"], r["population"], r["domain"]),
                         ("NINDS", "Epilepsy", C.WILDCARD, "Pediatric", "Outcomes"))

    def test_steward_level_population_applies_to_every_context(self):
        cls = [tree("NINDS", [
            node("Population", node("Adult")),
            node("Disease", node("TBI", node("Classification", node("Core"))), node("SCI")),
        ])]
        rows = C.walk_tree(cls)
        self.assertEqual([(r["context"], r["population"], r["tier"]) for r in rows],
                         [("TBI", "Adult", "Core"), ("SCI", "Adult", None)])

    def test_sub_context_path_and_tier_leaf(self):
        cls = [tree("NINDS", [
            node("Disease",
                 node("TBI",
                      node("Acute", node("Moderate", node("Classification", node("Core")))))),
        ])]
        rows = C.walk_tree(cls)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sub_context"], "Acute" + C.SEP + "Moderate")
        self.assertEqual(rows[0]["tier"], "Core")

    def test_non_disease_steward_uses_top_values_as_context(self):
        cls = [tree("Project 5", [node("Demographics", node("Tier 1")), node("Domain", node("Social"))])]
        rows = C.walk_tree(cls, canon_tier=lambda s: s if s.startswith("Tier") else None)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["context"], rows[0]["tier"], rows[0]["domain"]), ("Demographics", "Tier 1", "Social"))

    def test_canon_tier_drops_non_tier_values(self):
        cls = [tree("NINDS", [node("Disease", node("Epilepsy", node("Classification", node("Core"), node("Noise"))))])]
        rows = C.walk_tree(cls, canon_tier=lambda s: "Core" if s == "Core" else None)
        self.assertEqual([r["tier"] for r in rows], ["Core"])

    def test_real_nlm_record_classified_under_two_stewards(self):
        rows = C.walk_tree(BY_TINY["mZ7q9m2SPpZ"]["classification"])
        self.assertEqual({r["steward"] for r in rows}, {"NINDS", "NHLBI"})
        self.assertTrue(all(r["context"] for r in rows))

    def test_empty(self):
        self.assertEqual(C.walk_tree(None), [])
        self.assertEqual(C.walk_tree([]), [])
