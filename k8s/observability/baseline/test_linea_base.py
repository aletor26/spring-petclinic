"""Pruebas de linea_base.py (solo biblioteca estándar; no necesitan clúster).

  python3 -m unittest discover -s k8s/observability/baseline -p 'test_*.py'
"""
import csv
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import linea_base as lb  # noqa: E402

LOGS = "\n".join([
    '2026-11-27T15:00:10.1Z {"event": "started"}',
    # alerta de la versión activa: cuenta como notificación, pero no como primera alerta de lat-1
    '2026-11-27T15:03:00.5Z {"event":"notification","status":"firing","alerts":[{"status":"firing","startsAt":"2026-11-27T15:02:00Z","labels":{"alertname":"PetclinicHighLatency","version":"v1"}}]}',
    '2026-11-27T15:04:00.5Z {"event":"notification","status":"firing","alerts":[{"status":"firing","startsAt":"2026-11-27T15:03:30Z","labels":{"alertname":"PetclinicHighLatency","version":"lat-1"}}]}',
    '2026-11-27T15:05:00.5Z {"event":"notification","status":"resolved","alerts":[{"status":"resolved","labels":{"version":"lat-1"}}]}',
    '2026-11-27T15:20:00Z {"event":"notification","status":"firing","alerts":[{"status":"firing","labels":{"version":"lat-1"}}]}',
    "línea que no es JSON",
])


class ParseTime(unittest.TestCase):
    def test_formats(self):
        for raw in ("2026-11-27T15:04:05.123456789Z", "2026-11-27T10:04:05.12-05:00", "2026-11-27T15:04:05Z"):
            self.assertEqual(lb.fmt(lb.parse_time(raw)), "2026-11-27T15:04:05Z", raw)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            lb.parse_time("")


class Deployments(unittest.TestCase):
    def test_promote_or_abort_after_deploy(self):
        recs = [
            {"time": "2026-11-27T14:00:00Z", "action": "up", "result": "ok"},
            {"time": "2026-11-27T15:12:00Z", "action": "deploy", "result": "analysis-ok", "analysis_run": "ar-1"},
            {"time": "2026-11-27T15:14:00Z", "action": "abort", "result": "ok"},
        ]
        _, dec = lb.last_deployment(recs)
        self.assertEqual(dec, {"action": "abort", "time": "2026-11-27T15:14:00Z", "by": "persona"})

    def test_automatic_abort(self):
        _, dec = lb.last_deployment([{"time": "2026-11-27T15:12:00Z", "action": "deploy", "result": "analysis-failed"}])
        self.assertEqual(dec["by"], "analisis-automatico")

    def test_no_decision_yet(self):
        _, dec = lb.last_deployment([{"time": "2026-11-27T15:12:00Z", "action": "deploy", "result": "analysis-ok"}])
        self.assertIsNone(dec)

    def test_uses_last_deploy(self):
        recs = [
            {"time": "2026-11-27T15:00:00Z", "action": "deploy", "result": "analysis-ok", "version": "a"},
            {"time": "2026-11-27T15:01:00Z", "action": "promote", "result": "ok"},
            {"time": "2026-11-27T16:00:00Z", "action": "deploy", "result": "analysis-ok", "version": "b"},
        ]
        dep, dec = lb.last_deployment(recs)
        self.assertEqual(dep["version"], "b")
        self.assertIsNone(dec)


class Alerts(unittest.TestCase):
    def setUp(self):
        self.notes = lb.parse_sink_logs(LOGS)

    def test_parse_only_notifications(self):
        self.assertEqual(len(self.notes), 4)

    def test_first_alert_and_count(self):
        first, count = lb.summarize_alerts(self.notes, lb.parse_time("2026-11-27T15:01:00Z"),
                                           lb.parse_time("2026-11-27T15:14:00Z"), "lat-1")
        self.assertEqual(lb.fmt(first), "2026-11-27T15:04:00Z")
        self.assertEqual(count, 2)  # v1 + lat-1 (firing); el resolved y el de las 15:20 quedan fuera

    def test_ignores_alert_started_before_run(self):
        old = lb.parse_sink_logs(
            '2026-11-27T15:06:00Z {"event":"notification","status":"firing","alerts":[{"status":"firing",'
            '"startsAt":"2026-11-27T14:00:00Z","labels":{"alertname":"PostgresDown","version":"todas"}}]}')
        first, count = lb.summarize_alerts(old, lb.parse_time("2026-11-27T15:01:00Z"),
                                           lb.parse_time("2026-11-27T15:14:00Z"), "lat-1")
        self.assertIsNone(first)
        self.assertEqual(count, 1)


class SinkRestart(unittest.TestCase):
    START = lb.parse_time("2026-11-27T15:00:00Z")

    def test_running_since_before_run_is_ok(self):
        self.assertFalse(lb.sink_restarted_during(lb.parse_time("2026-11-27T14:00:00Z"), self.START))

    def test_restart_inside_run_is_detected(self):
        self.assertTrue(lb.sink_restarted_during(lb.parse_time("2026-11-27T15:05:00Z"), self.START))

    def test_not_running_is_detected(self):
        self.assertTrue(lb.sink_restarted_during(None, self.START))


class Indicators(unittest.TestCase):
    def test_rows_and_formulas(self):
        notes = lb.parse_sink_logs(LOGS)
        bad = lb.build_row("1", {"version": "lat-1", "analysis_run": "ar-1"},
                           {"action": "abort", "time": "2026-11-27T15:14:00Z", "by": "persona"},
                           lb.parse_time("2026-11-27T15:01:00Z"), "latency", notes)
        good = lb.build_row("2", {"version": "sana-1", "analysis_run": "ar-2"},
                            {"action": "promote", "time": "2026-11-27T16:12:00Z", "by": "persona"},
                            lb.parse_time("2026-11-27T16:00:00Z"), "ninguna", [])
        self.assertEqual((bad["detectada"], bad["falso_positivo"]), ("si", ""))
        self.assertEqual((good["detectada"], good["falso_positivo"]), ("", "no"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lb.csv"
            lb.append_row(path, bad)
            lb.append_row(path, good)
            with path.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        ind = lb.compute_indicators(rows)
        self.assertEqual(ind["tasa_deteccion"], 1.0)
        self.assertEqual(ind["tasa_falsos_positivos"], 0.0)
        self.assertAlmostEqual(ind["mttd_min"], 3.0)
        self.assertAlmostEqual(ind["tiempo_decision_min"], (13 + 12) / 2)
        self.assertEqual(ind["alertas_por_despliegue"], 1.0)
        self.assertIn("| latency | 1 | 1 | 1 |", lb.render_markdown(rows))


class Thresholds(unittest.TestCase):
    RULES = Path(__file__).resolve().parents[1] / "rules" / "petclinic-rules.yaml"

    def test_rewrite_keeps_one_block_and_format(self):
        text = self.RULES.read_text(encoding="utf-8")
        new = lb.rewrite_thresholds(text, {"/": 0.03, "/vets.html": 0.0456})
        self.assertEqual(new.count("# BEGIN umbrales-latencia"), 1)
        found = re.findall(r'expr: vector\(([\d.]+)\)\s+labels: \{uri: "([^"]+)"\}', new)
        self.assertEqual(found, [("0.0300", "/"), ("0.0456", "/vets.html")])
        self.assertEqual(text.split("# BEGIN umbrales-latencia")[0], new.split("# BEGIN umbrales-latencia")[0])
        self.assertEqual(text.split("# END umbrales-latencia")[1], new.split("# END umbrales-latencia")[1])

    def test_merge_keeps_current_and_extra_routes(self):
        current = lb.current_thresholds(self.RULES.read_text(encoding="utf-8"))
        current["/owners/new"] = 0.2  # ruta agregada solo en el YAML
        merged = lb.merge_thresholds({"/vets.html": 0.03}, current)
        self.assertEqual(merged["/vets.html"], 0.03)       # calibrada
        self.assertEqual(merged["/owners"], 0.5)           # sin datos: conserva el actual
        self.assertEqual(merged["/owners/new"], 0.2)       # no se borra
        self.assertEqual(list(merged)[-1], "/owners/new")  # va después de SLO_URIS

    def test_current_thresholds_reads_block(self):
        current = lb.current_thresholds(self.RULES.read_text(encoding="utf-8"))
        self.assertEqual(set(current), set(lb.SLO_URIS))

    def test_missing_marker_exits_without_writing(self):
        with self.assertRaises(SystemExit):
            lb.rewrite_thresholds("sin marcador", {"/": 0.1})

    def test_p95_query(self):
        q = lb.p95_query("sana-1", 600)
        self.assertIn('version="sana-1"', q)
        self.assertIn("[600s]", q)


if __name__ == "__main__":
    unittest.main()
