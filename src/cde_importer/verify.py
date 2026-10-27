"""
Verify a loaded dataset against what a run posted.

The metadata service only reads relationships per record (about half a second
each), so a full both-sided edge check of tens of thousands of records is not
affordable on every run. Verify therefore does three things:

1. Records: read every model back and compare key sets with the payload(s)
   posted. Missing keys fail the run; keys present in the dataset but absent
   from the run are reported as stale (the source no longer serves them).
2. SOURCED_FROM: every record must point at the provenance. The provenance
   record's inbound edges are read in full (one record, paged) and counted by
   source model, which checks every SOURCED_FROM edge at the cost of a few
   dozen requests.
3. The other edge types (CLASSIFIES, PART_OF, REPRESENTS) are checked from
   both sides on a random sample of records per edge type.
"""
import logging
import random

from . import client

log = logging.getLogger("cde_importer.verify")

PROVENANCE = "provenance"


def key_of(model, value):
    """A record's key tuple, from its value dict."""
    return tuple((value or {}).get(f) for f in client.KEY[model])


def _pair_key(model, val):
    """A relationship endpoint as the payload gives it (scalar or key dict) -> key tuple."""
    if isinstance(val, dict):
        return tuple(val.get(f) for f in client.KEY[model])
    return (val,)


class Expected:
    """Accumulates what a run posted across batches: distinct keys per model and
    distinct relationship pairs per edge type."""

    def __init__(self):
        self.keys = {m: set() for m in client.POST_ORDER}
        self.edges = {k: set() for _l, _s, _t, k in client.RELATIONSHIPS}

    def add(self, payload):
        for m in client.POST_ORDER:
            for r in payload.get(m) or []:
                self.keys[m].add(key_of(m, r))
        rels = payload.get("_rels") or {}
        for _label, src, tgt, key in client.RELATIONSHIPS:
            for s, t in rels.get(key) or []:
                self.edges[key].add((_pair_key(src, s), _pair_key(tgt, t)))
        return self

    def counts(self):
        return {m: len(k) for m, k in self.keys.items() if k}


class VerifyResult:
    def __init__(self):
        self.records = {}        # model -> {"expected", "dataset", "missing", "stale"}
        self.sourced_from = {}   # model -> {"expected", "found"}
        self.sampled = {}        # edge key -> {"checked", "missing"}
        self.problems = []       # human-readable failures

    @property
    def ok(self):
        return not self.problems

    def errors(self):
        """Rows for errors.csv: (stage, model, key, message)."""
        rows = []
        for m, r in self.records.items():
            for k in r["missing"]:
                rows.append(("verify", m, "|".join(str(x) for x in k), "posted but not found in the dataset"))
        for m, r in self.sourced_from.items():
            if r["found"] != r["expected"]:
                rows.append(("verify", m, "", f"SOURCED_FROM edges: expected {r['expected']}, found {r['found']}"))
        for key, r in self.sampled.items():
            for s, t in r["missing"]:
                rows.append(("verify", key, "|".join(str(x) for x in s), "edge not found to " + "|".join(str(x) for x in t)))
        return rows

    def summary(self):
        return {
            "ok": self.ok,
            "records": {m: {"expected": r["expected"], "dataset": r["dataset"], "missing": len(r["missing"]),
                            "stale": len(r["stale"])} for m, r in self.records.items()},
            "stale_keys": {m: sorted("|".join(str(x) for x in k) for k in r["stale"])[:500]
                           for m, r in self.records.items() if r["stale"]},
            "sourced_from": self.sourced_from,
            "sampled_edges": {k: {"checked": r["checked"], "missing": len(r["missing"])} for k, r in self.sampled.items()},
            "problems": self.problems,
        }


def read_keys(api2, token, dataset_q, model_ids, models=None):
    """model -> {key tuple: record id} for every record in the dataset."""
    out = {}
    for m in models or client.POST_ORDER:
        if m not in model_ids:
            continue
        out[m] = {key_of(m, r.get("value")): r["id"] for r in client.search_records(api2, token, dataset_q, model_ids[m])}
        log.info("read back %s: %d records", m, len(out[m]))
    return out


def verify(api2, token, dataset_q, model_ids, expected, sample=25, seed=None):
    """Compare the dataset with what the run posted. Returns a VerifyResult;
    result.ok is False when any posted record or edge is missing."""
    res = VerifyResult()
    by_id = {v: k for k, v in model_ids.items()}
    have = read_keys(api2, token, dataset_q, model_ids, [m for m in client.POST_ORDER if expected.keys.get(m)])

    for m, keys in expected.keys.items():
        if not keys:
            continue
        got = have.get(m, {})
        missing = sorted(k for k in keys if k not in got)
        stale = sorted(k for k in got if k not in keys)
        res.records[m] = {"expected": len(keys), "dataset": len(got), "missing": missing, "stale": stale}
        if missing:
            res.problems.append(f"{m}: {len(missing)} posted records not found in the dataset")

    # SOURCED_FROM: count the provenance record's inbound edges by source model
    prov = have.get(PROVENANCE) or {}
    exp_sf = {}
    for _label, src, tgt, key in client.RELATIONSHIPS:
        if tgt == PROVENANCE and expected.edges.get(key):
            exp_sf[src] = len(expected.edges[key])
    if exp_sf and prov:
        found = {}
        for prov_id in prov.values():
            for direction, rtype, rec in client.record_relationships(api2, token, dataset_q, prov_id):
                if direction == "inbound" and rtype == "SOURCED_FROM":
                    src = by_id.get(rec.get("model_id"))
                    found[src] = found.get(src, 0) + 1
        for src, n in exp_sf.items():
            res.sourced_from[src] = {"expected": n, "found": found.get(src, 0)}
            if found.get(src, 0) < n:
                res.problems.append(f"SOURCED_FROM {src}->provenance: expected {n}, found {found.get(src, 0)}")

    # other edge types: sample source records, read their edges, check the targets
    rng = random.Random(seed)
    for label, src, tgt, key in client.RELATIONSHIPS:
        pairs = expected.edges.get(key)
        if tgt == PROVENANCE or not pairs or sample <= 0:
            continue
        by_source = {}
        for s, t in pairs:
            by_source.setdefault(s, set()).add(t)
        chosen = rng.sample(sorted(by_source), min(sample, len(by_source)))
        checked, missing = 0, []
        src_ids = have.get(src) or {}
        for s in chosen:
            rid = src_ids.get(s)
            if rid is None:
                continue
            found = {key_of(tgt, rec.get("value")) for d, rtype, rec in
                     client.record_relationships(api2, token, dataset_q, rid)
                     if d == "outbound" and rtype == label and by_id.get(rec.get("model_id")) == tgt}
            checked += 1
            for t in by_source[s]:
                if t not in found:
                    missing.append((s, t))
        res.sampled[key] = {"checked": checked, "missing": missing}
        if missing:
            res.problems.append(f"{key}: {len(missing)} edges missing on a sample of {checked} source records")
    return res
