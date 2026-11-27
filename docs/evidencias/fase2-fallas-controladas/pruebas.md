# Evidencias de fallas controladas

Pruebas ejecutadas localmente en kind sobre la rama `feature/fallas-controladas`. Las capturas corresponden al dashboard Blue vs Green de Grafana y a la terminal.

## Resultados

| Escenario | Ejecución y resultado observado | Evidencias |
|---|---|---|
| Latencia | `bash k8s/bluegreen/bg.sh deploy ev-latency --window 2m --fault latency`. Análisis exitoso y Preview en pausa para promoción. Grafana mostró mayor latencia p95 en Preview que en la versión estable. | [Dashboard 1](01-latency-1.png), [Dashboard 2](01-latency-2.png) |
| Consulta lenta | `bash k8s/bluegreen/bg.sh deploy ev-slow-query --window 2m --fault slow-query`. Análisis exitoso. La ruta `/owners/{ownerId}` llegó aproximadamente a 981 ms de p95 medio y 1.05 s máximo; el pool Hikari de Preview llegó a 20 conexiones activas. | [Dashboard 1](02-slow-query-latencia-1.png), [Dashboard 2](02-slow-query-latencia-2.png) |
| Error al registrar visita | `bash k8s/bluegreen/bg.sh deploy ev-visit-write --window 2m --fault visit-write`. El análisis rechazó el Preview al superar el umbral de errores. Grafana mostró hasta 3.75 % de errores 5xx en Preview y Loki registró `FAULT=visit-write`. | [Grafana 1](03-visit-write-1.png), [Grafana 2](03-visit-write-2.png), [Grafana 3 / Loki](03-visit-write-3.png) |
| Fuga de heap | `bash k8s/bluegreen/bg.sh deploy ev-heap-leak --window 2m --fault heap-leak`. Análisis exitoso. Grafana mostró crecimiento del heap de Preview hasta 103 MiB, frente a un máximo aproximado de 58.2 MiB en estable. | [Dashboard 1](04-heap-leak-1.png), [Dashboard 2](04-heap-leak-2.png) |
| PostgreSQL caído | Se comprobó primero la respuesta normal, luego se escaló `petclinic-db` a cero. Las solicitudes a Active (`localhost:8080`) y Preview (`localhost:8081`) devolvieron HTTP 500; Grafana mostró 100 % de errores y Loki registró errores de conexión para ambas versiones. | [Estado inicial 1](05-postgres-baseline-1.png), [Estado inicial 2](05-postgres-baseline-2.png), [Grafana 1](05-db-caida-grafana-1.png), [Grafana 2](05-db-caida-grafana-2.png), [Loki](05-db-caida-grafana-3.png) |
| Recuperación de PostgreSQL | Se restauró `petclinic-db` a una réplica y se verificó que Active volvió a responder HTTP 200. | [Recuperación](06-db-restaurada.png) |
| Ruta `/oups` | `curl -i http://localhost:8080/oups` devolvió HTTP 500, correspondiente a la ruta de error intencional de PetClinic. | [Respuesta de `/oups`](07-oups.png) |

## Notas

- En las pruebas de despliegue, el análisis exitoso dejó el Preview en pausa para promoción; el escenario `visit-write` fue rechazado por el análisis.
- La prueba de PostgreSQL confirma errores en las dos versiones y la recuperación de Active tras restaurar la base.
- La respuesta HTTP 500 de `/oups` documenta el comportamiento intencional de la ruta. La exclusión de `/oups` de alertas debe verificarse con las reglas de alertas de la tarea de observabilidad.
- Las capturas son evidencia visual de las ejecuciones locales; los valores indicados son los observados en Grafana durante esas pruebas.
