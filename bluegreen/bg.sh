#!/usr/bin/env bash
# Blue-Green para Spring PetClinic con Docker Compose.
#
#   ./bg.sh up              Primer arranque: construye v1, levanta db + blue + proxy
#   ./bg.sh deploy <ver>    Despliega <ver> en el color inactivo, prueba y cambia el tráfico
#   ./bg.sh switch <color>  Cambia el tráfico a blue o green (sin reconstruir)
#   ./bg.sh rollback        Vuelve al color anterior (sigue corriendo)
#   ./bg.sh status          Muestra color activo, versiones y salud
#   ./bg.sh traffic         Envía una petición cada 0.5 s y muestra quién responde
#   ./bg.sh down            Apaga todo (agregar -v para borrar la base de datos)
set -euo pipefail

cd "$(dirname "$0")"
REPO_DIR=".."
PROXY_URL="http://localhost:8080"

# Estado local (no va al repo): color activo y versión de cada color
mkdir -p nginx/active
[ -f .env ] || printf 'BLUE_VERSION=v1\nGREEN_VERSION=v1\n' > .env
[ -f nginx/active/upstream.conf ] || echo 'server app-blue:8080;' > nginx/active/upstream.conf
[ -f nginx/active/color.conf ] || echo 'add_header X-Active-Color blue always;' > nginx/active/color.conf

port_of()   { [ "$1" = blue ] && echo 8081 || echo 8082; }
other_of()  { [ "$1" = blue ] && echo green || echo blue; }
active()    { sed -E 's/.*X-Active-Color ([a-z]+).*/\1/' nginx/active/color.conf; }
log()       { printf '\033[1;36m[bg]\033[0m %s\n' "$*"; }
fail()      { printf '\033[1;31m[bg] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

set_version() {  # set_version <color> <ver>  -> guarda la versión en .env
  local key; key="$(echo "$1" | tr a-z A-Z)_VERSION"
  sed -i.bak -E "s/^${key}=.*/${key}=$2/" .env && rm -f .env.bak
}

build() {  # build <ver> <color>
  log "Construyendo imagen petclinic:$1 desde el repositorio..."
  docker build -t "petclinic:$1" "$REPO_DIR"
  docker tag "petclinic:$1" "petclinic:$2"
}

wait_healthy() {  # wait_healthy <color>
  local c="petclinic-$1" s i
  log "Esperando que $c esté healthy..."
  for i in $(seq 1 60); do
    s=$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)
    [ "$s" = healthy ] && { log "$c healthy"; return 0; }
    [ "$s" = unhealthy ] && break
    sleep 3
  done
  docker logs --tail 30 "$c" || true
  return 1
}

smoke_test() {  # smoke_test <color>  -> pruebas funcionales antes de recibir tráfico
  local base="http://localhost:$(port_of "$1")" path code
  log "Smoke tests contra $1 ($base)"
  for path in /actuator/health / /vets.html "/owners?lastName=" /owners/1 /owners/find; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$base$path")
    printf '   %-22s %s\n' "$path" "$code"
    [ "$code" = 200 ] || return 1
  done
}

switch_to() {  # switch_to <color>
  local target="$1"
  log "Cambiando tráfico del proxy -> $target"
  echo "server app-$target:8080;" > nginx/active/upstream.conf
  echo "add_header X-Active-Color $target always;" > nginx/active/color.conf
  docker exec petclinic-proxy nginx -t -q
  docker exec petclinic-proxy nginx -s reload   # recarga en caliente, sin cortar conexiones
  sleep 1
  local seen; seen=$(curl -sI "$PROXY_URL/" | awk -F': ' 'tolower($1)=="x-active-color"{print $2}' | tr -d '\r')
  [ "$seen" = "$target" ] || fail "El proxy responde '$seen', se esperaba '$target'"
  log "Tráfico ahora en $target ✅"
}

cmd_up() {
  build v1 blue
  docker tag petclinic:v1 petclinic:green   # green existe pero no se levanta aún
  set_version blue v1
  echo 'server app-blue:8080;' > nginx/active/upstream.conf
  echo 'add_header X-Active-Color blue always;' > nginx/active/color.conf
  docker compose up -d db app-blue proxy
  wait_healthy blue || fail "blue no arrancó"
  log "Listo: $PROXY_URL (activo: blue v1)"
}

cmd_deploy() {
  local ver="${1:?Uso: ./bg.sh deploy <version>}" cur target
  cur=$(active); target=$(other_of "$cur")
  log "Activo: $cur  ->  nuevo despliegue $ver en $target"
  build "$ver" "$target"
  set_version "$target" "$ver"
  docker compose up -d --no-deps --force-recreate "app-$target"
  if ! wait_healthy "$target" || ! smoke_test "$target"; then
    fail "La versión $ver falló en $target. El tráfico SIGUE en $cur (sin impacto al usuario)."
  fi
  switch_to "$target"
  log "$cur se mantiene encendido para rollback inmediato: ./bg.sh rollback"
}

cmd_rollback() {
  local cur prev; cur=$(active); prev=$(other_of "$cur")
  docker inspect -f '{{.State.Health.Status}}' "petclinic-$prev" 2>/dev/null | grep -q healthy \
    || fail "$prev no está healthy, no se puede hacer rollback"
  log "Rollback: $cur -> $prev"
  switch_to "$prev"
}

cmd_status() {
  local cur; cur=$(active)
  echo "Color activo (proxy :8080): $cur"
  for c in blue green; do
    local h info
    h=$(docker inspect -f "{{.State.Health.Status}}" "petclinic-$c" 2>/dev/null) || h=apagado
    info=$(curl -s "http://localhost:$(port_of $c)/actuator/info" 2>/dev/null | grep -o '"app":{[^}]*}' || true)
    printf '  %-6s :%s  %-10s %s\n' "$c" "$(port_of $c)" "$h" "$info"
  done
}

cmd_traffic() {
  log "Ctrl+C para detener"
  local hdr code color
  while true; do
    hdr=$(curl -s -m 2 -o /dev/null -D - "$PROXY_URL/vets.html" || true)
    code=$(echo "$hdr" | head -1 | awk '{print $2}')
    color=$(echo "$hdr" | awk -F': ' 'tolower($1)=="x-active-color"{print $2}' | tr -d '\r')
    printf '%s  HTTP %s  color=%s\n' "$(date +%T)" "${code:-000}" "${color:--}"
    sleep 0.5
  done
}

case "${1:-}" in
  up)       cmd_up ;;
  deploy)   shift; cmd_deploy "$@" ;;
  switch)   [ "${2:-}" = blue ] || [ "${2:-}" = green ] || fail "Uso: ./bg.sh switch blue|green"; switch_to "$2" ;;
  rollback) cmd_rollback ;;
  status)   cmd_status ;;
  traffic)  cmd_traffic ;;
  down)     shift; docker compose down "$@" ;;
  *)        sed -n '2,11p' "$0" ;;
esac
