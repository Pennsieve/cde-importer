"""
Metadata-service client: Pennsieve auth, model id resolution, and loading a
payload (records + relationships) into a dataset in dependency order.

Auth: PENNSIEVE_SESSION_TOKEN, else PENNSIEVE_API_KEY + PENNSIEVE_API_SECRET
exchanged for a Cognito access token. All api2 calls carry the Origin header.
"""
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("cde_importer.client")

ORIGIN = "https://app.pennsieve.net"

# Transient statuses worth retrying (WAF/gateway 403 under burst, throttling, 5xx).
# All writes are idempotent by key, so a retry cannot double-write.
RETRY_CODES = {403, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 8
# Seconds between write requests. A rapid burst can trip the api2 authorizer's
# dataset-role lookup into a transient denial that is cached for minutes.
PACE = float(os.environ.get("PENNSIEVE_PACE", "0.1"))
# Max records / relationships per POST: small enough to commit under the API
# Gateway 30s integration timeout when the metadata DB is slow.
BATCH = int(os.environ.get("PENNSIEVE_BATCH", "500"))
TIMEOUT = 120

# Each catalog model's key FIELDS. The metadata-service derives a record id by
# hashing ALL x-pennsieve-key props, so a composite key must be supplied in full
# for record batches AND relationship lookups.
KEY = {"cde": ("cde_key",),
       "form": ("form_key",),
       "bundle": ("bundle_key",),
       "concept": ("concept_key",),
       "provenance": ("source_key",),
       "cde_classification": ("cde_key", "steward_org", "context", "sub_context", "population"),
       "bundle_classification": ("bundle_key", "steward_org", "context", "sub_context", "population"),
       "form_classification": ("form_key", "steward_org", "context", "sub_context", "population")}
POST_ORDER = ("provenance", "cde", "form", "bundle", "concept",
              "cde_classification", "bundle_classification", "form_classification")
# Relationship batches: (label, source model, target model, payload key).
RELATIONSHIPS = (
    ("CLASSIFIES", "cde_classification", "cde", "CLASSIFIES_cde"),
    ("CLASSIFIES", "bundle_classification", "bundle", "CLASSIFIES_bundle"),
    ("CLASSIFIES", "form_classification", "form", "CLASSIFIES_form"),
    ("PART_OF", "cde", "form", "PART_OF_form"),
    ("PART_OF", "cde", "bundle", "PART_OF_bundle"),
    ("REPRESENTS", "cde", "concept", "REPRESENTS"),
    ("SOURCED_FROM", "cde", "provenance", "SOURCED_FROM_cde"),
    ("SOURCED_FROM", "form", "provenance", "SOURCED_FROM_form"),
    ("SOURCED_FROM", "bundle", "provenance", "SOURCED_FROM_bundle"),
    ("SOURCED_FROM", "cde_classification", "provenance", "SOURCED_FROM_cde_classification"),
    ("SOURCED_FROM", "bundle_classification", "provenance", "SOURCED_FROM_bundle_classification"),
    ("SOURCED_FROM", "form_classification", "provenance", "SOURCED_FROM_form_classification"),
)


class AuthError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, label, code, body):
        super().__init__(f"{label} -> {code}: {body[:400]}")
        self.label, self.code, self.body = label, code, body


def get_token(api, env=os.environ):
    tok = env.get("PENNSIEVE_SESSION_TOKEN")
    if tok:
        return tok.strip()
    key, secret = env.get("PENNSIEVE_API_KEY"), env.get("PENNSIEVE_API_SECRET")
    if not (key and secret):
        raise AuthError("Set PENNSIEVE_SESSION_TOKEN, or PENNSIEVE_API_KEY + PENNSIEVE_API_SECRET.")
    cfg = json.load(urllib.request.urlopen(f"{api}/authentication/cognito-config", timeout=TIMEOUT))
    client_id = cfg["tokenPool"]["appClientId"]
    region = cfg["region"]
    body = json.dumps({
        "AuthParameters": {"USERNAME": key, "PASSWORD": secret},
        "AuthFlow": "USER_PASSWORD_AUTH", "ClientId": client_id,
    }).encode()
    req = urllib.request.Request(
        f"https://cognito-idp.{region}.amazonaws.com/", data=body,
        headers={"Content-Type": "application/x-amz-json-1.1",
                 "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"})
    res = json.load(urllib.request.urlopen(req, timeout=TIMEOUT))
    return res["AuthenticationResult"]["AccessToken"]


def api2_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": "application/json", "Origin": ORIGIN}


def urlopen_retry(make_req, label, sleep=time.sleep, attempts=MAX_ATTEMPTS):
    """urlopen with a request timeout and exponential backoff on transient gateway
    statuses and network stalls. Returns the response body text."""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(make_req(), timeout=TIMEOUT) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            msg = e.read().decode()
            if e.code in RETRY_CODES and attempt < attempts:
                wait = min(2 ** attempt, 60)
                log.warning("retry %d after %d (wait %ss): %s", attempt, e.code, wait, label)
                sleep(wait)
                continue
            raise ApiError(label, e.code, msg) from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < attempts:
                wait = min(2 ** attempt, 60)
                log.warning("retry %d after network error (wait %ss): %s: %s", attempt, wait, label, e)
                sleep(wait)
                continue
            raise


def get_json(url, token, label=None):
    raw = urlopen_retry(lambda: urllib.request.Request(url, headers=api2_headers(token)),
                        label or "GET " + url.split("?")[0])
    return json.loads(raw) if raw else {}


def post(url, token, payload, pace=None):
    data = json.dumps(payload).encode()
    body = urlopen_retry(
        lambda: urllib.request.Request(url, data=data, headers=api2_headers(token), method="POST"),
        "POST " + url.split("?")[0])
    time.sleep(PACE if pace is None else pace)
    return body


def switch_org(api, token, org_node):
    """v1 session org switch: the writing identity's active org must be the catalog
    workspace. Not needed for an org-scoped API key."""
    url = (f"{api}/session/switch-organization?organization_id={urllib.parse.quote(org_node, safe='')}"
           f"&api_key={urllib.parse.quote(token, safe='')}")
    urlopen_retry(lambda: urllib.request.Request(url, method="PUT"), "switch-org")


def resolve_models(api2, token, dataset_q):
    """name -> model id for the dataset. /metadata/models returns [{"model":{...}}], flat-tolerant."""
    models = get_json(f"{api2}/metadata/models?dataset_id={dataset_q}", token, "GET models")
    out = {}
    for x in models or []:
        m = x.get("model", x)
        if m.get("name"):
            out[m["name"]] = m.get("id")
    return out


def connect(api2, token, dataset_node):
    """-> (url-quoted dataset id, {model name: model id})."""
    dataset_q = urllib.parse.quote(dataset_node, safe="")
    return dataset_q, resolve_models(api2, token, dataset_q)


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def dedupe_records(recs, key_fields):
    """The metadata-service rejects batches with duplicate keys; last writer wins."""
    seen, out = set(), []
    for r in recs:
        k = tuple(r.get(f) for f in key_fields)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def key_object(model, val):
    """The key object the metadata-service re-hashes to resolve a record: a scalar
    for single-key models, or a {field: value} dict for composite keys."""
    fields = KEY[model]
    if isinstance(val, dict):
        return {f: val[f] for f in fields}
    return {fields[0]: val}


def _sig(val):
    return tuple(sorted(val.items())) if isinstance(val, dict) else val


def dedupe_pairs(pairs):
    seen, out = set(), []
    for s, t in pairs:
        k = (_sig(s), _sig(t))
        if k in seen:
            continue
        seen.add(k)
        out.append((s, t))
    return out


def post_data(api2, token, dataset_q, model_ids, payload, post_fn=post, batch=None):
    """Load one payload (records + relationships) into the dataset, models in
    dependency order, relationships after all records. Returns the counts posted
    per model and per relationship batch."""
    batch = batch or BATCH
    posted = {"records": {}, "relationships": {}}
    for m in POST_ORDER:
        recs = dedupe_records(payload.get(m) or [], KEY[m])
        if not recs:
            continue
        if m not in model_ids:
            raise KeyError(f"dataset has no model named {m!r}")
        for chunk in chunks(recs, batch):
            post_fn(f"{api2}/metadata/models/{model_ids[m]}/records?dataset_id={dataset_q}",
                    token, {"records": chunk})
        posted["records"][m] = len(recs)
        log.info("[ok] %s: %d records", m, len(recs))

    rels = payload.get("_rels") or {}
    for label, src, tgt, key in RELATIONSHIPS:
        pairs = dedupe_pairs(rels.get(key) or [])
        if not pairs:
            continue
        rr = [{"source_record": key_object(src, s), "target_record": key_object(tgt, t),
               "relationship_type": label} for s, t in pairs]
        for chunk in chunks(rr, batch):
            post_fn(f"{api2}/metadata/relationships?dataset_id={dataset_q}", token,
                    {"source_model_id": model_ids[src], "target_model_id": model_ids[tgt],
                     "record_relationships": chunk})
        posted["relationships"][key] = len(rr)
        log.info("[ok] %s %s->%s: %d", label, src, tgt, len(rr))
    return posted
