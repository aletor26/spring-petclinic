"""Comparación exploratoria por operación de un único archivo granular de k6."""

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

from scipy.stats import mannwhitneyu

from k6_report import build_report


def latency_analysis(path, records=None, min_samples=2, block_seconds=None):
    if min_samples < 2 or (block_seconds is not None and block_seconds < 1):
        raise ValueError("mínimo por destino >= 2 y tamaño de bloque positivo")
    report = build_report(path, records, include_samples=True)
    samples = report.pop("samples")
    groups = defaultdict(lambda: defaultdict(list))
    for sample in samples:
        key = sample["route"], sample["method"], sample["target"]
        timestamp = datetime.fromisoformat(sample["time"].replace("Z", "+00:00"))
        if timestamp.utcoffset() is None:
            raise ValueError("la hora de k6 debe incluir zona horaria")
        block = int(timestamp.timestamp() // block_seconds) if block_seconds else 0
        groups[key][block].append(sample["duration_ms"])

    report["mode"] = "observation"
    report["test"] = {"name": "Mann-Whitney U", "alternative": "preview_greater",
                      "unit": "block_median" if block_seconds else "request",
                      "block_seconds": block_seconds, "min_units_per_target": min_samples}
    for op in report["operations"]:
        key = op["route"], op["method"]
        active = groups[(*key, "active")]
        preview = groups[(*key, "preview")]
        if block_seconds:
            shared = sorted(active.keys() & preview.keys())
            a = [median(active[i]) for i in shared]
            b = [median(preview[i]) for i in shared]
        else:
            a, b = active.get(0, []), preview.get(0, [])
        result = {"active_units": len(a), "preview_units": len(b),
                  "delta_median_ms": median(b) - median(a) if a and b else None,
                  "p_value": None, "u_preview": None, "status": "insufficient_samples"}
        if min(len(a), len(b)) >= min_samples:
            # Asintótico con corrección de empates, incluido el caso de muestras pequeñas.
            test = mannwhitneyu(b, a, alternative="greater", method="asymptotic")
            result.update(p_value=float(test.pvalue), u_preview=float(test.statistic),
                          status="exploratory")
        op["comparison"] = result
    return report


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("file", type=Path)
    cli.add_argument("--deployments", type=Path)
    cli.add_argument("--min-samples", type=int, default=2)
    cli.add_argument("--block-seconds", type=int)
    args = cli.parse_args()
    try:
        print(json.dumps(latency_analysis(args.file, args.deployments, args.min_samples,
                                          args.block_seconds), indent=2, allow_nan=False))
    except (ValueError, OSError, EOFError, KeyError) as exc:
        cli.exit(1, f"Error: {exc}\n")
