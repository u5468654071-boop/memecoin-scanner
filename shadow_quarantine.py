"""Prospective matched-opportunity overlay; no providers, orders or source edits.

The treatment inherits quantities and exits of accepted control opportunities.
It does not model alternative entries when quarantine frees capital or capacity.
"""
import json
import math
import time
from types import SimpleNamespace

from memecoin_scanner import valid_address
from profile_plan import ProfilePlan, PROFILE_IDS
from providers import integer
from version import SCANNER_VERSION


RULE = {'id': 'shared-mint-two-losses-24h-v1', 'loss_count': 2,
        'lookback_seconds': 86400, 'quarantine_seconds': 86400,
        'window': '(t-86400,t]', 'active_interval': '[trigger,until)',
        'extend_while_active': False, 'loss_source': 'accepted_treatment_closes_only'}
ARMS = ('control', 'treatment')
MAX_EVENTS_PER_SYNC = 500


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _now(value):
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Fecha del experimento inválida')
    return value


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _source_plan(db, tables):
    if 'portfolio_config' not in tables:
        return None
    row = db.execute('SELECT plan,digest FROM portfolio_config WHERE id=1').fetchone()
    if not row:
        return None
    plan = ProfilePlan(json.loads(row['plan']))
    if plan.digest != row['digest']:
        raise ValueError('El plan de origen no coincide con su identificador')
    return plan


def _journal_gap(db, tables):
    if 'shadow_quarantine_guard' in tables and db.execute('SELECT 1 FROM shadow_quarantine_guard WHERE id=1').fetchone():
        return True
    if 'paper_journal_health' in tables:
        health = db.execute('SELECT healthy FROM paper_journal_health WHERE id=1').fetchone()
        return health is not None and health['healthy'] != 1
    return False


def _compatibility(db, config, tables):
    if _journal_gap(db, tables):
        return 'source_journal_gap'
    try:
        plan = _source_plan(db, tables)
        if plan is None or plan.digest != config['plan_hash'] or plan.encoded != config['plan_json']:
            return 'El plan de perfiles cambió o no está disponible'
        if config['scanner_version'] != SCANNER_VERSION or config['rule_json'] != _encoded(RULE):
            return 'La versión o la regla del experimento cambió'
        if 'paper_event_journal' not in tables:
            return 'El diario de operaciones no está disponible'
        for spec in plan.profiles:
            prefix = 'paper_' + spec['id']
            if prefix + '_account' not in tables or prefix + '_positions' not in tables:
                return 'Falta una cartera de origen'
            account = db.execute('SELECT policy FROM '+prefix+'_account WHERE id=1').fetchone()
            if not account or _encoded(json.loads(account['policy'])) != _encoded(spec['paper']):
                return 'La política de una cartera de origen cambió'
    except (TypeError, ValueError, KeyError):
        return 'Configuración del experimento o del origen no interpretable'
    return None


class ShadowQuarantine:
    def __init__(self, store):
        self.db = store.db
        if self.db.in_transaction:
            raise ValueError('Inicializar el experimento fuera de una transacción de la cartera')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS shadow_quarantine_config (
                id INTEGER PRIMARY KEY CHECK(id=1), started_at REAL NOT NULL,
                start_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL,
                last_event_at REAL NOT NULL, plan_hash TEXT NOT NULL, plan_json TEXT NOT NULL,
                scanner_version TEXT NOT NULL, rule_json TEXT NOT NULL, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS shadow_quarantine_accounts (
                arm TEXT NOT NULL CHECK(arm IN ('control','treatment')),
                profile TEXT NOT NULL, initial_micro INTEGER NOT NULL,
                cash_micro INTEGER NOT NULL CHECK(cash_micro>=0), PRIMARY KEY(arm,profile)
            );
            CREATE TABLE IF NOT EXISTS shadow_quarantine_guard (
                id INTEGER PRIMARY KEY CHECK(id=1), reason TEXT NOT NULL, detected_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_quarantine_positions (
                profile TEXT NOT NULL, position_id INTEGER NOT NULL, mint TEXT NOT NULL,
                observation_id INTEGER NOT NULL, opened_at REAL NOT NULL, opened_seq INTEGER NOT NULL UNIQUE,
                cost_micro INTEGER NOT NULL, quantity_raw TEXT NOT NULL,
                accepted INTEGER NOT NULL CHECK(accepted IN (0,1)), skip_reason TEXT,
                blocked_until REAL, closed_at REAL, closed_seq INTEGER UNIQUE,
                proceeds_micro INTEGER, pnl_micro INTEGER, PRIMARY KEY(profile,position_id)
            );
            CREATE TABLE IF NOT EXISTS shadow_quarantine_triggers (
                trigger_seq INTEGER PRIMARY KEY, mint TEXT NOT NULL, triggered_at REAL NOT NULL,
                until_at REAL NOT NULL, loss_positions TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS shadow_quarantine_mint_losses
                ON shadow_quarantine_positions(mint,accepted,closed_at);
        ''')

    def activate_if_flat(self, now=None):
        now = _now(now)
        if self.db.in_transaction:
            raise ValueError('Activar fuera de una transacción de la cartera')
        with self.db:
            self.db.execute('UPDATE shadow_quarantine_config SET last_seq=last_seq WHERE id=1')
            if self._stop_on_gap(now):
                return self.report(now)
            if self.db.execute('SELECT 1 FROM shadow_quarantine_config WHERE id=1').fetchone():
                return self.report(now)
            tables = _tables(self.db)
            plan = _source_plan(self.db, tables)
            if plan is None or 'paper_event_journal' not in tables:
                return {**self.report(now), 'waiting_reason': 'Esperando plan y diario de operaciones'}
            balances = {}
            for spec in plan.profiles:
                prefix = 'paper_' + spec['id']
                if prefix+'_positions' not in tables or prefix+'_account' not in tables:
                    return {**self.report(now), 'waiting_reason': 'Esperando las tres carteras'}
                if self.db.execute("SELECT 1 FROM "+prefix+"_positions WHERE state='open' LIMIT 1").fetchone():
                    return {**self.report(now), 'waiting_reason': 'Esperando que las tres carteras estén sin posiciones abiertas'}
                account = self.db.execute('SELECT cash_micro,policy FROM '+prefix+'_account WHERE id=1').fetchone()
                if (not account or type(account['cash_micro']) is not int or account['cash_micro'] < 0
                        or _encoded(json.loads(account['policy'])) != _encoded(spec['paper'])):
                    raise ValueError('Saldo o política de origen incompatible')
                balances[spec['id']] = account['cash_micro']
            last = self.db.execute('SELECT COALESCE(MAX(seq),0),MAX(happened_at) FROM paper_event_journal').fetchone()
            if last[1] is not None and last[1] > now:
                raise ValueError('El diario contiene operaciones posteriores al inicio solicitado')
            self.db.execute('INSERT INTO shadow_quarantine_config VALUES (1,?,?,?,?,?,?,?,?,NULL)',
                (now,last[0],last[0],now,plan.digest,plan.encoded,SCANNER_VERSION,_encoded(RULE)))
            for arm in ARMS:
                for profile,cash in balances.items():
                    self.db.execute('INSERT INTO shadow_quarantine_accounts VALUES (?,?,?,?)', (arm,profile,cash,cash))
        return self.report(now)

    def _stop_on_gap(self, now):
        if not _journal_gap(self.db, _tables(self.db)):
            return False
        self.db.execute('INSERT OR IGNORE INTO shadow_quarantine_guard VALUES (1,?,?)', ('source_journal_gap',now))
        return True

    def _validate(self, event, config, now):
        try:
            payload = json.loads(event['payload'])
            if (not isinstance(payload, dict) or event['profile'] not in PROFILE_IDS
                    or event['kind'] not in ('open','close') or type(event['position_id']) is not int
                    or event['position_id'] < 1 or not valid_address('solana', event['mint'])
                    or not config['last_event_at'] <= _now(event['happened_at']) <= now
                    or payload.get('scanner_version') != config['scanner_version']
                    or payload.get('profile_plan_hash') != config['plan_hash']
                    or type(payload.get('cost_micro')) is not int or payload['cost_micro'] <= 0
                    or type(payload.get('observation_id')) is not int or payload['observation_id'] < 1
                    or integer(payload.get('quantity_raw'), 1) is None):
                raise ValueError()
            if event['kind'] == 'close' and (
                    type(payload.get('proceeds_micro')) is not int or payload['proceeds_micro'] < 0
                    or type(payload.get('pnl_micro')) is not int
                    or payload['proceeds_micro']-payload['cost_micro'] != payload['pnl_micro']):
                raise ValueError()
        except (TypeError, ValueError, KeyError):
            raise ValueError('Evento incompatible o incoherente en el diario; comparación detenida') from None
        return payload

    def _active_block(self, mint, at):
        return self.db.execute('''SELECT * FROM shadow_quarantine_triggers
            WHERE mint=? AND triggered_at<=? AND until_at>? ORDER BY trigger_seq DESC LIMIT 1''', (mint,at,at)).fetchone()

    def _consume(self, event, payload):
        profile, position_id, at = event['profile'], event['position_id'], event['happened_at']
        position = self.db.execute('SELECT * FROM shadow_quarantine_positions WHERE profile=? AND position_id=?',
                                   (profile,position_id)).fetchone()
        if event['kind'] == 'open':
            if position:
                raise ValueError('Apertura duplicada en el diario')
            cost = payload['cost_micro']
            cash = {r['arm']:r['cash_micro'] for r in self.db.execute(
                'SELECT arm,cash_micro FROM shadow_quarantine_accounts WHERE profile=?', (profile,))}
            if set(cash) != set(ARMS) or cash['control'] < cost:
                raise ValueError('El control del experimento no cuadra con el saldo de sus oportunidades')
            block = self._active_block(event['mint'], at)
            # Do not credit quarantine with an omission that insufficient cash
            # would already have forced. blocked_until still preserves overlap.
            reason = 'insufficient_cash' if cash['treatment'] < cost else ('quarantine' if block else None)
            self.db.execute('''INSERT INTO shadow_quarantine_positions
                (profile,position_id,mint,observation_id,opened_at,opened_seq,cost_micro,quantity_raw,
                 accepted,skip_reason,blocked_until) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (profile,position_id,event['mint'],payload['observation_id'],at,event['seq'],cost,
                 str(integer(payload['quantity_raw'],1)),int(reason is None),reason,block['until_at'] if block else None))
            for arm in ARMS:
                if arm == 'control' or reason is None:
                    self.db.execute('UPDATE shadow_quarantine_accounts SET cash_micro=cash_micro-? WHERE arm=? AND profile=?',
                                    (cost,arm,profile))
            return
        if (not position or position['closed_seq'] is not None or position['mint'] != event['mint']
                or position['observation_id'] != payload['observation_id'] or position['cost_micro'] != payload['cost_micro']
                or position['quantity_raw'] != str(integer(payload['quantity_raw'],1)) or at < position['opened_at']):
            raise ValueError('Cierre sin apertura coincidente en el experimento')
        self.db.execute('''UPDATE shadow_quarantine_positions SET closed_at=?,closed_seq=?,proceeds_micro=?,pnl_micro=?
            WHERE profile=? AND position_id=?''', (at,event['seq'],payload['proceeds_micro'],payload['pnl_micro'],profile,position_id))
        for arm in ARMS:
            if arm == 'control' or position['accepted']:
                self.db.execute('UPDATE shadow_quarantine_accounts SET cash_micro=cash_micro+? WHERE arm=? AND profile=?',
                                (payload['proceeds_micro'],arm,profile))
        if position['accepted'] and payload['pnl_micro'] < 0 and not self._active_block(event['mint'], at):
            losses = self.db.execute('''SELECT profile,position_id,closed_seq FROM shadow_quarantine_positions
                WHERE mint=? AND accepted=1 AND pnl_micro<0 AND closed_at>? AND closed_at<=?
                ORDER BY closed_seq DESC''', (event['mint'],at-RULE['lookback_seconds'],at)).fetchall()
            if len(losses) >= RULE['loss_count']:
                self.db.execute('INSERT INTO shadow_quarantine_triggers VALUES (?,?,?,?,?)',
                    (event['seq'],event['mint'],at,at+RULE['quarantine_seconds'],_encoded([dict(p) for p in losses[:2]])))

    def sync(self, now=None):
        now = _now(now)
        if self.db.in_transaction:
            raise ValueError('Sincronizar fuera de una transacción de la cartera')
        error = None
        try:
            with self.db:
                self.db.execute('UPDATE shadow_quarantine_config SET last_seq=last_seq WHERE id=1')
                if self._stop_on_gap(now):
                    return self.report(now)
                row = self.db.execute('SELECT * FROM shadow_quarantine_config WHERE id=1').fetchone()
                if row is None or _compatibility(self.db, row, _tables(self.db)):
                    return self.report(now)
                config = dict(row)
                events = self.db.execute('SELECT * FROM paper_event_journal WHERE seq>? ORDER BY seq LIMIT ?',
                                         (config['last_seq'],MAX_EVENTS_PER_SYNC)).fetchall()
                for event in events:
                    payload = self._validate(event, config, now)
                    self._consume(event,payload)
                    config.update(last_seq=event['seq'],last_event_at=event['happened_at'])
                self.db.execute('UPDATE shadow_quarantine_config SET last_seq=?,last_event_at=?,last_error=NULL WHERE id=1',
                                (config['last_seq'],config['last_event_at']))
        except ValueError as exc:
            error = str(exc)
        if error:
            with self.db:
                self.db.execute('UPDATE shadow_quarantine_config SET last_error=? WHERE id=1', (error,))
        return self.report(now)

    def report(self, now=None):
        return shadow_report(SimpleNamespace(db=self.db), now)


def shadow_report(store, now=None):
    """Read one consistent snapshot; this function never initializes or mutates."""
    now, db = _now(now), store.db
    own_snapshot = not db.in_transaction
    if own_snapshot:
        db.execute('BEGIN')
    try:
        result = {'schema':1, 'mode':'paper_only', 'design':'matched_opportunity_overlay',
                  'active':False, 'status':'not_initialized', 'as_of':now, 'comparison_current':False,
                  'profitability_proven':False, 'rule':dict(RULE),
                  'limitations':[
                      'Estimación prospectiva sobre las oportunidades aceptadas por el control; no es una cartera autónoma.',
                      'El tratamiento hereda cantidad, costes y salida del control, sin nuevas consultas ni ejecuciones reales.',
                      'No simula entradas alternativas ni adelanta salidas cuando la cuarentena libera capital o capacidad.',
                      'Los cierres perdedores simultáneos entre perfiles cuentan por separado; no son observaciones independientes.',
                      'Pérdida evitada y ganancia descartada usan solo cierres posteriores de oportunidades omitidas; no garantizan rentabilidad.',
                      'Los límites de exposición y oportunidades proceden del control; el tratamiento puede omitir además por saldo insuficiente.',
                      'Si coinciden saldo insuficiente y cuarentena, la omisión se atribuye a saldo insuficiente.',
                      'Una cuarentena activa no se prolonga con otras pérdidas; los beneficios no borran pérdidas de la ventana móvil.']}
        tables = _tables(db)
        if 'shadow_quarantine_config' not in tables:
            if _journal_gap(db, tables):
                result.update(status='incompatible',incompatibility='source_journal_gap')
            return result
        config = db.execute('SELECT * FROM shadow_quarantine_config WHERE id=1').fetchone()
        if config is None:
            result['status'] = 'waiting_for_flat_start'
            if _journal_gap(db, tables):
                result.update(status='incompatible',incompatibility='source_journal_gap')
            return result
        incompatibility = _compatibility(db, config, tables)
        pending = (db.execute('SELECT count(*) FROM paper_event_journal WHERE seq>?', (config['last_seq'],)).fetchone()[0]
                   if 'paper_event_journal' in tables else None)
        result.update(active=True, status='incompatible' if incompatibility else ('error' if config['last_error'] else 'active'),
            incompatibility=incompatibility, last_error=config['last_error'], started_at=config['started_at'],
            start_seq=config['start_seq'], last_seq=config['last_seq'], pending_events=pending,
            scanner_version=config['scanner_version'], profile_plan_hash=config['plan_hash'])
        trusted = not incompatibility and not config['last_error'] and pending == 0 and now >= config['last_event_at']
        result['comparison_current'] = trusted
        if incompatibility:
            return result
        positions = [dict(p) for p in db.execute('SELECT * FROM shadow_quarantine_positions ORDER BY opened_seq')]
        policies = {p['id']:p['paper'] for p in json.loads(config['plan_json'])['profiles']}
        arms = {}
        for arm in ARMS:
            profiles = {}
            for account in db.execute('SELECT * FROM shadow_quarantine_accounts WHERE arm=? ORDER BY profile', (arm,)):
                held = [p for p in positions if p['profile']==account['profile'] and (arm=='control' or p['accepted'])]
                opened, closed = [p for p in held if p['closed_seq'] is None], [p for p in held if p['closed_seq'] is not None]
                marks, missing = 0, 0
                for p in opened:
                    table = 'paper_'+p['profile']+'_positions'
                    source = db.execute('SELECT * FROM '+table+' WHERE id=?', (p['position_id'],)).fetchone() if table in tables else None
                    if (not source or source['state']!='open' or source['mint']!=p['mint']
                            or source['observation_id']!=p['observation_id'] or source['cost_micro']!=p['cost_micro']
                            or source['quantity_raw']!=p['quantity_raw'] or source['last_error']
                            or type(source['mark_micro']) is not int or source['mark_micro']<0
                            or source['mark_at'] is None or not 0 <= now-source['mark_at'] <= policies[p['profile']]['mark_max_age_seconds']):
                        missing += 1
                    else:
                        marks += source['mark_micro']
                pnl = sum(p['pnl_micro'] for p in closed)
                profiles[account['profile']] = {'initial_usdc':account['initial_micro']/1e6,
                    'cash_usdc':account['cash_micro']/1e6, 'realized_pnl_usdc':pnl/1e6,
                    'equity_usdc':(account['cash_micro']+marks)/1e6 if trusted and not missing else None,
                    'closed_count':len(closed), 'open_count':len(opened), 'unpriced_or_stale_count':missing,
                    'wins':sum(p['pnl_micro']>0 for p in closed), 'losses':sum(p['pnl_micro']<0 for p in closed),
                    'unique_closed_mints':len({p['mint'] for p in closed})}
            values = list(profiles.values())
            arms[arm] = {'profiles':profiles, 'initial_usdc':sum(p['initial_usdc'] for p in values),
                'cash_usdc':sum(p['cash_usdc'] for p in values),
                'realized_pnl_usdc':sum(p['realized_pnl_usdc'] for p in values),
                'equity_usdc':sum(p['equity_usdc'] for p in values) if all(p['equity_usdc'] is not None for p in values) else None,
                'closed_count':sum(p['closed_count'] for p in values), 'open_count':sum(p['open_count'] for p in values)}
        skipped = {}
        for reason in ('quarantine','insufficient_cash'):
            omitted = [p for p in positions if p['skip_reason']==reason]
            closed = [p for p in omitted if p['closed_seq'] is not None]
            avoided = sum(-min(p['pnl_micro'],0) for p in closed)
            forgone = sum(max(p['pnl_micro'],0) for p in closed)
            skipped[reason] = {'openings_skipped':len(omitted), 'closed_opportunities':len(closed),
                'pending_opportunities':len(omitted)-len(closed), 'unique_mints':len({p['mint'] for p in omitted}),
                'loss_avoided_usdc':avoided/1e6, 'gain_forgone_usdc':forgone/1e6,
                'net_realized_difference_usdc':(avoided-forgone)/1e6}
        result.update(arms=arms, skipped=skipped,
            active_quarantines=[dict(row) for row in db.execute('''SELECT mint,triggered_at,until_at,trigger_seq
                FROM shadow_quarantine_triggers WHERE triggered_at<=? AND until_at>? ORDER BY until_at,mint''', (now,now))],
            quarantine_count=db.execute('SELECT count(*) FROM shadow_quarantine_triggers').fetchone()[0],
            realized_difference_usdc=arms['treatment']['realized_pnl_usdc']-arms['control']['realized_pnl_usdc'])
        return result
    finally:
        if own_snapshot:
            db.rollback()
