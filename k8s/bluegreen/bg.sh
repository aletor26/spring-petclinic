#!/usr/bin/env bash
# Blue-Green de Spring PetClinic en Kubernetes local (kind + Argo Rollouts).
#
#   ./bg.sh up [version]                     Crea registro + clúster, instala Argo y despliega la primera versión
#   ./bg.sh deploy [version] [--window 10m] [--fault tipo]  Despliega PREVIEW y corre el análisis:
#                                            smoke tests + k6 durante la ventana (por defecto 10m)
#   ./bg.sh promote                          Pasa el tráfico de usuarios a la versión en PREVIEW
#   ./bg.sh abort                            Descarta la versión en PREVIEW (el tráfico no cambia)
#   ./bg.sh rollback                         Vuelve a la versión anterior (tras haber promovido)
#   ./bg.sh status                           Estado del Rollout: activa, preview y análisis
#   ./bg.sh history                          Registro de despliegues (trazabilidad)
#   ./bg.sh results                          Copia los JSON de k6 del clúster a k6/results/
#   ./bg.sh traffic                          Petición cada 0.5 s a localhost:8080: versión y código HTTP
#   ./bg.sh load [segundos]                  k6 manual desde la máquina contra ACTIVE y PREVIEW
#   ./bg.sh down                             Borra el clúster y el registro local
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CLUSTER=petclinic
NS=petclinic
ARGO_VERSION=v1.10.0
REG_NAME=kind-registry
REG_PORT=5001
IMAGE_REPO="localhost:$REG_PORT/petclinic"
ACTIVE_URL=http://localhost:8080
PREVIEW_URL=http://localhost:8081
RECORDS="$HERE/records/deployments.jsonl"

log()  { printf '\033[1;36m[bg]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[bg] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
kc()   { kubectl --context "kind-$CLUSTER" "$@"; }
k()    { kc -n "$NS" "$@"; }
ro()   { kubectl argo rollouts --context "kind-$CLUSTER" -n "$NS" "$@"; }

version_at() { curl -s -m 2 "$1/actuator/info" | sed -nE 's/.*"app":\{[^}]*"version":"([^"]+)".*/\1/p'; }
commit_id()  { git -C "$REPO" rev-parse --short HEAD; }

default_version() {  # commit corto (+ -dirty si hay cambios sin commitear en el código)
  local v; v=$(commit_id)
  git -C "$REPO" diff --quiet HEAD -- src pom.xml Dockerfile || v="$v-dirty"
  echo "$v"
}

# ---------- trazabilidad ----------
record() {  # record <accion> <resultado>
  mkdir -p "$(dirname "$RECORDS")"
  local image stable current ar ar_phase="" window version
  image=$(k get rollout petclinic -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
  version=$(k get rollout petclinic -o jsonpath='{.spec.template.metadata.labels.version}' 2>/dev/null || true)
  stable=$(k get rollout petclinic -o jsonpath='{.status.stableRS}' 2>/dev/null || true)
  current=$(k get rollout petclinic -o jsonpath='{.status.currentPodHash}' 2>/dev/null || true)
  window=$(k get rollout petclinic -o jsonpath='{.spec.strategy.blueGreen.prePromotionAnalysis.args[1].value}' 2>/dev/null || true)
  ar=$(k get analysisrun --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true)
  if [ -n "$ar" ]; then ar_phase=$(k get analysisrun "$ar" -o jsonpath='{.status.phase}' 2>/dev/null || true); fi
  local fault; fault=$(k get rollout petclinic -o jsonpath='{.spec.template.spec.containers[0].env[1].value}' 2>/dev/null || true)
  printf '{"time":"%s","action":"%s","result":"%s","version":"%s","fault":"%s","commit":"%s","image":"%s","stable_hash":"%s","preview_hash":"%s","active_version":"%s","window":"%s","analysis_run":"%s","analysis_phase":"%s","k6_scenario":"k6/load.js"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$version" "${fault:-none}" "$(commit_id)" "$image" "$stable" "$current" \
    "$(version_at $ACTIVE_URL)" "$window" "$ar" "$ar_phase" >> "$RECORDS"
}

# ---------- infraestructura ----------
ensure_registry() {
  if [ "$(docker inspect -f '{{.State.Running}}' "$REG_NAME" 2>/dev/null || true)" != true ]; then
    log "Levantando registro local de imágenes en localhost:$REG_PORT"
    docker rm -f "$REG_NAME" >/dev/null 2>&1 || true
    docker run -d --restart=always -p "127.0.0.1:$REG_PORT:5000" --name "$REG_NAME" registry:2 >/dev/null
  fi
}

connect_registry() {  # el nodo de kind resuelve localhost:5001 hacia el contenedor del registro
  local node
  for node in $(kind get nodes --name "$CLUSTER"); do
    docker exec "$node" mkdir -p "/etc/containerd/certs.d/localhost:$REG_PORT"
    printf '[host."http://%s:5000"]\n' "$REG_NAME" | docker exec -i "$node" cp /dev/stdin "/etc/containerd/certs.d/localhost:$REG_PORT/hosts.toml"
  done
  docker network connect kind "$REG_NAME" 2>/dev/null || true
}

build_and_push() {  # build_and_push <version>  -> deja en IMAGE_REF la imagen fijada por digest
  log "Construyendo imagen $IMAGE_REPO:$1"
  docker build -t "$IMAGE_REPO:$1" "$REPO"
  docker push -q "$IMAGE_REPO:$1" >/dev/null
  IMAGE_REF=$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$IMAGE_REPO:$1" | grep "^$IMAGE_REPO@" | head -1)
  [ -n "$IMAGE_REF" ] || fail "No se pudo obtener el digest de $IMAGE_REPO:$1"
  log "Imagen fijada por digest: $IMAGE_REF"
}

apply_k6_script() {
  k create configmap k6-script --from-file=load.js="$HERE/k6/load.js" --dry-run=client -o yaml | k apply -f - >/dev/null
}

set_version() {  # set_version <version> <image-ref> <window> <fault>
  k patch rollout petclinic --type json -p "[
    {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/image\",\"value\":\"$2\"},
    {\"op\":\"replace\",\"path\":\"/spec/template/metadata/labels/version\",\"value\":\"$1\"},
    {\"op\":\"replace\",\"path\":\"/spec/template/metadata/annotations/petclinic~1commit\",\"value\":\"$(commit_id)\"},
    {\"op\":\"replace\",\"path\":\"/spec/template/metadata/annotations/petclinic~1image\",\"value\":\"$2\"},
    {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/env/0/value\",\"value\":\"$1\"},
    {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/env/1/value\",\"value\":\"$4\"},
    {\"op\":\"replace\",\"path\":\"/spec/strategy/blueGreen/prePromotionAnalysis/args/1/value\",\"value\":\"$3\"},
    {\"op\":\"replace\",\"path\":\"/spec/strategy/blueGreen/prePromotionAnalysis/args/2/value\",\"value\":\"$1\"},
    {\"op\":\"replace\",\"path\":\"/spec/strategy/blueGreen/prePromotionAnalysis/args/3/value\",\"value\":\"$4\"}]" >/dev/null
}

# ---------- comandos ----------
cmd_up() {
  local v="${1:-$(default_version)}"
  ensure_registry
  if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    log "Creando clúster kind '$CLUSTER'"
    kind create cluster --config "$HERE/kind-cluster.yaml"
  fi
  connect_registry
  log "Instalando Argo Rollouts $ARGO_VERSION"
  kc create namespace argo-rollouts --dry-run=client -o yaml | kc apply -f - >/dev/null
  kc apply --server-side -n argo-rollouts \
    -f "https://github.com/argoproj/argo-rollouts/releases/download/$ARGO_VERSION/install.yaml" >/dev/null
  kc -n argo-rollouts rollout status deploy/argo-rollouts --timeout=180s

  build_and_push "$v"
  log "Aplicando manifiestos"
  kc apply -f "$HERE/manifests/00-namespace.yaml" >/dev/null
  apply_k6_script
  kc apply -f "$HERE/manifests/10-postgres.yaml"
  k rollout status statefulset/petclinic-db --timeout=180s
  log "Inicializando esquema y datos (una sola vez, Job petclinic-db-init)"
  k create configmap db-init-sql \
    --from-file=schema.sql="$REPO/src/main/resources/db/postgres/schema.sql" \
    --from-file=data.sql="$REPO/src/main/resources/db/postgres/data.sql" \
    --dry-run=client -o yaml | k apply -f - >/dev/null
  k delete job petclinic-db-init --ignore-not-found >/dev/null
  kc apply -f "$HERE/manifests/15-db-init.yaml" >/dev/null
  k wait --for=condition=complete job/petclinic-db-init --timeout=180s
  local f
  for f in 20-services 30-analysis-template 50-k6; do kc apply -f "$HERE/manifests/$f.yaml"; done
  if k get rollout petclinic >/dev/null 2>&1; then
    set_version "$v" "$IMAGE_REF" 10m "${FAULT:-}"
  else
    sed -e "s|localhost:5001/petclinic:v1|$IMAGE_REF|" -e "s/version: v1/version: $v/" -e "s/value: v1/value: $v/" \
        -e "s/petclinic\/commit: \"none\"/petclinic\/commit: \"$(commit_id)\"/" \
        -e "s|petclinic/image: \"none\"|petclinic/image: \"$IMAGE_REF\"|" \
      "$HERE/manifests/40-rollout.yaml" | kc apply -f -
  fi
  ro status petclinic --timeout 300s
  record up ok
  log "Listo: $ACTIVE_URL (versión $v)"
}

cmd_deploy() {
  local v="" window=10m fault=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --window) window="$2"; shift 2 ;;
      --fault) fault="$2"; shift 2 ;;
      *) v="$1"; shift ;;
    esac
  done
  v="${v:-$(default_version)}"
  case "$fault" in ""|latency|slow-query|visit-write|heap-leak) ;; *) fail "FAULT inválido: $fault" ;; esac
  export FAULT="$fault"
  build_and_push "$v"
  apply_k6_script
  log "Desplegando $v en PREVIEW (los usuarios siguen en la versión actual)"
  set_version "$v" "$IMAGE_REF" "$window" "$fault"
  log "FAULT=${fault:-none}; análisis previo durante $window..."
  sleep 5
  local i phase
  for i in $(seq 1 480); do   # hasta 40 min
    phase=$(k get rollout petclinic -o jsonpath='{.status.phase}')
    case "$phase" in
      Paused)
        record deploy analysis-ok
        log "✅ Análisis OK. PREVIEW ($PREVIEW_URL) espera promoción: ./bg.sh promote"
        ro get rollout petclinic; return 0 ;;
      Degraded)
        record deploy analysis-failed
        ro get rollout petclinic
        fail "Análisis FALLÓ: $v descartada. El tráfico sigue en la versión estable. Detalle: ./bg.sh status" ;;
    esac
    [ $((i % 12)) -eq 0 ] && log "  ...analizando ($((i * 5 / 60)) min)"
    sleep 5
  done
  record deploy timeout
  ro get rollout petclinic; fail "Tiempo de espera agotado"
}

cmd_promote() {
  log "Promoviendo PREVIEW -> ACTIVE"
  ro promote petclinic
  ro status petclinic --timeout 180s
  record promote ok
  log "Versión activa ahora: $(version_at $ACTIVE_URL)"
}

cmd_rollback() {
  log "Rollback a la revisión anterior"
  ro undo petclinic
  sleep 3
  ro promote petclinic --full   # versión ya probada: se activa sin repetir el análisis
  ro status petclinic --timeout 180s
  record rollback ok
  log "Versión activa ahora: $(version_at $ACTIVE_URL)"
}

cmd_results() {  # copia los JSON del PVC k6-results a k6/results/
  local pod=k6-results-reader dest="$HERE/k6/results"
  mkdir -p "$dest"
  k delete pod "$pod" --ignore-not-found >/dev/null
  k run "$pod" --image=busybox:1.37 --restart=Never \
    --overrides='{"spec":{"containers":[{"name":"r","image":"busybox:1.37","command":["sleep","300"],"volumeMounts":[{"name":"res","mountPath":"/results"}]}],"volumes":[{"name":"res","persistentVolumeClaim":{"claimName":"k6-results"}}]}}' >/dev/null
  k wait --for=condition=Ready "pod/$pod" --timeout=120s >/dev/null
  k cp "$pod:/results" "$dest" >/dev/null
  k delete pod "$pod" --wait=false >/dev/null
  log "Resultados de k6 en k6/results/:"
  ls -1 "$dest"
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
  local file="manual-$(date +%Y%m%d-%H%M%S).json"
  log "k6: ${secs}s de tráfico idéntico a ACTIVE y PREVIEW -> k6/results/$file"
  docker run --rm -i --user "$(id -u):$(id -g)" --add-host=host.docker.internal:host-gateway \
    -e DURATION="${secs}s" \
    -e ACTIVE_URL=http://host.docker.internal:8080 \
    -e PREVIEW_URL=http://host.docker.internal:8081 \
    -v "$HERE/k6:/k6" \
    grafana/k6:0.57.0 run --quiet --out "json=/k6/results/$file" /k6/load.js
}

case "${1:-}" in
  up)       shift; cmd_up "$@" ;;
  deploy)   shift; cmd_deploy "$@" ;;
  promote)  cmd_promote ;;
  abort)    ro abort petclinic; record abort ok; log "PREVIEW descartada; el tráfico sigue en la versión activa" ;;
  rollback) cmd_rollback ;;
  status)   ro get rollout petclinic; echo "ACTIVE  $ACTIVE_URL  -> $(version_at $ACTIVE_URL)"; echo "PREVIEW $PREVIEW_URL -> $(version_at $PREVIEW_URL)" ;;
  history)  [ -f "$RECORDS" ] && cat "$RECORDS" || echo "Sin registros aún" ;;
  results)  cmd_results ;;
  traffic)  cmd_traffic ;;
  load)     shift; cmd_load "$@" ;;
  down)     kind delete cluster --name "$CLUSTER"; docker rm -f "$REG_NAME" >/dev/null 2>&1 || true ;;
  *)        sed -n '2,17p' "$0" ;;
esac
