"""
The eight catalog model schemas (vendored) and the workspace-template / dataset-
model operations built on them.

Every source dataset derives its models from the SAME workspace templates, so
their models and keys stay identical; the integrate merge depends on this. A
sync app must never bump a template or model version (existing records stay on
the old version until re-imported); it only checks, via check_models, that the
dataset's models match the vendored schemas and fails fast otherwise.
"""
import json
import logging
import urllib.parse
from importlib import resources

from . import client

log = logging.getLogger("cde_importer.models")

MODEL_ORDER = ["provenance", "cde", "form", "bundle", "concept",
               "cde_classification", "bundle_classification", "form_classification"]
DISPLAY = {
    "cde": "CDE",
    "form": "Form",
    "bundle": "Bundle",
    "concept": "Concept",
    "provenance": "Provenance",
    "cde_classification": "CDE Classification",
    "bundle_classification": "Bundle Classification",
    "form_classification": "Form Classification",
}


class ModelDriftError(Exception):
    def __init__(self, drift):
        self.drift = drift
        super().__init__("dataset models differ from the vendored schemas: " + ", ".join(sorted(drift)))


def schema_dir():
    return resources.files("cde_importer") / "schemas"


def load_schema(name):
    return json.loads((schema_dir() / f"{name}.schema.json").read_text())


def load_models(models_dir=None):
    """[{name, display_name, description, schema}] for the eight models, from the
    vendored schemas or from a directory of <name>.schema.json overrides."""
    out = []
    for name in MODEL_ORDER:
        if models_dir is not None:
            f = models_dir / f"{name}.schema.json"
            if not f.exists():
                raise FileNotFoundError(f"schema not found: {f}")
            schema = json.loads(f.read_text())
        else:
            schema = load_schema(name)
        out.append({
            "name": schema.get("title", name),
            "display_name": DISPLAY.get(name, name.replace("_", " ").title()),
            "description": schema.get("description", ""),
            "schema": schema,
        })
    return out


def _strip_descriptions(node):
    """The schema without its description texts, at every level."""
    if isinstance(node, dict):
        return {k: _strip_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_strip_descriptions(v) for v in node]
    return node


def schema_differs(stored, local, structural=False):
    """Order-independent JSON comparison of a stored schema vs a local one. With
    structural=True, description texts are ignored: a reworded description is
    documentation, not a model change, and must not block a sync."""
    def norm(s):
        if s is None:
            return None
        if isinstance(s, str):
            try:
                s = json.loads(s)
            except ValueError:
                return s
        if structural:
            s = _strip_descriptions(s)
        return json.dumps(s, sort_keys=True)
    return norm(stored) != norm(local)


def _post(api2, token, url, body):
    raw = client.post(url, token, body, pace=0)
    return json.loads(raw) if raw else {}


def list_templates(api2, token, org_node):
    """name -> {"id", "schema"} for the workspace's model templates."""
    org_q = urllib.parse.quote(org_node, safe="")
    existing = client.get_json(f"{api2}/metadata/templates?organization_id={org_q}", token, "GET templates")
    out = {}
    for item in existing or []:
        t = item.get("model_template", item)
        if t.get("name"):
            out[t["name"]] = {"id": t.get("id"), "schema": (t.get("latest_version") or {}).get("schema")}
    return out


def ensure_templates(api2, token, org_node, models, sync=False):
    """Ensure a workspace template per model. With sync=True, POST a new template
    version when the local schema drifts from the stored latest. Returns
    (name -> template_id, set of names whose version was bumped)."""
    org_q = urllib.parse.quote(org_node, safe="")
    have = list_templates(api2, token, org_node)
    ids, bumped = {n: t["id"] for n, t in have.items()}, set()
    for m in models:
        name = m["name"]
        if name in have:
            if sync and schema_differs(have[name]["schema"], m["schema"]):
                _post(api2, token, f"{api2}/metadata/templates/{ids[name]}/versions?organization_id={org_q}",
                      {"schema": m["schema"]})
                bumped.add(name)
                log.warning("template bumped (schema drift): %s (%s); records in datasets using it stay "
                            "on the old model version until re-imported", name, ids[name])
            else:
                log.info("template exists: %s (%s)", name, ids[name])
            continue
        body = {"name": name, "display_name": m["display_name"],
                "description": m["description"], "schema": m["schema"]}
        resp = _post(api2, token, f"{api2}/metadata/template?organization_id={org_q}", body)
        ids[name] = (resp.get("model_template") or {}).get("id")
        log.info("template created: %s (%s)", name, ids[name])
    return ids, bumped


def dataset_models(api2, token, dataset_node):
    """name -> {"id", "version", "schema"} for the dataset's models."""
    ds_q = urllib.parse.quote(dataset_node, safe="")
    current = client.get_json(f"{api2}/metadata/models?dataset_id={ds_q}", token, "GET models")
    out = {}
    for x in current or []:
        m = x.get("model", x)
        if not m.get("name"):
            continue
        lv = m.get("latest_version") or {}
        out[m["name"]] = {"id": m.get("id"), "version": lv.get("version"), "schema": lv.get("schema")}
    return out


def model_schema(api2, token, dataset_node, model_id):
    """The dataset model's current (latest-version) schema."""
    ds_q = urllib.parse.quote(dataset_node, safe="")
    d = client.get_json(f"{api2}/metadata/models/{model_id}?dataset_id={ds_q}", token, "GET model")
    m = d.get("model", d)
    return (m.get("latest_version") or {}).get("schema") or m.get("schema")


def instantiate_models(api2, token, dataset_node, template_ids, models, sync=False):
    """Create each model in the dataset from its template; skip models already
    present. With sync=True, a present model whose schema drifted from the local
    schema is upgraded to the template's latest version (new model version;
    records stay on the old version until re-imported). Never drops a model."""
    ds_q = urllib.parse.quote(dataset_node, safe="")
    have = dataset_models(api2, token, dataset_node)
    created = {}
    for m in models:
        name = m["name"]
        tid = template_ids.get(name)
        if name in have:
            current = have[name]["schema"] or model_schema(api2, token, dataset_node, have[name]["id"])
            if sync and tid and schema_differs(current, m["schema"]):
                _post(api2, token,
                      f"{api2}/metadata/templates/{tid}/models/{have[name]['id']}/versions?dataset_id={ds_q}", {})
                log.warning("model upgraded (schema drift): %s (%s); existing records stay on the old "
                            "version until re-imported", name, have[name]["id"])
            else:
                log.info("model exists: %s", name)
            continue
        if not tid:
            raise KeyError(f"no template id for {name}; cannot instantiate")
        resp = _post(api2, token, f"{api2}/metadata/templates/{tid}/models?dataset_id={ds_q}", {})
        created[name] = (resp.get("model") or {}).get("id")
        log.info("model created: %s (%s) from template %s", name, created[name], tid)
    return created


def check_models(api2, token, dataset_node, models=None):
    """Fail fast when the dataset is missing a model or a model's latest schema
    differs structurally from the vendored one (properties, types, keys, required;
    description texts are ignored). Returns name -> model id on success."""
    models = models or load_models()
    have = dataset_models(api2, token, dataset_node)
    drift = []
    for m in models:
        name = m["name"]
        if name not in have:
            drift.append(f"{name} (missing)")
            continue
        current = have[name]["schema"] or model_schema(api2, token, dataset_node, have[name]["id"])
        if schema_differs(current, m["schema"], structural=True):
            drift.append(name)
    if drift:
        raise ModelDriftError(drift)
    return {n: v["id"] for n, v in have.items()}
