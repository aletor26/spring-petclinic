# Latencias de k6 por revisión de PetClinic

Desde la raíz del repositorio, con Python 3, se puede analizar un archivo de la
carpeta `k6/results/` del artefacto `bluegreen-evidencias`. El mismo formato
se obtiene del clúster con `k8s/bluegreen/bg.sh results`.

```sh
python analyzer/k6_report.py RUTA_AL_JSON_GZ --deployments RUTA_A_DEPLOYMENTS_JSONL
python analyzer/k6_report.py RUTA_AL_JSON_GZ --samples > corrida.json
python -m unittest discover -s analyzer
```

El informe JSON agrupa las mediciones de `http_req_duration` por ruta etiquetada
y método HTTP efectivo. Conserva los errores HTTP y distingue el POST de una
visita del GET posterior a la redirección. Para cada destino muestra el número
de peticiones, la mediana y el p95 en milisegundos (interpolación lineal).
Cuando falta un destino, sus percentiles quedan en `null`. El campo
`both_targets_present` solo comprueba la presencia de muestras en ambos lados.

`--samples` agrega cada latencia con su hora, versión, destino, método, ruta y
estado HTTP para el análisis estadístico posterior. `--deployments` enlaza el
nombre original del archivo comprimido con el evento `deploy` mediante la
versión y los hashes de preview y stable. Comprueba las versiones observadas
y requiere una coincidencia única. Se pueden usar ambas opciones juntas.

Estos datos describen las peticiones durante la corrida. Para preparar la
referencia de Isolation Forest se deben cruzar las ventanas saludables con
las métricas de CPU, memoria, GC y conexiones de Prometheus.

## Análisis en modo lectura

Desde la raíz del repositorio, con Python 3.11 o 3.12 y un entorno virtual activo:

```sh
python -m pip install -r analyzer/requirements-test.txt
python -m unittest discover -s analyzer -v
python analyzer/latency_analysis.py RUTA_AL_JSON_GZ --deployments RUTA_A_DEPLOYMENTS_JSONL
```

Por operación obtendrás el número de observaciones, la diferencia de medianas
(preview menos active, en ms), U de preview y el valor p unilateral de
Mann-Whitney para la hipótesis de latencias mayores en preview. Se conservan
las peticiones de todos los estados HTTP. El valor p es exploratorio, sin
corrección por comparaciones múltiples. El mínimo técnico por lado es dos y
se ajusta con `--min-samples`; el mínimo operativo se calibra con carga real.

`--block-seconds N` usa la mediana de cada intervalo UTC de N segundos,
sin solapamiento y con datos de ambos destinos. El mínimo pasa a contar
bloques. Las cargas compartidas pueden seguir correlacionadas tras agrupar.
Para interpretar una regresión se necesitan cobertura, independencia defendible,
magnitud relevante y persistencia en corridas repetidas.

## Referencia de Isolation Forest

La entrada CSV tiene esta cabecera:

```csv
run_id,revision,target,load_profile,split,condition,vets_p95_ms,process_cpu_ratio,heap_mib,gc_pause_seconds,pool_active_mean
```

Una fila representa una revisión durante una ventana poscalentamiento de
10 minutos. Usa como `run_id` el AnalysisRun, conserva el hash de la revisión
en `revision` y registra el perfil de carga aplicado. Antes de exportar,
comprueba cobertura temporal, mínimo de peticiones y réplicas comparables.
Descarta ventanas con huecos. Las columnas numéricas propuestas son:

| Columna | Medición durante la ventana para esa revisión |
|---|---|
| vets_p95_ms | p95 de GET /vets.html en ms a partir del histograma HTTP agregado de la revisión |
| process_cpu_ratio | Media temporal del uso de CPU del proceso, promediado entre réplicas, de 0 a 1 |
| heap_mib | Heap usado en MiB, sumando áreas heap por pod y promediando entre réplicas y en el tiempo |
| gc_pause_seconds | Aumento acumulado de segundos en pausas de GC por pod, promediado entre réplicas |
| pool_active_mean | Conexiones activas del pool de PetClinic, promediadas entre réplicas y en el tiempo |

Usa `train` para corridas activas saludables a distintas cargas y
`calibration` para otras corridas activas saludables. Reserva `evaluation`
para corridas posteriores, saludables y con fallas conocidas. Cada corrida
pertenece a una única partición, aunque incluya active y preview.
`condition` acepta `healthy` y `fault`. El entrenamiento usa solo las cinco
columnas numéricas, con idéntica definición en entrenamiento e inferencia.

```sh
python analyzer/reference_model.py ventanas.csv referencia.joblib --quantile 0.95
```

El cuantil 0.95 ilustra un valor a calibrar. El entrenamiento fija la semilla,
calcula el umbral sobre el grupo de calibración y muestra las desviaciones
en evaluación. Un valor de desviación mayor indica una ventana más atípica
respecto de la referencia saludable. Conserva el CSV, las consultas de origen
y el resumen junto al modelo. Carga únicamente modelos generados localmente
por el equipo con la misma versión de scikit-learn.

## Servicio FastAPI

En PowerShell, apunta el servicio a los archivos del análisis y ejecútalo
desde la raíz del repositorio:

```powershell
$env:PETCLINIC_K6_FILE = 'C:\ruta\corrida.json.gz'
$env:PETCLINIC_DEPLOYMENTS = 'C:\ruta\deployments.jsonl'
python -m uvicorn observation_api:app --app-dir analyzer --host 127.0.0.1 --port 8000
```

En http://127.0.0.1:8000/docs puedes ejecutar `GET /observation/latency`.
Admite los parámetros opcionales `min_samples` y `block_seconds`. El registro
de despliegues también es opcional. Para puntuar una ventana, configura
`PETCLINIC_MODEL` con la ruta del modelo de confianza antes de iniciar el
servicio. `POST /observation/deviation` recibe las cinco columnas numéricas
como objeto JSON. Si todavía falta la referencia devuelve HTTP 503. Los dos
endpoints devuelven observaciones y carecen de acciones sobre Argo Rollouts.
