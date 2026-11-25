"""Entrena, calibra y evalúa Isolation Forest con ventanas etiquetadas por corrida."""

import argparse
import csv
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest


FEATURES = ("vets_p95_ms", "process_cpu_ratio", "heap_mib", "gc_pause_seconds",
            "pool_active_mean")
METADATA = ("run_id", "revision", "target", "load_profile", "split", "condition")


def vector(row):
    values = [float(row[name]) for name in FEATURES]
    if not all(math.isfinite(v) and v >= 0 for v in values) or values[1] > 1:
        raise ValueError("señal no finita, negativa o CPU fuera de [0,1]")
    return values


def read_windows(path):
    groups = {"train": [], "calibration": [], "evaluation": []}
    splits = {}
    seen = set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = set(FEATURES + METADATA) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"faltan columnas: {sorted(missing)}")
        for row in reader:
            try:
                if not all(row[k] and row[k].strip() == row[k] for k in METADATA):
                    raise ValueError("metadato vacío o con espacios externos")
                split, run = row["split"], row["run_id"]
                if split not in groups or row["condition"] not in {"healthy", "fault"}:
                    raise ValueError("partición o condición desconocida")
                if row["target"] not in {"active", "preview"}:
                    raise ValueError("destino desconocido")
                key = (run, row["target"])
                if key in seen or splits.setdefault(run, split) != split:
                    raise ValueError("ventana duplicada o corrida repartida entre particiones")
                seen.add(key)
                if split != "evaluation" and (row["condition"], row["target"]) != ("healthy", "active"):
                    raise ValueError("entrenamiento y calibración requieren una revisión activa saludable")
                groups[split].append((row, vector(row)))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"fila {reader.line_num}: {exc}") from exc
    if not all(groups.values()):
        raise ValueError("se requieren corridas independientes en las tres particiones")
    if len({r["load_profile"] for r, _ in groups["train"]}) < 2:
        raise ValueError("la referencia necesita distintos niveles de carga saludable")
    return groups


def fit_reference(path, quantile):
    if not 0 < quantile < 1:
        raise ValueError("el cuantil de calibración debe estar entre 0 y 1")
    groups = read_windows(path)
    estimator = IsolationForest(random_state=42)
    estimator.fit([x for _, x in groups["train"]])
    calibration = -estimator.score_samples([x for _, x in groups["calibration"]])
    threshold = float(np.quantile(calibration, quantile))
    rows = groups["evaluation"]
    scores = -estimator.score_samples([x for _, x in rows])
    outcomes = []
    for (row, _), score in zip(rows, scores):
        outcomes.append({**{k: row[k] for k in METADATA}, "deviation": float(score),
                         "above_threshold": bool(score > threshold)})
    return {
        "estimator": estimator, "features": FEATURES, "threshold": threshold,
        "quantile": quantile, "sklearn_version": sklearn.__version__,
        "reference_runs": {s: sorted({r["run_id"] for r, _ in group})
                           for s, group in groups.items()},
    }, outcomes


def save_model(model, destination):
    # Una interrupción al serializar nunca deja un modelo parcial en el destino.
    with NamedTemporaryFile(dir=destination.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        joblib.dump(model, tmp_path, compress=3)
        os.replace(tmp_path, destination)
    finally:
        tmp_path.unlink(missing_ok=True)


def load_model(path):
    # Solo archivos locales de confianza generados por esta herramienta.
    model = joblib.load(path)
    if model["sklearn_version"] != sklearn.__version__ or tuple(model["features"]) != FEATURES:
        raise ValueError("versión de scikit-learn o columnas incompatibles")
    return model


def score_window(model, row):
    score = float(-model["estimator"].score_samples([vector(row)])[0])
    return {"deviation": score, "threshold": model["threshold"],
            "above_threshold": score > model["threshold"], "mode": "observation"}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("windows", type=Path)
    cli.add_argument("model", type=Path)
    cli.add_argument("--quantile", type=float, required=True)
    args = cli.parse_args()
    try:
        reference, evaluation = fit_reference(args.windows, args.quantile)
        save_model(reference, args.model)
        print(json.dumps({"reference_runs": reference["reference_runs"],
                          "features": FEATURES, "quantile": args.quantile,
                          "threshold": reference["threshold"], "evaluation": evaluation},
                         indent=2, allow_nan=False))
    except (OSError, ValueError) as exc:
        cli.exit(1, f"Error: {exc}\n")
