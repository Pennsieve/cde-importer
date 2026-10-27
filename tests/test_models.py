import json
import pathlib
import tempfile
import unittest
from unittest import mock

from cde_importer import models as M


class SchemasTest(unittest.TestCase):
    def test_eight_vendored_schemas(self):
        ms = M.load_models()
        self.assertEqual([m["name"] for m in ms], M.MODEL_ORDER)
        for m in ms:
            self.assertIn("properties", m["schema"])
            keyed = [k for k, v in m["schema"]["properties"].items() if v.get("x-pennsieve-key")]
            self.assertTrue(keyed, m["name"])
            self.assertEqual(m["display_name"], M.DISPLAY[m["name"]])

    def test_cde_schema_shape(self):
        cde = M.load_schema("cde")
        self.assertEqual(cde["title"], "cde")
        self.assertIn("cde_key", cde["properties"])
        self.assertIn("cde_source", cde["properties"])

    def test_override_dir(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            for name in M.MODEL_ORDER:
                s = M.load_schema(name)
                s["description"] = "override"
                (d / f"{name}.schema.json").write_text(json.dumps(s))
            ms = M.load_models(d)
            self.assertTrue(all(m["description"] == "override" for m in ms))
            (d / "cde.schema.json").unlink()
            with self.assertRaises(FileNotFoundError):
                M.load_models(d)

    def test_schema_differs(self):
        a = {"b": 1, "a": [1, 2]}
        self.assertFalse(M.schema_differs(json.dumps({"a": [1, 2], "b": 1}), a))
        self.assertTrue(M.schema_differs({"a": [1, 2], "b": 2}, a))
        self.assertTrue(M.schema_differs(None, a))

    def test_structural_ignores_descriptions_only(self):
        local = M.load_schema("cde")
        reworded = json.loads(json.dumps(local))
        reworded["description"] = "old top-level text"
        reworded["properties"]["cde_source"]["description"] = "old wording"
        self.assertTrue(M.schema_differs(reworded, local))
        self.assertFalse(M.schema_differs(reworded, local, structural=True))
        retyped = json.loads(json.dumps(local))
        retyped["properties"]["cde_source"]["type"] = "integer"
        self.assertTrue(M.schema_differs(retyped, local, structural=True))
        dropped = json.loads(json.dumps(local))
        del dropped["properties"]["cde_source"]
        self.assertTrue(M.schema_differs(dropped, local, structural=True))


def _dataset_listing(models, drift=None, missing=(), reworded=None):
    out = []
    for m in models:
        if m["name"] in missing:
            continue
        schema = json.loads(json.dumps(m["schema"]))
        if m["name"] == drift:
            schema["properties"]["__extra"] = {"type": "string"}
        if m["name"] == reworded:
            schema["properties"]["cde_source"]["description"] = "old wording"
        out.append({"model": {"name": m["name"], "id": f"id-{m['name']}",
                              "latest_version": {"version": 1, "schema": schema}}})
    return out


class CheckModelsTest(unittest.TestCase):
    def setUp(self):
        self.models = M.load_models()

    def test_ok_returns_ids(self):
        with mock.patch.object(M.client, "get_json", return_value=_dataset_listing(self.models)):
            ids = M.check_models("api2", "t", "N:dataset:x")
        self.assertEqual(ids["cde"], "id-cde")
        self.assertEqual(len(ids), 8)

    def test_drift_raises(self):
        with mock.patch.object(M.client, "get_json", return_value=_dataset_listing(self.models, drift="cde")):
            with self.assertRaises(M.ModelDriftError) as cm:
                M.check_models("api2", "t", "N:dataset:x")
        self.assertEqual(cm.exception.drift, ["cde"])

    def test_reworded_description_is_not_drift(self):
        with mock.patch.object(M.client, "get_json", return_value=_dataset_listing(self.models, reworded="cde")):
            ids = M.check_models("api2", "t", "N:dataset:x")
        self.assertEqual(len(ids), 8)

    def test_missing_model_raises(self):
        with mock.patch.object(M.client, "get_json",
                               return_value=_dataset_listing(self.models, missing=("form", "concept"))):
            with self.assertRaises(M.ModelDriftError) as cm:
                M.check_models("api2", "t", "N:dataset:x")
        self.assertEqual(cm.exception.drift, ["form (missing)", "concept (missing)"])

    def test_schema_fetched_when_listing_lacks_it(self):
        listing = [{"model": {"name": m["name"], "id": f"id-{m['name']}"}} for m in self.models]
        by_id = {f"id-{m['name']}": {"model": {"latest_version": {"schema": m["schema"]}}} for m in self.models}

        def fake_get(url, token, label=None):
            if "/metadata/models?" in url:
                return listing
            mid = url.split("/metadata/models/")[1].split("?")[0]
            return by_id[mid]
        with mock.patch.object(M.client, "get_json", side_effect=fake_get):
            ids = M.check_models("api2", "t", "N:dataset:x")
        self.assertEqual(len(ids), 8)


class TemplatesTest(unittest.TestCase):
    def setUp(self):
        self.models = M.load_models()
        self.posts = []

        def fake_post(url, token, body, pace=None):
            self.posts.append((url, body))
            name = body.get("name") or "x"
            return json.dumps({"model_template": {"id": f"tid-{name}"}, "model": {"id": f"mid-{url[-8:]}"}})
        self.fake_post = fake_post

    def test_ensure_templates_creates_missing_only(self):
        existing = [{"model_template": {"name": "cde", "id": "tid-cde",
                                        "latest_version": {"schema": M.load_schema("cde")}}}]
        with mock.patch.object(M.client, "get_json", return_value=existing), \
                mock.patch.object(M.client, "post", side_effect=self.fake_post):
            ids, bumped = M.ensure_templates("api2", "t", "N:organization:o", self.models)
        self.assertEqual(bumped, set())
        self.assertEqual(len(ids), 8)
        self.assertEqual(ids["cde"], "tid-cde")
        self.assertEqual(len(self.posts), 7)
        self.assertTrue(all("/metadata/template?" in u for u, _ in self.posts))

    def test_ensure_templates_sync_bumps_on_drift(self):
        drifted = dict(M.load_schema("cde"))
        drifted["description"] = "old"
        existing = [{"model_template": {"name": m["name"], "id": f"tid-{m['name']}",
                                        "latest_version": {"schema": drifted if m["name"] == "cde" else m["schema"]}}}
                    for m in self.models]
        with mock.patch.object(M.client, "get_json", return_value=existing), \
                mock.patch.object(M.client, "post", side_effect=self.fake_post):
            _, bumped = M.ensure_templates("api2", "t", "N:organization:o", self.models)
            self.assertEqual(bumped, set())
            self.assertEqual(self.posts, [])
            _, bumped = M.ensure_templates("api2", "t", "N:organization:o", self.models, sync=True)
        self.assertEqual(bumped, {"cde"})
        self.assertIn("/metadata/templates/tid-cde/versions?", self.posts[0][0])

    def test_instantiate_models_skips_existing(self):
        listing = _dataset_listing(self.models, missing=("form_classification",))
        tids = {m["name"]: f"tid-{m['name']}" for m in self.models}
        with mock.patch.object(M.client, "get_json", return_value=listing), \
                mock.patch.object(M.client, "post", side_effect=self.fake_post):
            created = M.instantiate_models("api2", "t", "N:dataset:x", tids, self.models)
        self.assertEqual(list(created), ["form_classification"])
        self.assertEqual(len(self.posts), 1)
        self.assertIn("/metadata/templates/tid-form_classification/models?", self.posts[0][0])

    def test_instantiate_models_requires_template(self):
        with mock.patch.object(M.client, "get_json", return_value=[]):
            with self.assertRaises(KeyError):
                M.instantiate_models("api2", "t", "N:dataset:x", {}, self.models)
