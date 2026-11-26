# Observabilidad de Spring PetClinic (Prometheus, Grafana, Loki, Alloy)

Corresponde a la **Fase 3** de la propuesta (Cap. IV, Tabla 7): *"Dashboard que muestra blue y
green lado a lado"*. Rol: Observability Engineer. Funciona sobre el clúster de `k8s/bluegreen/`.

## Idea en una frase

Cada pod de PetClinic publica sus métricas y logs con la **versión** a la que pertenece. Así,
durante un despliegue blue-green, Grafana muestra la versión activa y la preview lado a lado y
el analizador puede compararlas.

```
pods petclinic (version=v1 | v2) ──/actuator/prometheus──► Prometheus ──► Grafana (dashboard)
            │                                                   │
            └── logs JSON (ECS) ──► Alloy ──► Loki ─────────────┘
                                                   Prometheus ──alertas──► Alertmanager ──► alert-sink
```

## Componentes

| Pieza | Archivo | Para qué |
|---|---|---|
| Instrumentación de la app | `pom.xml`, `build.gradle`, `application.properties` | `micrometer-registry-prometheus` e histogramas de latencia para p95/p99 por ruta |
| Logs en JSON | `k8s/bluegreen/manifests/40-rollout.yaml` | `LOGGING_STRUCTURED_FORMAT_CONSOLE=ecs`. Solo en el clúster: en local los logs siguen legibles |
| Prometheus + Alertmanager + Grafana | `values/kube-prometheus-stack.yaml` | Chart `kube-prometheus-stack`, retención de 2 días, sin reglas por defecto |
| Loki | `values/loki.yaml` | Modo SingleBinary, disco local, retención de 48 h |
| Grafana Alloy | `values/alloy.yaml` | Envía a Loki los logs de PetClinic y los eventos del Rollout |
| PodMonitor | `podmonitor.yaml` | Prometheus lee cada pod y conserva `version` y `rollouts_pod_template_hash` |
| Receptor de alertas | `alert-sink.yaml` | Webhook de prueba: escribe cada notificación en su log (y llega a Loki) |
| Dashboard | `dashboards/petclinic-bluegreen.json` | "PetClinic Blue vs Green" |
| Script | `obs.sh` | Todos los comandos |

## Uso

Requiere el clúster levantado (`k8s/bluegreen/bg.sh up`) y `helm` (`brew install helm`).

| Comando | Qué hace |
|---|---|
| `k8s/observability/obs.sh up` | Instala todo en el namespace `monitoring` (unos 3-5 min la primera vez) |
| `k8s/observability/obs.sh grafana` | Grafana en http://localhost:3000. Los dashboards se ven sin login (solo lectura); para editar: admin / admin |
| `k8s/observability/obs.sh prometheus` | Prometheus en http://localhost:9090 |
| `k8s/observability/obs.sh alertmanager` | Alertmanager en http://localhost:9093 |
| `k8s/observability/obs.sh status` | Pods de monitoring y pods de PetClinic que Prometheus está leyendo |
| `k8s/observability/obs.sh dashboards` | Recarga los JSON de `dashboards/` después de editarlos |
| `k8s/observability/obs.sh down` | Desinstala todo |

**Recursos:** con Docker Desktop, darle al menos 10-12 GB de memoria (Settings → Resources).
Durante un despliegue conviven 4 pods de PetClinic más el stack de monitoreo.

## Etiquetas que identifican cada versión

| Dónde | Etiquetas |
|---|---|
| Métricas (Prometheus) | `namespace`, `pod`, `version`, `rollouts_pod_template_hash`, `application="petclinic"` |
| Logs (Loki) | `namespace`, `pod`, `app`, `version`, `rollouts_pod_template_hash`, `level` |
| Eventos (Loki) | `job="loki.source.kubernetes_events"`, `namespace="petclinic"` |

`version` es la que pasa `bg.sh deploy <versión>` y `rollouts_pod_template_hash` coincide con
`stable-hash` / `preview-hash` del análisis y con `records/deployments.jsonl`.

## Métricas que usa el dashboard (apartado 4.5)

| Señal | Métrica |
|---|---|
| Solicitudes y errores por ruta | `http_server_requests_seconds_count` (`uri`, `status`, `outcome`) |
| Latencia p95 / p99 | `http_server_requests_seconds_bucket` |
| Memoria JVM | `jvm_memory_used_bytes{area="heap"}` |
| Pausas de GC | `jvm_gc_pause_seconds_*` |
| Pool de conexiones | `hikaricp_connections_active`, `hikaricp_connections_pending` |
| CPU | `process_cpu_usage` |

Son las mismas señales de la referencia de Isolation Forest del analizador (`vets_p95_ms`,
`process_cpu_ratio`, `heap_mib`, `gc_pause_seconds`, `pool_active_mean`).

## Dashboard "PetClinic Blue vs Green"

Variables `Versión` y `Ruta`. Paneles, todos separados por versión:

1. Solicitudes por segundo.
2. % de errores 5xx (sin `/oups`, que es un error provocado).
3. Latencia p95 y p99 por ruta.
4. Heap usado, pausas de GC y pool de conexiones (activas y en espera).
5. CPU del proceso.
6. Puntaje del analizador: lee la métrica `aiops_analyzer_score{version}`. Queda en
   "Pendiente" hasta que el analizador la publique.
7. Logs de error de Loki por versión.
8. Anotaciones con los eventos del Rollout (despliegue, pausa, promoción, aborto).

## Alertmanager

- `group_by: [alertname, version]`: una sola causa no genera un aviso por cada pod.
- Inhibición: `PetclinicDown` silencia `PetclinicHighLatency` y `PetclinicHighErrorRate` de la
  misma versión; `PostgresDown` las silencia en las dos versiones.
- Receptor: `alert-sink` (webhook dentro del clúster). Para contar las notificaciones de una
  corrida en Grafana → Explore → Loki: `{app="alert-sink"} |= "notification"`.
- Las reglas de alerta (umbrales) las agrega la tarea 3 con un `PrometheusRule` en cualquier
  namespace: Prometheus está configurado para tomarlos todos.
