# Despliegue Blue-Green de Spring PetClinic en Kubernetes (kind + Argo Rollouts)

Corresponde a la **Fase 4** de la propuesta (Cap. IV, Tabla 7): *"Despliegue y
rollback manual funcionando"*. Rol: DevOps Engineer.

## Idea en una frase

Hay dos versiones de la app vivas a la vez: la **activa** (la usan los usuarios) y la
**preview** (la nueva, en prueba). Solo cuando la nueva pasa las pruebas se le pasa
el tráfico. Si algo sale mal, se vuelve a la anterior al instante.

```
                         ┌──────────────── Clúster kind (dentro de Docker) ───────────────┐
usuarios ─► localhost:8080 ─► Service petclinic-active  ─► Pod versión estable ─┐          │
probador ─► localhost:8081 ─► Service petclinic-preview ─► Pod versión nueva  ──┼─► PostgreSQL (PVC)
                         │    Argo Rollouts decide a qué Pod apunta cada Service              │
                         └──────────────────────────────────────────────────────────────────┘
```

## Componentes

| Pieza | Archivo | Para qué |
|---|---|---|
| Clúster kind | `kind-cluster.yaml` | Kubernetes dentro de Docker. Expone 8080 (active) y 8081 (preview) |
| PostgreSQL + PVC | `manifests/10-postgres.yaml` | Base compartida por ambas versiones. El PVC guarda los datos en un disco que no se borra |
| Services | `manifests/20-services.yaml` | `petclinic-active` y `petclinic-preview` |
| Análisis previo | `manifests/30-analysis-template.yaml` | Smoke tests antes de promover. **Aquí se conecta el analizador AIOps** |
| Rollout | `manifests/40-rollout.yaml` | Estrategia blue-green, promoción manual, versión anterior viva 10 min |
| Script | `bg.sh` | Todos los comandos |
| Carga | `k6/load.js` | Mismo tráfico a active y preview, resultados por solicitud en JSON |
| Pipeline | `.github/workflows/bluegreen-k8s.yml` | Todo el flujo automático en GitHub Actions |

## Requisitos (Mac)

Docker (OrbStack o Docker Desktop) y:

```bash
brew install kind kubectl argoproj/tap/kubectl-argo-rollouts
```

## Uso

Desde la raíz del repo:

| Comando | Qué hace |
|---|---|
| `k8s/bluegreen/bg.sh up` | Crea el clúster, instala Argo Rollouts y despliega la primera versión |
| `k8s/bluegreen/bg.sh deploy [versión]` | Construye el código actual, lo levanta en PREVIEW y corre el análisis. Versión por defecto: commit actual |
| `k8s/bluegreen/bg.sh promote` | Pasa a los usuarios a la versión PREVIEW |
| `k8s/bluegreen/bg.sh abort` | Descarta la PREVIEW. Los usuarios no se enteran |
| `k8s/bluegreen/bg.sh rollback` | Vuelve a la versión anterior después de promover |
| `k8s/bluegreen/bg.sh status` | Árbol del Rollout: versión activa, preview, análisis |
| `k8s/bluegreen/bg.sh traffic` | Una petición cada 0.5 s: código HTTP y versión que respondió |
| `k8s/bluegreen/bg.sh load [seg]` | k6 a active y preview; resultados en `k6/results/*.json` |
| `k8s/bluegreen/bg.sh down` | Borra el clúster |

Panel visual de Argo (opcional): `kubectl argo rollouts dashboard` → http://localhost:3100

## Flujo de un despliegue

1. `deploy` construye `petclinic:<commit>` y la carga en kind (imagen ligada al commit).
2. Argo crea los pods nuevos y los conecta a **preview**. Los usuarios siguen en active.
3. **Análisis previo** (`prePromotionAnalysis`): un Job corre smoke tests contra preview
   (`/actuator/health`, `/`, `/vets.html`, `/owners?lastName=`, `/owners/1`, `/owners/find`).
   - Falla → Argo **aborta**. Active no cambia.
   - Pasa → el Rollout queda en pausa esperando `promote`.
4. `promote` → Argo cambia el Service active a los pods nuevos (sin corte).
5. La versión anterior queda viva 10 min: `rollback` es inmediato.

## Dónde entra la IA (otro equipo)

En el paso 3. El `AnalysisTemplate` hoy solo tiene smoke tests. El equipo del analizador
agrega una métrica `web` que consulta su API y aprueba si el puntaje es ≥ 75 (hay un
ejemplo comentado en `30-analysis-template.yaml`). Lo que este despliegue ya les entrega:

- Etiqueta `version` en cada pod y `rollouts-pod-template-hash` para separar blue y green.
- `k6/results/*.json`: cada solicitud con `target` (active/preview), `version`, `route`,
  `status` y latencia, para la prueba de Mann-Whitney.
- `/actuator/info` con la versión desplegada.

Pendiente de coordinar: exponer `/actuator/prometheus` (dependencia
`micrometer-registry-prometheus`) si lo hace el equipo de observabilidad.

## Pruebas realizadas (24/11/2026, Mac M-series con OrbStack)

| Prueba | Resultado |
|---|---|
| `up` desde cero + `deploy` + `load` + `promote` + `rollback` | OK en 100 s, 0 reinicios de pods |
| Tráfico continuo durante 2 deploys, promote, rollback y versión rota | 185 peticiones, **0 errores** |
| k6 20 s a active (v1) y preview (v4) | 201 solicitudes, 100 % HTTP 200 |
| Versión con error a propósito en `/vets.html` (`v3-roto`) | Análisis: `/vets.html -> 500` → **Degraded/abortado**, usuarios siguieron en v1 |

## Demo para el video

Terminal 1: `k8s/bluegreen/bg.sh traffic`

Terminal 2:
```bash
k8s/bluegreen/bg.sh up
k8s/bluegreen/bg.sh deploy v2
k8s/bluegreen/bg.sh status
k8s/bluegreen/bg.sh promote
k8s/bluegreen/bg.sh rollback
```

Versión defectuosa: agregar `if (true) throw new RuntimeException("bug");` al inicio de
`VetController.showVetList`, correr `k8s/bluegreen/bg.sh deploy v3-roto` (el análisis la
rechaza) y deshacer con `git checkout -- src`.

## Diferencias con la propuesta

- Clúster local con kind en lugar de un clúster real: mismo Kubernetes, sin VM ni costo.
- Imagen construida y cargada localmente (`imagePullPolicy: Never`) en vez de un registro.
- El análisis usa smoke tests hasta que el analizador AIOps esté listo.
