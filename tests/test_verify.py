import unittest
from unittest import mock

from cde_importer import client as CL, verify as V

MIDS = {m: f"id-{m}" for m in CL.POST_ORDER}
BY_ID = {v: k for k, v in MIDS.items()}


def _payload():
    cls = {"cde_key": "c1", "steward_org": "NINDS", "context": "TBI", "sub_context": "*", "population": "*"}
    return {
        "provenance": [{"source_key": "nlm"}],
        "cde": [{"cde_key": "c1"}, {"cde_key": "c2"}],
        "form": [{"form_key": "f1"}],
        "cde_classification": [cls],
        "concept": [{"concept_key": "caDSR:1"}],
        "_rels": {
            "SOURCED_FROM_cde": [("c1", "nlm"), ("c2", "nlm")],
            "SOURCED_FROM_form": [("f1", "nlm")],
            "SOURCED_FROM_cde_classification": [(cls, "nlm")],
            "CLASSIFIES_cde": [(cls, "c1")],
            "PART_OF_form": [("c1", "f1"), ("c2", "f1")],
            "REPRESENTS": [("c1", "caDSR:1")],
        },
    }


class ExpectedTest(unittest.TestCase):
    def test_accumulates_distinct_keys_and_edges_across_batches(self):
        e = V.Expected().add(_payload()).add(_payload())
        self.assertEqual(e.counts(), {"provenance": 1, "cde": 2, "form": 1, "concept": 1, "cde_classification": 1})
        self.assertEqual(len(e.edges["SOURCED_FROM_cde"]), 2)
        self.assertEqual(len(e.edges["PART_OF_form"]), 2)
        self.assertIn((("c1", "NINDS", "TBI", "*", "*"), ("c1",)), e.edges["CLASSIFIES_cde"])


class _Dataset:
    """A fake dataset: records per model and edges per record id."""

    def __init__(self, payload, drop_records=(), drop_edges=()):
        self.records = {m: [] for m in CL.POST_ORDER}
        self.edges = {}  # record id -> [(direction, type, other record)]
        ids = {}
        for m in CL.POST_ORDER:
            for r in payload.get(m) or []:
                k = V.key_of(m, r)
                if (m, k) in drop_records:
                    continue
                rid = f"{m}:{'|'.join(str(x) for x in k)}"
                ids[(m, k)] = rid
                self.records[m].append({"id": rid, "model_id": MIDS[m], "model_version": 1, "value": r})
        for label, src, tgt, key in CL.RELATIONSHIPS:
            for s, t in (payload.get("_rels") or {}).get(key) or []:
                sk, tk = V._pair_key(src, s), V._pair_key(tgt, t)
                if (key, sk) in drop_edges or (src, sk) not in ids or (tgt, tk) not in ids:
                    continue
                srec = next(r for r in self.records[src] if r["id"] == ids[(src, sk)])
                trec = next(r for r in self.records[tgt] if r["id"] == ids[(tgt, tk)])
                self.edges.setdefault(srec["id"], []).append(("outbound", label, trec))
                self.edges.setdefault(trec["id"], []).append(("inbound", label, srec))

    def search(self, api2, token, q, model_id, page_size=500):
        yield from self.records[BY_ID[model_id]]

    def rels(self, api2, token, q, rid, page_size=1000):
        yield from self.edges.get(rid, [])


def _run(ds, expected, sample=25, full_edges=True):
    with mock.patch.object(V.client, "search_records", side_effect=ds.search), \
            mock.patch.object(V.client, "record_relationships", side_effect=ds.rels):
        return V.verify("api2", "t", "ds", MIDS, expected, sample=sample, seed=1, full_edges=full_edges)


class VerifyTest(unittest.TestCase):
    def test_clean_dataset_passes(self):
        p = _payload()
        res = _run(_Dataset(p), V.Expected().add(p))
        self.assertTrue(res.ok, res.problems)
        self.assertEqual(res.records["cde"], {"expected": 2, "dataset": 2, "missing": [], "stale": []})
        self.assertEqual(res.sourced_from, {"cde": {"expected": 2, "found": 2}, "form": {"expected": 1, "found": 1},
                                            "cde_classification": {"expected": 1, "found": 1}})
        self.assertEqual(res.sampled["PART_OF_form"], {"checked": 2, "missing": []})
        self.assertEqual(res.errors(), [])
        self.assertTrue(res.summary()["ok"])

    def test_missing_record_fails(self):
        p = _payload()
        res = _run(_Dataset(p, drop_records={("cde", ("c2",))}), V.Expected().add(p))
        self.assertFalse(res.ok)
        self.assertEqual(res.records["cde"]["missing"], [("c2",)])
        self.assertIn("cde: 1 posted records not found", res.problems[0])
        self.assertEqual(res.errors()[0][:3], ("verify", "cde", "c2"))

    def test_stale_record_is_reported_not_failed(self):
        p = _payload()
        ds = _Dataset(p)
        ds.records["cde"].append({"id": "cde:old", "model_id": MIDS["cde"], "model_version": 1, "value": {"cde_key": "old"}})
        res = _run(ds, V.Expected().add(p))
        self.assertTrue(res.ok)
        self.assertEqual(res.records["cde"]["stale"], [("old",)])
        self.assertEqual(res.summary()["stale_keys"], {"cde": ["old"]})

    def test_missing_sourced_from_fails(self):
        p = _payload()
        res = _run(_Dataset(p, drop_edges={("SOURCED_FROM_cde", ("c2",))}), V.Expected().add(p))
        self.assertFalse(res.ok)
        self.assertEqual(res.sourced_from["cde"], {"expected": 2, "found": 1})
        self.assertTrue(any(r[3].startswith("SOURCED_FROM edges") for r in res.errors()))

    def test_missing_sampled_edge_fails(self):
        p = _payload()
        res = _run(_Dataset(p, drop_edges={("PART_OF_form", ("c2",))}), V.Expected().add(p))
        self.assertFalse(res.ok)
        self.assertEqual(res.sampled["PART_OF_form"]["missing"], [(("c2",), ("f1",))])

    def test_default_samples_sourced_from_instead_of_counting(self):
        p = _payload()
        res = _run(_Dataset(p, drop_edges={("SOURCED_FROM_cde", ("c2",))}), V.Expected().add(p), full_edges=False)
        self.assertEqual(res.sourced_from, {})
        self.assertIn("SOURCED_FROM_cde", res.sampled)
        self.assertFalse(res.ok)
        self.assertEqual(res.sampled["SOURCED_FROM_cde"]["missing"], [(("c2",), ("nlm",))])

    def test_read_values_keeps_ids_and_values(self):
        p = _payload()
        ds = _Dataset(p)
        with mock.patch.object(V.client, "search_records", side_effect=ds.search):
            vals = V.read_values("api2", "t", "ds", MIDS, ["cde"])
        self.assertEqual(set(vals), {"cde"})
        self.assertEqual(vals["cde"][("c1",)]["value"], {"cde_key": "c1"})
        self.assertTrue(vals["cde"][("c1",)]["id"])

    def test_sample_zero_skips_edge_sampling(self):
        p = _payload()
        res = _run(_Dataset(p, drop_edges={("PART_OF_form", ("c2",))}), V.Expected().add(p), sample=0)
        self.assertTrue(res.ok)
        self.assertEqual(res.sampled, {})

    def test_only_posted_models_are_read(self):
        p = _payload()
        ds = _Dataset(p)
        calls = []
        with mock.patch.object(V.client, "search_records", side_effect=lambda *a, **k: calls.append(a[3]) or ds.search(*a, **k)), \
                mock.patch.object(V.client, "record_relationships", side_effect=ds.rels):
            V.verify("api2", "t", "ds", MIDS, V.Expected().add(p), sample=0)
        self.assertNotIn(MIDS["bundle"], calls)
        self.assertIn(MIDS["cde"], calls)
