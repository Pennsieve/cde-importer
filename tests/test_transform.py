import json
import pathlib
import unittest

from cde_importer import transform as T

CDE = {
    "cde_name": "Adverse event CTCAE low level term name",
    "cde_definition": "Name of the low level term.",
    "cde_data_type": "Value List",
    "cde_source": "NLM CDE Repository",
    "steward_org": "NINDS",
    "nlm_identifier": "C02310",
    "nlm_view_url": "https://cde.nlm.nih.gov/deView?tinyId=mZ7q9m2SPpZ",
    "other_identifiers": "ISTH:1:38:20:428|NLM:mZ7q9m2SPpZ|bad entry",
    "dec_identifier": "3125302",
    "dec_terminology_source": "caDSR",
    "aliases": "AE term|LLT name",
    "pv_labels": "Yes|No|Unknown",
    "pv_codes": "Y|N",
    "pv_definitions": "|Negative|",
    "min_value": "0",
    "max_value": "abc",
    "references": "Doc A; Doc B",
}


class TransformCdeTest(unittest.TestCase):
    def test_key_from_nlm_identifier(self):
        r = T.transform_cde(CDE)
        self.assertEqual(r["cde_key"], "C02310")
        self.assertEqual(r["version"], 1)
        self.assertEqual(r["registration_status"], "Recorded")

    def test_key_passthrough_and_fallbacks(self):
        self.assertEqual(T.transform_cde({"cde_key": "k1", "cde_name": "x"})["cde_key"], "k1")
        self.assertEqual(T.canonical_key({"dec_identifier": "123"}), "123")
        self.assertEqual(T.canonical_key({"cde_name": "Blood  Pressure!"}), "name:blood pressure")

    def test_data_type_fallback(self):
        self.assertEqual(T.transform_cde({"cde_name": "x", "cde_data_type": "Weird"})["cde_data_type"], "Other")
        self.assertEqual(T.transform_cde(CDE)["cde_data_type"], "Value List")

    def test_lists_and_numbers(self):
        r = T.transform_cde(CDE)
        self.assertEqual(r["aliases"], ["AE term", "LLT name"])
        self.assertEqual(r["references"], ["Doc A", "Doc B"])
        self.assertEqual(r["min_value"], 0.0)
        self.assertNotIn("max_value", r)
        self.assertEqual(r["other_identifiers"], [{"label": "ISTH", "value": "1:38:20:428"},
                                                  {"label": "NLM", "value": "mZ7q9m2SPpZ"}])

    def test_permissible_values_alignment(self):
        pvs = T.build_permissible_values(CDE)
        self.assertEqual([p["label"] for p in pvs], ["Yes", "No", "Unknown"])
        self.assertEqual(pvs[0]["code"], "Y")
        self.assertNotIn("code", pvs[2])
        self.assertEqual(pvs[1]["definition"], "Negative")
        self.assertNotIn("definition", pvs[0])
        self.assertIsNone(T.build_permissible_values({"pv_labels": ""}))

    def test_nulls_dropped(self):
        r = T.transform_cde({"cde_name": "x"})
        self.assertNotIn("aliases", r)
        self.assertNotIn("permissible_values", r)
        self.assertNotIn("other_identifiers", r)

    def test_canon_tier(self):
        self.assertEqual(T.canon_tier("Supplemental-Highly Recommended"), "Supplemental - Highly Recommended")
        self.assertEqual(T.canon_tier("Basic"), "Basic")
        self.assertIsNone(T.canon_tier(""))


class XrefsAndConceptsTest(unittest.TestCase):
    def test_xrefs(self):
        r = T.transform_cde(CDE)
        x = T.build_xrefs(r)
        systems = [(e["system"], e["id"]) for e in x]
        self.assertEqual(systems[0], ("NINDS", "C02310"))
        self.assertIn(("NLM", "mZ7q9m2SPpZ"), systems)
        self.assertIn(("caDSR", "3125302"), systems)
        self.assertEqual(len(systems), len(set(systems)))
        nlm = next(e for e in x if e["system"] == "NLM")
        self.assertEqual(nlm["url"], CDE["nlm_view_url"])
        self.assertTrue(all(e["match"] == "carried" for e in x))

    def test_xrefs_tinyid_key(self):
        x = T.build_xrefs({"nlm_identifier": "mZ7q9m2SPpZ"})
        self.assertEqual(x, [{"system": "NLM", "id": "mZ7q9m2SPpZ", "match": "carried"}])
        self.assertIsNone(T.build_xrefs({"cde_name": "x"}))

    def test_derive_concepts(self):
        a = T.transform_cde(CDE)
        b = T.transform_cde({**CDE, "nlm_identifier": "C99", "dec_name": "Low level term"})
        concepts, reps = T.derive_concepts([a, b])
        self.assertEqual(len(concepts), 1)
        self.assertEqual(concepts[0]["concept_key"], "caDSR:3125302")
        self.assertEqual(concepts[0]["ontology"], "caDSR")
        self.assertEqual(reps, [("C02310", "caDSR:3125302"), ("C99", "caDSR:3125302")])


class ProvenanceTest(unittest.TestCase):
    def test_whitelist_and_overrides(self):
        p = T.transform_provenance({"source_key": "nlm", "provenance_name": "NLM", "bogus": 1,
                                    "kind": "production", "retrieved_at": None},
                                   license_="PD", attribution="NLM")
        self.assertEqual(p, {"source_key": "nlm", "provenance_name": "NLM", "status": "released",
                             "license": "PD", "attribution": "NLM"})

    def test_status_override(self):
        p = T.transform_provenance({"source_key": "x", "kind": "sample"}, status="deprecated")
        self.assertEqual(p["status"], "deprecated")


class BuildPayloadTest(unittest.TestCase):
    def setUp(self):
        cls_row = {"cde_key": "C02310", "steward_org": "NINDS", "context": "Epilepsy",
                   "sub_context": "*", "population": "*", "tier": "Core"}
        self.payload = T.build_payload(
            [CDE], [cls_row],
            [{"form_key": "nlm:f1", "form_name": "F"}], [],
            [{"bundle_key": "nlm:b1", "bundle_name": "B"}],
            [{"bundle_key": "nlm:b1", "steward_org": "NINDS", "context": "Epilepsy",
              "sub_context": "*", "population": "*"}],
            {"form": [("C02310", "nlm:f1")], "bundle": [("C02310", "nlm:b1")]},
            {"source_key": "nlm", "provenance_name": "NLM CDE Repository"}, "PD", "attr")

    def test_records(self):
        p = self.payload
        self.assertEqual(p["cde"][0]["cde_key"], "C02310")
        self.assertIn("xrefs", p["cde"][0])
        self.assertEqual(p["provenance"], [{"source_key": "nlm", "provenance_name": "NLM CDE Repository",
                                            "license": "PD", "attribution": "attr"}])
        self.assertEqual(len(p["concept"]), 1)

    def test_relationships(self):
        r = self.payload["_rels"]
        self.assertEqual(r["CLASSIFIES_cde"], [({"cde_key": "C02310", "steward_org": "NINDS", "context": "Epilepsy",
                                                 "sub_context": "*", "population": "*"}, "C02310")])
        self.assertEqual(r["CLASSIFIES_bundle"][0][1], "nlm:b1")
        self.assertEqual(r["PART_OF_form"], [("C02310", "nlm:f1")])
        self.assertEqual(r["REPRESENTS"], [("C02310", "caDSR:3125302")])
        for key in ("SOURCED_FROM_cde", "SOURCED_FROM_form", "SOURCED_FROM_bundle",
                    "SOURCED_FROM_cde_classification", "SOURCED_FROM_bundle_classification"):
            self.assertEqual({t for _s, t in r[key]}, {"nlm"}, key)
        self.assertEqual(r["SOURCED_FROM_form_classification"], [])

    def test_summarize_and_emit(self):
        s = T.summarize(self.payload)
        self.assertEqual((s["cde"], s["form"], s["bundle"], s["provenance"]), (1, 1, 1, 1))
        self.assertEqual(s["relationships"], 10)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = T.emit_payload(d, self.payload)
            names = sorted(p.name for p in pathlib.Path(out).iterdir())
            self.assertEqual(len(names), 9)
            self.assertIn("records_cde.json", names)
            self.assertEqual(json.loads((out / "records_cde.json").read_text())["records"][0]["cde_key"], "C02310")

    def test_empty_payload(self):
        e = T.empty_payload()
        self.assertEqual(T.summarize(e)["relationships"], 0)
        self.assertEqual(sorted(e), sorted(list(T.MODELS) + ["_rels"]))


class MergePayloadsTest(unittest.TestCase):
    def test_records_once_edges_once_last_writer_wins(self):
        a = {"provenance": [{"source_key": "nlm"}], "cde": [{"cde_key": "c1", "cde_name": "A"}, {"cde_key": "c2"}],
             "_rels": {"SOURCED_FROM_cde": [("c1", "nlm"), ("c2", "nlm")],
                       "CLASSIFIES_cde": [({"cde_key": "c1", "steward_org": "NINDS", "context": "X",
                                            "sub_context": "*", "population": "*"}, "c1")]}}
        b = {"cde": [{"cde_key": "c1", "cde_name": "B"}, {"cde_key": "c3"}],
             "_rels": {"SOURCED_FROM_cde": [("c1", "nlm"), ("c3", "nlm")],
                       "CLASSIFIES_cde": [({"cde_key": "c1", "steward_org": "NHLBI", "context": "Y",
                                            "sub_context": "*", "population": "*"}, "c1")]}}
        m = T.merge_payloads([a, b])
        self.assertEqual([r["cde_key"] for r in m["cde"]], ["c1", "c2", "c3"])
        self.assertEqual(next(r for r in m["cde"] if r["cde_key"] == "c1")["cde_name"], "B")
        self.assertEqual(len(m["provenance"]), 1)
        self.assertEqual(sorted(m["_rels"]["SOURCED_FROM_cde"]), [("c1", "nlm"), ("c2", "nlm"), ("c3", "nlm")])
        self.assertEqual(len(m["_rels"]["CLASSIFIES_cde"]), 2)
        self.assertEqual(m["form"], [])
        self.assertEqual(T.summarize(m)["relationships"], 5)

    def test_merge_empty(self):
        m = T.merge_payloads([])
        self.assertEqual(T.summarize(m)["relationships"], 0)
