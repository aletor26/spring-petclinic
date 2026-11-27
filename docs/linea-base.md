# Línea base: escenario "antes" (fase 2 y R2)

Roles: SRE + Observability Engineer. **Criterio de cierre (Tabla 7):** *indicadores base registrados.*

El "antes" no es la revisión manual sola. Según R2 es la **alternativa A bien configurada**:
umbrales fijos más Alertmanager con agrupación, deduplicación e inhibición. Así la mejora
posterior se puede atribuir a la IA y no a una línea base débil.

| Pieza | Archivo |
|---|---|
| Reglas de alerta (`PrometheusRule`) | `k8s/observability/rules/petclinic-rules.yaml` |
| Alertmanager (agrupación, inhibición, receptor) | `k8s/observability/values/kube-prometheus-stack.yaml` |
| Registro de corridas, calibración e indicadores | `k8s/observability/baseline/linea_base.py` |
| Corridas de calibración (no cuentan para la línea base) | `docs/calibracion.csv` |
| Corridas de la línea base | `docs/linea-base.csv` |
| Tabla de indicadores | `docs/linea-base-indicadores.md` (la genera el script) |

## 1. SLO

| SLO | Objetivo | Cómo se mide (regla de grabación) |
|---|---|---|
| Disponibilidad (respuestas no 5xx, sin `/oups` ni `/actuator`) | ≥ 99 % | `petclinic:availability:ratio_rate10m` |
| Latencia p95 por ruta | ≤ p95 sano × 1,5 | `petclinic:latency_p95_seconds:rate2m` frente a `petclinic:slo_latency_p95_threshold_seconds` |
| Registro de visitas exitoso | ≥ 99,5 % | `petclinic:visit_success:ratio_rate10m` |

Todas están separadas por `version`, así que se ve blue y green por separado.

Los umbrales de latencia del repositorio son **provisionales (0,5 s)** hasta calibrarlos (paso 3.1).
Tabla de umbrales calibrados (completar con la salida de `linea_base.py calibrar`):

| Ruta | p95 sano (ms) | Umbral (ms) |
|---|---|---|
| `/` | | |
| `/vets.html` | | |
| `/owners` | | |
| `/owners/find` | | |
| `/owners/{ownerId}` | | |
| `/owners/{ownerId}/pets/{petId}/visits/new` | | |

## 2. Reglas de alerta (umbrales fijos)

| Alerta | Condición | Para | Fuente |
|---|---|---|---|
| `PetclinicHighErrorRate` | 5xx > 1 % por versión (sin `/oups` ni `/actuator`), con > 0,1 req/s | 2 min | Micrometer |
| `PetclinicHighLatency` | p95 de una ruta > su umbral, con tráfico en esa ruta | 2 min | Micrometer |
| `PetclinicPoolSaturated` | `hikaricp_connections_pending` > 0 | 1 min | Micrometer |
| `PetclinicHeapHigh` | heap usado > 85 % del máximo | 5 min | Micrometer |
| `PetclinicDown` | `up == 0` para un pod con más de 3 min de vida | 1 min | Prometheus |
| `PostgresDown` | `petclinic-db` sin réplicas listas | 1 min | kube-state-metrics |
| `PetclinicPodRestarts` | reinicios > 0 en 10 min | — | kube-state-metrics |

Todas llevan la etiqueta `version`. `PostgresDown` usa `version="todas"` porque la base es
compartida. Decisiones de diseño:

- **`PetclinicDown` ignora los primeros 3 minutos de vida del pod.** Al desplegar una versión, su
  endpoint de métricas tarda en responder y eso no es una falla. Si el pod nunca arranca, la
  alerta salta igual al pasar ese tiempo.
- **`PostgresDown` no usa una sonda de conexión.** No hay un exporter de PostgreSQL. Se usa
  `kube_statefulset_status_replicas_ready`, que es exactamente lo que cambia en el escenario
  operativo (`scale --replicas=0`). Tampoco se usa `absent()`, porque un reinicio de
  kube-state-metrics daría un falso aviso.
- **`PetclinicPodRestarts` necesita la versión del pod.** Para eso, kube-state-metrics publica
  las etiquetas `app` y `version` del pod (`metricLabelsAllowlist` en los valores de Helm).
- **Inhibición** (sección 1.4 del documento de tareas; configurada en `values/kube-prometheus-stack.yaml`): `PetclinicDown` silencia la latencia y los errores de su misma
  versión. `PostgresDown` los silencia en las dos y además silencia `PetclinicPoolSaturated`,
  porque con la base caída los hilos se quedan esperando una conexión: es la misma causa.

Comprobar en Prometheus (`obs.sh prometheus` → *Alerts*) que el grupo `petclinic-alertas`
aparece con las 7 reglas.

**Pruebas sin clúster.** `k8s/observability/rules/tests/run.sh` corre `promtool check rules`
(sintaxis y lint) y `promtool test rules` con series sintéticas. Cada alerta tiene una prueba que
comprueba que salta cuando debe y que no salta en los casos que deben ignorarse: errores en
`/oups`, sondas `/actuator`, pod recién creado, rutas sin umbral y áreas del heap sin máximo. El
workflow `alert-rules.yml` lo corre en cada cambio de las reglas.

## 3. Protocolo de medición

### 3.0 Preparación (una vez)

```bash
k8s/bluegreen/bg.sh up
k8s/observability/obs.sh up      # instala el stack y aplica las reglas
k8s/observability/obs.sh status  # los pods de petclinic con up=1
```

Si el stack ya estaba instalado, vuelve a correr `obs.sh up`: actualiza kube-state-metrics con
la nueva etiqueta y aplica las reglas.

### 3.1 Calibración (3 corridas sanas)

Las corridas de calibración se hacen con los umbrales provisionales, así que **van a otro CSV** y
no cuentan para los indicadores.

```bash
k8s/bluegreen/bg.sh deploy cal-1 --window 10m
k8s/bluegreen/bg.sh promote
python3 k8s/observability/baseline/linea_base.py registrar --corrida 1 --csv docs/calibracion.csv
# repetir con cal-2 y cal-3
python3 k8s/observability/baseline/linea_base.py calibrar --dry-run   # revisar los valores
python3 k8s/observability/baseline/linea_base.py calibrar             # escribe los umbrales
k8s/observability/obs.sh rules
```

Hay que calibrar antes de 2 días desde las corridas, porque esa es la retención de Prometheus.
Después copia la tabla que imprime el script en la sección 1.

### 3.2 Corridas de la línea base

Para cada escenario del catálogo (sección 2 de las tareas) se hacen **3 corridas**. Además, **5
corridas de una versión sana** para medir los falsos positivos.

| Escenario | `--fault` en `registrar` | Cómo se provoca |
|---|---|---|
| Versión sana | `ninguna` | `bg.sh deploy sana-N` |
| `latency` | `latency` | `bg.sh deploy latency-N --fault latency` |
| `slow-query` | `slow-query` | `bg.sh deploy slowq-N --fault slow-query` |
| `visit-write` | `visit-write` | `bg.sh deploy visit-N --fault visit-write` |
| `heap-leak` | `heap-leak` | `bg.sh deploy heap-N --fault heap-leak` |
| Caída de PostgreSQL | `postgres-down` | versión sana; en el minuto 3 de la ventana, `kubectl -n petclinic scale statefulset petclinic-db --replicas=0` y 3 min después `--replicas=1` |
| `/oups` (control de falso positivo) | `oups` | versión sana; durante la ventana, `while true; do curl -s -o /dev/null localhost:8081/oups; sleep 1; done` |

`--fault` en `bg.sh` llega con la rama `feature/fallas-controladas`. `ninguna` y `oups` cuentan
como versiones sanas; el resto, como defectuosas.

Cada corrida:

1. En otra terminal, `k8s/bluegreen/bg.sh traffic`.
2. `k8s/bluegreen/bg.sh deploy <versión> --window 10m [--fault <valor>]`. Usa un nombre de
   versión distinto en cada corrida, porque las métricas se separan por `version`.
3. Una persona mira Grafana (dashboard *PetClinic Blue vs Green*) y las alertas
   (`obs.sh alertmanager`) y decide con `bg.sh promote` o `bg.sh abort`. Ese es el escenario sin
   IA. Si el análisis de Argo falla solo, el script lo registra como `abort` con
   `decidido_por=analisis-automatico`.
4. Registrar la corrida:

   ```bash
   python3 k8s/observability/baseline/linea_base.py registrar --corrida <n> --fault <valor> [--notas "..."]
   ```

5. Si se promovió una versión defectuosa, volver a la sana antes de la siguiente corrida:
   `bg.sh rollback`.

### Columnas de `docs/linea-base.csv`

| Columna | Cómo la obtiene el script |
|---|---|
| `corrida` | `--corrida` |
| `version` / `fault` | la versión del último `deploy` en `records/deployments.jsonl`; `fault` sale del registro o de `--fault` |
| `inicio_trafico_preview` | creación del AnalysisRun, que es cuando empieza el análisis hacia preview. El registro `deploy` de `bg.sh` se escribe al **terminar** el análisis, por eso no sirve como inicio |
| `primera_alerta` | primera notificación `firing` que recibió `alert-sink` con una alerta de esa versión (o `version="todas"`) que **empezó durante la corrida** (`startsAt` ≥ inicio), entre el inicio y la decisión. Así no cuentan los reavisos de alertas de una corrida anterior |
| `decision` / `hora_decision` | el `promote` o `abort` siguiente en `deployments.jsonl` |
| `detectada` | solo en defectuosas: `si` si se abortó |
| `falso_positivo` | solo en sanas: `si` si se abortó |
| `alertas_recibidas` | notificaciones `firing` que llegaron a `alert-sink` (todas las versiones) entre el inicio y la decisión |
| `decidido_por` | `persona` o `analisis-automatico` |
| `analysis_run` | para no registrar dos veces la misma corrida |

El script lee las notificaciones con `kubectl logs --timestamps deploy/alert-sink`. Si el
contenedor de `alert-sink` se reinició durante la corrida, sus logs ya no tienen todas las
notificaciones: el script **se niega a registrar** y pide contarlas en Loki
(`{app="alert-sink"} |= "notification"`). Con `--forzar` registra igual y lo deja anotado en `notas`.

## 4. Indicadores (Tabla 6)

```bash
python3 k8s/observability/baseline/linea_base.py indicadores --salida docs/linea-base-indicadores.md
```

| Indicador | Fórmula |
|---|---|
| Tasa de detección | versiones defectuosas abortadas / versiones defectuosas |
| Tasa de falsos positivos | versiones sanas abortadas / versiones sanas |
| MTTD | promedio de (`primera_alerta` − `inicio_trafico_preview`), en las defectuosas con alerta |
| Tiempo hasta la decisión | promedio de (`hora_decision` − `inicio_trafico_preview`) |
| Volumen de alertas por despliegue | promedio de `alertas_recibidas` |

El script también separa los resultados por escenario. Esa tabla va en el Cap. 4 del informe
(escenario "antes") y se entrega al equipo de IA.
