"""Observaciones futuras del mismo par. No simula fills ni inventa precios históricos."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import statistics
import time
from pathlib import Path
from urllib.parse import quote

HORIZONS = (1, 6, 24)


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, observed_at REAL NOT NULL,
                chain TEXT NOT NULL, token TEXT NOT NULL, pair TEXT,
                price REAL, eligible INTEGER NOT NULL, payload TEXT NOT NULL,
                UNIQUE(run_id, chain, token)
            );
            CREATE TABLE IF NOT EXISTS outcomes (
                observation_id INTEGER NOT NULL REFERENCES observations(id),
                horizon INTEGER NOT NULL, due_at REAL NOT NULL, deadline REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', checked_at REAL,
                price REAL, liquidity REAL, return_pct REAL, error TEXT,
                PRIMARY KEY(observation_id, horizon)
            );
            CREATE INDEX IF NOT EXISTS due_outcomes ON outcomes(status, due_at);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def record(self, row, run_id):
        from memecoin_scanner import number
        observed_at = dt.datetime.fromisoformat(row["scanned_at"].replace("Z", "+00:00")).timestamp()
        price = number(row.get("price_usd"), 0)
        with self.db:
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO observations
                   (run_id, observed_at, chain, token, pair, price, eligible, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, observed_at, row["chain"], row["base_address"], row.get("pair_address"),
                 price, int(row["quality_pass"]), json.dumps(row, ensure_ascii=False, allow_nan=False)))
            if not cursor.rowcount:
                return
            for horizon in HORIZONS:
                due = observed_at + horizon * 3600
                # Tolerancia explícita: 20% del horizonte. Se conserva la hora real.
                deadline = due + horizon * 3600 * 0.2
                status = "pending" if price and row.get("pair_address") else "untrackable"
                self.db.execute("""INSERT INTO outcomes
                    (observation_id, horizon, due_at, deadline, status) VALUES (?, ?, ?, ?, ?)""",
                    (cursor.lastrowid, horizon, due, deadline, status))

    def evaluate_due(self, fetch, now=None):
        from memecoin_scanner import DEX_BASE, number, obj, same_address
        current_time = (lambda: now) if now is not None else time.time
        tasks = self.db.execute("""SELECT o.*, t.horizon, t.deadline FROM outcomes t
            JOIN observations o ON o.id=t.observation_id
            WHERE t.status='pending' AND t.due_at<=? ORDER BY t.deadline""", (current_time(),)).fetchall()
        cache = {}
        for task in tasks:
            checked = current_time()
            price = liquidity = result = error = None
            if checked > task["deadline"]:
                status = "missed"
                error = "No hubo consulta dentro de la ventana; no se usa el precio de hoy como histórico."
            else:
                key = (task["chain"], task["pair"])
                if key not in cache:
                    try:
                        response = fetch(f"{DEX_BASE}/latest/dex/pairs/{quote(key[0], safe='')}/{quote(key[1], safe='')}", quiet_404=True)
                        cache[key] = (response, None, current_time())
                    except RuntimeError as exc:
                        cache[key] = (None, str(exc), current_time())
                data, error, checked = cache[key]
                if checked > task["deadline"]:
                    status = "missed"
                    error = "La respuesta llegó fuera de la ventana de observación."
                elif error:
                    # Conservar pending para reintentar durante la ventana.
                    status = "pending"
                else:
                    pairs = obj(data).get("pairs")
                    if data is not None and not isinstance(pairs, list):
                        status, error = "pending", "Formato de pares inesperado"
                    else:
                        pair = next((p for p in (pairs or []) if isinstance(p, dict)
                                     and p.get("chainId") == task["chain"]
                                     and same_address(task["chain"], p.get("pairAddress"), task["pair"])
                                     and same_address(task["chain"], obj(p.get("baseToken")).get("address"), task["token"])), None)
                        if pair is None:
                            status, error = "unavailable", "Par original no disponible"
                        else:
                            price = number(pair.get("priceUsd"), 0)
                            liquidity = number(obj(pair.get("liquidity")).get("usd"), 0)
                            if not price or liquidity is None or liquidity <= 0:
                                status, error = "unavailable", "Precio o liquidez ausentes/cero; salida no verificable"
                            else:
                                result = (price / task["price"] - 1) * 100
                                if number(result) is None:
                                    result = None
                                    status, error = "unavailable", "Retorno no finito"
                                else:
                                    status = "observed"
            with self.db:
                self.db.execute("""UPDATE outcomes SET status=?, checked_at=?, price=?, liquidity=?,
                    return_pct=?, error=? WHERE observation_id=? AND horizon=? AND status='pending'""",
                    (status, checked, price, liquidity, result, error, task["id"], task["horizon"]))

    def report(self, now=None):
        now = time.time() if now is None else now
        rows = self.db.execute("""SELECT t.*, o.eligible, o.chain, o.token, o.payload FROM outcomes t
                                JOIN observations o ON o.id=t.observation_id""").fetchall()
        groups = []
        # Separar políticas/versiones: cambiar umbrales no debe mezclar cohortes silenciosamente.
        buckets = {}
        for row in rows:
            payload = json.loads(row["payload"])
            cohort = json.dumps({"version": payload.get("scanner_version"), "policy": payload.get("policy"),
                                 "enhanced_policy": payload.get("enhanced_policy"),
                                 "baseline_v04_policy": payload.get("baseline_v04_policy"),
                                 "sizes_usdc": sorted(q['amount_usdc'] for q in payload.get('exit_quotes', []))},
                                sort_keys=True)
            buckets.setdefault((cohort, row["horizon"], row["eligible"]), []).append(row)
        for (cohort, horizon, eligible), entries in sorted(buckets.items()):
            counts = {}
            returns = []
            for row in entries:
                status = row["status"]
                if status == "pending" and now > row["deadline"]:
                    status = "missed"
                counts[status] = counts.get(status, 0) + 1
                if status == "observed":
                    returns.append(row["return_pct"])
            due = sum(row["due_at"] <= now for row in entries)
            groups.append({
                **json.loads(cohort), "horizon_hours": horizon, "quality_pass": bool(eligible),
                "observations": len(entries), "unique_tokens": len({(r["chain"], r["token"]) for r in entries}),
                "due_observations": due, "status_counts": counts,
                "coverage_of_due_pct": round(100 * len(returns) / due, 2) if due else None,
                "median_gross_return_pct_observed_only": statistics.median(returns) if returns else None,
                "positive_pct_observed_only": 100 * sum(r > 0 for r in returns) / len(returns) if returns else None,
                "worst_gross_return_pct_observed_only": min(returns) if returns else None,
            })
        return {"groups": groups, "limitations": [
            "Variaciones brutas de precios indicativos del mismo par, sin comisiones, slippage ni ejecución.",
            "Los casos unavailable/missed/untrackable siguen en los recuentos; no se imputan como retorno cero.",
            "Las estadísticas de retornos excluyen esos casos y pueden tener sesgo de supervivencia.",
            "Varias observaciones del mismo token no son muestras independientes; se muestra unique_tokens.",
            "Hace falta ejecutar --evaluate durante cada ventana; esto no programa tareas automáticamente.",
        ]}
