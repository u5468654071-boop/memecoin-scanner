"""Cartera exclusivamente ficticia: contabilidad persistente, sin firmas ni órdenes."""
from __future__ import annotations

import datetime as dt
import json
import math
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from memecoin_scanner import number, valid_address
from providers import USDC, integer
from version import SCANNER_VERSION


def micro_usdc(value):
    try:
        scaled = Decimal(str(value)) * 1000000
        if not scaled.is_finite() or scaled < 0 or scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError('USDC debe ser finito, no negativo y tener como máximo seis decimales') from None


@dataclass(frozen=True)
class PaperPolicy:
    initial_usdc: float = 1000
    order_usdc: float = 25
    max_positions: int = 3
    stop_loss_pct: float = 15
    take_profit_pct: float = 30
    trailing_stop_pct: float = 10
    trailing_activation_pct: float = 15
    max_hold_seconds: int = 3600
    daily_loss_limit_usdc: float = 50
    slippage_bps: int = 50
    fixed_cost_usdc_per_side: float = 0.05
    signal_max_age_seconds: int = 60
    quote_max_age_seconds: int = 30
    mark_max_age_seconds: int = 120
    reentry_cooldown_seconds: int = 3600

    def validate(self):
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in asdict(self).values()):
            raise ValueError('La política ficticia solo admite números finitos')
        for key in ('max_positions', 'slippage_bps', 'max_hold_seconds', 'signal_max_age_seconds',
                    'quote_max_age_seconds', 'mark_max_age_seconds', 'reentry_cooldown_seconds'):
            if not isinstance(getattr(self, key), int):
                raise ValueError(key + ' debe ser un entero')
        for key in ('initial_usdc', 'order_usdc', 'daily_loss_limit_usdc', 'fixed_cost_usdc_per_side'):
            micro_usdc(getattr(self, key))
        if (not 0 < self.order_usdc <= self.initial_usdc <= 1000000
                or not 1 <= self.max_positions <= 20
                or not 0 < self.daily_loss_limit_usdc <= self.initial_usdc
                or not 0 < self.stop_loss_pct <= 100 or not 0 < self.take_profit_pct <= 10000
                or not 0 < self.trailing_stop_pct <= 100 or not 0 <= self.trailing_activation_pct <= 10000
                or not 0 <= self.slippage_bps < 10000 or self.fixed_cost_usdc_per_side < 0
                or self.order_usdc + self.fixed_cost_usdc_per_side > self.initial_usdc
                or not 1 <= self.signal_max_age_seconds <= 300
                or not 1 <= self.quote_max_age_seconds <= 30
                or not self.quote_max_age_seconds <= self.mark_max_age_seconds <= 600
                or self.max_hold_seconds < 60 or self.reentry_cooldown_seconds < 60):
            raise ValueError('Política de simulación fuera de intervalo')
        return self

    @classmethod
    def load(cls, path):
        try:
            return cls(**json.loads(path.read_text(encoding='utf-8'))).validate()
        except (TypeError, json.JSONDecodeError):
            raise ValueError('Archivo de política ficticia inválido') from None


class PaperLedger:
    def __init__(self, store, policy=None):
        self.store, self.db, self.policy = store, store.db, policy
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY CHECK(id=1), created_at REAL NOT NULL,
                initial_micro INTEGER NOT NULL, cash_micro INTEGER NOT NULL CHECK(cash_micro>=0),
                policy TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL UNIQUE REFERENCES observations(id),
                mint TEXT NOT NULL, opened_at REAL NOT NULL, closed_at REAL,
                quantity_raw TEXT NOT NULL, cost_micro INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('open','closed')),
                entry_quote TEXT NOT NULL, exit_quote TEXT,
                mark_micro INTEGER, mark_at REAL, peak_micro INTEGER,
                exit_reason TEXT, last_error TEXT, pnl_micro INTEGER
            );
            CREATE UNIQUE INDEX IF NOT EXISTS paper_one_open_mint ON paper_positions(mint) WHERE state='open';
        ''')
        if policy is not None:
            policy.validate()
            encoded = json.dumps(asdict(policy), sort_keys=True)
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO paper_account VALUES (1,?,?,?,?)',
                                (time.time(), micro_usdc(policy.initial_usdc), micro_usdc(policy.initial_usdc), encoded))
                existing = self.db.execute('SELECT policy FROM paper_account WHERE id=1').fetchone()[0]
                if existing != encoded:
                    raise ValueError('Esta cartera ya tiene otra política. Conserva el archivo original o usa otra base de datos.')

    def open_positions(self):
        return self.db.execute("SELECT * FROM paper_positions WHERE state='open' ORDER BY opened_at,id").fetchall()

    def entry_blockers(self, now, paused=False):
        reasons = ['pausa manual'] if (paused() if callable(paused) else paused) else []
        positions = self.open_positions()
        account = self.db.execute('SELECT * FROM paper_account WHERE id=1').fetchone()
        if len(positions) >= self.policy.max_positions:
            reasons.append('máximo de posiciones ficticias')
        cost = micro_usdc(self.policy.order_usdc) + micro_usdc(self.policy.fixed_cost_usdc_per_side)
        if account['cash_micro'] < cost:
            reasons.append('saldo ficticio insuficiente')
        day_start = dt.datetime.fromtimestamp(now, dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        # Las ganancias no borran pérdidas previas del día. Las pérdidas abiertas también cuentan.
        losses = self.db.execute('SELECT COALESCE(SUM(-MIN(pnl_micro,0)),0) FROM paper_positions WHERE closed_at>=?',
                                 (day_start,)).fetchone()[0]
        for pos in positions:
            if (pos['last_error'] or pos['exit_reason'] or pos['mark_at'] is None or pos['mark_at'] > now
                    or now - pos['mark_at'] > self.policy.mark_max_age_seconds):
                reasons.append('posición pendiente de salida o valoración reciente')
            if pos['mark_micro'] is not None:
                losses += max(0, pos['cost_micro'] - pos['mark_micro'])
        if losses >= micro_usdc(self.policy.daily_loss_limit_usdc):
            reasons.append('límite de pérdidas ficticias del día alcanzado')
        return list(dict.fromkeys(reasons))

    def _latest(self, mint):
        return self.db.execute('''SELECT * FROM observations WHERE chain='solana' AND token=?
            ORDER BY observed_at DESC,id DESC LIMIT 1''', (mint,)).fetchone()

    def _entry_signal(self, signal, now):
        if signal is None or not 0 <= now - signal['observed_at'] <= self.policy.signal_max_age_seconds:
            return False
        row = json.loads(signal['payload'])
        return (row.get('scanner_version') == SCANNER_VERSION and row.get('state') == 'candidate'
                and row.get('quality_pass') is True and valid_address('solana', row.get('base_address'))
                and row.get('base_address') == signal['token'] and row.get('chain') == 'solana'
                and any(q.get('status') == 'quoted' and q.get('amount_usdc') == self.policy.order_usdc
                        for q in row.get('exit_quotes', [])))

    def _quote_ok(self, quote, input_mint, output_mint, amount, now):
        if not isinstance(quote, dict) or integer(amount, 1) is None:
            return False
        received = number(quote.get('received_at'), 0)
        return (quote.get('input_mint') == input_mint and quote.get('output_mint') == output_mint
                and integer(quote.get('in_amount'), 1) == integer(amount, 1)
                and integer(quote.get('out_amount'), 1) is not None and received is not None
                and 0 <= now - received <= self.policy.quote_max_age_seconds)

    def _net_exit(self, raw):
        after_slippage = int(raw) * (10000 - self.policy.slippage_bps) // 10000
        return max(0, after_slippage - micro_usdc(self.policy.fixed_cost_usdc_per_side))

    def try_open(self, signal_id, jupiter, clock=time.time, paused=False):
        now = clock()
        signal = self.db.execute('SELECT * FROM observations WHERE id=?', (signal_id,)).fetchone()
        if not self._entry_signal(signal, now) or self.entry_blockers(now, paused) or not jupiter.enabled:
            return False
        latest = self._latest(signal['token'])
        if latest['id'] != signal_id or self.db.execute('SELECT 1 FROM paper_positions WHERE observation_id=?', (signal_id,)).fetchone():
            return False
        if self.db.execute('''SELECT 1 FROM paper_positions WHERE mint=? AND
            (state='open' OR closed_at>?)''', (signal['token'], now-self.policy.reentry_cooldown_seconds)).fetchone():
            return False
        try:
            trip = jupiter.round_trip(signal['token'], self.policy.order_usdc)
        except RuntimeError:
            return False
        now = clock()
        row = json.loads(signal['payload'])
        buy, sell = trip.get('buy', {}), trip.get('sell', {})
        limit = number(row.get('enhanced_policy', {}).get('max_round_trip_loss_pct'), 0)
        loss = number(trip.get('round_trip_loss_pct'))
        if (trip.get('status') != 'quoted' or limit is None or loss is None or loss > limit
                or not self._quote_ok(buy, USDC, signal['token'], micro_usdc(self.policy.order_usdc), now)
                or not self._quote_ok(sell, signal['token'], USDC, buy.get('out_amount'), now)):
            return False
        quantity = int(buy['out_amount']) * (10000-self.policy.slippage_bps) // 10000
        if quantity <= 0:
            return False
        cost = micro_usdc(self.policy.order_usdc) + micro_usdc(self.policy.fixed_cost_usdc_per_side)
        # Cotizar también la cantidad exacta restante tras el supuesto de slippage de entrada.
        try:
            mark = jupiter.quote(signal['token'], USDC, str(quantity))
        except RuntimeError:
            return False
        now = clock()
        if (not self._quote_ok(mark, signal['token'], USDC, quantity, now)
                or not self._quote_ok(buy, USDC, signal['token'], micro_usdc(self.policy.order_usdc), now)
                or not self._quote_ok(sell, signal['token'], USDC, buy.get('out_amount'), now)):
            return False
        value = self._net_exit(mark['out_amount'])
        with self.db:
            # Adquirir escritura antes de revalidar saldo y señal; la red queda fuera de la transacción.
            self.db.execute('UPDATE paper_account SET cash_micro=cash_micro WHERE id=1')
            if (not self._entry_signal(signal, now) or self._latest(signal['token'])['id'] != signal_id
                    or self.entry_blockers(now, paused)
                    or self.db.execute('SELECT 1 FROM paper_positions WHERE observation_id=? OR (mint=? AND state=?)',
                                       (signal_id, signal['token'], 'open')).fetchone()):
                return False
            self.db.execute('''INSERT INTO paper_positions
                (observation_id,mint,opened_at,quantity_raw,cost_micro,state,entry_quote,mark_micro,mark_at,peak_micro)
                VALUES (?,?,?,?,?,'open',?,?,?,?)''',
                (signal_id, signal['token'], now, str(quantity), cost, json.dumps(trip), value, now, value))
            self.db.execute('UPDATE paper_account SET cash_micro=cash_micro-? WHERE id=1', (cost,))
        return True

    def refresh_positions(self, jupiter, clock=time.time, close_all=False):
        for pos in self.open_positions():
            now, reason = clock(), pos['exit_reason']
            latest = self._latest(pos['mint'])
            if (close_all() if callable(close_all) else close_all):
                reason = reason or 'cierre manual solicitado'
            elif now - pos['opened_at'] >= self.policy.max_hold_seconds:
                reason = reason or 'tiempo máximo'
            elif (latest and latest['observed_at'] >= pos['opened_at']
                  and latest['observed_at'] <= now and not json.loads(latest['payload']).get('quality_pass')):
                reason = reason or 'candidato invalidado'
            try:
                if not jupiter.enabled:
                    raise RuntimeError('Jupiter no configurado')
                quote = jupiter.quote(pos['mint'], USDC, pos['quantity_raw'])
                now = clock()
                if not self._quote_ok(quote, pos['mint'], USDC, pos['quantity_raw'], now):
                    raise RuntimeError('cotización incompleta o caducada')
            except RuntimeError:
                with self.db:
                    self.db.execute('''UPDATE paper_positions SET last_error=?,exit_reason=? WHERE id=? AND state='open' ''',
                        ('sin cotización de salida; saldo permanece comprometido', reason, pos['id']))
                continue
            value = self._net_exit(quote['out_amount'])
            peak = max(pos['peak_micro'] or 0, value)
            pnl_pct = (value / pos['cost_micro'] - 1) * 100
            if pnl_pct <= -self.policy.stop_loss_pct:
                reason = reason or 'stop-loss observado'
            elif pnl_pct >= self.policy.take_profit_pct:
                reason = reason or 'objetivo observado'
            elif (peak / pos['cost_micro'] - 1) * 100 >= self.policy.trailing_activation_pct and peak > 0:
                if (1 - value / peak) * 100 >= self.policy.trailing_stop_pct:
                    reason = reason or 'retroceso desde máximo observado'
            if now - pos['opened_at'] >= self.policy.max_hold_seconds:
                reason = reason or 'tiempo máximo'
            with self.db:
                changed = self.db.execute('''UPDATE paper_positions SET mark_micro=?,mark_at=?,peak_micro=?,
                    last_error=NULL,exit_reason=? WHERE id=? AND state='open' ''',
                    (value, now, peak, reason, pos['id'])).rowcount
                if changed and reason:
                    self.db.execute('''UPDATE paper_positions SET state='closed',closed_at=?,exit_quote=?,pnl_micro=?
                        WHERE id=?''', (now, json.dumps(quote), value-pos['cost_micro'], pos['id']))
                    self.db.execute('UPDATE paper_account SET cash_micro=cash_micro+? WHERE id=1', (value,))

    def tick(self, jupiter, clock=time.time, paused=False, close_all=False):
        self.refresh_positions(jupiter, clock, close_all)
        def blocked():
            return ((paused() if callable(paused) else paused)
                    or (close_all() if callable(close_all) else close_all))
        if not self.entry_blockers(clock(), blocked):
            signals = self.db.execute('''SELECT id,payload,observed_at FROM observations WHERE chain='solana' AND eligible=1
                AND observed_at>=? ORDER BY observed_at DESC,id DESC LIMIT 100''',
                (clock()-self.policy.signal_max_age_seconds,)).fetchall()
            signals = sorted(signals, key=lambda s: (-(number(json.loads(s['payload']).get('research_score')) or 0),
                                                     -s['observed_at'], s['id']))
            for signal in signals:
                if self.entry_blockers(clock(), blocked):
                    break
                self.try_open(signal['id'], jupiter, clock, blocked)
        return self.report(clock(), blocked)

    def report(self, now=None, paused=False):
        now = time.time() if now is None else now
        account = self.db.execute('SELECT * FROM paper_account WHERE id=1').fetchone()
        if account is None:
            return {'mode': 'paper_only', 'initialized': False}
        positions = self.open_positions()
        stale = sum(p['last_error'] is not None or p['mark_at'] is None or p['mark_at'] > now
                    or now-p['mark_at'] > json.loads(account['policy'])['mark_max_age_seconds'] for p in positions)
        marks = sum(p['mark_micro'] or 0 for p in positions)
        realized = self.db.execute("SELECT COALESCE(SUM(pnl_micro),0),count(*) FROM paper_positions WHERE state='closed'").fetchone()
        result = {'mode': 'paper_only', 'initialized': True, 'as_of': now,
                  'initial_usdc': account['initial_micro']/1e6, 'cash_usdc': account['cash_micro']/1e6,
                  'committed_cost_usdc': sum(p['cost_micro'] for p in positions)/1e6,
                  'realized_pnl_usdc': realized[0]/1e6, 'closed_positions': realized[1],
                  'equity_usdc': (account['cash_micro']+marks)/1e6 if not stale else None,
                  'unpriced_or_stale_positions': stale, 'policy': json.loads(account['policy']),
                  'recent_closed': [dict(p) for p in self.db.execute('''SELECT id,mint,opened_at,closed_at,
                      cost_micro,mark_micro,pnl_micro,exit_reason FROM paper_positions WHERE state='closed'
                      ORDER BY closed_at DESC,id DESC LIMIT 20''')],
                  'open_positions': [{k:p[k] for k in ('id','mint','opened_at','quantity_raw','cost_micro',
                                                       'mark_micro','mark_at','exit_reason','last_error')} for p in positions],
                  'limitations': ['Dinero ficticio y cotizaciones indicativas; no son operaciones ejecutadas.',
                      'Comisiones fijas y slippage son supuestos. No se modela completamente MEV ni impacto propio.',
                      'Los límites se revisan al consultar, no garantizan un precio de salida ni una pérdida máxima.']}
        if self.policy is not None:
            result['entry_blockers'] = self.entry_blockers(now, paused)
        return result
