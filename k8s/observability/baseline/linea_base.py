#!/usr/bin/env python3
"""Línea base (tarea 3): registra cada corrida y calcula los indicadores de la Tabla 6.

Solo usa la biblioteca estándar de Python y kubectl (contexto kind-petclinic).

  registrar   Agrega una fila a docs/linea-base.csv con el último despliegue de
              k8s/bluegreen/records/deployments.jsonl y las notificaciones de alert-sink.
  indicadores Calcula tasa de detección, falsos positivos, MTTD, tiempo hasta la decisión
              y volumen de alertas por despliegue.
  calibrar    Calcula el p95 sano por ruta con las corridas sanas de un CSV y reescribe los
              umbrales (p95 sano × 1,5) en k8s/observability/rules/petclinic-rules.yaml.

Ejemplos:
  python3 k8s/observability/baseline/linea_base.py registrar --corrida 1 --fault latency
  python3 k8s/observability/baseline/linea_base.py indicadores
  python3 k8s/observability/baseline/linea_base.py calibrar --csv docs/calibracion.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RECORDS = REPO / "k8s/bluegreen/records/deployments.jsonl"
DEFAULT_CSV = REPO / "docs/linea-base.csv"
RULES = REPO / "k8s/observability/rules/petclinic-rules.yaml"
CONTEXT = "kind-petclinic"
PROMETHEUS = "/api/v1/namespaces/monitoring/services/kps-kube-prometheus-stack-prometheus:9090/proxy"

# Escenarios del catálogo (sección 2). "sanos" cuentan para falsos positivos;
# el resto son defectuosos y cuentan para la tasa de detección.
HEALTHY = {"ninguna", "oups"}
DEFECTIVE = {"latency", "slow-query", "visit-write", "heap-leak", "postgres-down"}
ALL_FAULTS = HEALTHY | DEFECTIVE

COLUMNS = [
    "corrida", "version", "fault", "inicio_trafico_preview", "primera_alerta",
    "decision", "hora_decision", "detectada", "falso_positivo", "alertas_recibidas",
    "decidido_por", "analysis_run", "notas",
]
LATENCY_FACTOR = 1.5
SLO_URIS = [
    "/", "/vets.html", "/owners", "/owners/find", "/owners/{ownerId}",
    "/owners/{ownerId}/pets/{petId}/visits/new",
]


# ---------------------------------------------------------------- utilidades
def parse_time(value: str) -> datetime:
    """ISO 8601 (con Z, offset o nanosegundos de kubectl) -> datetime en UTC."""
    value = value.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", value)
    if m and m.group(2):  # Python < 3.11 exige exactamente 6 decimales
        value = m.group(1) + "." + m.group(2)[1:7].ljust(6, "0") + m.group(3)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def kubectl(*args: str) -> str:
    cmd = ["kubectl", "--context", CONTEXT, *args]
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    except FileNotFoundError:
        sys.exit("No encuentro kubectl en el PATH.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"{' '.join(cmd)}: {e.stderr.strip()}") from e


def fail(msg: str) -> None:
    sys.exit(f"[linea-base] ERROR: {msg}")


# ---------------------------------------------------------------- deployments.jsonl
def load_records(path: Path) -> list[dict]:
    if not path.exists():
        fail(f"No existe {path}. Primero corre k8s/bluegreen/bg.sh deploy.")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def last_deployment(records: list[dict]) -> tuple[dict, dict | None]:
    """Último 'deploy' y la decisión que lo cierra (promote/abort o fallo automático)."""
    idx = max((i for i, r in enumerate(records) if r.get("action") == "deploy"), default=None)
    if idx is None:
        fail("deployments.jsonl no tiene ningún 'deploy'.")
    deploy = records[idx]
    if deploy.get("result") == "analysis-failed":
        # Argo Rollouts abortó solo: el análisis (smoke tests + k6) falló.
        return deploy, {"action": "abort", "time": deploy["time"], "by": "analisis-automatico"}
    for r in records[idx + 1:]:
        if r.get("action") in ("promote", "abort"):
            return deploy, {"action": r["action"], "time": r["time"], "by": "persona"}
    return deploy, None


def analysis_start(deploy: dict) -> datetime | None:
    """Inicio del tráfico de análisis hacia preview = creación del AnalysisRun.

    El registro 'deploy' de bg.sh se escribe al TERMINAR el análisis, por eso no sirve como inicio.
    """
    ar = deploy.get("analysis_run")
    if not ar:
        return None
    try:
        out = kubectl("-n", "petclinic", "get", "analysisrun", ar, "-o", "jsonpath={.metadata.creationTimestamp}")
    except RuntimeError:
        return None
    return parse_time(out) if out.strip() else None


# ---------------------------------------------------------------- alert-sink
def parse_sink_logs(text: str) -> list[dict]:
    """Líneas de `kubectl logs --timestamps deploy/alert-sink` -> notificaciones."""
    out = []
    for line in text.splitlines():
        ts, _, payload = line.partition(" ")
        try:
            msg = json.loads(payload)
        except ValueError:
            continue
        if msg.get("event") != "notification":
            continue
        msg["received_at"] = parse_time(ts)
        out.append(msg)
    return out


def sink_notifications(since: datetime) -> list[dict]:
    text = kubectl("-n", "monitoring", "logs", "deploy/alert-sink", "--timestamps",
                   f"--since-time={fmt(since)}")
    return parse_sink_logs(text)


def sink_running_since() -> datetime | None:
    """Desde cuándo corre el contenedor de alert-sink (sus logs anteriores se pierden al reiniciarse)."""
    out = kubectl("-n", "monitoring", "get", "pods", "-l", "app=alert-sink", "-o",
                  "jsonpath={.items[0].status.containerStatuses[0].state.running.startedAt}")
    return parse_time(out) if out.strip() else None


def sink_restarted_during(running_since: datetime | None, start: datetime) -> bool:
    """True si alert-sink no estaba corriendo desde el inicio de la corrida."""
    return running_since is None or running_since > start


def _started_in_run(alert: dict, start: datetime) -> bool:
    """La alerta está activa y empezó durante la corrida (no es un reaviso de una corrida anterior)."""
    if alert.get("status") != "firing":
        return False
    try:
        return parse_time(alert.get("startsAt") or "") >= start
    except ValueError:
        return True  # sin startsAt legible: se cuenta por la hora de recepción


def summarize_alerts(notes: list[dict], start: datetime, end: datetime, version: str) -> tuple[datetime | None, int]:
    """(primera alerta de la versión preview o de la base, notificaciones 'firing' en la ventana).

    primera_alerta solo considera alertas de esa versión (o version="todas") que empezaron dentro de
    la corrida. alertas_recibidas cuenta todas las notificaciones 'firing' que llegaron al equipo.
    """
    in_window = [n for n in notes if start <= n["received_at"] <= end and n.get("status") == "firing"]
    relevant = [
        n["received_at"] for n in in_window
        if any((a.get("labels") or {}).get("version") in (version, "todas") and _started_in_run(a, start)
               for a in n.get("alerts", []))
    ]
    return (min(relevant) if relevant else None), len(in_window)


# ---------------------------------------------------------------- registrar
def build_row(corrida: str, deploy: dict, decision: dict, start: datetime, fault: str,
              notes: list[dict], notas: str = "") -> dict:
    version = deploy.get("version", "")
    decided_at = parse_time(decision["time"])
    first, count = summarize_alerts(notes, start, decided_at, version)
    aborted = decision["action"] == "abort"
    return {
        "corrida": corrida,
        "version": version,
        "fault": fault,
        "inicio_trafico_preview": fmt(start),
        "primera_alerta": fmt(first),
        "decision": decision["action"],
        "hora_decision": fmt(decided_at),
        "detectada": ("si" if aborted else "no") if fault in DEFECTIVE else "",
        "falso_positivo": ("si" if aborted else "no") if fault in HEALTHY else "",
        "alertas_recibidas": str(count),
        "decidido_por": decision["by"],
        "analysis_run": deploy.get("analysis_run", ""),
        "notas": notas,
    }


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)


def cmd_registrar(args: argparse.Namespace) -> None:
    deploy, decision = last_deployment(load_records(Path(args.records)))
    if decision is None:
        fail("El último despliegue todavía no tiene decisión. Corre bg.sh promote o bg.sh abort.")
    fault = args.fault or deploy.get("fault") or "ninguna"
    if fault in ("none", ""):
        fault = "ninguna"
    if fault not in ALL_FAULTS:
        fail(f"fault desconocida: {fault}. Valores: {', '.join(sorted(ALL_FAULTS))}")
    start = parse_time(args.inicio) if args.inicio else analysis_start(deploy)
    if start is None:
        fail("No pude leer el inicio del AnalysisRun. Pásalo con --inicio 2026-11-27T15:04:05Z.")
    if sink_restarted_during(sink_running_since(), start):
        msg = ("alert-sink se reinició durante la corrida: sus logs no tienen todas las notificaciones. "
               "Cuéntalas en Loki ({app=\"alert-sink\"} |= \"notification\")")
        if not args.forzar:
            fail(msg + " y registra con --forzar para dejarlo anotado.")
        print(f"[linea-base] Aviso: {msg}.")
        args.notas = ((args.notas or "") + " [alert-sink reiniciado: conteo incompleto]").strip()
    notes = sink_notifications(start)
    row = build_row(str(args.corrida), deploy, decision, start, fault, notes, args.notas or "")
    csv_path = Path(args.csv)
    if csv_path.exists():
        with csv_path.open(encoding="utf-8") as f:
            if any(r.get("analysis_run") == row["analysis_run"] and row["analysis_run"] for r in csv.DictReader(f)):
                fail(f"El AnalysisRun {row['analysis_run']} ya está registrado en {csv_path}.")
    append_row(csv_path, row)
    print(f"[linea-base] Corrida {row['corrida']} registrada en {csv_path}:")
    for k in COLUMNS:
        print(f"  {k:24} {row[k]}")


# ---------------------------------------------------------------- indicadores
def compute_indicators(rows: list[dict]) -> dict:
    def mean_minutes(pairs):
        vals = [(parse_time(b) - parse_time(a)).total_seconds() / 60 for a, b in pairs if a and b]
        return (statistics.mean(vals), len(vals)) if vals else (None, 0)

    defective = [r for r in rows if r["fault"] in DEFECTIVE]
    healthy = [r for r in rows if r["fault"] in HEALTHY]
    detected = sum(r["detectada"] == "si" for r in defective)
    fps = sum(r["falso_positivo"] == "si" for r in healthy)
    mttd, n_mttd = mean_minutes((r["inicio_trafico_preview"], r["primera_alerta"]) for r in defective)
    ttd, n_ttd = mean_minutes((r["inicio_trafico_preview"], r["hora_decision"]) for r in rows)
    alerts = [int(r["alertas_recibidas"] or 0) for r in rows]
    return {
        "corridas": len(rows),
        "defectuosas": len(defective),
        "detectadas": detected,
        "tasa_deteccion": detected / len(defective) if defective else None,
        "sanas": len(healthy),
        "falsos_positivos": fps,
        "tasa_falsos_positivos": fps / len(healthy) if healthy else None,
        "mttd_min": mttd,
        "mttd_n": n_mttd,
        "sin_alerta": len(defective) - n_mttd,
        "tiempo_decision_min": ttd,
        "tiempo_decision_n": n_ttd,
        "alertas_por_despliegue": statistics.mean(alerts) if alerts else None,
    }


def _pct(x):
    return "—" if x is None else f"{x * 100:.1f} %"


def _num(x, unit=""):
    return "—" if x is None else f"{x:.2f}{unit}"


def render_markdown(rows: list[dict]) -> str:
    ind = compute_indicators(rows)
    lines = [
        "| Indicador | Fórmula | Valor |",
        "|---|---|---|",
        f"| Tasa de detección | defectuosas abortadas / defectuosas | {_pct(ind['tasa_deteccion'])} ({ind['detectadas']}/{ind['defectuosas']}) |",
        f"| Tasa de falsos positivos | sanas abortadas / sanas | {_pct(ind['tasa_falsos_positivos'])} ({ind['falsos_positivos']}/{ind['sanas']}) |",
        f"| MTTD | promedio(primera_alerta - inicio_trafico_preview) | {_num(ind['mttd_min'], ' min')} (n={ind['mttd_n']}; {ind['sin_alerta']} defectuosas sin alerta) |",
        f"| Tiempo hasta la decisión | promedio(hora_decision - inicio_trafico_preview) | {_num(ind['tiempo_decision_min'], ' min')} (n={ind['tiempo_decision_n']}) |",
        f"| Volumen de alertas por despliegue | promedio(alertas_recibidas) | {_num(ind['alertas_por_despliegue'])} |",
        "",
        "Por escenario:",
        "",
        "| Escenario | Corridas | Abortadas | Con alerta | MTTD (min) | Alertas/despliegue |",
        "|---|---|---|---|---|---|",
    ]
    for fault in sorted({r["fault"] for r in rows}):
        sub = [r for r in rows if r["fault"] == fault]
        ind_f = compute_indicators(sub)
        mttd_vals = [(parse_time(r["primera_alerta"]) - parse_time(r["inicio_trafico_preview"])).total_seconds() / 60
                     for r in sub if r["primera_alerta"]]
        lines.append(
            f"| {fault} | {len(sub)} | {sum(r['decision'] == 'abort' for r in sub)} | {len(mttd_vals)} | "
            f"{_num(statistics.mean(mttd_vals)) if mttd_vals else '—'} | {_num(ind_f['alertas_por_despliegue'])} |")
    return "\n".join(lines) + "\n"


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        fail(f"No existe {path}. Registra corridas con: linea_base.py registrar")
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cmd_indicadores(args: argparse.Namespace) -> None:
    md = render_markdown(read_csv(Path(args.csv)))
    print(md)
    if args.salida:
        Path(args.salida).write_text(md, encoding="utf-8")
        print(f"[linea-base] Tabla guardada en {args.salida}")


# ---------------------------------------------------------------- calibrar
def prom_query(query: str, at: datetime) -> list[dict]:
    qs = urllib.parse.urlencode({"query": query, "time": at.timestamp()})
    data = json.loads(kubectl("get", "--raw", f"{PROMETHEUS}/api/v1/query?{qs}"))
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus: {data}")
    return data["data"]["result"]


def p95_query(version: str, seconds: int) -> str:
    return (
        "histogram_quantile(0.95, sum by (le, uri) (increase(http_server_requests_seconds_bucket"
        f'{{namespace="petclinic", version="{version}", uri!~"/oups|/actuator.*"}}[{seconds}s])))'
    )


def thresholds_block(thresholds: dict[str, float], indent: str = "        ") -> str:
    out = [f"{indent}# BEGIN umbrales-latencia"]
    out.append(f"{indent}# Calibrado {fmt(datetime.now(timezone.utc))}: p95 sano promedio × {LATENCY_FACTOR}")
    for uri, value in thresholds.items():
        out.append(f"{indent}- record: petclinic:slo_latency_p95_threshold_seconds")
        out.append(f"{indent}  expr: vector({value:.4f})")
        out.append(f'{indent}  labels: {{uri: "{uri}"}}')
    out.append(f"{indent}# END umbrales-latencia")
    return "\n".join(out)


def current_thresholds(text: str) -> dict[str, float]:
    """Umbrales que ya están en el bloque de reglas, por ruta."""
    return {u: float(v) for v, u in re.findall(r'expr: vector\(([\d.]+)\)\s+labels: \{uri: "([^"]+)"\}', text)}


def merge_thresholds(calibrated: dict[str, float], current: dict[str, float]) -> dict[str, float]:
    """Calibrados primero; las rutas sin datos conservan su umbral actual.

    Orden: las rutas de SLO_URIS y después cualquier otra que ya tuviera umbral en el YAML, para no
    borrar en silencio una ruta agregada solo en las reglas.
    """
    merged = {**current, **calibrated}
    ordered = {u: merged[u] for u in SLO_URIS if u in merged}
    ordered.update({u: v for u, v in merged.items() if u not in ordered})
    return ordered


def rewrite_thresholds(text: str, thresholds: dict[str, float]) -> str:
    pattern = re.compile(r"^(?P<indent>[ \t]*)# BEGIN umbrales-latencia.*?# END umbrales-latencia", re.S | re.M)
    m = pattern.search(text)
    if not m:
        fail(f"No encontré el bloque 'BEGIN/END umbrales-latencia' en {RULES}")
    return text[:m.start()] + thresholds_block(thresholds, m.group("indent")) + text[m.end():]


def cmd_calibrar(args: argparse.Namespace) -> None:
    rows = [r for r in read_csv(Path(args.csv)) if r["fault"] == "ninguna"]
    if len(rows) < 3:
        print(f"[linea-base] Aviso: hay {len(rows)} corridas sanas; la propuesta pide 3 o más.")
    if not rows:
        fail("No hay corridas sanas (fault=ninguna) en el CSV.")
    per_uri: dict[str, list[float]] = {}
    for r in rows:
        start, end = parse_time(r["inicio_trafico_preview"]), parse_time(r["hora_decision"])
        seconds = max(60, int((end - start).total_seconds()))
        for s in prom_query(p95_query(r["version"], seconds), end):
            value = float(s["value"][1])
            if value == value:  # descarta NaN (ruta sin tráfico)
                per_uri.setdefault(s["metric"].get("uri", ""), []).append(value)
        print(f"  corrida {r['corrida']} ({r['version']}): {seconds}s analizados")
    thresholds = {}
    print("\n| Ruta | p95 sano promedio (ms) | Umbral = × 1,5 (ms) | Corridas |\n|---|---|---|---|")
    for uri in SLO_URIS:
        vals = per_uri.get(uri)
        if not vals:
            print(f"| {uri} | sin datos | se mantiene el valor actual | 0 |")
            continue
        healthy = statistics.mean(vals)
        thresholds[uri] = healthy * LATENCY_FACTOR
        print(f"| {uri} | {healthy * 1000:.1f} | {thresholds[uri] * 1000:.1f} | {len(vals)} |")
    if not thresholds:
        fail("Prometheus no devolvió latencias. ¿Las corridas tienen menos de 2 días (retención)?")
    if args.dry_run:
        return
    text = RULES.read_text(encoding="utf-8")
    # Conserva los umbrales actuales de las rutas sin datos.
    ordered = merge_thresholds(thresholds, current_thresholds(text))
    # Primero se calcula el texto nuevo (falla sin tocar nada si falta el marcador) y luego se
    # reemplaza el archivo de forma atómica, para no dejar nunca las reglas vacías o a medias.
    new_text = rewrite_thresholds(text, ordered)
    tmp = RULES.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)
    os.replace(tmp, RULES)
    print(f"\n[linea-base] Umbrales escritos en {RULES.relative_to(REPO)}. Aplícalos con: k8s/observability/obs.sh rules")


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("registrar", help="agrega la última corrida al CSV")
    r.add_argument("--corrida", required=True, help="número de corrida")
    r.add_argument("--fault", choices=sorted(ALL_FAULTS),
                   help="escenario; por defecto el campo 'fault' de deployments.jsonl o 'ninguna'")
    r.add_argument("--inicio", help="inicio del tráfico a preview (ISO 8601) si no se puede leer del AnalysisRun")
    r.add_argument("--notas", help="observaciones libres")
    r.add_argument("--forzar", action="store_true",
                   help="registra aunque alert-sink se haya reiniciado durante la corrida (queda anotado)")
    r.add_argument("--csv", default=str(DEFAULT_CSV))
    r.add_argument("--records", default=str(RECORDS))
    r.set_defaults(func=cmd_registrar)

    i = sub.add_parser("indicadores", help="tabla de indicadores (Tabla 6)")
    i.add_argument("--csv", default=str(DEFAULT_CSV))
    i.add_argument("--salida", help="guarda la tabla en un archivo Markdown")
    i.set_defaults(func=cmd_indicadores)

    c = sub.add_parser("calibrar", help="umbrales de latencia con las corridas sanas")
    c.add_argument("--csv", default=str(REPO / "docs/calibracion.csv"))
    c.add_argument("--dry-run", action="store_true", help="solo muestra los umbrales, no edita las reglas")
    c.set_defaults(func=cmd_calibrar)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
