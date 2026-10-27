"""
Run report: run.json and errors.csv in OUTPUT_DIR.

A Run collects what the app did (dataset, steps, posted counts, verify result,
timings) and writes it once at the end; write() is also safe to call on
failure so a partial report survives.
"""
import csv
import json
import pathlib
import time
from datetime import datetime, timezone

from . import __version__


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Run:
    def __init__(self, app, out_dir, **context):
        self.app = app
        self.out_dir = pathlib.Path(out_dir)
        self.started = time.time()
        self.data = {"app": app, "library": __version__, "started_at": _now(), "status": "running",
                     **context, "steps": {}, "posted": {}, "totals": {}}
        self.errors = []

    def step(self, name):
        """Context manager timing one step."""
        run = self

        class _Step:
            def __enter__(self_):
                self_.t = time.time()
                return self_

            def __exit__(self_, exc_type, exc, tb):
                run.data["steps"][name] = {"seconds": round(time.time() - self_.t, 1),
                                           "ok": exc_type is None}
                return False
        return _Step()

    def posted(self, batch, counts):
        """Record one batch's posted counts and add them to the totals."""
        self.data["posted"][batch] = counts
        for m, n in (counts.get("records") or {}).items():
            self.data["totals"][m] = self.data["totals"].get(m, 0) + n

    def verified(self, result):
        self.data["verify"] = result.summary()
        self.errors.extend(result.errors())

    def error(self, stage, model, key, message):
        self.errors.append((stage, model, key, message))

    def finish(self, status="ok", **extra):
        self.data.update(extra)
        self.data["status"] = status
        self.data["finished_at"] = _now()
        self.data["seconds"] = round(time.time() - self.started, 1)
        return self.write()

    def write(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "run.json").write_text(json.dumps(self.data, indent=1))
        if self.errors:
            with (self.out_dir / "errors.csv").open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["stage", "model", "key", "message"])
                w.writerows(self.errors)
        return self.data
