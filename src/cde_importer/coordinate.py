#!/usr/bin/env python3
"""
Walk an NLM/NINDS classification tree into FLAT scoping rows for the classification
models (shared by fetch_nlm / fetch_ninds). Each row:

    {steward, context, sub_context, population, domain, tier, extra}

The scoping columns (context, sub_context, population) are the record identity.
WILDCARD ("*") means "applies to ALL values of this axis" — a row with
sub_context="*" applies to the whole context; population="*" to every population.

  context     = the disease / study / collection (value under the steward's "Disease"
                axis, or a top value node for non-disease stewards)
  sub_context = the scenario / severity / phase within the context (a value node
                nested under the disease before the Classification axis); "*" if none
  population  = "Population" axis value (steward- or context-level); "*" if none
  domain      = level-1 of the "Domain" axis (descriptive; not identity)
  tier        = the "Classification" axis value (or a bare tier leaf like NLM "Tier 1")
  extra       = reserved escape hatch for other steward axes (unused for now -> None)

This replaces the earlier coordinate/facets/serialization encoding with plain columns.
"""

WILDCARD = "*"
SEP = " › "  # joins levels of a sub_context path (matched as a prefix downstream)
TIER_AXIS = "Classification"
CONTEXT_AXES = {"Disease"}
POPULATION_AXES = {"Population"}
DOMAIN_AXES = {"Domain"}
KNOWN_AXES = {TIER_AXIS} | CONTEXT_AXES | POPULATION_AXES | DOMAIN_AXES | {"Subdomain", "Category"}


def _steward_name(root):
    s = root.get("stewardOrg")
    return s.get("name") if isinstance(s, dict) else s


def _first_leaf(axis_node):
    """First level-1 value name under an axis node (its immediate children)."""
    for ch in (axis_node.get("elements") or []):
        if ch.get("name"):
            return ch.get("name")
    return None


def _emit(steward, context, node, sub_context, population, domain, canon_tier, rows):
    """Emit rows for a context/sub-context value node. Reads its child axes for a
    tier (Classification), a Domain, a context-level Population, and deeper
    sub-context value nodes (recursed). A bare tier-valued leaf (no Classification
    axis, e.g. NLM 'Tier 1') is treated as the tier."""
    tiers, subs = [], []
    dom, pop = domain, population
    for ch in (node.get("elements") or []):
        nm = ch.get("name")
        if not nm:
            continue
        if nm == TIER_AXIS:
            for g in (ch.get("elements") or []):
                if g.get("name"):
                    t = canon_tier(g["name"]) if canon_tier else g["name"]
                    if t:
                        tiers.append(t)
        elif nm in DOMAIN_AXES:
            if dom is None:
                dom = _first_leaf(ch)
        elif nm in POPULATION_AXES:
            p = _first_leaf(ch)
            if p:
                pop = p
        elif nm in KNOWN_AXES:
            pass  # Subdomain / Category — descriptive, dropped from the flat row
        else:
            kids = ch.get("elements") or []
            t = canon_tier(nm) if canon_tier else None
            if t and not kids:
                tiers.append(t)          # bare tier leaf (Project 5 'Tier 1')
            else:
                subs.append(ch)          # deeper scenario / sub-context

    if tiers:
        for t in tiers:
            rows.append({"steward": steward, "context": context, "sub_context": sub_context,
                         "population": pop, "domain": dom, "tier": t})
    elif not subs:
        rows.append({"steward": steward, "context": context, "sub_context": sub_context,
                     "population": pop, "domain": dom, "tier": None})
    for sc in subs:
        child = sc.get("name")
        # extend the sub_context PATH so deeper nesting is preserved (matched as a
        # prefix at resolve time). At the top level sub_context is the WILDCARD.
        new_sub = child if sub_context in (WILDCARD, None, "") else sub_context + SEP + child
        _emit(steward, context, sc, new_sub, pop, dom, canon_tier, rows)


def walk_tree(classifications, canon_tier=None):
    """An NLM/NINDS classification tree -> a list of flat classification rows.
    `canon_tier(str)->str|None` canonicalizes a Classification value (drops non-tier
    values). One row per (steward, context, sub_context, population) with a tier, plus
    a tier-less placement row for a leaf context that carries no tier."""
    rows = []
    for root in (classifications or []):
        steward = _steward_name(root)
        top = root.get("elements") or []
        disease_axis = next((t for t in top if t.get("name") in CONTEXT_AXES), None)
        # steward-level Population / Domain siblings apply to every context under this steward
        st_pop, st_dom = None, None
        for t in top:
            nm = t.get("name")
            if not nm or t is disease_axis:
                continue
            if nm in POPULATION_AXES:
                st_pop = st_pop or _first_leaf(t)
            elif nm in DOMAIN_AXES:
                st_dom = st_dom or _first_leaf(t)
        if disease_axis:
            for d in (disease_axis.get("elements") or []):
                if d.get("name"):
                    _emit(steward, d.get("name"), d, WILDCARD, st_pop or WILDCARD, st_dom, canon_tier, rows)
        else:
            for t in top:
                nm = t.get("name")
                if nm and nm not in KNOWN_AXES:
                    _emit(steward, nm, t, WILDCARD, st_pop or WILDCARD, st_dom, canon_tier, rows)
    return rows
