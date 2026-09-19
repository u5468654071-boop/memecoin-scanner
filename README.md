# Memecoin Scanner v0.9.0

Escáner de investigación para Solana: detecta lanzamientos, conserva su evolución, comprueba permisos y concentración, consulta actividad orgánica y cotizaciones de salida, y genera alertas locales explicables. **No conecta wallets, firma transacciones ni envía órdenes.** Python 3.9 o posterior.

Estado: versión de investigación, con 212 pruebas automáticas. Incluye tres carteras exclusivamente ficticias, servicios Docker para VPS y avisos opcionales por Telegram. Aún hay que reunir resultados prospectivos; no se ha demostrado rentabilidad. Consulta [VALIDATION.md](VALIDATION.md) para ver la cobertura del escáner y [TELEGRAM.md](TELEGRAM.md) para vincular los avisos.

Para dejarlo funcionando en un servidor, sigue [SERVER.md](SERVER.md). Escanea y simula entradas y salidas en segundo plano, guarda posiciones tras reinicios y permite pausar o cerrar la cartera ficticia. No necesita wallet ni fondos. Los parámetros de simulación son supuestos de prueba, no una estrategia validada.

La revisión 0.5.1 introdujo un tramo continuo de observaciones válidas, detecta retiradas de liquidez desde máximos intermedios y separa los informes por versión, política y tamaños solicitados. Los históricos 0.5.0 se conservan, pero no cuentan como confirmación de las nuevas decisiones.

## Tres perfiles en el VPS

[Conservador, equilibrado y agresivo](PROFILES.md): 600, 300 y 100 USDC virtuales; entradas de 50, 25 y 10, filtros y salidas distintos, exposición conjunta limitada y resultados separados. Comparten observaciones y cuotas. Compose activa este plan por defecto; el CLI de escaneo aislado conserva su política base si no se indica `--profiles profiles.json`. El cambio de versión reinicia la confirmación de candidatos y conserva el historial.

## Selección y evaluación v0.9

Con `--profiles profiles.json`, una cola persistente preselecciona pools DEX por lotes antes de gastar consultas profundas. El VPS descubre hasta 30 tokens por fuente y mantiene el límite de tres análisis completos por ciclo. Los eventos de creación quedan registrados; una migración o una lista de mercado puede llevar el token a preselección. Se reserva capacidad para confirmar candidatas y revisar posiciones abiertas.

El VPS añade listas Jupiter de mayor actividad (5 minutos) y tendencia (1 hora): consulta 100 resultados por categoría, elimina edades conocidas incompatibles y conserva hasta 30 por lista. Antes del análisis profundo, edad, liquidez, Organic Score y compradores deben encajar conjuntamente en al menos un perfil. Ninguna lista aprueba una entrada. Las revisitas comparten capacidad con nuevos tokens para evitar que una llegada continua las bloquee.

Las comprobaciones distinguen datos ausentes, umbrales incumplidos y edad pendiente. Ningún dato desconocido se convierte en una aprobación. Se mantienen las asignaciones, límites y filtros críticos del plan de tres carteras.

Una muestra prospectiva determinista compara cotizaciones de monedas seleccionadas y descartadas a 1, 2 y 4 horas: hasta 24 muestras nuevas al día, 10 USDC hipotéticos, costes asumidos y fallos visibles. No debita las carteras ni constituye una simulación de sus stops. Consulta [RESEARCH.md](RESEARCH.md).

```bash
docker compose exec paper python server.py coverage
```

## Arranque rápido

```bash
# Un escaneo. Funciona sin instalar paquetes, con cobertura limitada si falta Jupiter.
python3 memecoin_scanner.py --limit 5 --max-tokens 10

# Resultado completo
python3 memecoin_scanner.py --report
python3 memecoin_scanner.py --alerts
```

Cada ejecución escribe `data/latest_v05.json`, un CSV y `data/scanner.sqlite3`. El JSON distingue motivos de rechazo, comprobaciones pendientes, fuentes y antigüedad de datos. Los históricos anteriores se conservan.

**Para obtener candidatos en v0.5 se necesita `JUPITER_API_KEY`.** Sin ella se observan tokens y se guardan resultados, pero no se simula que se hayan comprobado la actividad orgánica y las rutas de salida. También se necesita un RPC de Solana accesible; el RPC público predeterminado puede responder 429. Puedes indicar uno de tu proveedor:

```bash
export JUPITER_API_KEY="TU_CLAVE_DE_API_LOCAL"
export SOLANA_RPC_URL="https://URL_DE_TU_RPC"
```

Configura las variables en tu máquina. No hacen falta claves privadas, seed phrases ni fondos. `.env.example` es solo una referencia; el programa **lee variables del entorno, no carga `.env` automáticamente**. No subas claves a GitHub. Las URLs de RPC y claves de API no se escriben en los resultados ni en errores.

## Nuevos lanzamientos y observación continua

El stream de creación/migración requiere una única dependencia opcional:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-stream.txt

# Captura eventos gratuitos durante un minuto; no escanea ni suscribe operaciones de pago.
python memecoin_scanner.py --stream --stream-seconds 60

# Captura y escanea en primer plano. Ctrl+C detiene ambos; el historial permanece.
python memecoin_scanner.py --watch --with-stream --interval 60 --max-tokens 10

# Observación sin stream; añade descubrimiento de Jupiter si tienes clave.
python memecoin_scanner.py --watch --candidates boosted,profiles,jupiter --interval 60

# Prueba acotada: dos ciclos, sin dejar procesos permanentes.
python memecoin_scanner.py --watch --cycles 2 --limit 2 --max-tokens 2
```

Estos comandos sin `--profiles` conservan la cola anterior. No instalan servicios; el despliegue continuo con Docker se describe en SERVER.md. El intervalo es un mínimo: si las consultas tardan más, el siguiente ciclo empieza al terminar. Se atienden los vencimientos de seguimiento antes del lote y entre tokens. Los candidatos pendientes se intercalan con nuevos descubrimientos, priorizando pendientes menos recientemente analizados. Los rechazados esperan al menos 15 minutos entre reanálisis y los incompletos 5 minutos; reaparecer en una lista no salta ese descanso. Las direcciones manuales se revisan en cada ciclo. La cola activa considera tokens vistos por primera vez en las últimas 72 horas; el historial no se borra.

El stream utiliza una conexión y solo `subscribeNewToken` / `subscribeMigration`. Deduplica eventos, reconecta y registra huecos. **No promete recuperar eventos no recibidos**: los intervalos sin conexión aparecen en el informe. Si el proveedor no envía `blockTime`, conserva la hora de recepción sin presentarla como hora de creación on-chain. Un sufijo `pump` no se usa como prueba de origen.

## Qué comprueba ahora

| Área | Implementación | Límite explícito |
| --- | --- | --- |
| Fases | Detectado, bonding curve, nuevo pool, migración reciente y establecido; fechas separadas | La fase inicial queda en observación: no hay analizador propio de bonding curves |
| Permisos | `getAccountInfo` de Solana, programa, emisión, congelación, supply y decimales | Extensiones Token-2022 no soportadas bloquean candidatura; no se ignoran |
| Pool exacto | Vincula dirección y ambos mints con su mercado en el informe completo de RugCheck | El bloqueo LP sigue siendo información externa, no una verificación independiente del contrato de bloqueo |
| Holders | Resuelve las 20 mayores cuentas de token a sus propietarios, suma cuentas del mismo dueño y excluye reservas identificadas | Es una muestra; los porcentajes por dueño son límites inferiores, no un censo completo |
| Grupos relacionados | Lee los grupos `insiderNetworks` de RugCheck y su concentración reportada | No construye un grafo propio; no atribuye identidad, no suma grupos que podrían solaparse |
| Actividad | Jupiter Organic Score y estadísticas por ventanas, con control de caducidad | La clasificación del proveedor no demuestra personas reales ni rentabilidad |
| Trayectoria | Múltiples snapshots espaciados, actividad orgánica sostenida y deterioro de liquidez | Ventanas móviles no se suman como compradores nuevos; respuestas de caché repetidas no cuentan como confirmaciones |
| Entrada/salida | Cotizaciones Jupiter v2 sin `taker`, en USDC y por tamaño | Son cotizaciones independientes; no simulan el efecto de nuestra propia compra ni garantizan ejecución |
| Alertas | Candidato e invalidación en SQLite/JSON; aperturas, cierres e incidencias de simulación por Telegram si se configura | Telegram requiere vincular un chat privado; las candidaturas no se envían cada ciclo |

Un cambio de pool no reinicia las fechas conocidas del token. La trayectoria de liquidez del nuevo pool empieza por separado. No llamamos “seguro” a un candidato: significa que supera las comprobaciones implementadas bajo la política indicada.

## Decisión y ranking

La referencia v0.4 se conserva para comparar resultados en el mismo universo. La v0.5 quita el veto fijo de 30 minutos y exige evidencia temporal en su lugar. Sin `--profiles`, `--min-age-hours` afecta a la referencia v0.4; el criterio temporal de v0.5 se ajusta con `--min-observation-seconds` y `--min-samples`.

En el CLI sin perfiles, además de los filtros de mercado heredados:

- Tres snapshots válidos separados al menos 60 segundos y que abarquen al menos 180 segundos, del mismo token, pool, versión y política. Un hueco mayor que `--data-max-age-seconds` (300 por defecto) interrumpe la confirmación; solo se mira la última media hora.
- Actualizaciones de Jupiter distintas, no repetir la misma respuesta en caché.
- Organic Score ≥ 20 y al menos 5 compradores orgánicos reportados en la ventana móvil de 5 minutos de varias observaciones.
- Caída de liquidez desde el máximo de las observaciones usadas no superior al 20%.
- Autoridades mint/freeze revocadas y programa interpretado directamente.
- LP del pool exacto reportada ≥ 80%.
- Ningún dueño de la muestra > 20% del supply; los diez mayores de la muestra no superan el 60%.
- Ningún grupo relacionado reportado > 30% del supply.
- Cotizaciones para todos los tamaños solicitados, con coste de ida y vuelta ≤ 5% y sin caducar.

**Estos umbrales son heurísticos iniciales, no una estrategia optimizada.** Cada observación guarda las políticas. Un riesgo crítico no se compensa con popularidad o liquidez. La salida distingue `candidate`, `observing`, `insufficient_data` y `rejected`; el último significa incumplir filtros, no prueba de estafa.

El ranking de quienes pasan pondera a partes iguales la prioridad descriptiva de mercado heredada y el Organic Score. Se muestran por separado completitud de datos, riesgos, trayectoria y cotizaciones. El porcentaje de completitud es una lista de comprobaciones disponibles, **no una probabilidad de seguridad o beneficio**.

```bash
# Tres tamaños hipotéticos, no órdenes reales
python3 memecoin_scanner.py --sizes-usdc 50,100,250 --max-tokens 5

# Direcciones o consultas elegidas por ti
python3 memecoin_scanner.py --tokens DIRECCION1,DIRECCION2
python3 memecoin_scanner.py --search 'consulta'

# Solo volver a analizar la cola persistente, sin nuevas listas
python3 memecoin_scanner.py --candidates '' --max-tokens 10

python3 memecoin_scanner.py --help
```

Las alertas candidatas caducan a los 5 minutos. Se renuevan al volver a superar comprobaciones tras ese plazo; no se duplican en cada ciclo. Si el token deja de cumplir, se invalida la anterior. Guarda la alerta junto con sus datos: no la interpretes como recomendación vigente horas después.

## Evaluar utilidad sin inventar un backtest

Además del estudio de v0.8 descrito en [RESEARCH.md](RESEARCH.md), cada observación conserva dos mediciones históricas a 1, 6 y 24 horas:

1. Precio indicativo del mismo par: conserva la referencia v0.4.
2. Cotización de venta de la cantidad de tokens que habría dado la cotización inicial de compra. La salida puede usar otra ruta/pool, pero debe ser del mismo mint.

```bash
python3 memecoin_scanner.py --evaluate
python3 memecoin_scanner.py --report
```

`--watch` ya ejecuta la evaluación mientras está abierto. Fuera del bucle, ejecuta `--evaluate` durante las ventanas de vencimiento. No puede recuperar una cotización histórica que no se consultó a tiempo.

El reporte compara selección v0.5, referencia v0.4 y referencia sencilla de liquidez/actividad sobre las mismas observaciones. Separa versiones, políticas, huella del plan, perfiles, conjunto de tamaños solicitado, selección/descarte, horizonte y tamaño. Muestra cobertura y tokens únicos. Mantiene casos sin datos (`untrackable`), plazos perdidos (`missed`) y errores pendientes; no les inventa un retorno cero.

La medición de salida publica un escenario estresado: por defecto descuenta 100 puntos básicos del importe de salida y 0,10 USDC adicionales del resultado. Son **supuestos**, configurables con `--exit-stress-bps` y `--fixed-cost-usdc`; no representan una estimación exacta de gas, slippage o MEV. Las comisiones embebidas en una cotización no deben restarse de nuevo como si no estuvieran incluidas. Las medianas de retornos observables aún pueden sufrir sesgo de supervivencia.

No se ha entrenado un modelo de ML ni demostrado mejora de rentabilidad. El historial permitirá comparar políticas prospectivamente. Repetir el escaneo de una moneda no convierte sus observaciones en muestras independientes.

## Límites operativos

- Límite predeterminado de 2.000 solicitudes **por proveedor y día UTC**, persistente y compartido entre reinicios. Cuenta reintentos. Ajusta `--daily-api-limit` a tu plan: limitar solicitudes no equivale a limitar euros facturados.
- Las APIs tienen timeout, tamaño de respuesta máximo, pausas, reintentos acotados y tratamiento de 429. Una falta de cobertura impide aprobar comprobaciones dependientes.
- Un bloqueo del sistema impide dos escaneos/evaluaciones simultáneos sobre la misma base de datos y se libera al terminar o caer el proceso. El colector del stream puede trabajar junto al escáner; SQLite serializa las transacciones. No dirijas dos bases de datos diferentes al mismo CSV de salida.
- SQLite, eventos, estado y alertas sobreviven al reinicio. La escritura de una observación v0.5, sus evaluaciones y alertas es atómica. Los JSON de salida se reemplazan de forma atómica.
- `--report` y `--alerts` son lecturas sin red. `--stream` sin eventos devuelve 2; un escaneo sin datos de mercado utilizables también devuelve 2. Un escaneo con datos puede devolver 0 aunque falte Jupiter o ningún token pase: comprueba `state`, `provider_errors` y `configuration`.
- Las fuentes de promociones y perfiles tienen sesgo. Ni el stream ni las listas cubren todos los lanzamientos de Solana.

## Verificación y compatibilidad

```bash
python3 -m unittest discover -s tests -v
# Para incluir la prueba de WebSocket local, instala antes requirements-stream.txt.
python3 -m py_compile memecoin_scanner.py scanner_v05.py engine.py providers.py observation_store.py event_stream.py tracking.py

# Mantener comportamiento v0.4 explícitamente
python3 memecoin_scanner.py --legacy --limit 5
```

Los tests no requieren claves ni APIs externas. Incluyen una conexión WebSocket a un servidor local de prueba, respuestas simuladas de Jupiter, persistencia, rollback, deduplicación, fases, cuotas, identificación de pools y evaluación temporal. La CI incluye Python 3.9, 3.12 y 3.13; consulta el informe de validación para saber qué se ejecutó realmente.

Referencias: [Jupiter Tokens](https://developers.jup.ag/docs/tokens/token-information), [Jupiter cotizaciones](https://developers.jup.ag/docs/swap/order-and-execute), [Solana RPC](https://solana.com/docs/rpc/http/getaccountinfo), [Token Extensions](https://solana.com/docs/tokens/extensions), [PumpPortal](https://pumpportal.fun/data-api/real-time/), [RugCheck](https://api.rugcheck.xyz/swagger/index.html).
