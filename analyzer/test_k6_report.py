import gzip
import json
import tempfile
import unittest
from pathlib import Path

from k6_report import build_report


def point(target, duration, method="POST"):
    return {
        "type": "Point",
        "metric": "http_req_duration",
        "data": {
            "time": "2026-11-24T16:59:20Z",
            "value": duration,
            "tags": {
                "target": target, "version": "v1" if target == "active" else "v2",
                "method": method, "route": "POST /visits/new",
                "status": "302" if method == "POST" else "200",
            },
        },
    }


class K6ReportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write_run(self, points, compressed=False):
        path = self.root / "date_v2_preview-b_stable-a.json"
        if compressed:
            path = path.with_suffix(".json.gz")
        opener = gzip.open if compressed else open
        with opener(path, "wt", encoding="utf-8") as stream:
            for record in points:
                stream.write(json.dumps(record) + "\n")
        return path

    def test_redirects_setup_and_percentiles(self):
        points = [point("active", 10), point("active", 30), point("preview", 25),
                  point("preview", 3, "GET"), point("setup", 999),
                  {"type": "Metric", "metric": "http_req_duration"},
                  {"type": "Point", "metric": "checks"}]
        for compressed in (True, False):
            report = build_report(self.write_run(points, compressed), include_samples=True)
            get, post = report["operations"]
            self.assertEqual(len(report["samples"]), 4)
            self.assertEqual(get["active"], {"count": 0, "median_ms": None, "p95_ms": None})
            self.assertFalse(get["both_targets_present"])
            self.assertTrue(post["both_targets_present"])
            self.assertEqual(post["active"], {"count": 2, "median_ms": 20, "p95_ms": 29})
            self.assertEqual(report["samples"][0]["status"], "302")

    def test_deployment_join_requires_one_matching_revision_pair(self):
        path = self.write_run([point("active", 2), point("preview", 3)], compressed=True)
        record = {"action": "deploy", "stable_hash": "a", "preview_hash": "b",
                  "version": "v2", "active_version": "v1", "analysis_run": "run-02"}
        records = self.root / "deployments.jsonl"
        records.write_text(json.dumps(record) + "\n" + json.dumps(dict(record, action="promote")))
        self.assertEqual(build_report(path, records)["deployment"]["analysis_run"], "run-02")
        for invalid in ([record, record], [dict(record, stable_hash="c")],
                        [dict(record, active_version="v3")]):
            records.write_text("\n".join(json.dumps(item) for item in invalid))
            with self.assertRaises(ValueError):
                build_report(path, records)

    def test_rejects_missing_or_mixed_versions_and_invalid_durations(self):
        for version in ("unknown", "", "v3"):
            changed = point("active", 15)
            changed["data"]["tags"]["version"] = version
            with self.assertRaises(ValueError):
                build_report(self.write_run([point("active", 10), changed]))
        for value in (-1, True, float("inf")):
            with self.assertRaises(ValueError):
                build_report(self.write_run([point("preview", value)]))
        with self.assertRaises(ValueError):
            build_report(self.write_run([point("setup", 10)]))


if __name__ == "__main__":
    unittest.main()
