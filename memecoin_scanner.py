#!/usr/bin/env python3
"""Escáner de observación v0.4. Sin wallets ni órdenes. Python 3.9+, sin dependencias."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import random
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = "0.4.0"
DEX_BASE = "https://api.dexscreener.com"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
LOG_PATH = Path("data/scan_log_v04.csv")
DB_PATH = Path("data/scanner.sqlite3")
MAX_RESPONSE_BYTES = 8_000_000
# Direcciones, nunca símbolos: un token malicioso también puede llamarse USDC.
QUOTE_MINTS = {
    "solana": {
        "So11111111111111111111111111111111111111112",
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    }
}


def number(value, minimum=None, maximum=None):
    """Los datos ausentes, booleanos, NaN e infinitos nunca son ceros válidos."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(result):
        return None
    if minimum is not None and result < minimum:
        return None
    if maximum is not None and result > maximum:
        return None
    return result


def obj(value):
    return value if isinstance(value, dict) else {}


def utc_string(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat(timespec="seconds")


def same_address(chain, a, b):
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return a == b if chain == "solana" else a.lower() == b.lower()


def valid_address(chain, address):
    pattern = r"[1-9A-HJ-NP-Za-km-z]{32,44}" if chain == "solana" else r"0x[0-9a-fA-F]{40}"
    return isinstance(address, str) and re.fullmatch(pattern, address) is not None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class APIClient:
    """Solo hosts conocidos; ritmo conservador y reintentos acotados."""
    def __init__(self, timeout=12, retries=3, interval=1.1):
        self.timeout, self.retries, self.interval = timeout, retries, interval
        self.last_request = {}
        self.opener = build_opener(NoRedirect())

    def get(self, url, quiet_404=False):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in
                {"api.dexscreener.com", "api.rugcheck.xyz"} or parsed.username
                or parsed.password or parsed.port not in (None, 443)):
            raise RuntimeError("URL de API no permitida")
        last_error = None
        for attempt in range(self.retries):
            wait = self.interval - (time.monotonic() - self.last_request.get(parsed.hostname, -1e9))
            if wait > 0:
                time.sleep(wait)
            self.last_request[parsed.hostname] = time.monotonic()
            retry_after = None
            try:
                request = Request(url, headers={"User-Agent": f"memecoin-scanner/{VERSION}",
                                               "Accept": "application/json"})
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("Respuesta de API demasiado grande")
                return json.loads(raw.decode("utf-8"))
            except HTTPError as error:
                if error.code == 404 and quiet_404:
                    return None
                last_error = f"HTTP {error.code}"
                if error.code not in (408, 429, 500, 502, 503, 504):
                    break
                header = error.headers.get("Retry-After") if error.headers else None
                retry_after = number(header, minimum=0)
                if header and retry_after is None:
                    try:
                        retry_after = max(0, parsedate_to_datetime(header).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
            except (URLError, TimeoutError, OSError, ValueError) as error:
                last_error = type(error).__name__
            if attempt + 1 < self.retries:
                # No reintentar antes de un Retry-After largo; propagar fallo temporal.
                if retry_after is not None and retry_after > 30:
                    break
                time.sleep(max(retry_after or 0, min(2 ** attempt + random.random(), 8)))
        raise RuntimeError(f"{parsed.hostname}{parsed.path}: {last_error}")


CLIENT = APIClient()


def http_get(url, quiet_404=False):
    return CLIENT.get(url, quiet_404=quiet_404)


CANDIDATE_SOURCES = {"boosted": "/token-boosts/latest/v1", "profiles": "/token-profiles/latest/v1"}


def gather_candidates(chain, sources, limit_per_source, queries=()):
    """Mezcla fuentes por turnos y conserva su procedencia; no puntúa promoción."""
    groups, errors = [], []
    requests = [(name, DEX_BASE + CANDIDATE_SOURCES[name]) for name in sources]
    requests += [("search:" + query, DEX_BASE + "/latest/dex/search?" + urlencode({"q": query}))
                 for query in queries]
    for name, url in requests:
        try:
            data = http_get(url)
            if name.startswith("search:"):
                data = obj(data).get("pairs")
            if not isinstance(data, list):
                raise RuntimeError("formato de candidatos inesperado")
            addresses = []
            for item in data:
                item = obj(item)
                address = (obj(item.get("baseToken")).get("address") if name.startswith("search:")
                           else item.get("tokenAddress"))
                if item.get("chainId") == chain and valid_address(chain, address):
                    if address not in addresses:
                        addresses.append(address)
            groups.append((name, addresses[:limit_per_source]))
        except RuntimeError as error:
            errors.append(f"{name}: {error}")
    provenance = {}
    for index in range(limit_per_source):
        for name, addresses in groups:
            if index < len(addresses):
                provenance.setdefault(addresses[index], []).append(name)
    return provenance, errors


def get_pairs_for_token(chain, token_address):
    data = http_get(f"{DEX_BASE}/token-pairs/v1/{quote(chain, safe='')}/{quote(token_address, safe='')}")
    if not isinstance(data, list):
        raise RuntimeError("formato de pares inesperado")
    return data


def select_pair(pairs, chain, token_address):
    # priceUsd y transacciones describen baseToken, nunca el token quote solicitado.
    matches = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == chain
               and same_address(chain, obj(p.get("baseToken")).get("address"), token_address)
               and isinstance(p.get("pairAddress"), str) and p["pairAddress"]]
    if not matches:
        return None
    trusted = [p for p in matches if obj(p.get("quoteToken")).get("address") in QUOTE_MINTS.get(chain, set())]
    return max(trusted or matches, key=lambda p: (
        number(obj(p.get("liquidity")).get("usd"), 0) or 0,
        number(obj(p.get("volume")).get("h1"), 0) or 0,
        p["pairAddress"],
    ))


def score_community(pair):
    """Metadatos declarados, sin abrir URLs controladas por los promotores."""
    info = obj(pair.get("info"))
    websites = info.get("websites") if isinstance(info.get("websites"), list) else []
    socials = info.get("socials") if isinstance(info.get("socials"), list) else []
    return {"num_websites_declared": len(websites), "num_socials_declared": len(socials),
            "community_notes": "Enlaces declarados; comunidad y usuarios no verificados. No afectan al ranking."}


def score_market(pair, now=None):
    now = time.time() if now is None else now
    liquidity = number(obj(pair.get("liquidity")).get("usd"), 0)
    price = number(pair.get("priceUsd"), 0)
    price = price if price and price > 0 else None
    created = number(pair.get("pairCreatedAt"), 1)
    age = (now - created / 1000) / 3600 if created is not None else None
    if age is not None and age < 0:
        age = None
    volume = obj(pair.get("volume"))
    change = obj(pair.get("priceChange"))
    txns = obj(pair.get("txns"))
    result = {
        "pair_address": pair.get("pairAddress"), "chain": pair.get("chainId"),
        "dex": pair.get("dexId"), "base_symbol": obj(pair.get("baseToken")).get("symbol"),
        "base_address": obj(pair.get("baseToken")).get("address"),
        "quote_address": obj(pair.get("quoteToken")).get("address"),
        "price_usd": price, "liquidity_usd": liquidity, "liquidity_known": liquidity is not None,
        "fdv": number(pair.get("fdv"), 0), "market_cap": number(pair.get("marketCap"), 0),
        "age_hours": age, "pair_created_at": created / 1000 if created is not None and age is not None else None,
    }
    for window in ("m5", "h1", "h6", "h24"):
        result["volume_" + window] = number(volume.get(window), 0)
        result["price_change_" + window] = number(change.get(window))
        for side in ("buys", "sells"):
            value = number(obj(txns.get(window)).get(side), 0, 1e12)
            result[side + "_" + window] = value if value is not None and value.is_integer() else None
    flags, score = [], 0
    if liquidity is None:
        flags.append("liquidez desconocida")
    elif liquidity < 5000:
        score += 3
        flags.append("liquidez muy baja")
    elif liquidity < 20000:
        score += 1
        flags.append("liquidez baja")
    fdv = result["fdv"]
    result["fdv_liquidity_ratio"] = number(fdv / liquidity, 0) if fdv and liquidity else None
    if result["fdv_liquidity_ratio"] is not None and result["fdv_liquidity_ratio"] > 50:
        score += 3 if result["fdv_liquidity_ratio"] > 200 else 1
        flags.append("FDV elevado frente a liquidez")
    buys, sells = result["buys_h24"], result["sells_h24"]
    if buys is not None and sells is not None:
        total = buys + sells
        if (result["volume_h24"] or 0) > 50000 and total < 20:
            score += 2
            flags.append("volumen elevado con pocas transacciones; posible manipulación")
        if total > 30 and sells / total > 0.75:
            score += 2
            flags.append("predominio extremo de ventas")
    if (result["price_change_h1"] or 0) > 100:
        score += 3
        flags.append("subida superior al 100% en una hora")
    if (result["price_change_h1"] or 0) < -40:
        score += 3
        flags.append("caída superior al 40% en una hora")
    result.update(market_score=score, market_flags=flags)
    return result


def get_rugcheck_summary(mint):
    return http_get(f"{RUGCHECK_BASE}/tokens/{quote(mint, safe='')}/report/summary", quiet_404=True)


def score_rugcheck(summary):
    result = {"rugcheck_status": "unavailable", "rugcheck_score_0_100": None,
              "rugcheck_flags": [], "lp_locked_pct": None, "has_danger_flag": None}
    if not isinstance(summary, dict) or summary.get("error"):
        return result
    risks = summary.get("risks")
    score = number(summary.get("score_normalised"), 0, 100)
    lp = number(summary.get("lpLockedPct"), 0, 100)
    if not isinstance(risks, list) or any(not isinstance(r, dict) or not isinstance(r.get("name"), str)
                                         or not r["name"].strip() or not isinstance(r.get("level"), str)
                                         or r["level"].lower() not in ("danger", "warn", "warning", "info")
                                         for r in risks):
        result["rugcheck_status"] = "invalid"
        return result
    result.update(
        rugcheck_status="ok" if score is not None and lp is not None else "incomplete",
        rugcheck_score_0_100=score, lp_locked_pct=lp,
        has_danger_flag=any(r["level"].lower() == "danger" for r in risks),
        rugcheck_flags=[f"{r['name']} ({r['level']})" for r in risks],
    )
    return result


@dataclass(frozen=True)
class Policy:
    min_age_hours: float = 0.5
    max_age_hours: float = 48
    min_liquidity: float = 20000
    min_lp_locked_pct: float = 80
    max_rugcheck_score: float = 30
    min_txns_h1: int = 30
    min_sells_h1: int = 5
    min_volume_h1: float = 1000
    max_fdv_liquidity: float = 100
    max_volume_liquidity_h1: float = 10
    max_price_change_h1: float = 100
    min_price_change_h1: float = -30
    max_market_score: int = 4


def quality_checks(row, policy):
    """Distinguir ausencia, umbral incumplido y edad pendiente en cada comprobación."""
    checks = []
    def add(code, reason, missing=False, waiting=False):
        checks.append({'code': code, 'reason': reason,
                       'status': 'missing' if missing else ('waiting' if waiting else 'blocked')})
    if row.get("analysis_status") != "ok":
        add('market_data', "análisis de mercado incompleto", True)
    if row.get("chain") != "solana":
        add('chain', "validación on-chain solo disponible para Solana", True)
    if row.get("quote_address") not in QUOTE_MINTS.get(row.get("chain"), set()):
        add('quote_asset', "activo de cotización no admitido", not row.get('quote_address'))
    age = number(row.get("age_hours"), 0)
    if age is None or not policy.min_age_hours <= age <= policy.max_age_hours:
        add('age', "edad del par desconocida o fuera del intervalo", age is None,
            age is not None and age < policy.min_age_hours)
    liq = number(row.get("liquidity_usd"), 0)
    if liq is None or liq < policy.min_liquidity or liq == 0:
        add('liquidity', "liquidez desconocida o insuficiente", liq is None)
    price = number(row.get("price_usd"), 0)
    if price is None or price == 0:
        add('price', "precio desconocido o inválido", price is None)
    if row.get("rugcheck_status") != "ok":
        add('rugcheck', "RugCheck no disponible o incompleto", True)
    if row.get("has_danger_flag") is not False:
        add('danger', "riesgos danger presentes o sin verificar", row.get('has_danger_flag') is not True)
    lp = number(row.get("lp_locked_pct"), 0, 100)
    if lp is None or lp < policy.min_lp_locked_pct:
        add('lp', "porcentaje de LP bloqueada desconocido o insuficiente", lp is None)
    rug_score = number(row.get("rugcheck_score_0_100"), 0, 100)
    if rug_score is None or rug_score > policy.max_rugcheck_score:
        add('rug_score', "score de RugCheck desconocido o elevado", rug_score is None)
    buys, sells = number(row.get("buys_h1"), 0), number(row.get("sells_h1"), 0)
    if buys is None or sells is None or buys + sells <= 0 or buys + sells < policy.min_txns_h1:
        add('transactions', "actividad de una hora desconocida o insuficiente", buys is None or sells is None)
    if sells is None or sells < policy.min_sells_h1:
        add('sells', "pocas ventas observadas; no prueba que se pueda vender", sells is None)
    if buys is not None and sells is not None and buys + sells > 0:
        ratio = buys / (buys + sells)
        if not 0.2 <= ratio <= 0.9:
            add('flow_balance', "desequilibrio extremo de compras/ventas")
    volume = number(row.get("volume_h1"), 0)
    if volume is None or volume < policy.min_volume_h1:
        add('volume', "volumen de una hora desconocido o insuficiente", volume is None)
    if liq and volume is not None and volume / liq > policy.max_volume_liquidity_h1:
        add('turnover', "rotación de volumen extrema; posible manipulación")
    fdv_ratio = number(row.get("fdv_liquidity_ratio"), 0)
    if fdv_ratio is None or fdv_ratio > policy.max_fdv_liquidity:
        add('fdv_liquidity', "FDV/liquidez desconocido o excesivo", fdv_ratio is None)
    change = number(row.get("price_change_h1"))
    if change is None or not policy.min_price_change_h1 <= change <= policy.max_price_change_h1:
        add('price_change', "variación de precio desconocida o extrema", change is None)
    market_score = number(row.get("market_score"), 0)
    if market_score is None or market_score > policy.max_market_score:
        add('market_risk', "múltiples señales de riesgo de mercado", market_score is None)
    return checks


def quality_reasons(row, policy):
    return [check['reason'] for check in quality_checks(row, policy)]


def passes_quality_filter(row, max_age_hours=48, min_liquidity=20000):
    reasons = quality_reasons(row, Policy(max_age_hours=max_age_hours, min_liquidity=min_liquidity))
    return not reasons, reasons


def rank_candidate(row):
    """Prioridad de investigación 0–100, sin calibrar; nunca probabilidad ni rentabilidad."""
    if not row.get("quality_pass"):
        return None, {}
    buys, sells = row["buys_h1"], row["sells_h1"]
    components = {
        "liquidity": 30 * min(1, math.log10(1 + row["liquidity_usd"] / 20000) / math.log10(26)),
        "activity": 20 * min(1, math.log10(1 + buys + sells) / 3),
        "two_sided_flow": 15 * (1 - abs(buys - sells) / (buys + sells)),
        "rugcheck": 20 * (1 - row["rugcheck_score_0_100"] / 100),
        "lp_reported": 15 * row["lp_locked_pct"] / 100,
    }
    return round(sum(components.values()), 2), {k: round(v, 2) for k, v in components.items()}


def analyze_token(chain, token_address, policy=None, sources=(), now=None):
    policy = policy or Policy()
    row = {"scanner_version": VERSION, "chain": chain, "base_address": token_address,
           "base_symbol": None, "sources": list(sources), "analysis_status": "unavailable",
           "error": None}
    try:
        pair = select_pair(get_pairs_for_token(chain, token_address), chain, token_address)
        if pair is None:
            row["error"] = "sin par donde el token solicitado sea baseToken"
        else:
            row.update(score_market(pair, now=now))
            row.update(score_community(pair))
            row["analysis_status"] = "ok"
            row["url"] = f"https://dexscreener.com/{quote(chain, safe='')}/{quote(row['pair_address'], safe='')}"
    except RuntimeError as error:
        row["error"] = str(error)
    rug = score_rugcheck(None)
    if chain == "solana" and row["analysis_status"] == "ok":
        try:
            rug = score_rugcheck(get_rugcheck_summary(token_address))
        except RuntimeError as error:
            row["error"] = str(error)
            rug["rugcheck_status"] = "error"
    elif chain != "solana":
        rug["rugcheck_status"] = "unsupported"
    row.update(rug)
    row["risk_flags"] = row.get("market_flags", []) + row.get("rugcheck_flags", [])
    # Mantener el score antiguo para comparar, pero no convertir ausencia en riesgo cero.
    row["combined_score"] = (round(row["market_score"] + row["rugcheck_score_0_100"] / 8, 2)
                             if row["analysis_status"] == "ok" and rug["rugcheck_status"] == "ok" else None)
    row["quality_fail_reasons"] = quality_reasons(row, policy)
    row["quality_pass"] = not row["quality_fail_reasons"]
    row["research_score"], row["score_components"] = rank_candidate(row)
    row["policy"] = asdict(policy)
    row["scanned_at"] = utc_string(time.time() if now is None else now)
    return row


CSV_FIELDS = ["scanner_version", "scanned_at", "chain", "base_address", "base_symbol", "pair_address",
              "sources", "analysis_status", "error", "price_usd", "liquidity_usd", "age_hours",
              "fdv", "volume_h1", "buys_h1", "sells_h1", "price_change_h1", "rugcheck_status",
              "rugcheck_score_0_100", "lp_locked_pct", "has_danger_flag", "combined_score",
              "quality_pass", "quality_fail_reasons", "state", "lifecycle", "dimensions", "decision_checks", "research_score", "score_components", "risk_flags", "policy", "url"]


def csv_cell(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    # Evitar fórmulas al abrir símbolos/nombres externos en una hoja de cálculo.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def log_results(rows, path=None):
    if not rows:
        return
    path = Path(path or LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        with path.open(newline="", encoding="utf-8") as handle:
            old_header = next(csv.reader(handle), [])
        if old_header != CSV_FIELDS:
            archive = path.with_name(f"{path.stem}_schema_{uuid.uuid4().hex}{path.suffix}")
            path.rename(archive)
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows({key: csv_cell(row.get(key)) for key in CSV_FIELDS} for row in rows)


def terminal_text(value):
    return "".join(c if c.isprintable() else "?" for c in str(value))


def legacy_main(argv=None):
    ap = argparse.ArgumentParser(description="Escáner de observación: riesgo, selección explicable y seguimiento sin operar")
    ap.add_argument("--chain", choices=("solana", "base", "ethereum"), default="solana")
    ap.add_argument("--tokens", help="Direcciones separadas por coma")
    ap.add_argument("--candidates", default="boosted,profiles", help="boosted,profiles; cadena vacía para solo búsquedas")
    ap.add_argument("--search", action="append", default=[], help="Consulta DexScreener; se puede repetir")
    ap.add_argument("--limit", type=int, default=20, help="Máximo por fuente")
    ap.add_argument("--max-tokens", type=int, default=60, help="Máximo total por ejecución")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--quality-filter", action="store_true", help="Compatible con v0.3; el filtro ahora siempre está activo")
    for name, default in asdict(Policy()).items():
        ap.add_argument("--" + name.replace("_", "-"), type=type(default), default=default)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--log", type=Path, default=LOG_PATH)
    ap.add_argument("--json-output", type=Path)
    ap.add_argument("--evaluate", action="store_true", help="Consulta observaciones vencidas a 1/6/24h y muestra cobertura")
    ap.add_argument("--report", action="store_true", help="Resumen del seguimiento guardado; sin red")
    args = ap.parse_args(argv)
    if not 1 <= args.limit <= 100 or not 1 <= args.max_tokens <= 500 or args.top < 1:
        ap.error("limit 1–100, max-tokens 1–500 y top >= 1")
    policy = Policy(**{name: getattr(args, name) for name in asdict(Policy())})
    if any(not math.isfinite(v) for v in asdict(policy).values()):
        ap.error("los umbrales deben ser finitos")
    if any(v < 0 for k, v in asdict(policy).items() if k != "min_price_change_h1"):
        ap.error("umbrales negativos no permitidos salvo min-price-change-h1")
    if (policy.min_age_hours > policy.max_age_hours or policy.min_lp_locked_pct > 100
            or policy.max_rugcheck_score > 100 or policy.min_liquidity <= 0
            or policy.min_price_change_h1 > policy.max_price_change_h1):
        ap.error("intervalos de la política inválidos")
    from tracking import Store
    with Store(args.db) as store:
        if args.evaluate or args.report:
            if args.evaluate:
                store.evaluate_due(http_get)
            print(json.dumps(store.report(), ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        sources = list(dict.fromkeys(s.strip() for s in args.candidates.split(",") if s.strip()))
        if any(s not in CANDIDATE_SOURCES for s in sources):
            ap.error("fuentes admitidas: boosted,profiles")
        errors = []
        if args.tokens is not None:
            tokens = list(dict.fromkeys(t.strip() for t in args.tokens.split(",") if t.strip()))
            if not tokens or any(not valid_address(args.chain, t) for t in tokens):
                ap.error("dirección de token vacía o inválida para la cadena")
            provenance = {t: ["manual"] for t in tokens}
        else:
            if not sources and not args.search:
                ap.error("selecciona fuentes, búsquedas o tokens")
            provenance, errors = gather_candidates(args.chain, sources, args.limit, args.search)
        for error in errors:
            print("Fuente no disponible: " + terminal_text(error), file=sys.stderr)
        run_id = uuid.uuid4().hex
        rows = []
        for address, source_names in list(provenance.items())[:args.max_tokens]:
            print("Analizando " + address, file=sys.stderr)
            row = analyze_token(args.chain, address, policy, source_names)
            store.record(row, run_id)
            rows.append(row)
        rows.sort(key=lambda row: (not row["quality_pass"], -(row["research_score"] or 0), row["base_address"]))
        log_results(rows, args.log)
        result = {"scanner_version": VERSION, "run_id": run_id, "source_errors": errors,
                  "policy": asdict(policy), "rows": rows}
        if args.json_output:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        survivors = [row for row in rows if row["quality_pass"]]
        print(f"\n{len(survivors)}/{len(rows)} candidatos pasan los filtros. Prioridad de investigación, NO probabilidad de subida.")
        for row in survivors[:args.top]:
            print(f"{terminal_text(row['base_symbol'])[:20]:20} {row['research_score']:6.2f}/100 "
                  f"liquidez ${row['liquidity_usd']:,.0f} | {row['url']}")
        for row in rows:
            if not row["quality_pass"]:
                print(f"DESCARTADO {terminal_text(row.get('base_symbol') or row['base_address'])}: "
                      + "; ".join(row["quality_fail_reasons"]))
        print(f"Historial: {args.db} | CSV: {args.log}")
        if not rows or all(row["analysis_status"] != "ok" for row in rows):
            print("Sin datos de mercado utilizables.", file=sys.stderr)
            return 2
        if args.chain == "solana" and all(row["rugcheck_status"] != "ok" for row in rows):
            print("Sin cobertura completa de RugCheck; ningún candidato validado.", file=sys.stderr)
            return 2
        return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--legacy" in argv:
        argv.remove("--legacy")
        return legacy_main(argv)
    from scanner_v05 import main as enhanced_main
    return enhanced_main(argv)


if __name__ == "__main__":
    sys.exit(main())
