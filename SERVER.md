# VPS: escaneo y simulación automática v0.8.1

Dos servicios: `scanner` analiza tokens; `paper` mantiene tres carteras de dinero ficticio con cotizaciones de Jupiter. Comparten SQLite y límites de solicitudes. **No firman ni envían transacciones y no necesitan wallet, SOL ni USDC reales.** No existe un interruptor para activar operaciones reales.

## Preparación

Necesitas un VPS Linux con acceso SSH, Docker Engine y el complemento Compose 2.24 o posterior. La instalación de Docker depende del sistema; sigue las instrucciones oficiales para [Ubuntu](https://docs.docker.com/engine/install/ubuntu/) o [Debian](https://docs.docker.com/engine/install/debian/). No se abre ningún puerto web ni se necesita dominio.

Usa una carpeta dedicada y la versión que incluya estos archivos:

```bash
git clone https://github.com/u5468654071-boop/memecoin-scanner.git
cd memecoin-scanner
git checkout main
cp .env.example .env.server
chmod 600 .env.server
nano .env.server
```

También puedes extraer el paquete v0.8.1 en una carpeta nueva y continuar desde `cp .env.example .env.server`. Para actualizar una instalación existente, conserva primero una copia de su base de datos; no mezcles carpetas ni volúmenes de experimentos distintos.

Dentro de `.env.server`, configura `JUPITER_API_KEY` y, si tienes uno, `SOLANA_RPC_URL`. La clave se introduce en el servidor, nunca en GitHub ni en el chat. Compose carga este archivo; ejecutar Python directamente requiere exportar las variables. No hace falta instalar el CLI de Jupiter.

Opciones adicionales de `.env.server`:

```dotenv
DAILY_API_LIMIT=20000
EXIT_API_RESERVE=5000
SCAN_INTERVAL_SECONDS=60
PAPER_INTERVAL_SECONDS=60
SCAN_MAX_TOKENS=3
DISCOVERY_LIMIT=30
```

El límite diario es **por proveedor y día UTC**, compartido entre procesos, no por cada contenedor. El escáner deja de consultar al llegar a 15.000 con estos valores; el simulador puede utilizar el resto hasta 20.000. Las revisiones de salida tienen prioridad dentro de cada ciclo del simulador. La reserva reduce la competencia por cuota, pero no garantiza cobertura continua: las API pueden fallar o agotar su cuota. Ambos procesos comparten una separación mínima de 1,1 segundos entre llamadas de un mismo proveedor y las pausas solicitadas por él. Otras aplicaciones con la misma clave no comparten este contador.

El RPC público puede limitarte. Ajusta el presupuesto al plan de tus proveedores; no es un límite de facturación. Si faltan datos, no se abre una posición ficticia.

## Arrancar y consultar

```bash
docker compose config --quiet
docker compose build
docker compose run --rm --no-deps --entrypoint python paper -m unittest discover -s tests -v
docker compose up -d
docker compose ps
docker compose logs --tail=60 scanner paper
docker compose exec paper python server.py report
docker compose exec paper python server.py coverage
```

Usa `config --quiet`: la salida completa de `docker compose config` podría mostrar variables del entorno. Los contenedores funcionan como usuario sin privilegios, con sistema de archivos de solo lectura salvo datos y `/tmp`. No se copian claves a la imagen. Los logs de Docker rotan; los datos históricos de SQLite y el CSV crecen y necesitan espacio y copias periódicas.

`restart: unless-stopped` reinicia procesos que terminan y permite recuperar el servicio cuando Docker arranca. El estado `healthy` solo indica progreso reciente del bucle (últimos 15 minutos), **no** calidad de datos, conexión satisfactoria a Jupiter ni rentabilidad. Docker no reinicia automáticamente un proceso solo por estar `unhealthy`: consulta los logs y, si procede, usa `docker compose restart scanner paper`. Puedes activar el servicio opcional de [avisos por Telegram](TELEGRAM.md); requiere vincularlo antes de enviar mensajes.

El informe muestra saldo ficticio, posiciones, cierres, resultado y bloqueos de entrada por perfil, además del agregado. Incluye rentabilidad ficticia, aciertos, factor de beneficio y drawdown de las valoraciones observadas, con sus limitaciones. Si alguna posición carece de una valoración reciente, `equity_usdc` es `null`: no se inventa su valor ni se asume que vale cero. Que no haya compras puede ser correcto: exige superar todas las comprobaciones, incluida la confirmación temporal.

## Búsqueda y medición prospectiva

`DISCOVERY_LIMIT` controla el tamaño de las listas (1–100); `SCAN_MAX_TOKENS` limita el análisis profundo (1–20). Son límites distintos. La preselección examina un lote de hasta 30 tokens por ciclo con una consulta Jupiter y una DexScreener. Usa identidades exactas, datos vigentes, el pool y la edad conocidos; `ready` solo significa pendiente de análisis completo. Las reservas de riesgo no se relajan.

Con tres análisis por ciclo, se reservan hasta dos para confirmar candidatas y uno para explorar; las posiciones abiertas tienen prioridad de actualización. Si no hay novedades se utiliza la capacidad restante para confirmar. El seguimiento continuo de una moneda pendiente tiene una ventana de 30 minutos. Los riesgos se revisan tras 15 minutos, los datos incompletos tras tres minutos; reaparecer en una lista no borra el descanso. Los eventos tempranos permanecen en SQLite y no saturan directamente el análisis profundo.

El comando `coverage` muestra preselección, repeticiones de observación, causas de datos ausentes y resultados del estudio de [RESEARCH.md](RESEARCH.md). También aparecen en `scanner-report.json`. La cobertura analiza las últimas 24 horas con un máximo declarado de 5.000 observaciones, separando la versión actual. Las muestras del estudio se separan por versión y plan y usan la cuota del escáner, incluida su reserva para el servicio de salidas.

Al actualizar desde v0.7 se conservan los saldos y el plan 600/300/100. Solo se añaden tablas; las observaciones anteriores siguen disponibles. La confirmación temporal empieza con los datos de la nueva versión. El estudio no abre posiciones en las carteras, no envía avisos de compras y no usa dinero real.

## Reglas de las tres carteras ficticias

`profiles.json` define el experimento activo de Compose. El detalle completo está en [PROFILES.md](PROFILES.md):

- Conservador: 600 USDC virtuales, 50 por entrada; equilibrado: 300 y 25; agresivo: 100 y 10.
- Dos posiciones como máximo por perfil. Los filtros exigen progresivamente más liquidez, menor concentración y más confirmación en el conservador.
- Límite conjunto de exposición de 180 USDC y 70 en una misma moneda, incluidos costes de entrada.
- Bloqueo conjunto por 30 USDC de pérdidas del día; límites individuales de 15, 10 y 5. Las ganancias no compensan esas pérdidas.
- Salidas de todos los perfiles antes de nuevas entradas. Una salida o valoración pendiente bloquea las entradas en los tres.
- Señales de hasta 60 segundos, cotizaciones de hasta 30 segundos y nuevas consultas antes de abrir cada posición.
- Supuestos de slippage por lado de 0,5%, 0,5% y 1%, y 0,05 USDC de coste fijo por lado. No se vuelven a sumar las comisiones ya incluidas en las cotizaciones.
- Saldos, posiciones y plan persisten juntos. Telegram incluye el perfil en cada operación.

Las salidas se revisan al consultar, aproximadamente cada minuto con la configuración inicial; no al tocar el precio. Un salto de precio puede producir pérdidas mayores que el umbral. Si no hay ruta, la posición permanece abierta, el dinero sigue comprometido y el cierre solicitado se conserva para reintentarlo. Se usa la cotización que llegue después, sin inventar una ejecución al precio del stop.

El plan completo de las tres carteras inicializadas queda fijado. Cambiar el JSON hace que el simulador se detenga; restaura el archivo original para continuar. Para probar parámetros distintos, usa otra carpeta, otro proyecto Compose (`docker compose -p otro-experimento ...`) y un volumen nuevo. Conserva el historial anterior. Una actualización de versión del escáner reinicia la confirmación temporal de candidatos, pero no borra posiciones existentes.

Esto no reproduce completamente MEV, congestión, gas, impacto propio ni fallos de ejecución. No constituye prueba de rentabilidad ni validación para operar dinero real.

## Pausar, cerrar y detener

```bash
# Pausar entradas; sigue valorando e intentando salidas.
docker compose exec paper python server.py pause

# Solicitar cierre ficticio de todas las posiciones y mantener entradas pausadas.
docker compose exec paper python server.py close-all

# Ver posiciones pendientes y resultados.
docker compose exec paper python server.py report

# Reanudar; si pediste close-all, exige que se hayan cerrado todas.
docker compose exec paper python server.py resume

# Detener ambos servicios, conservando sus datos.
docker compose stop

# Volver a iniciar después de una parada voluntaria.
docker compose up -d
```

Detener servicios no cierra posiciones, ni siquiera las ficticias. Durante la parada no se observan precios ni se ejecutan reglas; al reanudar consulta cotizaciones actuales. Las pausas manuales sobreviven a reinicios. Evita `docker compose down -v`: elimina el volumen con el historial y la cartera.

## Actualizar desde v0.6

Guarda una copia consistente antes de actualizar. La migración automática a los tres perfiles exige que la cartera anterior siga con 1.000 USDC y nunca haya tenido posiciones; si tuvo actividad, se bloquea y conserva el estado. La cuenta anterior queda archivada y no se suma al capital nuevo. Guarda `profiles.json` junto con cada copia.

## Copias y restauración

La copia usa la API de respaldo de SQLite; no copies solo el `.sqlite3` mientras está abierto en modo WAL.

```bash
# El nombre debe ser nuevo; nunca sobrescribe una copia anterior.
docker compose exec paper python server.py backup --backup-to /data/backups/copia-01.sqlite3
mkdir -p backups
docker compose cp paper:/data/backups/copia-01.sqlite3 ./backups/copia-01.sqlite3
cp profiles.json ./backups/profiles-01.json
```

Para restaurar, usa una carpeta y un proyecto Compose nuevos con su propio volumen, configurando el archivo de política que acompañaba a la copia y `.env.server`. En esa carpeta, con la copia disponible en `backups/copia-01.sqlite3`:

```bash
docker compose -p restauracion create
docker compose -p restauracion cp backups/copia-01.sqlite3 paper:/data/scanner.sqlite3
docker compose -p restauracion run --rm --no-deps --user 0:0 --entrypoint chown paper 10001:10001 /data/scanner.sqlite3
docker compose -p restauracion run --rm --no-deps paper pause
docker compose -p restauracion run --rm --no-deps paper report
```

Verifica saldos y posiciones antes de arrancar con `docker compose -p restauracion up -d`. Las entradas permanecen pausadas hasta ejecutar `resume`; la valoración y las reglas de salida empiezan al arrancar. No sobrescribas una base abierta ni mezcles un archivo restaurado con los antiguos archivos `-wal` y `-shm`.

Referencias operativas: [Compose services](https://docs.docker.com/reference/compose-file/services/) y [políticas de reinicio](https://docs.docker.com/engine/containers/start-containers-automatically/).

Desde v0.8.1, la preselección reserva 20 de los 30 huecos a tokens de listas de mercado y 10 a migraciones; los huecos no utilizados se ceden al otro grupo. Entre migraciones aún no consultadas, se atienden primero las más recientes. Así, importar el historial del stream no bloquea durante decenas de ciclos las listas actuales.
