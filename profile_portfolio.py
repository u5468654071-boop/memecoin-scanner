"""Carteras ficticias separadas con control de exposición en una transacción compartida."""
import datetime as dt
import json
import time
from dataclasses import asdict

from paper_trading import PaperLedger, PaperPolicy, micro_usdc
from profile_plan import ProfilePlan


def active_plan(store):
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='portfolio_config'").fetchone():
        return None
    row = store.db.execute('SELECT plan FROM portfolio_config WHERE id=1').fetchone()
    return ProfilePlan(json.loads(row[0])) if row else None


class ProfilePortfolio:
    def __init__(self, store, plan):
        self.store, self.db, self.plan = store, store.db, plan
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS portfolio_config (
                id INTEGER PRIMARY KEY CHECK(id=1), created_at REAL NOT NULL, plan TEXT NOT NULL, digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profile_metrics (
                profile TEXT PRIMARY KEY, peak_micro INTEGER NOT NULL, max_drawdown_pct REAL NOT NULL DEFAULT 0,
                priced_samples INTEGER NOT NULL DEFAULT 0, unpriced_samples INTEGER NOT NULL DEFAULT 0,
                last_at REAL
            );
        ''')
        self.ledgers = {p['id']: PaperLedger(store, profile_id=p['id'], plan_hash=plan.digest) for p in plan.profiles}
        with self.db:
            # Escritura primero: serializa inicialización, comparación de plan y presupuestos.
            self.db.execute('UPDATE portfolio_config SET digest=digest WHERE id=1')
            current = self.db.execute('SELECT * FROM portfolio_config WHERE id=1').fetchone()
            if current and current['digest'] != plan.digest:
                raise ValueError('El plan de estas carteras ya está fijado; conserva profiles.json o crea otro experimento')
            if not current:
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_account'").fetchone():
                    old = self.db.execute('SELECT * FROM paper_account WHERE id=1').fetchone()
                    if old and (old['cash_micro'] != old['initial_micro']
                                or old['initial_micro'] != micro_usdc(plan.data['total_initial_usdc'])
                                or self.db.execute('SELECT 1 FROM paper_positions LIMIT 1').fetchone()):
                        raise ValueError('La cartera anterior tiene actividad o presupuesto distinto; migración automática bloqueada')
                self.db.execute('INSERT INTO portfolio_config VALUES (1,?,?,?)', (time.time(), plan.encoded, plan.digest))
            for profile in plan.profiles:
                ledger = self.ledgers[profile['id']]
                policy = PaperPolicy(**profile['paper']).validate()
                encoded = json.dumps(asdict(policy), sort_keys=True)
                self.db.execute(f'INSERT OR IGNORE INTO {ledger.account_table} VALUES (1,?,?,?,?)',
                                (time.time(), micro_usdc(policy.initial_usdc), micro_usdc(policy.initial_usdc), encoded))
                if self.db.execute(f'SELECT policy FROM {ledger.account_table} WHERE id=1').fetchone()[0] != encoded:
                    raise ValueError('La política persistida del perfil no coincide')
                self.db.execute('INSERT OR IGNORE INTO profile_metrics(profile,peak_micro) VALUES (?,?)',
                                (profile['id'], micro_usdc(policy.initial_usdc)))
                ledger.policy = policy

    def open_positions(self):
        return [{**dict(pos), 'profile': ident} for ident, ledger in self.ledgers.items() for pos in ledger.open_positions()]

    def losses(self, now):
        day = dt.datetime.fromtimestamp(now, dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        realized = sum(self.db.execute(f'SELECT COALESCE(SUM(-MIN(pnl_micro,0)),0) FROM {ledger.positions_table} WHERE closed_at>=?',
                                      (day,)).fetchone()[0] for ledger in self.ledgers.values())
        floating = sum(max(0, pos['cost_micro']-(pos['mark_micro'] or 0)) for pos in self.open_positions())
        return realized+floating

    def global_blockers(self, now):
        reasons = []
        if self.losses(now) >= micro_usdc(self.plan.data['daily_loss_limit_usdc']):
            reasons.append('límite conjunto de pérdidas ficticias del día')
        for pos in self.open_positions():
            policy = self.ledgers[pos['profile']].policy
            if (pos['last_error'] or pos['exit_reason'] or pos['mark_at'] is None
                    or not 0 <= now-pos['mark_at'] <= policy.mark_max_age_seconds):
                reasons.append('posición pendiente de salida o valoración en alguna cartera')
                break
        if sum(p['cost_micro'] for p in self.open_positions()) >= micro_usdc(self.plan.data['max_total_exposure_usdc']):
            reasons.append('límite conjunto de exposición')
        return reasons

    def allows(self, ident, mint, now):
        if self.global_blockers(now):
            return False
        policy = self.ledgers[ident].policy
        cost = micro_usdc(policy.order_usdc)+micro_usdc(policy.fixed_cost_usdc_per_side)
        positions = self.open_positions()
        return (sum(p['cost_micro'] for p in positions)+cost <= micro_usdc(self.plan.data['max_total_exposure_usdc'])
                and sum(p['cost_micro'] for p in positions if p['mint'] == mint)+cost <= micro_usdc(self.plan.data['max_token_exposure_usdc']))

    def tick(self, jupiter, clock=time.time, paused=False, close_all=False):
        # Todas las salidas tienen prioridad sobre cualquier entrada de los tres perfiles.
        for ledger in self.ledgers.values():
            ledger.refresh_positions(jupiter, clock, close_all)
        def blocked():
            return ((paused() if callable(paused) else paused)
                    or (close_all() if callable(close_all) else close_all))
        for ident, ledger in self.ledgers.items():
            ledger.enter_positions(jupiter, clock, blocked,
                                   lambda mint, now, ident=ident: self.allows(ident, mint, now))
        now = clock()
        with self.db:
            for ident, ledger in self.ledgers.items():
                report = ledger.report(now)
                metrics = self.db.execute('SELECT * FROM profile_metrics WHERE profile=?', (ident,)).fetchone()
                if report['equity_usdc'] is None:
                    self.db.execute('UPDATE profile_metrics SET unpriced_samples=unpriced_samples+1,last_at=? WHERE profile=?', (now, ident))
                    continue
                equity = micro_usdc(report['equity_usdc'])
                peak = max(metrics['peak_micro'], equity)
                drawdown = max(metrics['max_drawdown_pct'], 100*(1-equity/peak) if peak else 0)
                self.db.execute('''UPDATE profile_metrics SET peak_micro=?,max_drawdown_pct=?,
                    priced_samples=priced_samples+1,last_at=? WHERE profile=?''', (peak, drawdown, now, ident))
        return self.report(now, blocked)

    def report(self, now=None, paused=False):
        now = time.time() if now is None else now
        profiles = {}
        for spec in self.plan.profiles:
            ident, ledger = spec['id'], self.ledgers[spec['id']]
            result = ledger.report(now, paused)
            stats = self.db.execute(f'''SELECT count(*),COALESCE(SUM(pnl_micro>0),0),
                COALESCE(SUM(MAX(pnl_micro,0)),0),COALESCE(SUM(-MIN(pnl_micro,0)),0)
                FROM {ledger.positions_table} WHERE state='closed' ''').fetchone()
            metrics = self.db.execute('SELECT * FROM profile_metrics WHERE profile=?', (ident,)).fetchone()
            all_positions = self.db.execute(f'SELECT count(*) FROM {ledger.positions_table}').fetchone()[0]
            result.update(profile=ident, label=spec['label'], realized_return_pct=result['realized_pnl_usdc']/result['initial_usdc']*100,
                          equity_return_pct=None if result['equity_usdc'] is None else (result['equity_usdc']/result['initial_usdc']-1)*100,
                          win_rate_pct=100*stats[1]/stats[0] if stats[0] else None,
                          profit_factor=stats[2]/stats[3] if stats[3] else None,
                          max_observed_drawdown_pct=metrics['max_drawdown_pct'],
                          unpriced_samples=metrics['unpriced_samples'],
                          assumed_fixed_costs_usdc=(all_positions+stats[0])*ledger.policy.fixed_cost_usdc_per_side)
            profiles[ident] = result
        reports = list(profiles.values())
        return {'mode': 'paper_only', 'initialized': True, 'as_of': now, 'profile_plan_hash': self.plan.digest,
                'profiles': profiles, 'initial_usdc': self.plan.data['total_initial_usdc'],
                'cash_usdc': sum(r['cash_usdc'] for r in reports),
                'equity_usdc': sum(r['equity_usdc'] for r in reports) if all(r['equity_usdc'] is not None for r in reports) else None,
                'realized_pnl_usdc': sum(r['realized_pnl_usdc'] for r in reports),
                'closed_positions': sum(r['closed_positions'] for r in reports),
                'open_positions': self.open_positions(), 'entry_blockers': self.global_blockers(now)
                    + (['entradas pausadas manualmente'] if (paused() if callable(paused) else paused) else []),
                'global_limits': {k:v for k,v in self.plan.data.items() if k not in ('profiles', 'schema')},
                'limitations': ['Solo simulación; filtros experimentales sin rentabilidad demostrada.',
                    'Drawdown de valoraciones observadas, incompleto entre consultas o cuando falta precio.',
                    'Los límites se revisan al consultar; no garantizan la pérdida máxima ni ejecución.',
                    'Perfiles sobre el mismo universo; los límites conjuntos pueden impedir entradas coincidentes.']}
