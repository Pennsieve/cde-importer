import csv
import json
import pathlib
import tempfile
import unittest

from cde_importer import report as R, verify as V


class RunTest(unittest.TestCase):
    def test_run_json_and_totals(self):
        with tempfile.TemporaryDirectory() as d:
            run = R.Run("test-app", d, dataset="N:dataset:x", environment="dev")
            with run.step("fetch"):
                pass
            run.posted("ninds", {"records": {"cde": 3, "provenance": 1}, "relationships": {"SOURCED_FROM_cde": 3}})
            run.posted("phenx", {"records": {"cde": 2}, "relationships": {}})
            data = run.finish("ok", orgs=["NINDS", "PhenX"])
            j = json.loads((pathlib.Path(d) / "run.json").read_text())
        self.assertEqual(j["status"], "ok")
        self.assertEqual(j["totals"], {"cde": 5, "provenance": 1})
        self.assertEqual(j["dataset"], "N:dataset:x")
        self.assertEqual(j["orgs"], ["NINDS", "PhenX"])
        self.assertIn("fetch", j["steps"])
        self.assertTrue(j["steps"]["fetch"]["ok"])
        self.assertIn("library", j)
        self.assertEqual(data, j)

    def test_step_records_failure_and_reraises(self):
        with tempfile.TemporaryDirectory() as d:
            run = R.Run("test-app", d)
            with self.assertRaises(ValueError):
                with run.step("load"):
                    raise ValueError("boom")
            run.finish("failed")
            j = json.loads((pathlib.Path(d) / "run.json").read_text())
        self.assertFalse(j["steps"]["load"]["ok"])
        self.assertEqual(j["status"], "failed")

    def test_errors_csv_from_verify_and_manual(self):
        res = V.VerifyResult()
        res.records["cde"] = {"expected": 2, "dataset": 1, "missing": [("c2",)], "stale": []}
        res.problems.append("cde: 1 posted records not found in the dataset")
        with tempfile.TemporaryDirectory() as d:
            run = R.Run("test-app", d)
            run.verified(res)
            run.error("read", "cde", "row 7", "bad value")
            run.finish("failed")
            rows = list(csv.reader((pathlib.Path(d) / "errors.csv").open()))
            j = json.loads((pathlib.Path(d) / "run.json").read_text())
        self.assertEqual(rows[0], ["stage", "model", "key", "message"])
        self.assertEqual(rows[1], ["verify", "cde", "c2", "posted but not found in the dataset"])
        self.assertEqual(rows[2], ["read", "cde", "row 7", "bad value"])
        self.assertFalse(j["verify"]["ok"])

    def test_no_errors_csv_when_clean(self):
        with tempfile.TemporaryDirectory() as d:
            R.Run("test-app", d).finish("ok")
            self.assertFalse((pathlib.Path(d) / "errors.csv").exists())
