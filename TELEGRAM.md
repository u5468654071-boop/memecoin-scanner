# Avisos de simulación por Telegram

El servicio opcional `telegram` envía a un único chat privado: aperturas ficticias con importe y mint; cierres con resultado neto ficticio y motivo; problemas de valoración/salida y su recuperación; falta de progreso de `scanner` o `paper` y recuperación. Todos los mensajes empiezan por **SIMULACIÓN**. No recibe órdenes de compra, cambios de configuración ni comandos para operar. No usa wallet.

## Vincular

1. Crea un bot dedicado con `/newbot` en [@BotFather](https://t.me/BotFather). Guarda el token en el VPS, en una sesión SSH, sin pegarlo en chats ni subirlo a GitHub:

```bash
cd /opt/memecoin-scanner
python3 - <<'PY'
import getpass, os, re
token = getpass.getpass('Token de Telegram (oculto): ')
if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,100}', token):
    raise SystemExit('Formato inválido; no se ha guardado')
fd = os.open('.env.telegram', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.fchmod(fd, 0o600)
with os.fdopen(fd, 'w') as out:
    out.write('TELEGRAM_BOT_TOKEN=' + token + '\n')
PY
```

2. Construye el servicio y genera el enlace. `.env.telegram` está excluido de Git y de la imagen; solamente el servicio de avisos recibe su contenido. No necesita las claves de Jupiter o del RPC.

```bash
docker compose --profile telegram config --quiet
docker compose build telegram
docker compose run --rm --no-deps telegram pair
docker compose --profile telegram up -d --no-deps telegram
```

3. Abre el enlace que imprimió `pair` en tu Telegram y pulsa **Iniciar**. No compartas ese enlace: quien lo utilice vincula su chat. Es de un solo uso, caduca en una hora y solo acepta mensajes privados recientes del propio usuario. No basta con escribir `/start` sin el código. El servicio manda una confirmación y conserva la vinculación tras reinicios. Si hay un webhook existente, la configuración se detiene sin eliminarlo.

4. Comprueba la entrega:

```bash
docker compose exec telegram python telegram_notifications.py status
docker compose --profile telegram ps
```

`paired: true` confirma la vinculación; `sent` cuenta mensajes confirmados por Telegram, no leídos por el destinatario. `pending` es la cola; `delivery_error` señala el último fallo del mensaje pendiente más antiguo. `healthy` solo indica progreso del servicio en los últimos dos minutos: no demuestra entrega. La confirmación debe aparecer en tu móvil.

Si caduca el enlace, detén solo el servicio de avisos, genera otro y vuelve a arrancarlo. Una vinculación ya completada no se sobrescribe:

```bash
docker compose stop telegram
docker compose run --rm --no-deps telegram pair
docker compose --profile telegram up -d --no-deps telegram
```

## Funcionamiento y límites

- Comprueba el registro persistente de posiciones cada cinco segundos, sujeto a latencia de Telegram. Envía como máximo un mensaje por ciclo y respeta `retry_after`, con espera creciente ante errores. Las reglas de entrada y salida siguen evaluándose al ritmo del simulador, no de Telegram.
- Conserva aperturas/cierres incluso si ocurren entre dos consultas o durante un reinicio del servicio. Al vincular, incluye posiciones abiertas y operaciones desde la creación del enlace, sin reenviar todos los cierres antiguos. Las incidencias de valoración son cambios observados por sondeo; una incidencia que aparezca y se resuelva entre dos consultas puede no verse.
- `notifications.sqlite3` contiene la vinculación, cambios observados y una cola duradera. Un fallo de Telegram no bloquea el escáner ni modifica la cartera. No se borra un mensaje hasta obtener confirmación de la API. Una caída después del envío y antes de guardar su confirmación puede duplicar el aviso: la misma **Referencia** identifica el mismo evento. No se promete entrega exactamente una vez.
- Se alerta si un servicio está detenido o sin progreso reciente (15 minutos); se dejan tres minutos iniciales tras vincular para su arranque. Esto no verifica la calidad de las APIs ni la rentabilidad. Si cae todo el VPS, su red o el propio servicio de avisos, no puede mandar una alerta en ese momento; haría falta un monitor externo.
- El bot manda mensajes solo al chat vinculado; no responde a otros usuarios. Rechaza tokens de otro bot al reiniciar. Nunca registra el token ni el texto completo de errores HTTP, cuya URL podría contenerlo. Los mensajes no habilitan HTML/Markdown ni vistas previas de enlaces.
- El perfil es opcional. Para arrancar todo tras una parada voluntaria, usa `docker compose --profile telegram up -d`; para detener los tres servicios, `docker compose --profile telegram stop`. Un reinicio de Docker recupera los contenedores habilitados mediante `unless-stopped`.

Para rotar el token, genéralo en BotFather, repite el paso de escritura oculta en `.env.telegram` y ejecuta `docker compose --profile telegram up -d --no-deps --force-recreate telegram`. La vinculación se mantiene si es el mismo bot. Nunca imprimas `docker compose config` sin `--quiet`.

## Copia de la cola

La copia de cartera de `server.py backup` no incluye la base de avisos. Con el servicio parado, SQLite cerrado y sin copiar archivos WAL activos:

```bash
docker compose stop telegram
mkdir -p backups
docker compose cp telegram:/data/notifications.sqlite3 ./backups/notifications.sqlite3
chmod 600 ./backups/notifications.sqlite3
docker compose --profile telegram up -d --no-deps telegram
```

Conserva esa copia de manera privada. Una restauración antigua puede reenviar avisos que ya se entregaron después de la copia. No sustituyas la base de un servicio en marcha.

Validación añadida: 12 pruebas sin red para vinculación, caducidad, rechazo de grupos/repeticiones, cola atómica, reinicios, aperturas/cierres entre consultas, avisos por cambio, espera de errores 429 y redacción de errores. La suite completa suma 138 pruebas.

Referencias: [Bot API](https://core.telegram.org/bots/api#sendmessage), [vinculación mediante enlaces](https://core.telegram.org/bots/features#deep-linking).

## Tres perfiles (v0.7.0)

Al activar el plan se envía una confirmación única con las asignaciones de 600/300/100 USDC virtuales. Cada apertura, cierre o incidencia identifica Conservador, Equilibrado o Agresivo; las referencias son distintas por cartera. La vinculación existente y la cola persisten al actualizar. Los avisos se generan a partir de la contabilidad de simulación, sin operar una wallet.
