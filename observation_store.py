"""Eventos, fases, alertas y presupuestos persistentes sobre el historial v0.4."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time

from providers import timestamp
from tracking import Store


class ObservationStore(Store):
    def __init__(self, path):
        super().__init__(path)
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS tokens_v5 (
                chain TEXT NOT NULL, mint TEXT NOT NULL, first_seen REAL NOT NULL,
                last_seen REAL NOT NULL, created_at REAL, migrated_at REAL, first_pool_at REAL,
                last_scanned REAL, state TEXT, last_alert_at REAL, PRIMARY KEY(chain,mint)
            );
            CREATE TABLE IF NOT EXISTS events_v5 (
                event_id TEXT PRIMARY KEY, source TEXT NOT NULL, chain TEXT NOT NULL,
                mint TEXT NOT NULL, kind TEXT NOT NULL, received_at REAL NOT NULL,
                event_at REAL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stream_gaps_v5 (
                id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
                reason TEXT NOT NULL, recovered INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS alerts_v5 (
                id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL REFERENCES observations(id),
                chain TEXT NOT NULL, mint TEXT NOT NULL, kind TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, payload TEXT NOT NULL,
                UNIQUE(observation_id,kind)
            );
            CREATE TABLE IF NOT EXISTS usage_v5 (
                provider TEXT NOT NULL, day TEXT NOT NULL, calls INTEGER NOT NULL,
                PRIMARY KEY(provider,day)
            );
            CREATE TABLE IF NOT EXISTS provider_pacing (
                provider TEXT PRIMARY KEY, next_at REAL NOT NULL DEFAULT 0,
                cooldown_until REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS exit_outcomes_v5 (
                observation_id INTEGER NOT NULL REFERENCES observations(id), horizon INTEGER NOT NULL,
                size_usdc REAL NOT NULL, due_at REAL NOT NULL, deadline REAL NOT NULL,
                status TEXT NOT NULL, checked_at REAL, quoted_return_pct REAL,
                stressed_return_pct REAL, payload TEXT, PRIMARY KEY(observation_id,horizon,size_usdc)
            );
            CREATE INDEX IF NOT EXISTS obs_token_time ON observations(chain,token,observed_at);
        ''')

    def reserve_call(self, provider, limit, now=None, min_interval=0):
        """Devuelve espera sin consumir cuota, o 0 al reservar una llamada ahora."""
        now = time.time() if now is None else now
        day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
        with self.db:
            # La primera escritura serializa las comprobaciones entre procesos.
            self.db.execute('INSERT OR IGNORE INTO provider_pacing(provider) VALUES (?)', (provider,))
            pace = self.db.execute('SELECT * FROM provider_pacing WHERE provider=?', (provider,)).fetchone()
            if pace['cooldown_until'] > now:
                raise RuntimeError(provider + ': pausa indicada por el proveedor; reintentar más tarde')
            if min_interval and pace['next_at'] > now:
                return pace['next_at'] - now
            self.db.execute('INSERT OR IGNORE INTO usage_v5 VALUES (?,?,0)', (provider, day))
            changed = self.db.execute('UPDATE usage_v5 SET calls=calls+1 WHERE provider=? AND day=? AND calls<?',
                                      (provider, day, limit)).rowcount
            if not changed:
                raise RuntimeError(provider + ': presupuesto diario de solicitudes agotado')
            self.db.execute('UPDATE provider_pacing SET next_at=? WHERE provider=?', (now+min_interval, provider))
        return 0

    def set_provider_cooldown(self, provider, until):
        with self.db:
            self.db.execute('''INSERT INTO provider_pacing(provider,cooldown_until) VALUES (?,?)
                ON CONFLICT(provider) DO UPDATE SET cooldown_until=MAX(cooldown_until,excluded.cooldown_until)''',
                (provider, until))

    def note_token(self, chain, mint, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('''INSERT INTO tokens_v5 (chain,mint,first_seen,last_seen) VALUES (?,?,?,?)
                ON CONFLICT(chain,mint) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen)''',
                (chain, mint, now, now))

    def ingest_event(self, event):
        """Entrada ya normalizada. ID estable por firma/tipo/mint; recepción nunca sustituye blockTime."""
        raw = json.dumps(event['payload'], sort_keys=True, ensure_ascii=False, allow_nan=False)
        signature = event.get('signature')
        identity = signature if signature else hashlib.sha256(raw.encode()).hexdigest()
        event_id = ':'.join([event['source'], event['kind'], event['mint'], identity])
        now = event['received_at']
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO events_v5 VALUES (?,?,?,?,?,?,?,?)',
                (event_id, event['source'], 'solana', event['mint'], event['kind'], now,
                 event.get('event_at'), raw)).rowcount
            if not inserted:
                return False
            self.db.execute('''INSERT INTO tokens_v5 (chain,mint,first_seen,last_seen) VALUES ('solana',?,?,?)
                ON CONFLICT(chain,mint) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen)''',
                (event['mint'], now, now))
            # Sin blockTime se conserva solo recepción y tipo; no inventar una fecha de creación.
            if event.get('event_at') is not None:
                column = 'created_at' if event['kind'] == 'create' else 'migrated_at'
                self.db.execute(f'''UPDATE tokens_v5 SET {column}=CASE WHEN {column} IS NULL THEN ?
                    ELSE MIN({column},?) END WHERE chain='solana' AND mint=?''',
                    (event['event_at'], event['event_at'], event['mint']))
        return True

    def lifecycle(self, row, organic, now):
        chain, mint = row['chain'], row['base_address']
        self.note_token(chain, mint, now)
        first_pool = organic.get('first_pool_at') if organic.get('status') == 'ok' else None
        age = row.get('age_hours')
        if age is not None and age >= 0:
            pair_at = row.get('pair_created_at')
            if pair_at is None:
                pair_at = (timestamp(row.get('scanned_at')) or now) - age * 3600
            first_pool = min(first_pool, pair_at) if first_pool is not None else pair_at
        if first_pool is not None and first_pool <= now:
            with self.db:
                self.db.execute('''UPDATE tokens_v5 SET first_pool_at=CASE WHEN first_pool_at IS NULL THEN ?
                    ELSE MIN(first_pool_at,?) END WHERE chain=? AND mint=?''', (first_pool, first_pool, chain, mint))
        token = dict(self.db.execute('SELECT * FROM tokens_v5 WHERE chain=? AND mint=?', (chain, mint)).fetchone())
        migration_event = self.db.execute("SELECT received_at FROM events_v5 WHERE chain=? AND mint=? AND kind='migrate' ORDER BY received_at LIMIT 1", (chain, mint)).fetchone()
        creation_event = self.db.execute("SELECT received_at FROM events_v5 WHERE chain=? AND mint=? AND kind='create' ORDER BY received_at LIMIT 1", (chain, mint)).fetchone()
        if row.get('dex') == 'pumpfun':
            phase = 'bonding_curve'
        elif migration_event is not None:
            # Si no hay hora on-chain, describir migración observada, no edad real.
            migrated = token['migrated_at'] if token['migrated_at'] is not None else migration_event['received_at']
            phase = 'recent_migration' if now - migrated < 3600 else 'established'
        elif row.get('pair_address'):
            phase = 'new_pool' if token['first_pool_at'] is not None and now - token['first_pool_at'] < 1800 else 'established'
        elif creation_event is not None:
            phase = 'detected'
        else:
            phase = 'unknown'
        return {'phase': phase, 'first_seen': token['first_seen'], 'created_at': token['created_at'],
                'migrated_at': token['migrated_at'], 'first_pool_at': token['first_pool_at'],
                'migration_observed': migration_event is not None,
                'token_age_hours': (now - token['created_at']) / 3600 if token['created_at'] else None}

    def history(self, chain, mint, before, seconds=3600):
        entries = self.db.execute('''SELECT payload FROM observations WHERE chain=? AND token=?
            AND observed_at<? AND observed_at>=? ORDER BY observed_at DESC LIMIT 120''',
            (chain, mint, before, before - seconds)).fetchall()
        return [json.loads(item['payload']) for item in entries]

    def is_due(self, chain, mint, now=None, interval=60):
        now = time.time() if now is None else now
        row = self.db.execute('SELECT last_scanned,state FROM tokens_v5 WHERE chain=? AND mint=?', (chain,mint)).fetchone()
        if row is None or row['last_scanned'] is None:
            return True
        pause = max(interval, 900 if row['state'] == 'rejected' else (300 if row['state'] == 'insufficient_data' else interval))
        return row['last_scanned'] <= now - pause

    def due_tokens(self, chain, limit, now=None, interval=60, max_watch_hours=72, prioritize_observing=False):
        now = time.time() if now is None else now
        priority = "CASE WHEN state IN ('candidate','observing') THEN 0 ELSE 1 END," if prioritize_observing else ''
        entries = self.db.execute(f"""SELECT mint FROM tokens_v5 WHERE chain=? AND first_seen>=?
            AND (last_scanned IS NULL OR last_scanned<=? - CASE WHEN state='rejected' THEN ?
                 WHEN state='insufficient_data' THEN ? ELSE ? END)
            ORDER BY {priority} COALESCE(last_scanned,0), first_seen LIMIT ?""",
            (chain, now - max_watch_hours * 3600, now, max(interval,900), max(interval,300), interval, limit)).fetchall()
        return [r['mint'] for r in entries]

    def record_enriched(self, row, run_id, alert_ttl=300):
        from memecoin_scanner import number
        from tracking import HORIZONS
        now = timestamp(row['scanned_at'])
        chain, mint, state = row['chain'], row['base_address'], row['state']
        # Observación, evaluaciones, estado y alertas en una única transacción.
        with self.db:
            cursor = self.db.execute("""INSERT OR IGNORE INTO observations
                (run_id,observed_at,chain,token,pair,price,eligible,payload) VALUES (?,?,?,?,?,?,?,?)""",
                (run_id, now, chain, mint, row.get('pair_address'), number(row.get('price_usd'), 0),
                 int(row['quality_pass']), json.dumps(row, ensure_ascii=False, allow_nan=False)))
            if not cursor.rowcount:
                return self.db.execute('SELECT id FROM observations WHERE run_id=? AND chain=? AND token=?',
                                       (run_id, chain, mint)).fetchone()['id']
            oid = cursor.lastrowid
            for horizon in HORIZONS:
                due = now + horizon * 3600
                status = 'pending' if row.get('price_usd') and row.get('pair_address') else 'untrackable'
                self.db.execute('INSERT INTO outcomes (observation_id,horizon,due_at,deadline,status) VALUES (?,?,?,?,?)',
                                (oid, horizon, due, due + horizon * 720, status))
            self.db.execute("""INSERT INTO tokens_v5 (chain,mint,first_seen,last_seen) VALUES (?,?,?,?)
                ON CONFLICT(chain,mint) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen)""", (chain, mint, now, now))
            previous = self.db.execute('SELECT state,last_alert_at FROM tokens_v5 WHERE chain=? AND mint=?', (chain, mint)).fetchone()
            kind = None
            if state == 'candidate' and (previous['state'] != 'candidate' or previous['last_alert_at'] is None
                                          or now - previous['last_alert_at'] >= alert_ttl):
                kind = 'candidate'
            elif previous['state'] == 'candidate' and state != 'candidate':
                kind = 'invalidated'
            if state != 'candidate':
                self.db.execute('UPDATE alerts_v5 SET expires_at=MIN(expires_at,?) WHERE chain=? AND mint=? AND kind=?',
                                (now, chain, mint, 'candidate'))
            if kind:
                payload = {'state': state, 'symbol': row.get('base_symbol'), 'mint': mint,
                           'reasons': row['decision_reasons'], 'scores': row['dimensions'],
                           'evidence': row.get('selection_evidence', []),
                           'invalidates_if': row.get('invalidates_if', []),
                           'expires_at': now + alert_ttl, 'observation_id': oid}
                self.db.execute("""INSERT INTO alerts_v5
                    (observation_id,chain,mint,kind,created_at,expires_at,payload) VALUES (?,?,?,?,?,?,?)""",
                    (oid, chain, mint, kind, now, now + alert_ttl, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            self.db.execute("""UPDATE tokens_v5 SET last_scanned=?,state=?,last_alert_at=CASE WHEN ? IS NOT NULL
                 THEN ? ELSE last_alert_at END WHERE chain=? AND mint=?""", (now, state, kind, now, chain, mint))
            for quote in row.get('exit_quotes', []):
                initial = 'pending' if quote['status'] == 'quoted' else 'untrackable'
                for horizon in HORIZONS:
                    due = now + horizon * 3600
                    self.db.execute('INSERT INTO exit_outcomes_v5 VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL)',
                                    (oid, horizon, quote['amount_usdc'], due, due + horizon * 720, initial))
        return oid

    def alerts(self, now=None, limit=50):
        now = time.time() if now is None else now
        rows = self.db.execute('SELECT * FROM alerts_v5 ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [{**dict(r), 'payload': json.loads(r['payload']), 'active': r['expires_at'] > now} for r in rows]

    def open_gap(self, reason, now=None):
        now = time.time() if now is None else now
        with self.db:
            return self.db.execute('INSERT INTO stream_gaps_v5(started_at,reason) VALUES (?,?)', (now, reason)).lastrowid

    def close_gap(self, gap_id, now=None):
        with self.db:
            self.db.execute('UPDATE stream_gaps_v5 SET ended_at=? WHERE id=?', (time.time() if now is None else now, gap_id))

    def evaluate_exits(self, jupiter, now=None, max_tasks=None):
        clock = (lambda: now) if now is not None else time.time
        tasks = self.db.execute('''SELECT e.*,o.token,o.payload AS entry FROM exit_outcomes_v5 e
            JOIN observations o ON o.id=e.observation_id WHERE e.status='pending' AND e.due_at<=?
            AND (e.checked_at IS NULL OR e.checked_at<=? OR e.deadline<?) ORDER BY e.deadline LIMIT ?''',
            (clock(), clock() - 60, clock(), -1 if max_tasks is None else max_tasks)).fetchall()
        for task in tasks:
            checked, status, gross, stressed, detail = clock(), 'pending', None, None, {}
            if checked > task['deadline']:
                status = 'missed'
            elif not jupiter.enabled:
                detail = {'reason': 'Jupiter no configurado; reintentar dentro de ventana'}
            else:
                entry = json.loads(task['entry'])
                initial = next((q for q in entry.get('exit_quotes', []) if q['amount_usdc'] == task['size_usdc'] and q['status'] == 'quoted'), None)
                if initial is None:
                    status = 'untrackable'
                else:
                    try:
                        from providers import USDC
                        quote = jupiter.quote(task['token'], USDC, initial['buy']['out_amount'])
                        checked = clock()
                        if checked > task['deadline']:
                            status = 'missed'
                        else:
                            returned = int(quote['out_amount']) / 1e6
                            gross = (returned / task['size_usdc'] - 1) * 100
                            policy = entry['enhanced_policy']
                            stressed = ((returned * (1 - policy['exit_stress_bps'] / 10000)
                                         - policy['fixed_cost_usdc']) / task['size_usdc'] - 1) * 100
                            detail, status = {'quote': quote, 'entry_pool': entry.get('pair_address'),
                                              'exit_scope': 'token; ruta puede cambiar',
                                              'assumed_stress_bps': policy['exit_stress_bps'],
                                              'assumed_fixed_cost_usdc': policy['fixed_cost_usdc']}, 'quoted'
                    except RuntimeError as exc:
                        # Una ausencia de ruta puede ser temporal; conservar hasta el plazo.
                        detail = {'reason': str(exc)}
                        checked = clock()
                        if checked > task['deadline']:
                            status = 'missed'
            with self.db:
                self.db.execute('''UPDATE exit_outcomes_v5 SET status=?,checked_at=?,quoted_return_pct=?,
                    stressed_return_pct=?,payload=? WHERE observation_id=? AND horizon=? AND size_usdc=? AND status='pending' ''',
                    (status, checked, gross, stressed, json.dumps(detail), task['observation_id'], task['horizon'], task['size_usdc']))

    def report_enhanced(self, now=None):
        import statistics
        now = time.time() if now is None else now
        data = self.db.execute('''SELECT e.*,o.payload AS entry FROM exit_outcomes_v5 e
            JOIN observations o ON o.id=e.observation_id''').fetchall()
        buckets = {}
        for row in data:
            entry = json.loads(row['entry'])
            policy = json.dumps({'version': entry.get('scanner_version'), 'market': entry['policy'],
                                 'profile_plan_hash': entry.get('profile_plan_hash'),
                                 'enhanced': entry['enhanced_policy'],
                                 'sizes_usdc': sorted(q['amount_usdc'] for q in entry.get('exit_quotes', [])),
                                 'baseline_v04': entry.get('baseline_v04_policy')}, sort_keys=True)
            selectors = [('v05', entry['quality_pass']), ('v04_baseline', entry.get('baseline_v04_pass', False)),
                         ('liquidity_activity_baseline', entry.get('simple_baseline_pass', False))]
            selectors.extend((ident, result['quality_pass']) for ident, result in entry.get('profiles', {}).items()
                             if any(q['amount_usdc'] == row['size_usdc'] for q in result['exit_quotes']))
            for name, selected in selectors:
                key = (policy, name, bool(selected), row['horizon'], row['size_usdc'])
                buckets.setdefault(key, []).append(row)
        groups = []
        for (policy, name, selected, horizon, size), rows in sorted(buckets.items()):
            states = {}
            returns = []
            due = 0
            for r in rows:
                status = 'missed' if r['status'] == 'pending' and r['deadline'] < now else r['status']
                states[status] = states.get(status, 0) + 1
                due += r['due_at'] <= now
                if status == 'quoted':
                    returns.append(r['stressed_return_pct'])
            groups.append({'policy': json.loads(policy), 'selector': name, 'selected': selected,
                           'horizon_hours': horizon, 'size_usdc': size, 'observations': len(rows),
                           'unique_tokens': len({json.loads(r['entry'])['base_address'] for r in rows}),
                           'due': due, 'states': states, 'coverage_pct': len(returns) / due * 100 if due else None,
                           'median_stressed_return_pct_observed_only': statistics.median(returns) if returns else None})
        return {'exit_quote_comparisons': groups, 'active_alerts': self.alerts(now),
                'api_usage': [dict(r) for r in self.db.execute('SELECT * FROM usage_v5 ORDER BY day DESC,provider LIMIT 20')],
                'stream_gaps': [dict(r) for r in self.db.execute('SELECT * FROM stream_gaps_v5 ORDER BY id DESC LIMIT 20')],
                'limitations': ['Cotizaciones indicativas, no operaciones ni ejecución garantizada.',
                    'Retorno estresado usa costes asumidos, no costes reales medidos.',
                    'Comparaciones sobre el mismo universo observado; no elimina sesgo de descubrimiento.',
                    'Casos no observables permanecen en estados; las medianas excluyen esos casos.',
                    'Huecos del stream se registran; PumpPortal no ofrece aquí replay verificado.']}
