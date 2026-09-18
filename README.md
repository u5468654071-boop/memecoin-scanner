# Escáner de riesgo de memecoins — v0.2

## Qué es esto (y qué no es)

Después de ver esa lista de vídeos de YouTube sobre "bots de IA que hacen
trading de memecoins", esto es la versión honesta de lo que realmente se
puede construir con datos públicos y verificables:

- **Es** una herramienta que yo mismo ejecuto: busca candidatos, consulta
  datos de mercado y riesgo on-chain real, y puntúa. Tú no tienes que buscar
  nada a mano ni dar de alta ninguna cuenta para la versión actual.
- **No es** un bot que ejecuta operaciones. No toca ninguna wallet, no
  gestiona claves privadas, no coloca ni una sola orden. Eso es intencional:
  no voy a operar con tu capital ni custodiar tus claves.
- **No es** una máquina de predecir qué memecoin va a multiplicarse. El
  score mide señales de manipulación/trampa conocidas, no potencial de
  subida. Un score bajo no significa "cómpralo" — solo que no se detectaron
  las señales concretas que este script sabe buscar.

## Qué cambia en la v0.2

Antes solo miraba datos de mercado (DexScreener). Ahora combino dos fuentes
reales, las dos gratis y sin API key:

1. **DexScreener** — descubrimiento de candidatos (tokens "boosted" +
   perfiles recientes, dos conjuntos distintos) y datos de mercado: liquidez,
   FDV, volumen, edad del par, ratio compras/ventas.
2. **RugCheck** (`api.rugcheck.xyz`) — riesgo **on-chain verificado**, no
   heurística de mercado: si el creador tiene historial de rugs previos, si
   la propiedad está concentrada en pocas wallets, si la liquidez está
   realmente bloqueada (`lpLockedPct`), metadata mutable, etc.

Los dos scores se combinan en `combined_score`, y cada fila lleva el detalle
de qué se detectó exactamente (`risk_flags`), para que nunca sea una caja
negra.

### Lo que encontré en la última ejecución en vivo (Solana, 31 tokens analizados)

Los tres casos más claros, con datos reales del momento del escaneo:

- **2Trucks1Pu / MPGA** — RugCheck marca "**Creator history of rugged
  tokens**" (danger): la wallet creadora ya ha hecho rug pull antes en otros
  tokens. Liquidez reportada en $0 pese a volumen de miles de dólares.
- **LWAINZ** — "Top 10 holders high ownership", "Single holder ownership" y
  "Low Liquidity", los tres en nivel danger: la propiedad está concentrada en
  muy pocas manos, patrón clásico previo a un rug.
- **SLABSY / LAMA** — mismo aviso de creador con historial de rugs, en
  tokens con algo más de liquidez y de vida.

Y varios sin ninguna señal detectada por ahora (SATS, TCAT, STEVE, ANT,
$ROKHA, BAG, juicelee) — lo cual **no es una recomendación de compra**, solo
significa que no dispararon ninguna de las alarmas que el script conoce.

Todo esto está en `scan_log.csv`, con la dirección real de cada token para
que puedas verificarlo tú mismo en rugcheck.xyz o dexscreener.com si quieres.

## Limitación que no voy a maquillar

No puedo leer directamente el firehose de lanzamientos nuevos de pump.fun
(`frontend-api.pump.fun`): su Cloudflare bloquea las IPs de datacenter desde
las que yo opero (lo comprobé, error 1016). Por eso el descubrimiento usa
DexScreener (boosted + perfiles) en vez de "cada moneda que se crea en
tiempo real", que es lo que muestran los vídeos más agresivos. Alternativas
reales si quieres ese nivel de velocidad: un feed de pago (Bitquery, Helius
webhooks, websocket de PumpPortal) contratado por ti, o correr esto desde tu
propia IP residencial en vez de la mía.

## Cómo se ejecuta

```bash
python3 memecoin_scanner.py --chain solana --candidates boosted,profiles --limit 20
python3 memecoin_scanner.py --chain solana --tokens <direccion1>,<direccion2>

# Filtro de calidad: descarta lo que no cumple unos mínimos (edad, liquidez,
# sin avisos "danger" de RugCheck, LP bloqueada, sin patrón de pump/wash trading).
# Esto NO predice rendimiento futuro — solo descarta lo peor conocido.
python3 memecoin_scanner.py --chain solana --candidates boosted,profiles --limit 30 \
    --quality-filter --max-age-hours 48 --min-liquidity 3000
```

### Sobre el filtro de calidad

Pasar el filtro significa: creado hace menos de X horas, con liquidez mínima
real, sin ningún aviso "danger" de RugCheck (mint/freeze authority, creador
con historial de rugs, holders concentrados...), con al menos la mitad de la
liquidez bloqueada, y sin el patrón de mercado de "pump vertical + pocas
transacciones" que suele preceder a un rug.

Lo que **no** significa: que vaya a subir, que sea buena inversión, ni que
esté libre de riesgo. Un informe independiente (recogido por CoinDesk en
2025) estimó que el 98% de los tokens de pump.fun terminan siendo rug pulls
o algún tipo de fraude — pump.fun lo discutió, pero incluso tomando esa
cifra con pinzas, la base de partida en este nicho es que casi todo muere o
es un timo. Pasar un filtro de "no tiene las peores señales conocidas" no
cambia esa base de partida, solo descarta los casos más obvios.

## El verdadero "backtest" es correr esto en el tiempo

No voy a inventar un histórico falso de "qué memecoin habría explotado". Lo
honesto, con la disciplina que ya aplicaste en betlab/valorbet, es:

1. Yo sigo corriendo este escáner periódicamente y `scan_log.csv` va
   acumulando filas con timestamp y dirección real de cada token.
2. Dentro de unas semanas, cruzamos cada fila con el precio real que tuvo
   después ese token, y comprobamos si el combined_score alto de verdad se
   correlaciona con rugs/caídas — o si es ruido.
3. Solo si esa correlación aparece de forma consistente (y sobrevive a
   limpiar los datos, como en tu artículo del margen negativo) tendría
   sentido plantearse automatizar avisos en serio.

## Próximos pasos posibles

- Seguir corriéndolo yo periódicamente y traerte un resumen (puedo
  programarlo como tarea recurrente).
- Ampliar la cobertura de descubrimiento con otra chain (Base, Ethereum) o
  con búsquedas por palabra clave en DexScreener.
- Conectarlo como una fuente más dentro de TradeLog Elite, como pestaña de
  "candidatos observados" en vez de operaciones reales.

Dime si quieres que programe esto para que lo siga corriendo yo solo cada
cierto tiempo, o si con ejecutarlo cuando lo pidas es suficiente.
