import csv
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from latency_analysis import latency_analysis
from observation_api import create_app
from reference_model import FEATURES, METADATA, fit_reference, read_windows, save_model
from test_k6_report import point


class ObservationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def run_file(self):
        points = [point(t, v) for t, v in (("active", 1), ("preview", 5),
                                            ("active", 2), ("preview", 6))]
        path = self.root / "run.json"
        path.write_text("\n".join(json.dumps(p) for p in points))
        return path

    def test_mann_whitney_direction_and_common_blocks(self):
        path = self.run_file()
        result = latency_analysis(path)["operations"][0]["comparison"]
        self.assertEqual(result["delta_median_ms"], 4)
        self.assertEqual(result["u_preview"], 4)
        self.assertLess(result["p_value"], 0.5)
        blocked = latency_analysis(path, block_seconds=10)["operations"][0]["comparison"]
        self.assertIsNone(blocked["p_value"])
        self.assertEqual((blocked["active_units"], blocked["preview_units"]), (1, 1))

    def window(self, run, split, load="medium"):
        return {**dict.fromkeys(FEATURES, 0.5), "run_id": run, "revision": "stable-a",
                "target": "active", "load_profile": load, "split": split, "condition": "healthy"}

    def csv_path(self, rows):
        path = self.root / "windows.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=METADATA + FEATURES)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_resume_and_score_known_local_reference(self):
        rows = [self.window("a", "train", "low"), self.window("b", "train"),
                self.window("c", "calibration"), self.window("d", "evaluation")]
        rows[1]["heap_mib"] = 20
        rows[-1].update(condition="fault", target="preview")
        ref, evaluation = fit_reference(self.csv_path(rows), 0.95)
        self.assertEqual(ref["reference_runs"]["train"], ["a", "b"])
        path = self.root / "model.joblib"
        save_model(ref, path)
        with TestClient(create_app(self.run_file(), model_path=path)) as client:
            data = client.get("/observation/latency").json()
            self.assertEqual(data["operations"][0]["comparison"]["status"], "exploratory")
            response = client.post("/observation/deviation", json={k: rows[-1][k] for k in FEATURES})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["deviation"], evaluation[0]["deviation"])
            self.assertEqual(response.json()["above_threshold"], evaluation[0]["above_threshold"])

    def test_reject_unhealthy_reference_leakage_and_missing_signals(self):
        base = [self.window("a", "train", "low"), self.window("b", "train"),
                self.window("c", "calibration"), self.window("d", "evaluation")]
        for index, changes in ((0, {"condition": "fault"}), (3, {"run_id": "a", "target": "preview"}),
                               (1, {"pool_active_mean": ""}), (2, {"heap_mib": "nan"}),
                               (0, {"load_profile": "medium"})):
            rows = [dict(r) for r in base]
            rows[index].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                read_windows(self.csv_path(rows))

    def test_unavailable_evidence(self):
        with TestClient(create_app()) as client:
            self.assertEqual(client.get("/observation/latency").status_code, 503)
            self.assertEqual(client.post("/observation/deviation",
                                         json=dict.fromkeys(FEATURES, 0.5)).status_code, 503)
            self.assertEqual(client.post("/observation/deviation", json={}).status_code, 422)
