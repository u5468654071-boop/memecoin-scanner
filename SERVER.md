# VPS: escaneo y simulación automática v0.6.0

Dos servicios: `scanner` analiza tokens; `paper` mantiene una cartera de dinero ficticio con cotizaciones de Jupiter. Comparten SQLite y límites de solicitudes. **No firman ni envían transacciones y no necesitan wallet, SOL ni USDC reales.** No existe un interruptor para activar operaciones reales.

## Preparación

Necesitas un VPS Linux con acceso SSH, Docker Engine y el complemento Compose. La instalación de Docker depende del sistema; sigue las instrucciones oficiales para [Ubuntu](https://docs.docker.com/engine/install/ubuntu/) o [Debian](https://docs.docker.com/engine/install/debian/). No se abre ningún puerto web ni se necesita dominio.

Usa una carpeta dedicada y la versión que incluya estos archivos:

```bash
git clone https://github.com/u5468654071-boop/memecoin-scanner.git
cd memecoin-scanner
git checkout main
cp .env.example .env.server
chmod 600 .env.server
nano .env.server
```

También puedes extraer el paquete v0.6.0 en una carpeta nueva y continuar desde `cp .env.example .env.server`. Para actualizar una instalación existente, conserva primero una copia de su base de datos; no mezcles carpetas ni volúmenes de experimentos distintos.

Dentro de `.env.server`, configura `JUPITER_API_KEY` y, si tienes uno, `SOLANA_RPC_URL`. La clave se introduce en el servidor, nunca en GitHub ni en el chat. Compose carga este archivo; ejecutar Python directamente requiere exportar las variables. No hace falta instalar el CLI de Jupiter.

Opciones adicionales de `.env.server`:

```dotenv
DAILY_API_LIMIT=20000
EXIT_API_RESERVE=5000
SCAN_INTERVAL_SECONDS=60
PAPER_INTERVAL_SECONDS=60
SCAN_MAX_TOKENS=3
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
```

Usa `config --quiet`: la salida completa de `docker compose config` podría mostrar variables del entorno. Los contenedores funcionan como usuario sin privilegios, con sistema de archivos de solo lectura salvo datos y `/tmp`. No se copian claves a la imagen. Los logs de Docker rotan; los datos históricos de SQLite y el CSV crecen y necesitan espacio y copias periódicas.

`restart: unless-stopped` reinicia procesos que terminan y permite recuperar el servicio cuando Docker arranca. El estado `healthy` solo indica progreso reciente del bucle (últimos 15 minutos), **no** calidad de datos, conexión satisfactoria a Jupiter ni rentabilidad. Docker no reinicia automáticamente un proceso solo por estar `unhealthy`: consulta los logs y, si procede, usa `docker compose restart scanner paper`. No se ha instalado un sistema externo de avisos.

El informe muestra saldo ficticio, coste comprometido, posiciones, cierres recientes, resultado realizado y bloqueos de entrada. Si alguna posición carece de una valoración reciente, `equity_usdc` es `null`: no se inventa su valor ni se asume que vale cero. Que no haya compras puede ser correcto: exige superar todas las comprobaciones, incluida la confirmación temporal.

## Reglas de la cartera ficticia

`paper-policy.json` define el experimento. Los valores iniciales son ilustrativos:

- 1.000 USDC virtuales; 25 por entrada y hasta tres posiciones.
- Señal candidata de esta versión con antigüedad máxima de 60 segundos. Se prioriza el ranking del escáner y se vuelven a consultar compra y venta. Todas las cotizaciones deben conservar identidad, importe y antigüedad máxima de 30 segundos.
- Supuesto adicional de 0,5% de slippage por lado y 0,05 USDC por lado. Las comisiones incluidas en las cotizaciones no se vuelven a sumar. La cantidad ficticia de tokens se reduce por el slippage de entrada y se cotiza esa cantidad exacta para valorar/salir.
- Salida por pérdida observada del 15%, ganancia observada del 30%, retroceso del 10% tras observar al menos un 15% de ganancia, o una hora de permanencia. También intenta salir si una nueva observación invalida al candidato.
- Bloquea nuevas entradas cuando las pérdidas realizadas del día UTC, sin compensarlas con ganancias, más pérdidas actuales abiertas alcanzan 50 USDC. También bloquea con posiciones pendientes de venta o valoración. No garantiza limitar la pérdida total a esa cifra.
- Tras cerrar un token espera una hora antes de otra entrada. Una misma señal no puede duplicar una posición; saldo y movimientos se guardan juntos en una transacción.

Las salidas se revisan al consultar, aproximadamente cada minuto con la configuración inicial; no al tocar el precio. Un salto de precio puede producir pérdidas mayores que el umbral. Si no hay ruta, la posición permanece abierta, el dinero sigue comprometido y el cierre solicitado se conserva para reintentarlo. Se usa la cotización que llegue después, sin inventar una ejecución al precio del stop.

La política de una cartera inicializada queda fijada. Cambiar el JSON hace que el simulador se detenga; restaura el archivo original para continuar. Para probar parámetros distintos, usa otra carpeta, otro proyecto Compose (`docker compose -p otro-experimento ...`) y un volumen nuevo. Conserva el historial anterior. Una actualización de versión del escáner reinicia la confirmación temporal de candidatos, pero no borra posiciones existentes.

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

## Copias y restauración

La copia usa la API de respaldo de SQLite; no copies solo el `.sqlite3` mientras está abierto en modo WAL.

```bash
# El nombre debe ser nuevo; nunca sobrescribe una copia anterior.
docker compose exec paper python server.py backup --backup-to /data/backups/copia-01.sqlite3
mkdir -p backups
docker compose cp paper:/data/backups/copia-01.sqlite3 ./backups/copia-01.sqlite3
cp paper-policy.json ./backups/paper-policy-01.json
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
