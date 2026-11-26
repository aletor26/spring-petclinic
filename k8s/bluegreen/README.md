# Despliegue Blue-Green de Spring PetClinic en Kubernetes (kind + Argo Rollouts)

Corresponde a la **Fase 4** de la propuesta (Cap. IV, Tabla 7): *"Despliegue y
rollback manual funcionando"*, y a la recomendación **R1** (Cap. V). Rol: DevOps Engineer.

## Idea en una frase

Hay dos versiones de la app vivas a la vez: la **activa** (la usan los usuarios) y la
**preview** (la nueva, en prueba). Solo cuando la nueva pasa el análisis se le pasa
el tráfico. Si algo sale mal, se vuelve a la anterior al instante.

```
                      ┌────────────────────── Clúster kind (dentro de Docker) ──────────────────────┐
usuarios ─► :8080 ─►  │ Service petclinic-active  ─► 2 pods versión estable ─┐                      │
probador ─► :8081 ─►  │ Service petclinic-preview ─► 2 pods versión nueva  ──┼─► PostgreSQL (PVC)   │
                      │        ▲ Argo Rollouts decide a qué pods apunta cada Service                │
                      │        └ Análisis previo: smoke tests + k6 (mismo tráfico a ambas) ─► PVC k6 │
                      └──────────────────────────────────────────────────────────────────────────────┘
   registro local localhost:5001  ─► imágenes fijadas por digest (sha256)
```

## Componentes

| Pieza | Archivo | Para qué |
|---|---|---|
| Clúster kind | `kind-cluster.yaml` | Kubernetes dentro de Docker. Expone 8080 (active) y 8081 (preview) |
| Registro local | `bg.sh` (contenedor `kind-registry`) | Guarda las imágenes para fijarlas por **digest** |
| PostgreSQL + PVC | `manifests/10-postgres.yaml` | Base compartida por ambas versiones. El PVC guarda los datos en un disco que no se borra |
| Inicialización de la BD | `manifests/15-db-init.yaml` | Job que crea el esquema y los datos de ejemplo **una sola vez** |
| Services | `manifests/20-services.yaml` | `petclinic-active` y `petclinic-preview` |
| Análisis previo | `manifests/30-analysis-template.yaml` | Smoke tests + k6 en la ventana. **Aquí se conecta el analizador AIOps** |
| Rollout | `manifests/40-rollout.yaml` | Blue-green, 2 réplicas, promoción manual, versión anterior viva 10 min |
| Resultados k6 | `manifests/50-k6.yaml` | PVC donde k6 guarda cada solicitud |
| Script | `bg.sh` | Todos los comandos |
| Carga | `k6/load.js` | Mismo tráfico a active y preview, con flujo funcional (registrar visita) |
| Pipeline | `.github/workflows/bluegreen-k8s.yml` | Todo el flujo automático en GitHub Actions |

## Requisitos (Mac)

Docker (OrbStack o Docker Desktop) y:

```bash
brew install kind kubectl argoproj/tap/kubectl-argo-rollouts
```

Puertos libres: 8080, 8081 y 5001.

## Uso

Desde la raíz del repo:

| Comando | Qué hace |
|---|---|
| `k8s/bluegreen/bg.sh up` | Registro + clúster + Argo Rollouts + primera versión |
| `k8s/bluegreen/bg.sh deploy [versión] [--window 10m]` | Construye el código actual, lo levanta en PREVIEW y corre el análisis durante la ventana. Versión por defecto: commit actual |
| `k8s/bluegreen/bg.sh promote` | Pasa a los usuarios a la versión PREVIEW |
| `k8s/bluegreen/bg.sh abort` | Descarta la PREVIEW. Los usuarios no se enteran |
| `k8s/bluegreen/bg.sh rollback` | Vuelve a la versión anterior después de promover |
| `k8s/bluegreen/bg.sh status` | Árbol del Rollout: versión activa, preview, análisis |
| `k8s/bluegreen/bg.sh history` | Registro de cada despliegue (trazabilidad) |
| `k8s/bluegreen/bg.sh results` | Copia los JSON de k6 del clúster a `k6/results/` |
| `k8s/bluegreen/bg.sh traffic` | Una petición cada 0.5 s: código HTTP y versión que respondió |
| `k8s/bluegreen/bg.sh load [seg]` | k6 manual desde la máquina contra active y preview |
| `k8s/bluegreen/bg.sh down` | Borra el clúster y el registro |

Panel visual de Argo (opcional): `kubectl argo rollouts dashboard` → http://localhost:3100

## Flujo de un despliegue

1. `deploy` construye la imagen, la sube al registro local y la referencia por **digest**
   (`localhost:5001/petclinic@sha256:...`). El commit y la imagen quedan como anotaciones del pod.
2. Argo crea 2 pods nuevos y los conecta a **preview**. Los usuarios siguen en active.
3. **Análisis previo** (`prePromotionAnalysis`), dos verificaciones en paralelo:
   - **smoke-tests**: `/actuator/health`, `/`, `/vets.html`, `/owners?lastName=`, `/owners/1`, `/owners/find` deben dar 200.
   - **k6-window**: durante la ventana (10 min por defecto, como dice la propuesta) k6 envía
     5 solicitudes/s **a cada versión**. El 10 % registra una visita y comprueba que aparece
     en la ficha del dueño. Falla si preview supera 1 % de errores o de checks fallidos.
     Cada solicitud se guarda en el PVC `k6-results`.
   - Si una falla → Argo **aborta** y detiene la otra. Active no cambia.
   - Si pasan → el Rollout queda en pausa esperando `promote`.
4. `promote` → Argo cambia el Service active a los pods nuevos (sin corte).
5. La versión anterior queda viva 10 min: `rollback` es inmediato.
6. Cada acción (`up`, `deploy`, `promote`, `abort`, `rollback`) se guarda en
   `records/deployments.jsonl`.

## Trazabilidad (Cap. IV, 4.8)

Cada línea de `records/deployments.jsonl` contiene: fecha, acción, resultado, versión, commit,
imagen por digest, hash de la versión estable y de la preview, versión activa, ventana,
AnalysisRun y su resultado, y escenario de k6. Los JSON de k6 se nombran
`<fecha>_<versión>_preview-<hash>_stable-<hash>.json.gz`, así se sabe qué versiones se compararon.

## Cambios de ingeniería base (R1)

| R1 pide | Hecho |
|---|---|
| Fijar la imagen por digest y vincularla al commit | Registro local + `@sha256:...` + anotación `petclinic/commit` |
| Almacenamiento persistente para PostgreSQL | StatefulSet con PVC |
| Evaluar más de una réplica | 2 réplicas por versión |
| Restringir Actuator | Solo `health`, `info`, `prometheus` (`/actuator/env` y `/actuator/beans` dan 404) |

**Hallazgo:** al pasar a 2 réplicas, ambas ejecutaban los scripts de datos de ejemplo al
arrancar y chocaban (`duplicate key value violates unique constraint "unique_owner_pet_name"`).
La réplica afectada quedaba en reinicio permanente. Solución: la base se inicializa una sola
vez con el Job `petclinic-db-init` y la app arranca con `SPRING_SQL_INIT_MODE=never`.
Separar la inicialización de la base del despliegue de la app es lo que recomienda 4.8.

## Dónde entra la IA (otro equipo)

En el paso 3. El equipo del analizador agrega una tercera métrica `web` al
`AnalysisTemplate` que consulta su API y aprueba si el puntaje es ≥ 75 (ejemplo comentado en
`30-analysis-template.yaml`). Lo que este despliegue ya les entrega:

- Args del análisis: `stable-hash`, `preview-hash`, `preview-version`, `window`.
- Etiqueta `version` y `rollouts-pod-template-hash` en cada pod para separar las versiones.
- PVC `k6-results` con cada solicitud (`target`, `version`, `route`, `status`, latencia),
  para la prueba de Mann-Whitney.
- `/actuator/info` con la versión desplegada.

- `/actuator/prometheus` con métricas por versión (Prometheus, Grafana y Loki en
  `k8s/observability/`).

## Pruebas realizadas (24/11/2026, Mac con OrbStack)

| Prueba | Resultado |
|---|---|
| `up` desde cero con 2 réplicas | OK, 0 reinicios |
| `deploy v2 --window 2m` | smoke-tests ✔ y k6-window ✔: 1327 solicitudes, 0 % errores, 1202/1202 checks (incluye registro de visitas) |
| Tráfico continuo durante deploy, promote, rollback y versión rota | 340 peticiones, **0 errores** |
| Versión con error en `/vets.html` (`v3-roto`) | smoke-tests ✖ → aborto en **17 s** sin esperar la ventana; usuarios siguieron en v1 |
| Actuator | `health` 200, `info` 200, `env` 404, `beans` 404 |

## Demo para el video

Terminal 1: `k8s/bluegreen/bg.sh traffic`

Terminal 2:
```bash
k8s/bluegreen/bg.sh up
k8s/bluegreen/bg.sh deploy v2 --window 2m
k8s/bluegreen/bg.sh status
k8s/bluegreen/bg.sh promote
k8s/bluegreen/bg.sh rollback
k8s/bluegreen/bg.sh history
```

Versión defectuosa: agregar `if (true) throw new RuntimeException("bug");` al inicio de
`VetController.showVetList`, correr `k8s/bluegreen/bg.sh deploy v3-roto` (el análisis la
rechaza) y deshacer con `git checkout -- src`.

## Diferencias con la propuesta

- Clúster local con kind en lugar de un clúster real: mismo Kubernetes, sin VM ni costo.
- Registro de imágenes local (`localhost:5001`) en lugar de uno público.
- El análisis usa smoke tests y límites básicos de k6 hasta que el analizador AIOps esté listo.
- La base se inicializa con un Job y no desde la app (ver hallazgo).
- En GitHub Actions la ventana es de 1 min para que el pipeline no tarde demasiado.
