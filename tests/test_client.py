import io
import json
import unittest
import urllib.error
from unittest import mock

from cde_importer import client as CL


def _http_error(code, body="err"):
    return urllib.error.HTTPError("http://x", code, "msg", {}, io.BytesIO(body.encode()))


class _Resp:
    def __init__(self, body):
        self._b = body.encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TokenTest(unittest.TestCase):
    def test_session_token_wins(self):
        self.assertEqual(CL.get_token("api", env={"PENNSIEVE_SESSION_TOKEN": " t "}), "t")

    def test_missing_credentials(self):
        with self.assertRaises(CL.AuthError):
            CL.get_token("api", env={})

    def test_headers_carry_origin(self):
        h = CL.api2_headers("tok")
        self.assertEqual(h["Authorization"], "Bearer tok")
        self.assertEqual(h["Origin"], CL.ORIGIN)


class RetryTest(unittest.TestCase):
    def test_retries_transient_then_succeeds(self):
        calls = iter([_http_error(503), _http_error(403), _Resp('{"ok":1}')])
        sleeps = []

        def fake_urlopen(req, timeout=None):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(CL.urllib.request, "urlopen", side_effect=fake_urlopen):
            body = CL.urlopen_retry(lambda: "req", "x", sleep=sleeps.append)
        self.assertEqual(body, '{"ok":1}')
        self.assertEqual(sleeps, [2, 4])

    def test_non_transient_raises_api_error(self):
        with mock.patch.object(CL.urllib.request, "urlopen", side_effect=_http_error(400, "bad key")):
            with self.assertRaises(CL.ApiError) as cm:
                CL.urlopen_retry(lambda: "req", "POST x", sleep=lambda s: None)
        self.assertEqual(cm.exception.code, 400)
        self.assertIn("bad key", str(cm.exception))

    def test_gives_up_after_max_attempts(self):
        with mock.patch.object(CL.urllib.request, "urlopen", side_effect=_http_error(503)):
            with self.assertRaises(CL.ApiError):
                CL.urlopen_retry(lambda: "req", "x", sleep=lambda s: None, attempts=3)

    def test_network_error_retried(self):
        calls = iter([urllib.error.URLError("stall"), _Resp("ok")])

        def fake_urlopen(req, timeout=None):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(CL.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertEqual(CL.urlopen_retry(lambda: "req", "x", sleep=lambda s: None), "ok")


class KeysTest(unittest.TestCase):
    def test_dedupe_records_last_writer_dropped(self):
        recs = [{"cde_key": "a", "v": 1}, {"cde_key": "b"}, {"cde_key": "a", "v": 2}]
        self.assertEqual(CL.dedupe_records(recs, ("cde_key",)), [{"cde_key": "a", "v": 1}, {"cde_key": "b"}])

    def test_composite_key_object(self):
        row = {"cde_key": "a", "steward_org": "NINDS", "context": "TBI", "sub_context": "*",
               "population": "*", "tier": "Core"}
        self.assertEqual(CL.key_object("cde_classification", row),
                         {"cde_key": "a", "steward_org": "NINDS", "context": "TBI", "sub_context": "*",
                          "population": "*"})
        self.assertEqual(CL.key_object("cde", "a"), {"cde_key": "a"})

    def test_dedupe_pairs(self):
        pairs = [("a", "p"), ({"k": 1, "j": 2}, "p"), ({"j": 2, "k": 1}, "p"), ("a", "p")]
        self.assertEqual(len(CL.dedupe_pairs(pairs)), 2)

    def test_resolve_models_flat_and_nested(self):
        with mock.patch.object(CL, "get_json", return_value=[{"model": {"name": "cde", "id": "m1"}},
                                                             {"name": "form", "id": "m2"}]):
            self.assertEqual(CL.resolve_models("api2", "t", "ds"), {"cde": "m1", "form": "m2"})

    def test_connect_quotes_dataset(self):
        with mock.patch.object(CL, "resolve_models", return_value={}) as rm:
            q, _ = CL.connect("api2", "t", "N:dataset:abc")
        self.assertEqual(q, "N%3Adataset%3Aabc")
        rm.assert_called_once_with("api2", "t", "N%3Adataset%3Aabc")


class PostDataTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.mid = {m: f"id-{m}" for m in CL.POST_ORDER}

        def fake_post(url, token, body):
            self.calls.append((url, body))
            return "{}"
        self.post = fake_post

    def _payload(self, n_cde=3):
        return {
            "provenance": [{"source_key": "nlm"}],
            "cde": [{"cde_key": f"c{i}"} for i in range(n_cde)] + [{"cde_key": "c0"}],
            "cde_classification": [{"cde_key": "c0", "steward_org": "S", "context": "X",
                                    "sub_context": "*", "population": "*"}],
            "_rels": {
                "SOURCED_FROM_cde": [(f"c{i}", "nlm") for i in range(n_cde)] + [("c0", "nlm")],
                "CLASSIFIES_cde": [({"cde_key": "c0", "steward_org": "S", "context": "X",
                                     "sub_context": "*", "population": "*"}, "c0")],
            },
        }

    def test_order_dedupe_and_counts(self):
        posted = CL.post_data("api2", "t", "ds", self.mid, self._payload(), post_fn=self.post)
        urls = [u for u, _ in self.calls]
        self.assertEqual(urls[:3], ["api2/metadata/models/id-provenance/records?dataset_id=ds",
                                    "api2/metadata/models/id-cde/records?dataset_id=ds",
                                    "api2/metadata/models/id-cde_classification/records?dataset_id=ds"])
        self.assertEqual(len(self.calls[1][1]["records"]), 3)
        self.assertEqual(posted["records"], {"provenance": 1, "cde": 3, "cde_classification": 1})
        rel_calls = [b for u, b in self.calls if "relationships" in u]
        self.assertEqual(len(rel_calls), 2)
        self.assertEqual(rel_calls[0]["source_model_id"], "id-cde_classification")
        self.assertEqual(rel_calls[0]["record_relationships"][0]["relationship_type"], "CLASSIFIES")
        self.assertEqual(rel_calls[1]["target_model_id"], "id-provenance")
        self.assertEqual(len(rel_calls[1]["record_relationships"]), 3)
        self.assertEqual(posted["relationships"], {"CLASSIFIES_cde": 1, "SOURCED_FROM_cde": 3})

    def test_batching(self):
        CL.post_data("api2", "t", "ds", self.mid, self._payload(n_cde=7), post_fn=self.post, batch=3)
        cde_batches = [len(b["records"]) for u, b in self.calls if "id-cde/" in u]
        self.assertEqual(cde_batches, [3, 3, 1])
        rel_batches = [len(b["record_relationships"]) for u, b in self.calls
                       if "relationships" in u and b["source_model_id"] == "id-cde"]
        self.assertEqual(rel_batches, [3, 3, 1])

    def test_missing_model_raises(self):
        with self.assertRaises(KeyError):
            CL.post_data("api2", "t", "ds", {"provenance": "p"}, self._payload(), post_fn=self.post)

    def test_empty_models_skipped(self):
        CL.post_data("api2", "t", "ds", self.mid, {"cde": [], "_rels": {}}, post_fn=self.post)
        self.assertEqual(self.calls, [])


class AuthTest(unittest.TestCase):
    def test_renews_after_max_age_and_on_demand(self):
        now = [1000.0]
        with mock.patch.object(CL, "get_token", side_effect=["t1", "t2", "t3"]) as gt:
            a = CL.Auth("api", env={}, max_age=100, clock=lambda: now[0])
            self.assertEqual(a.token, "t1")
            self.assertEqual(a.token, "t1")
            now[0] += 101
            self.assertEqual(a.token, "t2")
            a.refresh()
            self.assertEqual(a.token, "t3")
            self.assertEqual(str(a), "t3")
        self.assertEqual(gt.call_count, 3)

    def test_headers_use_current_token(self):
        with mock.patch.object(CL, "get_token", return_value="tok"):
            self.assertEqual(CL.api2_headers(CL.Auth("api", env={}))["Authorization"], "Bearer tok")

    def test_auth_error_refreshes_once_then_retries(self):
        calls = iter([_http_error(403), _Resp("ok")])
        refreshed = []

        def fake_urlopen(req, timeout=None):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(CL.urllib.request, "urlopen", side_effect=fake_urlopen):
            body = CL.urlopen_retry(lambda: "req", "x", sleep=lambda s: None, on_auth_error=lambda: refreshed.append(1))
        self.assertEqual(body, "ok")
        self.assertEqual(refreshed, [1])

    def test_persistent_403_after_refresh_falls_back_to_backoff(self):
        sleeps = []
        with mock.patch.object(CL.urllib.request, "urlopen", side_effect=_http_error(403)):
            with self.assertRaises(CL.ApiError):
                CL.urlopen_retry(lambda: "req", "x", sleep=sleeps.append, attempts=4, on_auth_error=lambda: None)
        self.assertEqual(sleeps, [4, 8])
