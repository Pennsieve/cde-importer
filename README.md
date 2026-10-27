# cde-importer

Shared library for the Pennsieve CDE catalog sync applications (`cde-sync-nlm`,
`cde-sync-ninds`, `cde-sync-pte-xlsx`). It holds the logic that is the same for
every source: transforming reader output into catalog records, the eight catalog
model schemas, and the metadata-service client that loads records and
relationships. It has no readers and no CLI; each application brings its own
reader and calls into this package.

## Install

Pin a tag in the application's `requirements.txt`:

```
cde-importer @ git+https://github.com/Pennsieve/cde-importer@v0.1.1
```

Python 3.13, no runtime dependencies.

## Modules

| module | contents |
|---|---|
| `cde_importer.transform` | `transform_cde`, `canonical_key`, permissible values, xrefs, concept derivation, `transform_provenance`, `build_payload`, `emit_payload` |
| `cde_importer.coordinate` | `walk_tree`: an NLM/NINDS classification tree to flat classification rows |
| `cde_importer.client` | token exchange, `Origin` header, model id resolution, `post_data` in dependency order with batching, pacing and retry |
| `cde_importer.models` | the eight vendored schemas, template ensure/sync, dataset model instantiation, `check_models` (fail fast when a dataset's models drift from the vendored schemas) |

## Usage

```python
from cde_importer import client, models, transform

token = client.get_token(api)                      # PENNSIEVE_API_KEY + PENNSIEVE_API_SECRET
dataset_q, model_ids = client.connect(api2, token, dataset_node)
models.check_models(api2, token, dataset_node)     # raises ModelDriftError on drift

payload = transform.build_payload(cde_inputs, cde_cls, forms, form_cls, bundles, bundle_cls,
                                  memberships, prov_input, license_, attribution)
client.post_data(api2, token, dataset_q, model_ids, payload)
```

The reader supplies `cde_inputs` (flat dicts with the column names the schema
uses, pipe-joined lists), already-keyed classification rows, forms and bundles,
memberships, and one provenance input. See the docstrings in `transform.py`.

## Schemas

`src/cde_importer/schemas/*.schema.json` are the source of truth for the catalog
models and are copied from `cde-service/catalog/models`. A property rename is a
breaking change for every consumer of the published catalog and must be a new
minor version here.

## Development

```
pip install -e ".[test]"
python -m pytest
```

Tags are semver: `v0.1.0` is the first cut (transform, coordinate, client,
models).
