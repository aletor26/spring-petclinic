#!/usr/bin/env bash
# Blue-Green de Spring PetClinic en Kubernetes local (kind + Argo Rollouts).
#
#   ./bg.sh up                 Crea el clúster, instala Argo y despliega la primera versión
#   ./bg.sh deploy [version]   Despliega el código actual en PREVIEW y corre el análisis
#                              (versión por defecto: commit actual)
#   ./bg.sh promote            Pasa el tráfico de usuarios a la versión en PREVIEW
#   ./bg.sh abort              Descarta la versión en PREVIEW (el tráfico no cambia)
#   ./bg.sh rollback           Vuelve a la versión anterior (tras haber promovido)
#   ./bg.sh status             Estado del Rollout: activa, preview y análisis
#   ./bg.sh traffic            Petición cada 0.5 s a localhost:8080, muestra versión y código HTTP
#   ./bg.sh load [segundos]    k6: mismo tráfico a ACTIVE y PREVIEW, guarda resultados JSON
#   ./bg.sh down               Borra el clúster
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CLUSTER=petclinic
NS=petclinic
ARGO_VERSION=v1.10.0
ACTIVE_URL=http://localhost:8080
PREVIEW_URL=http://localhost:8081

log()  { printf '\033[1;36m[bg]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[bg] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
k()    { kubectl --context "kind-$CLUSTER" -n "$NS" "$@"; }
ro()   { kubectl argo rollouts --context "kind-$CLUSTER" -n "$NS" "$@"; }

default_version() {  # commit corto (+ -dirty si hay cambios sin commitear)
  local v; v=$(git -C "$REPO" rev-parse --short HEAD)
  git -C "$REPO" diff --quiet HEAD -- src pom.xml || v="$v-dirty"
  echo "$v"
}

build_and_load() {  # build_and_load <version>
  log "Construyendo imagen petclinic:$1"
  docker build -t "petclinic:$1" "$REPO"
  log "Cargando imagen en el clúster kind"
  kind load docker-image "petclinic:$1" --name "$CLUSTER"
}

set_version() {  # cambia imagen, etiqueta version y variable INFO_APP_VERSION del Rollout
  k patch rollout petclinic --type json -p "[
    {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/image\",\"value\":\"petclinic:$1\"},
    {\"op\":\"replace\",\"path\":\"/spec/template/metadata/labels/version\",\"value\":\"$1\"},
    {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/env/0/value\",\"value\":\"$1\"}]"
}

version_at() { curl -s -m 2 "$1/actuator/info" | sed -nE 's/.*"app":\{[^}]*"version":"([^"]+)".*/\1/p'; }

cmd_up() {
  if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    log "Creando clúster kind '$CLUSTER'"
    kind create cluster --config "$HERE/kind-cluster.yaml"
  fi
  log "Instalando Argo Rollouts $ARGO_VERSION"
  kubectl --context "kind-$CLUSTER" create namespace argo-rollouts --dry-run=client -o yaml | kubectl --context "kind-$CLUSTER" apply -f -
  kubectl --context "kind-$CLUSTER" apply --server-side -n argo-rollouts \
    -f "https://github.com/argoproj/argo-rollouts/releases/download/$ARGO_VERSION/install.yaml" >/dev/null
  kubectl --context "kind-$CLUSTER" -n argo-rollouts rollout status deploy/argo-rollouts --timeout=180s

  local v="${1:-$(default_version)}"
  build_and_load "$v"
  log "Aplicando manifiestos"
  for f in "$HERE"/manifests/[0-3]*.yaml; do kubectl --context "kind-$CLUSTER" apply -f "$f"; done
  sed -e "s/petclinic:v1/petclinic:$v/" -e "s/version: v1/version: $v/" -e "s/value: v1/value: $v/" \
    "$HERE/manifests/40-rollout.yaml" | kubectl --context "kind-$CLUSTER" apply -f -
  k rollout status statefulset/petclinic-db --timeout=180s
  ro status petclinic --timeout 300s
  log "Listo: $ACTIVE_URL (versión $v)"
}

cmd_deploy() {
  local v="${1:-$(default_version)}"
  build_and_load "$v"
  log "Desplegando $v en PREVIEW (los usuarios siguen en la versión actual)"
  set_version "$v"
  log "Esperando que PREVIEW esté lista y el análisis termine..."
  sleep 5
  for _ in $(seq 1 120); do
    local phase; phase=$(k get rollout petclinic -o jsonpath='{.status.phase}')
    case "$phase" in
      Paused)   log "✅ Análisis OK. PREVIEW ($PREVIEW_URL) espera promoción: ./bg.sh promote"; ro get rollout petclinic; return 0 ;;
      Degraded) ro get rollout petclinic; fail "Análisis FALLÓ: $v descartada. El tráfico sigue en la versión estable." ;;
    esac
    sleep 5
  done
  ro get rollout petclinic; fail "Tiempo de espera agotado"
}

cmd_promote() {
  log "Promoviendo PREVIEW -> ACTIVE"
  ro promote petclinic
  ro status petclinic --timeout 120s
  log "Versión activa ahora: $(version_at $ACTIVE_URL)"
}

cmd_rollback() {
  log "Rollback a la revisión anterior"
  ro undo petclinic
  sleep 3
  ro promote petclinic --full   # versión ya probada: se activa sin repetir el análisis
  ro status petclinic --timeout 180s
  log "Versión activa ahora: $(version_at $ACTIVE_URL)"
}

cmd_traffic() {
  log "Ctrl+C para detener"
  while true; do
    local code ver
    code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "$ACTIVE_URL/vets.html" || true)
    ver=$(version_at "$ACTIVE_URL" || true)
    printf '%s  HTTP %s  version=%s\n' "$(date +%T)" "${code:-000}" "${ver:--}"
    sleep 0.5
  done
}

cmd_load() {
  local secs="${1:-60}" out="$HERE/k6/results"
  mkdir -p "$out"
  local file="k6-$(date +%Y%m%d-%H%M%S).json"
  log "k6: ${secs}s de tráfico idéntico a ACTIVE y PREVIEW -> k6/results/$file"
  docker run --rm -i --user "$(id -u):$(id -g)" --add-host=host.docker.internal:host-gateway \
    -e DURATION="${secs}s" \
    -e ACTIVE_URL=http://host.docker.internal:8080 \
    -e PREVIEW_URL=http://host.docker.internal:8081 \
    -e ACTIVE_VERSION="$(version_at $ACTIVE_URL)" \
    -e PREVIEW_VERSION="$(version_at $PREVIEW_URL)" \
    -v "$HERE/k6:/k6" \
    grafana/k6:0.57.0 run --quiet --out "json=/k6/results/$file" /k6/load.js
}

case "${1:-}" in
  up)       shift; cmd_up "$@" ;;
  deploy)   shift; cmd_deploy "$@" ;;
  promote)  cmd_promote ;;
  abort)    ro abort petclinic; log "PREVIEW descartada; el tráfico sigue en la versión activa" ;;
  rollback) cmd_rollback ;;
  status)   ro get rollout petclinic; echo "ACTIVE  $ACTIVE_URL  -> $(version_at $ACTIVE_URL)"; echo "PREVIEW $PREVIEW_URL -> $(version_at $PREVIEW_URL)" ;;
  traffic)  cmd_traffic ;;
  load)     shift; cmd_load "$@" ;;
  down)     kind delete cluster --name "$CLUSTER" ;;
  *)        sed -n '2,15p' "$0" ;;
esac
