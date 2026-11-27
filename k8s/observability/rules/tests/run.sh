#!/usr/bin/env bash
# Valida y prueba las reglas de la línea base con promtool (sin clúster).
#   k8s/observability/rules/tests/run.sh
# Usa promtool si está instalado (brew install prometheus); si no, la imagen de Prometheus en Docker.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROM_IMAGE=prom/prometheus:v3.16.0
YQ_IMAGE=mikefarah/yq:4

# 1. Quitar el envoltorio del PrometheusRule: promtool solo entiende "groups".
if command -v yq >/dev/null; then
  yq '{"groups": .spec.groups}' "$HERE/../petclinic-rules.yaml" > "$HERE/rules.generated.yaml"
else
  docker run --rm -i "$YQ_IMAGE" '{"groups": .spec.groups}' < "$HERE/../petclinic-rules.yaml" > "$HERE/rules.generated.yaml"
fi

# 2. Sintaxis + lint, y 3. pruebas unitarias de cada alerta.
if command -v promtool >/dev/null; then
  (cd "$HERE" && promtool check rules rules.generated.yaml && promtool test rules petclinic-rules.test.yaml)
else
  docker run --rm -v "$HERE:/w" -w /w --entrypoint sh "$PROM_IMAGE" \
    -c 'promtool check rules rules.generated.yaml && promtool test rules petclinic-rules.test.yaml'
fi
