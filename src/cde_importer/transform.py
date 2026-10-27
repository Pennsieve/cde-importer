"""
Transform reader output into CDE catalog records and relationships.

A reader (NLM fetch, NINDS fetch, spreadsheet) hands over flat input dicts whose
keys are the catalog column names; list-valued columns are pipe-joined strings
(`a|b|c`). transform_cde turns one input into a `cde` record; build_payload wires
a whole run (cdes, classifications, forms, bundles, memberships, one provenance)
into the record-set that client.post_data loads.
"""
import json
import pathlib
import re

MODELS = ("cde", "form", "bundle", "concept", "provenance",
          "cde_classification", "bundle_classification", "form_classification")

DATATYPES = {"Number", "Text", "Value List", "Date", "Datetime", "Time",
             "File/URI/URL", "Geolocation", "Other"}

# Canonical NINDS tier vocabulary; unknown source tiers are preserved verbatim.
TIER_MAP = {
    "core": "Core", "disease core": "Core",
    "supplemental": "Supplemental",
    "supplemental - highly recommended": "Supplemental - Highly Recommended",
    "supplemental-highly recommended": "Supplemental - Highly Recommended",
    "supplemental highly recommended": "Supplemental - Highly Recommended",
    "exploratory": "Exploratory",
    "not applicable": "Not Applicable",
}

NINDS_CODE_RE = re.compile(r"^C\d+$")

# Provenance fields the provenance model accepts. Readers may carry extra fields;
# the metadata-service rejects unknown properties, so whitelist to the schema.
PROV_FIELDS = {
    "source_key", "provenance_name", "status", "study_type", "source_org", "source_url",
    "license", "attribution", "retrieved_at", "workgroup", "etl_version",
}
_KIND_TO_STATUS = {"sample": "draft", "preliminary": "draft", "production": "released", "final": "released"}


def pipesplit(v):
    if v is None:
        return []
    return [s.strip() for s in str(v).split("|")]


def norm_name(name):
    return "name:" + re.sub(r"[^a-zA-Z0-9]+", " ", (name or "").lower()).strip()


def canonical_key(d):
    return (d.get("nlm_identifier") or "").strip() \
        or (d.get("dec_identifier") or "").strip() \
        or norm_name(d.get("cde_name"))


def canon_tier(raw):
    t = (raw or "").strip()
    return TIER_MAP.get(t.lower(), t) if t else None


def num_or_none(v):
    try:
        return float(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def build_permissible_values(d):
    labels = pipesplit(d.get("pv_labels"))
    if not labels or labels == [""]:
        return None
    codes = pipesplit(d.get("pv_codes"))
    defs = pipesplit(d.get("pv_definitions"))
    systems = pipesplit(d.get("pv_code_systems"))
    system_versions = pipesplit(d.get("pv_code_system_versions"))
    concepts = pipesplit(d.get("pv_concept_identifiers"))
    sources = pipesplit(d.get("pv_terminology_sources"))

    def at(lst, i):
        return lst[i] if i < len(lst) and lst[i] != "" else None

    pvs = []
    for i, label in enumerate(labels):
        pvs.append({k: v for k, v in {
            "ordinal": i, "label": label,
            "code": at(codes, i), "definition": at(defs, i),
            "code_system": at(systems, i),
            "code_system_version": at(system_versions, i),
            "concept_curie": at(concepts, i),
            "terminology_source": at(sources, i),
        }.items() if v is not None})
    return pvs


def _ref_list(v):
    """references is an array in the schema; sources give a string (NLM joins its
    reference documents with '; '). Split into a list, else None."""
    if not v:
        return None
    parts = [s.strip() for s in re.split(r"\s*[;|]\s*", str(v)) if s.strip()]
    return parts or None


def transform_cde(d, default_status="Recorded"):
    dt = d.get("cde_data_type")
    return {k: v for k, v in {
        # a curated source (spreadsheet) supplies cde_key directly; otherwise compute it.
        "cde_key": d.get("cde_key") or canonical_key(d),
        "version": 1,
        "registration_status": d.get("registration_status") or default_status,
        "cde_name": d.get("cde_name"),
        "variable_name": d.get("variable_name"),
        "cde_type": d.get("cde_type"),
        "concatenation_rule": d.get("concatenation_rule"),
        "calculation_formula": d.get("calculation_formula"),
        "additional_instructions": d.get("additional_instructions"),
        "input_restrictions": d.get("input_restrictions"),
        "cde_definition": d.get("cde_definition"),
        "cde_data_type": dt if dt in DATATYPES else "Other",
        "cde_source": d.get("cde_source"),
        "source_version": d.get("source_version"),
        "copyright_status": d.get("copyright_status"),
        "nih_endorsed": d.get("nih_endorsed"),
        "steward_org": d.get("steward_org"),
        "preferred_question_text": d.get("preferred_question_text"),
        "aliases": pipesplit(d.get("aliases")) or None,
        "keywords": pipesplit(d.get("keywords")) or None,
        "permissible_values": build_permissible_values(d),
        "unit_of_measure": d.get("unit_of_measure"),
        "min_value": num_or_none(d.get("min_value")),
        "max_value": num_or_none(d.get("max_value")),
        "cde_origin": d.get("cde_origin"),
        "population": d.get("population"),
        "references": _ref_list(d.get("references")),
        "nlm_identifier": d.get("nlm_identifier"),
        "dec_identifier": d.get("dec_identifier"),
        "dec_terminology_source": d.get("dec_terminology_source"),
        "dec_name": d.get("dec_name"),
        "nlm_view_url": d.get("nlm_view_url"),
        "other_identifiers": [
            {"label": p.split(":", 1)[0], "value": p.split(":", 1)[1]}
            for p in pipesplit(d.get("other_identifiers")) if ":" in p
        ] or None,
    }.items() if v is not None}


def build_xrefs(rec):
    """Cross-reference crosswalk from a transformed cde record's id fields: the
    primary upstream id, other_identifiers (already labeled), and caDSR DEC ids.
    `match: carried` = ingested with the record (a curator-asserted `exact`
    equivalence is added later). Deduped by (system, id). This is what keeps the
    minted persistent_id non-lock-in: the crosswalk is published alongside it."""
    xrefs, seen = [], set()

    def add(system, value, match="carried"):
        system = (system or "").strip()
        value = (value or "").strip()
        if not system or not value or (system, value) in seen:
            return
        seen.add((system, value))
        xrefs.append({"system": system, "id": value, "match": match})

    nlm = (rec.get("nlm_identifier") or "").strip()
    if nlm:
        # NINDS catalog codes are C-prefixed; anything else in this field is an NLM tinyId.
        add("NINDS" if NINDS_CODE_RE.match(nlm) else "NLM", nlm)
    for oi in (rec.get("other_identifiers") or []):
        add(oi.get("label"), oi.get("value"))
    ids, srcs = pipesplit(rec.get("dec_identifier")), pipesplit(rec.get("dec_terminology_source"))
    for i, did in enumerate(ids):
        src = (srcs[i] if i < len(srcs) and srcs[i] else (srcs[0] if srcs else "caDSR")) or "caDSR"
        add(src, did)

    url = (rec.get("nlm_view_url") or "").strip()
    if url:
        for x in xrefs:
            if x["system"] == "NLM":
                x["url"] = url
                break
    return xrefs or None


def derive_concepts(cde_records):
    """Concept records + REPRESENTS pairs from each CDE's dec_identifier (caDSR DEC)."""
    concepts, reps = {}, []
    for c in cde_records:
        ids = pipesplit(c.get("dec_identifier"))
        names = pipesplit(c.get("dec_name"))
        srcs = pipesplit(c.get("dec_terminology_source"))
        for i, did in enumerate(ids):
            if not did:
                continue
            onto = (srcs[i] if i < len(srcs) and srcs[i] else (srcs[0] if srcs else "caDSR")) or "caDSR"
            curie = f"{onto}:{did}"
            label = (names[i] if i < len(names) and names[i] else (names[0] if names else did)) or did
            # No fabricated IRI: only the caDSR public id is known. Leave iri unset.
            concepts.setdefault(curie, {k: v for k, v in {
                "concept_key": curie, "curie": curie, "concept_name": label, "ontology": onto,
            }.items() if v is not None})
            reps.append((c["cde_key"], curie))
    return list(concepts.values()), reps


def transform_provenance(d, license_=None, attribution=None, status=None):
    r = {k: v for k, v in d.items() if v is not None and k in PROV_FIELDS}
    if "status" not in r and d.get("kind"):
        r["status"] = _KIND_TO_STATUS.get(str(d["kind"]).lower(), "draft")
    if status:
        r["status"] = status
    if license_:
        r["license"] = license_
    if attribution:
        r["attribution"] = attribution
    return r


CDE_CLS_KEY = ("cde_key", "steward_org", "context", "sub_context", "population")
BUNDLE_CLS_KEY = ("bundle_key", "steward_org", "context", "sub_context", "population")
FORM_CLS_KEY = ("form_key", "steward_org", "context", "sub_context", "population")


def _ckey(rec, fields):
    """The composite-key dict for a classification record (its x-pennsieve-key subset)."""
    return {f: rec.get(f) for f in fields}


def build_payload(cde_inputs, cde_cls, forms, form_cls, bundles, bundle_cls,
                  memberships, prov_input, license_=None, attribution=None, status=None):
    """Assemble one run's record-set. The reader hands over already-typed records
    (classification rows already carry their subject key + coordinate); this applies
    transform_cde to the raw cde inputs, derives concepts, and wires every
    relationship (CLASSIFIES / PART_OF / REPRESENTS / SOURCED_FROM). One provenance.

    memberships: {"form": [(cde_key, form_key), ...], "bundle": [(cde_key, bundle_key), ...]}
    """
    cde_records = []
    for d in cde_inputs:
        r = transform_cde(d)
        xr = build_xrefs(r)
        if xr:
            r["xrefs"] = xr
        cde_records.append(r)
    concept_records, represents = derive_concepts(cde_records)
    prov_records = [transform_provenance(prov_input, license_, attribution, status)]
    prov_key = prov_records[0]["source_key"]

    CK, BK, FK = CDE_CLS_KEY, BUNDLE_CLS_KEY, FORM_CLS_KEY
    rels = {
        "CLASSIFIES_cde": [(_ckey(e, CK), e["cde_key"]) for e in cde_cls],
        "CLASSIFIES_bundle": [(_ckey(e, BK), e["bundle_key"]) for e in bundle_cls],
        "CLASSIFIES_form": [(_ckey(e, FK), e["form_key"]) for e in form_cls],
        "PART_OF_form": list(memberships.get("form", [])),
        "PART_OF_bundle": list(memberships.get("bundle", [])),
        "REPRESENTS": represents,
        "SOURCED_FROM_cde": [(r["cde_key"], prov_key) for r in cde_records],
        "SOURCED_FROM_form": [(r["form_key"], prov_key) for r in forms if r.get("form_key")],
        "SOURCED_FROM_bundle": [(r["bundle_key"], prov_key) for r in bundles if r.get("bundle_key")],
        "SOURCED_FROM_cde_classification": [(_ckey(e, CK), prov_key) for e in cde_cls],
        "SOURCED_FROM_bundle_classification": [(_ckey(e, BK), prov_key) for e in bundle_cls],
        "SOURCED_FROM_form_classification": [(_ckey(e, FK), prov_key) for e in form_cls],
    }
    return {"cde": cde_records, "form": forms, "bundle": bundles, "concept": concept_records,
            "provenance": prov_records, "cde_classification": cde_cls,
            "bundle_classification": bundle_cls, "form_classification": form_cls, "_rels": rels}


def merge_payloads(payloads):
    """Merge several payloads into one: records deduplicated by key (last writer
    wins), relationship pairs deduplicated. Posting one merged payload means every
    record is created once and every edge after all records, which matters because
    re-posting a record drops the edges already attached to it."""
    from .client import KEY, dedupe_pairs
    merged = empty_payload()
    seen = {m: {} for m in MODELS}
    for p in payloads:
        for m in MODELS:
            for r in p.get(m) or []:
                seen[m][tuple(r.get(f) for f in KEY[m])] = r
        for k, pairs in (p.get("_rels") or {}).items():
            merged["_rels"].setdefault(k, []).extend(pairs)
    for m in MODELS:
        merged[m] = list(seen[m].values())
    merged["_rels"] = {k: dedupe_pairs(v) for k, v in merged["_rels"].items()}
    return merged


def same_value(posted, stored):
    """True when a posted record equals the stored one: nulls dropped, JSON
    round-tripped (so 1 == 1.0, tuples == lists), keys compared in any order."""
    def norm(v):
        return json.loads(json.dumps({k: x for k, x in (v or {}).items() if x is not None}, sort_keys=True))
    return norm(posted) == norm(stored)


def plan_changes(payload, stored):
    """Split a merged payload into what must be posted, given the dataset's current
    records (`stored`: model -> {key tuple: {"value": ...}}, from verify.read_values).
    A record is posted when it is new or its value changed. An edge is posted when
    either endpoint is posted, because a re-posted record drops the edges it had.
    Returns (payload_to_post, stats) with stats[model] = {new, changed, unchanged}."""
    from .client import KEY, RELATIONSHIPS
    out = empty_payload()
    post_keys = {m: set() for m in MODELS}
    stats = {}
    for m in MODELS:
        have = stored.get(m) or {}
        new = changed = unchanged = 0
        for r in payload.get(m) or []:
            k = tuple(r.get(f) for f in KEY[m])
            if k not in have:
                new += 1
            elif not same_value(r, have[k].get("value")):
                changed += 1
            else:
                unchanged += 1
                continue
            out[m].append(r)
            post_keys[m].add(k)
        if new or changed or unchanged:
            stats[m] = {"new": new, "changed": changed, "unchanged": unchanged}

    def pk(model, val):
        return tuple(val.get(f) for f in KEY[model]) if isinstance(val, dict) else (val,)

    rels = payload.get("_rels") or {}
    for _label, src, tgt, key in RELATIONSHIPS:
        kept = [(s, t) for s, t in rels.get(key) or []
                if pk(src, s) in post_keys[src] or pk(tgt, t) in post_keys[tgt]]
        if kept:
            out["_rels"][key] = kept
    return out, stats


def empty_payload():
    """A payload with no records, for partial passes (e.g. forms only)."""
    return {m: [] for m in MODELS} | {"_rels": {}}


def summarize(payload):
    s = {m: len(payload.get(m) or []) for m in MODELS}
    s["relationships"] = sum(len(v) for v in (payload.get("_rels") or {}).values())
    return s


def emit_payload(out_dir, payload):
    """Write a payload to disk as records_<model>.json + relationships.json."""
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for m in MODELS:
        (out / f"records_{m}.json").write_text(json.dumps({"records": payload.get(m) or []}))
    (out / "relationships.json").write_text(json.dumps(payload.get("_rels") or {}))
    return out
