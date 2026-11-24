# Despliegue Blue-Green de Spring PetClinic (Docker)

## ¿Qué es Blue-Green?

Se tienen **dos copias** de la aplicación corriendo a la vez:

- **Blue**: la versión que usan los usuarios ahora.
- **Green**: la versión nueva, que se prueba sin que nadie la vea.

Un **proxy (Nginx)** decide a cuál de las dos llega el tráfico. Cuando la versión
nueva pasa las pruebas, el proxy cambia de color en menos de un segundo. Si algo
sale mal, se vuelve al color anterior al instante (**rollback**), porque la versión
vieja sigue encendida.

```
                 ┌──────────────► petclinic-blue  :8081 (v1)
usuario ─► :8080 │ Nginx proxy                          │
                 └ ─ ─ ─ ─ ─ ─ ─► petclinic-green :8082 (v2) ├──► PostgreSQL (compartida)
```

Ambos colores usan **la misma base de datos**, así los datos no se pierden al cambiar.

## Requisitos

- Docker Desktop (con Docker Compose). No hace falta Java ni Maven: se compila dentro de Docker.
- Puertos libres: 8080, 8081, 8082.

## Uso

Todo se hace con `bluegreen/bg.sh`, desde la raíz del repo.

| Comando | Qué hace |
|---|---|
| `./bluegreen/bg.sh up` | Primer arranque: compila v1 y levanta db + blue + proxy |
| `./bluegreen/bg.sh deploy v2` | Compila el código actual como `v2`, lo levanta en el color inactivo, corre smoke tests y, si pasan, cambia el tráfico |
| `./bluegreen/bg.sh rollback` | Regresa el tráfico al color anterior |
| `./bluegreen/bg.sh switch blue\|green` | Cambia el tráfico a mano |
| `./bluegreen/bg.sh status` | Color activo, versión y salud de cada color |
| `./bluegreen/bg.sh traffic` | Petición cada 0.5 s: muestra código HTTP y color que respondió |
| `./bluegreen/bg.sh down` | Apaga todo (`down -v` borra también la base de datos) |

URLs:

- App (lo que ve el usuario): http://localhost:8080
- Blue directo: http://localhost:8081 · Green directo: http://localhost:8082
- Versión de cada color: http://localhost:8081/actuator/info

## Flujo de `deploy`

1. Lee qué color está activo (por ejemplo, blue).
2. Construye la imagen `petclinic:<versión>` y la etiqueta como el color inactivo (green).
3. Recrea solo el contenedor green. Blue sigue atendiendo.
4. Espera a que green esté `healthy` (`/actuator/health`, que incluye la base de datos).
5. **Smoke tests** contra green: `/actuator/health`, `/`, `/vets.html`, `/owners?lastName=`,
   `/owners/1`, `/owners/find`. Todas deben responder 200.
6. Si algo falla: se detiene y **el tráfico sigue en blue**. El usuario no nota nada.
7. Si todo pasa: reescribe `nginx/active/*.conf` y ejecuta `nginx -s reload`
   (recarga en caliente, sin cortar conexiones).
8. Blue queda encendido para un rollback inmediato.

## Guion sugerido para la demo / video

En una terminal:

```bash
./bluegreen/bg.sh traffic
```

En otra:

```bash
./bluegreen/bg.sh up
./bluegreen/bg.sh deploy v2      # el tráfico pasa de blue a green, todo en HTTP 200
./bluegreen/bg.sh rollback       # vuelve a blue al instante
./bluegreen/bg.sh status
```

Demo de versión defectuosa (prueba que blue-green protege al usuario):

1. Meter un error a propósito, por ejemplo en `VetController.showVetList`:
   `if (true) throw new RuntimeException("bug simulado");`
2. `./bluegreen/bg.sh deploy v3-roto` → el smoke test de `/vets.html` da 500, el
   despliegue se cancela y el tráfico sigue en la versión buena.
3. Deshacer el cambio con `git checkout -- src/`.

## Resultados de la prueba local (24/11/2026)

- `deploy v2`: cambio blue → green con tráfico continuo, **0 respuestas con error** en
  37 peticiones durante el cambio y el rollback.
- `deploy v3-roto`: `/vets.html` devolvió 500 en el color inactivo; el despliegue se
  detuvo y el proxy siguió respondiendo 200 desde green.

## Archivos

- `../Dockerfile`: compilación multi-stage (Maven + JRE 17).
- `docker-compose.yml`: db, app-blue, app-green, proxy.
- `nginx/default.conf`: proxy; incluye el color activo desde `nginx/active/`.
- `nginx/active/upstream.conf` y `color.conf`: color activo. Los crea y reescribe `bg.sh` (no se suben al repo).
- `.env`: versión desplegada en cada color. También local, lo crea `bg.sh`.

## Relación con AIOps (siguiente paso)

Blue-green deja dos versiones vivas lado a lado. Eso permite que una herramienta
AIOps **compare métricas de blue contra green** (errores, latencia, JVM) antes y
después del cambio, y decida el rollback sola en vez de depender solo de los smoke
tests. Para eso falta:

- Agregar `micrometer-registry-prometheus` para exponer `/actuator/prometheus`.
- Prometheus + Grafana leyendo ambos colores (etiqueta `color`).
- Una regla o detector de anomalías que llame a `./bg.sh rollback` si green empeora.
