# Evidencias: fallas controladas

Estado: código y pruebas unitarias listos; falta correr el clúster y guardar capturas reales.

Guarda las capturas de Grafana y logs en esta carpeta. En cada captura debe verse la versión
Preview (`version`) y el intervalo de tiempo de la prueba.

| Escenario | Qué mostrar | Archivo sugerido |
|---|---|---|
| `latency` | p95/p99 de Preview mayor que Blue durante el deploy | `01-latency.png` |
| `slow-query` | `/owners`, conexiones activas/en espera y aumento de latencia | `02-slow-query-pool.png` |
| `visit-write` | logs del error y 5xx en `POST .../visits/new`; sondas saludables | `03-visit-write.png` |
| `heap-leak` | heap de Preview creciendo al solicitar `/vets.html` | `04-heap-leak.png` |
| PostgreSQL caído | errores de conexión durante escala a 0 y recuperación a 1 | `05-postgres-down.png` |
| `/oups` | excepción intencional etiquetada con la ruta `/oups` | `06-oups-control.png` |

Para cada despliegue, anota comando, versión, hora, resultado del análisis y enlace/nombre de
la captura. La prueba de alertas que excluye `/oups` depende también de las reglas de la tarea 3.
