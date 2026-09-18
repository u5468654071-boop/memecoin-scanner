#!/usr/bin/env python3
"""
memecoin_scanner.py — v0.2

Escáner de RIESGO para memecoins. Combina:
  - DexScreener (datos de mercado: liquidez, volumen, FDV, edad, txns) — gratis, sin key.
  - RugCheck (riesgo on-chain real en Solana: mint/freeze authority, historial del
    creador, % de liquidez bloqueada, holders) — gratis, sin key, verificado en vivo.

IMPORTANTE — qué es y qué NO es esto:
  - Es una herramienta de solo lectura. No coloca órdenes, no gestiona claves
    privadas, no se conecta a ningún exchange ni wallet. Yo (Claude) hago el
    descubrimiento y la puntuación; tú decides si actúas o no con esa info.
  - El "combined_score" es una heurística transparente y editable, NO una
    predicción de precio ni una recomendación de compra/venta.
  - Un score bajo no significa "es seguro comprar". Solo significa que no se
    detectaron las señales de riesgo concretas que este script sabe buscar.
  - pump.fun (frontend-api.pump.fun) bloquea IPs de datacenter con Cloudflare,
    así que no se puede leer desde aquí; por eso el descubrimiento usa
    DexScreener (boosted + profiles), no el firehose de lanzamientos nuevos.

Uso:
    python memecoin_scanner.py --chain solana --candidates boosted,profiles --limit 25
    python memecoin_scanner.py --chain solana --tokens <direccion1>,<direccion2>

Cada ejecución añade filas a scan_log.csv con timestamp, para poder revisar
más adelante qué pasó realmente con los tokens marcados de alto riesgo — ese
es el backtest de verdad, no las cifras sin auditar de un vídeo de YouTube.
"""
import argparse
import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

DEX_BASE = "https://api.dexscreener.com"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
LOG_PATH = Path(__file__).parent / "scan_log.csv"


def http_get(url, retries=3, timeout=15, quiet_404=False):
    last_err = None
    for _ in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "memecoin-scanner/0.2"})
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 404 and quiet_404:
                return None
            last_err = e
            time.sleep(1.0)
        except URLError as e:
            last_err = e
            time.sleep(1.0)
    raise RuntimeError(f"Fallo al consultar {url}: {last_err}")


# ---------------------------------------------------------------------------
# Descubrimiento de candidatos (DexScreener)
# ---------------------------------------------------------------------------

def get_boosted_candidates(chain: str, limit: int):
    """Tokens actualmente 'boosteados' (promoción PAGADA) en DexScreener.
    Un boost no es señal de calidad — es publicidad. Se usa solo como fuente
    de candidatos a auditar."""
    data = http_get(f"{DEX_BASE}/token-boosts/latest/v1")
    return [
        item["tokenAddress"] for item in data
        if item.get("chainId") == chain
    ][:limit]


def get_profile_candidates(chain: str, limit: int):
    """Tokens que han enviado un 'perfil' a DexScreener recientemente. También
    es autopromoción (el proyecto paga/rellena su ficha), pero es un conjunto
    distinto al de 'boosted' y amplía la cobertura de descubrimiento."""
    data = http_get(f"{DEX_BASE}/token-profiles/latest/v1")
    return [
        item["tokenAddress"] for item in data
        if item.get("chainId") == chain
    ][:limit]


CANDIDATE_SOURCES = {
    "boosted": get_boosted_candidates,
    "profiles": get_profile_candidates,
}


def gather_candidates(chain: str, sources: list, limit_per_source: int):
    addresses, seen = [], set()
    for name in sources:
        fn = CANDIDATE_SOURCES[name]
        try:
            found = fn(chain, limit_per_source)
        except RuntimeError as e:
            print(f"  ! fuente '{name}' falló: {e}", file=sys.stderr)
            continue
        new = 0
        for addr in found:
            if addr not in seen:
                addresses.append(addr)
                seen.add(addr)
                new += 1
        print(f"  fuente '{name}': {len(found)} candidatos ({new} nuevos)")
    return addresses


def get_pairs_for_token(chain: str, token_address: str):
    data = http_get(f"{DEX_BASE}/token-pairs/v1/{chain}/{token_address}")
    return data or []


# ---------------------------------------------------------------------------
# "Comunidad / sistema" — presencia real, verificada, no autodeclarada
# ---------------------------------------------------------------------------

def url_resolves(url: str, timeout: int = 8) -> bool:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (memecoin-scanner/0.3)"}, method="HEAD")
        with urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (memecoin-scanner/0.3)"})
            with urlopen(req, timeout=timeout) as resp:
                return 200 <= resp.status < 400
        except Exception:
            return False


def score_community(pair: dict) -> dict:
    """Puntúa presencia verificada (no solo autodeclarada) de web/redes.
    Un enlace que no carga resta más que no tener enlace: sugiere abandono
    o que el enlace nunca fue real."""
    info = pair.get("info") or {}
    websites = info.get("websites") or []
    socials = info.get("socials") or []

    score = 0
    notes = []

    if not websites and not socials:
        notes.append("sin web ni redes enlazadas en DexScreener")
    else:
        working_site = False
        for w in websites:
            url = w.get("url") if isinstance(w, dict) else w
            if url and url_resolves(url):
                working_site = True
                notes.append(f"web verificada OK ({url})")
            elif url:
                notes.append(f"web enlazada pero NO responde ({url})")
        if websites and not working_site:
            score -= 2
        elif working_site:
            score += 2

        if socials:
            types = sorted({s.get("type", "?") for s in socials if isinstance(s, dict)})
            notes.append(f"redes enlazadas: {', '.join(types)} ({len(socials)})")
            score += min(len(socials), 2)

    return {
        "community_score": score,
        "community_notes": "; ".join(notes) if notes else "sin datos",
        "has_working_website": any(
            (w.get("url") if isinstance(w, dict) else w) and url_resolves(w.get("url") if isinstance(w, dict) else w)
            for w in websites
        ) if websites else False,
        "num_socials": len(socials),
    }


# ---------------------------------------------------------------------------
# Riesgo de mercado (DexScreener)
# ---------------------------------------------------------------------------

def score_market(pair: dict) -> dict:
    """Heurística de riesgo de MERCADO, transparente y editable. Alto = más
    señales de manipulación / trampa conocida, no más "potencial de subida"."""
    flags = []
    score = 0

    liquidity_obj = pair.get("liquidity")
    liquidity_known = liquidity_obj is not None
    liquidity_usd = (liquidity_obj or {}).get("usd") or 0
    fdv = pair.get("fdv") or 0
    volume_h24 = (pair.get("volume") or {}).get("h24") or 0
    txns_h24 = (pair.get("txns") or {}).get("h24") or {}
    buys = txns_h24.get("buys", 0) or 0
    sells = txns_h24.get("sells", 0) or 0
    created_at = pair.get("pairCreatedAt")
    price_change_h1 = (pair.get("priceChange") or {}).get("h1") or 0
    price_change_h24 = (pair.get("priceChange") or {}).get("h24") or 0

    age_hours = None
    if created_at:
        age_hours = (time.time() * 1000 - created_at) / 3_600_000

    if not liquidity_known:
        # DexScreener no reporta liquidity.usd para algunos pares (p.ej.
        # cotizados contra el propio token PUMP en vez de SOL/USDC). Esto NO
        # significa que la liquidez sea $0 — significa que este dato no está
        # disponible. Antes de la v0.3 este caso se contaba como "$0", lo cual
        # era engañoso.
        flags.append("liquidez no reportada por DexScreener (par cotizado contra un token no estándar)")
    elif liquidity_usd < 5000:
        score += 3
        flags.append(f"liquidez muy baja (${liquidity_usd:,.0f})")
    elif liquidity_usd < 20000:
        score += 1
        flags.append(f"liquidez baja (${liquidity_usd:,.0f})")

    if liquidity_known and liquidity_usd > 0 and fdv > 0:
        ratio = fdv / liquidity_usd
        if ratio > 200:
            score += 3
            flags.append(f"FDV/liquidez extremo ({ratio:.0f}x)")
        elif ratio > 50:
            score += 1
            flags.append(f"FDV/liquidez alto ({ratio:.0f}x)")

    if volume_h24 > 50000 and (buys + sells) < 20:
        score += 2
        flags.append("volumen alto con pocas transacciones (posible wash trading)")

    total_txns = buys + sells
    if total_txns > 30:
        sell_ratio = sells / total_txns
        if sell_ratio > 0.75:
            score += 2
            flags.append(f"ventas muy superiores a compras ({sell_ratio:.0%})")

    if age_hours is not None and age_hours < 24 and price_change_h1 > 100:
        score += 3
        flags.append(f"pump vertical con {age_hours:.1f}h de vida (+{price_change_h1:.0f}% en 1h)")

    return {
        "market_score": score,
        "market_flags": flags,
        "pair_address": pair.get("pairAddress"),
        "chain": pair.get("chainId"),
        "dex": pair.get("dexId"),
        "base_symbol": (pair.get("baseToken") or {}).get("symbol"),
        "base_address": (pair.get("baseToken") or {}).get("address"),
        "price_usd": pair.get("priceUsd"),
        "liquidity_usd": liquidity_usd,
        "liquidity_known": liquidity_known,
        "fdv": fdv,
        "volume_h24": volume_h24,
        "buys_h24": buys,
        "sells_h24": sells,
        "age_hours": round(age_hours, 1) if age_hours is not None else None,
        "price_change_h1": price_change_h1,
        "price_change_h24": price_change_h24,
        "url": pair.get("url"),
    }


# ---------------------------------------------------------------------------
# Riesgo on-chain real (RugCheck — solo Solana)
# ---------------------------------------------------------------------------

def get_rugcheck_summary(mint: str):
    """Reporte público de RugCheck para un mint de Solana. Sin API key.
    Devuelve None si el token no está indexado (típico en tokens recién
    creados o de otras chains)."""
    return http_get(f"{RUGCHECK_BASE}/tokens/{mint}/report/summary", quiet_404=True)


def score_rugcheck(summary: dict) -> dict:
    if not summary:
        return {"rugcheck_score": None, "rugcheck_flags": [], "lp_locked_pct": None, "has_danger": None}

    risks = summary.get("risks") or []
    danger_flags = [
        f"{r.get('name')} ({r.get('level')})"
        for r in risks
        if r.get("level") in ("danger", "warn")
    ]
    has_danger = any(r.get("level") == "danger" for r in risks)
    return {
        "rugcheck_score": summary.get("score_normalised"),
        "rugcheck_flags": danger_flags,
        "lp_locked_pct": summary.get("lpLockedPct"),
        "has_danger": has_danger,
    }


# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------

def analyze_token(chain: str, token_address: str) -> dict | None:
    try:
        pairs = get_pairs_for_token(chain, token_address)
    except RuntimeError as e:
        print(f"  ! {token_address}: {e}", file=sys.stderr)
        return None
    if not pairs:
        return None

    best_pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    market = score_market(best_pair)
    community = score_community(best_pair)

    rug = {"rugcheck_score": None, "rugcheck_flags": [], "lp_locked_pct": None, "has_danger": None}
    if chain == "solana":
        try:
            summary = get_rugcheck_summary(token_address)
            rug = score_rugcheck(summary)
        except RuntimeError as e:
            print(f"  ! RugCheck {token_address}: {e}", file=sys.stderr)
        time.sleep(0.3)  # ser educados con la API pública

    all_flags = list(market["market_flags"]) + list(rug["rugcheck_flags"])
    # RugCheck normaliza 0-100 (más alto = peor). Lo pasamos a la misma escala
    # aproximada que nuestros puntos de mercado (0-13) dividiendo entre 8.
    combined = market["market_score"]
    if rug["rugcheck_score"] is not None:
        combined += round(rug["rugcheck_score"] / 8)

    return {
        "base_symbol": market["base_symbol"],
        "base_address": market["base_address"],
        "chain": market["chain"],
        "dex": market["dex"],
        "price_usd": market["price_usd"],
        "liquidity_usd": market["liquidity_usd"],
        "liquidity_known": market["liquidity_known"],
        "fdv": market["fdv"],
        "volume_h24": market["volume_h24"],
        "age_hours": market["age_hours"],
        "price_change_h1": market["price_change_h1"],
        "market_score": market["market_score"],
        "rugcheck_score_0_100": rug["rugcheck_score"],
        "lp_locked_pct": rug["lp_locked_pct"],
        "has_danger_flag": rug["has_danger"],
        "combined_score": combined,
        "community_score": community["community_score"],
        "has_working_website": community["has_working_website"],
        "num_socials": community["num_socials"],
        "community_notes": community["community_notes"],
        "risk_flags": "; ".join(all_flags) if all_flags else "sin señales de riesgo detectadas",
        "url": market["url"],
        "scanned_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


# ---------------------------------------------------------------------------
# Filtro de "calidad técnica" (NO es una predicción de rendimiento)
# ---------------------------------------------------------------------------

def passes_quality_filter(row: dict, max_age_hours: float, min_liquidity: float) -> tuple:
    """Reglas puramente descriptivas del estado ACTUAL del token. Pasar este
    filtro no significa "va a subir" — significa "ahora mismo no muestra las
    señales de riesgo/trampa que este script sabe detectar, tiene actividad
    real y es reciente". Es un filtro de descarte, no un predictor."""
    reasons_fail = []

    age = row.get("age_hours")
    if age is None or age > max_age_hours:
        reasons_fail.append(f"edad > {max_age_hours}h")

    if (row.get("liquidity_usd") or 0) < min_liquidity:
        reasons_fail.append(f"liquidez < ${min_liquidity:,.0f}")

    if row.get("has_danger_flag"):
        reasons_fail.append("RugCheck marca al menos un riesgo 'danger'")

    lp = row.get("lp_locked_pct")
    if lp is not None and lp < 50:
        reasons_fail.append(f"solo {lp:.0f}% de liquidez bloqueada")

    if row.get("market_score", 0) >= 5:
        reasons_fail.append("varias señales de riesgo de mercado (posible pump/wash trading)")

    return (len(reasons_fail) == 0, reasons_fail)


def log_results(rows):
    """Escribe en scan_log.csv. Si el esquema de columnas cambió desde la
    última vez (p.ej. al añadir el score de comunidad en la v0.3), el fichero
    viejo se archiva en vez de seguir añadiendo filas con más columnas de las
    que dice la cabecera — eso desalineaba los datos al releerlos (bug real
    detectado y corregido el 18 sep 2026)."""
    fieldnames = list(rows[0].keys())

    if LOG_PATH.exists():
        with open(LOG_PATH, "r", newline="", encoding="utf-8") as f:
            existing_header = next(csv.reader(f), [])
        if existing_header and existing_header != fieldnames:
            archive_path = LOG_PATH.with_name(
                LOG_PATH.stem + f"_schema_hasta_{dt.date.today().isoformat()}.csv"
            )
            if not archive_path.exists():
                LOG_PATH.rename(archive_path)
                print(f"(esquema de columnas cambió: histórico anterior archivado en {archive_path.name})")

    is_new = not LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser(
        description="Escáner de riesgo de memecoins (solo lectura, sin ejecución de órdenes)"
    )
    ap.add_argument("--chain", default="solana", help="chainId de DexScreener, p.ej. solana, ethereum, base")
    ap.add_argument("--tokens", help="Direcciones de token separadas por coma (salta el descubrimiento automático)")
    ap.add_argument(
        "--candidates", default="boosted,profiles",
        help="Fuentes de descubrimiento separadas por coma: boosted,profiles",
    )
    ap.add_argument("--limit", type=int, default=20, help="Máximo de candidatos por fuente")
    ap.add_argument(
        "--quality-filter", action="store_true",
        help="Además de puntuar, aplica un filtro de descarte (edad, liquidez, riesgo on-chain). NO predice rendimiento.",
    )
    ap.add_argument("--max-age-hours", type=float, default=48, help="Edad máxima del par para el filtro de calidad")
    ap.add_argument("--min-liquidity", type=float, default=3000, help="Liquidez mínima en USD para el filtro de calidad")
    args = ap.parse_args()

    if args.tokens:
        token_addresses = [t.strip() for t in args.tokens.split(",") if t.strip()]
    else:
        sources = [s.strip() for s in args.candidates.split(",") if s.strip()]
        print(f"Buscando candidatos en {args.chain} ({', '.join(sources)})...")
        token_addresses = gather_candidates(args.chain, sources, args.limit)

    print(f"Analizando {len(token_addresses)} tokens (mercado + on-chain)...\n")

    all_rows = []
    for addr in token_addresses:
        row = analyze_token(args.chain, addr)
        if row:
            all_rows.append(row)

    if not all_rows:
        print("No se obtuvieron resultados.")
        return

    all_rows.sort(key=lambda r: r["combined_score"], reverse=True)

    print(f"{'SÍMBOLO':<10} {'SCORE':<6} {'LIQ USD':<14} {'LP LOCK':<8} {'EDAD(h)':<8} SEÑALES")
    for r in all_rows:
        liq = f"{r['liquidity_usd']:,.0f}" if r.get("liquidity_known", True) else "n/d"
        lp = f"{r['lp_locked_pct']:.0f}%" if r["lp_locked_pct"] is not None else "n/d"
        print(f"{str(r['base_symbol'])[:10]:<10} {r['combined_score']:<6} {liq:<14} {lp:<8} {str(r['age_hours']):<8} {r['risk_flags']}")

    if args.quality_filter:
        for r in all_rows:
            ok, reasons = passes_quality_filter(r, args.max_age_hours, args.min_liquidity)
            r["quality_pass"] = ok
            r["quality_fail_reasons"] = "; ".join(reasons) if reasons else ""

        survivors = [r for r in all_rows if r["quality_pass"]]
        # Entre los que sobreviven al filtro de riesgo, ordenamos por
        # "comunidad verificada" (web que responde + redes enlazadas) — no
        # por si van a subir, que eso no lo mide nadie.
        survivors.sort(key=lambda r: r["community_score"], reverse=True)

        print(
            f"Filtro de calidad (edad<{args.max_age_hours}h, liquidez>=${args.min_liquidity:,.0f}, "
            f"sin danger de RugCheck, LP bloqueada>=50%, sin patrón de pump/wash trading),\n"
            f"ordenado por señales de comunidad/sistema VERIFICADAS (no autodeclaradas):\n"
        )
        if survivors:
            print(f"{'SÍMBOLO':<10} {'LIQ USD':<12} {'EDAD(h)':<8} {'COMUNIDAD':<10} DETALLE")
            for r in survivors:
                liq = r["liquidity_usd"] or 0
                print(f"{str(r['base_symbol'])[:10]:<10} {liq:<12,.0f} {str(r['age_hours']):<8} {r['community_score']:<10} {r['community_notes']}")
                print(f"           -> {r['url']}")
        else:
            print("Ninguno de los candidatos analizados pasa el filtro ahora mismo.")

        print(f"\n({len(survivors)}/{len(all_rows)} pasan el filtro de riesgo. El orden por comunidad es solo eso —")
        print("orden por señales verificables, NO una predicción de rendimiento ni una recomendación de compra.)")

    log_results(all_rows)
    print(f"\nGuardado en {LOG_PATH} (para poder revisar más adelante qué pasó con cada uno).")


if __name__ == "__main__":
    main()
