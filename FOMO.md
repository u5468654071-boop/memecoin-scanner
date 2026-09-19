# Fomo como fuente complementaria de descubrimiento

La versión 0.10.0 admite capturas del panel web de Fomo en `fomo_source.py`. No es una API oficial ni un servicio que recopile datos por sí solo. El colector externo tiene que abrir Fomo en un navegador autorizado y entregar una captura reciente. No se extraen cookies ni credenciales al VPS.

## Qué entra en el escáner

Se admiten hasta 30 enlaces Solana por vista: En tendencia, Graduados y Más holdeados. Se guardan dirección, URL, categoría y hora real de captura. Se excluyen el ticker superior, otras cadenas y el panel de detalle. Es una muestra de las filas renderizadas, no un catálogo completo ni un stream de operaciones.

Las etiquetas `fomo_trending`, `fomo_graduated` y `fomo_most_held` identifican procedencia. Aparecer en Graduados no demuestra una migración on-chain. Tampoco se interpretan PnL, recomendaciones, posiciones de usuarios o volumen de Fomo como criterios de aprobación.

Los tokens van a la cola habitual y pasan la preselección conjunta de cada perfil, análisis de permisos y concentración, confirmación temporal y cotizaciones Jupiter. Se conservan cuotas, tamaños, prioridad de posiciones abiertas y tiempos de espera. La ampliación del universo se separa mediante la versión 0.10.0; no demuestra que mejore la rentabilidad.

## Contrato y entrega

`fomo_source.py prepare captures.json --output inbox.json` convierte una lista de objetos `{view, captured_at, snapshot}` procedentes del DOM visible del navegador. `captured_at` es un ISO 8601 con zona horaria de la lectura real; la captura conjunta usa la fecha más antigua. Las vistas válidas son `trending`, `graduated`, `most_held`. Si cambia el idioma o la estructura del panel, el conversor falla y debe revisarse; no adivina enlaces.

`fomo_source.py validate inbox.json` comprueba el contrato. Se rechazan archivos mayores de 128 KiB, más de 90 filas, campos no permitidos, URLs ajenas, identidades inválidas y capturas con más de cinco minutos o fechas futuras. No debe cambiarse la fecha de un archivo antiguo para volver a enviarlo.

El receptor `fomo_receive.py --output RUTA_FIJA/inbox.json` lee el JSON por stdin, valida, bloquea concurrentemente y reemplaza el archivo de forma atómica. Solo se confirma recepción; la importación al escáner se comprueba por separado. Un reenvío idéntico es idempotente; una entrega antigua o conflictiva conserva el último archivo válido.

Para un transporte SSH, el administrador crea un usuario sin privilegios, sin contraseña utilizable, con una clave dedicada y `restrict,command="/usr/bin/python3 /opt/memecoin-scanner/fomo_receive.py --output /opt/memecoin-scanner/data/fomo-inbox/inbox.json"` en `authorized_keys`. El usuario solo necesita escribir en esa carpeta; el grupo 10001 del contenedor solo necesita leerla. El archivo de claves autorizadas y su directorio deben pertenecer a root para que el receptor no pueda ampliarse permisos. El receptor rechaza comandos enviados por el cliente. No usar una clave SSH de administración para la recogida periódica.

Compose monta `./data/fomo-inbox` en `/fomo` de solo lectura. El escáner lee `/fomo/inbox.json` al comenzar cada ciclo. Sin Compose puede indicarse `--fomo-inbox RUTA` al escáner o `FOMO_INBOX_FILE` a `server.py scan`. Requiere perfiles Solana y no admite `--tokens` simultáneo.

## Estado y programación

`scanner-report.json` contiene `fomo_source`: `awaiting_capture`, `imported`, `current`, `stale`, `out_of_order`, `invalid` o `unreadable`, y el último recibo con fecha y número de tokens. Una captura caduca después de cinco minutos y no se vuelve a importar. Los tokens ya descubiertos pueden seguir revisándose con datos nuevos de los proveedores habituales; caducar Fomo no obliga a cerrar una posición.

La recogida periódica debe programarse aparte. En una instalación con el navegador de Codex, necesita que el Mac esté encendido, Codex disponible y Fomo conserve la sesión. Una cadencia horaria es una fuente suplementaria, no una detección en tiempo real: entre capturas, es normal que el archivo indique `stale`. Si se pierde la sesión o falla Fomo, no se actualiza la fecha, no se reenvía la muestra anterior como nueva y el resto del bot continúa en el VPS.

Para desactivar esta fuente basta con detener el colector; para deshabilitarla explícitamente, retirar `--fomo-inbox` de la orden del escáner. La simulación y sus avisos Telegram continúan con las demás fuentes.

## Verificación

Las pruebas cubren formato y límites, frescura, deduplicación, reenvíos tras reinicios, entregas fuera de orden, escritura atómica, conservación de esperas y un ciclo de escaneo que descarta una moneda de Fomo sin liquidez. Ejecutar `python -m unittest discover -s tests -v`. Las pruebas no acreditan ventaja económica.
