"""Resume las latencias HTTP de una corrida blue-green de PetClinic."""

import argparse
import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median


def json_lines(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("se esperaba un objeto JSON")
                except ValueError as exc:
                    raise ValueError(f"{path.name}, línea {number}: {exc}") from exc
                yield number, record


def read_samples(path):
    for number, point in json_lines(path):
        if point.get("type") != "Point" or point.get("metric") != "http_req_duration":
            continue
        try:
            data = point["data"]
            tags = data["tags"]
            target = tags["target"]
            if target == "setup":
                continue
            if target not in ("active", "preview"):
                raise ValueError(f"destino inesperado: {target}")
            sample = {key: tags[key] for key in ("target", "version", "route", "method", "status")}
            sample["time"] = data["time"]
            if not all(isinstance(value, str) and value.strip() for value in sample.values()):
                raise ValueError("etiqueta u hora vacía o inválida")
            if sample["version"] == "unknown":
                raise ValueError("versión desconocida")
            value = data["value"]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("latencia inválida")
            sample["duration_ms"] = value
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path.name}, línea {number}: {exc}") from exc
        yield sample


def match_deployment(path, records_path, versions):
    matches = []
    for _, record in json_lines(records_path):
        if record.get("action") != "deploy":
            continue
        suffix = "_{version}_preview-{preview_hash}_stable-{stable_hash}.json.gz".format_map(record)
        if path.name.endswith(suffix):
            matches.append(record)
    if len(matches) != 1:
        raise ValueError(f"se esperaba un deploy identificable para {path.name}, encontrados: {len(matches)}")
    record = matches[0]
    for target, field in (("active", "active_version"), ("preview", "version")):
        if target in versions and versions[target] != record[field]:
            raise ValueError(f"la versión de {target} difiere del registro de despliegue")
    return record


def latency_summary(values):
    if not values:
        return {"count": 0, "median_ms": None, "p95_ms": None}
    ordered = sorted(values)
    # Interpolación lineal, también válida con una única observación.
    position = (len(ordered) - 1) * 0.95
    lower = math.floor(position)
    p95 = ordered[lower] + (position - lower) * (ordered[math.ceil(position)] - ordered[lower])
    return {"count": len(ordered), "median_ms": median(ordered), "p95_ms": p95}


def build_report(path, records_path=None, include_samples=False):
    operations = defaultdict(lambda: defaultdict(list))
    versions = {}
    samples = []
    for sample in read_samples(path):
        target = sample["target"]
        if versions.setdefault(target, sample["version"]) != sample["version"]:
            raise ValueError(f"más de una versión en {target} durante la corrida")
        operations[sample["route"], sample["method"]][target].append(sample["duration_ms"])
        if include_samples:
            samples.append(sample)
    if not operations:
        raise ValueError("sin muestras de latencia de active o preview")

    report = {"file": path.name, "versions": versions, "operations": []}
    if records_path:
        report["deployment"] = match_deployment(path, records_path, versions)
    for (route, method), groups in sorted(operations.items()):
        report["operations"].append({
            "route": route,
            "method": method,
            "both_targets_present": "active" in groups and "preview" in groups,
            "active": latency_summary(groups.get("active")),
            "preview": latency_summary(groups.get("preview")),
        })
    if include_samples:
        report["samples"] = samples
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="salida granular de k6 (.json o .json.gz)")
    parser.add_argument("--deployments", type=Path, help="registro deployments.jsonl del análisis")
    parser.add_argument("--samples", action="store_true", help="incluir las latencias individuales")
    args = parser.parse_args()
    try:
        report = build_report(args.file, args.deployments, args.samples)
    except (OSError, EOFError, KeyError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    json.dump(report, sys.stdout, indent=2, allow_nan=False)
    print()


if __name__ == "__main__":
    main()
