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
