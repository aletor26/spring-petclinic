#!/usr/bin/env bash
# Observabilidad de PetClinic en el clúster kind (fase 3 de la propuesta).
# Requiere el clúster de k8s/bluegreen (bg.sh up).
#
#   ./obs.sh up            Instala Prometheus, Alertmanager, Grafana, Loki y Alloy en el namespace monitoring
#   ./obs.sh down          Desinstala todo y borra el namespace monitoring
#   ./obs.sh status        Pods de monitoring y objetivos de Prometheus
#   ./obs.sh dashboards    Vuelve a cargar los dashboards de dashboards/*.json
#   ./obs.sh grafana       Abre Grafana en http://localhost:3000 (admin / admin)
#   ./obs.sh prometheus    Abre Prometheus en http://localhost:9090
#   ./obs.sh alertmanager  Abre Alertmanager en http://localhost:9093
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CLUSTER=petclinic
NS=monitoring
KPS_VERSION=93.10.1     # prometheus-community/kube-prometheus-stack
LOKI_VERSION=7.6.0      # grafana/loki
ALLOY_VERSION=1.15.0    # grafana/alloy

log()  { printf '\033[1;35m[obs]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[obs] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
kc()   { kubectl --context "kind-$CLUSTER" "$@"; }
k()    { kc -n "$NS" "$@"; }
hl()   { helm --kube-context "kind-$CLUSTER" -n "$NS" "$@"; }

need() { command -v "$1" >/dev/null || fail "Falta '$1'. Instalar con: brew install $2"; }

check() {
  need kubectl kubectl
  need helm helm
  kc get ns petclinic >/dev/null 2>&1 || fail "No encuentro el clúster kind-$CLUSTER. Primero: k8s/bluegreen/bg.sh up"
}

load_dashboards() {
  local f name
  for f in "$HERE"/dashboards/*.json; do
    name="dashboard-$(basename "$f" .json)"
    k create configmap "$name" --from-file="$(basename "$f")=$f" --dry-run=client -o yaml \
      | kc label --local -f - grafana_dashboard=1 -o yaml \
      | kc apply -f - >/dev/null
    log "Dashboard cargado: $(basename "$f")"
  done
}

up() {
  check
  log "Repositorios de Helm"
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update prometheus-community grafana >/dev/null

  log "Prometheus + Alertmanager + Grafana (kube-prometheus-stack $KPS_VERSION)"
  hl upgrade --install kps prometheus-community/kube-prometheus-stack --version "$KPS_VERSION" \
    --create-namespace -f "$HERE/values/kube-prometheus-stack.yaml" --wait --timeout 10m

  log "Loki $LOKI_VERSION (SingleBinary)"
  hl upgrade --install loki grafana/loki --version "$LOKI_VERSION" \
    -f "$HERE/values/loki.yaml" --wait --timeout 10m

  log "Grafana Alloy $ALLOY_VERSION"
  hl upgrade --install alloy grafana/alloy --version "$ALLOY_VERSION" \
    -f "$HERE/values/alloy.yaml" --wait --timeout 5m

  log "PodMonitor de PetClinic y receptor de alertas"
  kc apply -f "$HERE/podmonitor.yaml" -f "$HERE/alert-sink.yaml" >/dev/null
  k rollout status deploy/alert-sink --timeout=120s >/dev/null

  load_dashboards
  log "Listo. Grafana: k8s/observability/obs.sh grafana  →  http://localhost:3000 (admin / admin)"
}

down() {
  check
  hl uninstall alloy loki kps 2>/dev/null || true
  kc delete ns "$NS" --ignore-not-found
  log "Observabilidad eliminada"
}

status() {
  check
  k get pods
  log "Pods de PetClinic que Prometheus está leyendo (up=1 es OK):"
  # Consulta a Prometheus a través del proxy de la API de Kubernetes (sin port-forward)
  kc get --raw "/api/v1/namespaces/$NS/services/kps-kube-prometheus-stack-prometheus:9090/proxy/api/v1/query?query=up%7Bnamespace%3D%22petclinic%22%7D" 2>/dev/null \
    | python3 -c 'import json,sys
for r in json.load(sys.stdin)["data"]["result"]:
    m = r["metric"]; print("  %s  version=%s  up=%s" % (m.get("pod"), m.get("version"), r["value"][1]))' \
    || log "Prometheus todavía no responde"
}

forward() {  # forward <service> <puerto local> <puerto remoto> <nombre>
  check
  log "$4 en http://localhost:$2  (Ctrl+C para cerrar)"
  k port-forward "svc/$1" "$2:$3"
}

case "${1:-}" in
  up)           up ;;
  down)         down ;;
  status)       status ;;
  dashboards)   check; load_dashboards ;;
  grafana)      forward kps-grafana 3000 80 Grafana ;;
  prometheus)   forward kps-kube-prometheus-stack-prometheus 9090 9090 Prometheus ;;
  alertmanager) forward kps-kube-prometheus-stack-alertmanager 9093 9093 Alertmanager ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
