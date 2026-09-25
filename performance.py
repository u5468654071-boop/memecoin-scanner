"""Resultados ficticios de un intervalo cerrado por la izquierda, sin revalorar el pasado."""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from collections import Counter, defaultdict

from paper_trading import micro_usdc


PROFILE_IDS = ('conservative', 'balanced', 'aggressive')


def _utc(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def _window(since, until):
    try:
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in (since, until)) or since >= until):
            raise ValueError
        return {'since': since, 'until': until, 'since_utc': _utc(since), 'until_utc': _utc(until),
                'bounds': '[since, until)',
                'open_at_end_definition': 'opened_at < until and (closed_at is null or closed_at >= until)'}
    except (ValueError, OverflowError, OSError):
        raise ValueError('El intervalo requiere dos épocas UTC finitas, representables y since < until') from None


def _object(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def _text(value):
    return value if isinstance(value, str) and value else None


def _accounting(rows, since, until):
    closed = [p for p in rows if p['closed_at'] is not None and since <= p['closed_at'] < until]
    if any(p['state'] != 'closed' or not isinstance(p['pnl_micro'], int) for p in closed):
        raise ValueError('Posición cerrada sin estado o PnL entero coherente; no se calcula un resultado parcial')
    net = sum(p['pnl_micro'] for p in closed)
    return {'opened_count': sum(since <= p['opened_at'] < until for p in rows),
            'closed_count': len(closed),
            'open_at_end_count': sum(p['closed_at'] is None or p['closed_at'] >= until for p in rows),
            'realized_pnl_micro': net, 'realized_pnl_usdc': net / 1000000}, closed


def _metrics(rows, since, until, fixed_micro):
    result, closed = _accounting(rows, since, until)
    wins = sum(p['pnl_micro'] > 0 for p in closed)
    losses = sum(p['pnl_micro'] < 0 for p in closed)
    gross_profit = sum(max(0, p['pnl_micro']) for p in closed)
    gross_loss = sum(max(0, -p['pnl_micro']) for p in closed)
    mints = defaultdict(list)
    reasons, evidence_kinds, risk_checks, price_triggers = Counter(), Counter(), Counter(), Counter()
    missing_evidence = invalid_evidence = 0
    for position in closed:
        mints[position['mint']].append(position)
        reasons[position['exit_reason'] or '(sin motivo registrado)'] += 1
        raw = position['exit_evidence']
        evidence = _object(raw)
        if raw is None:
            missing_evidence += 1
        elif evidence is None:
            invalid_evidence += 1
        else:
            evidence_kinds[_text(evidence.get('kind')) or '(sin tipo registrado)'] += 1
            checks = evidence.get('checks', [])
            if isinstance(checks, list):
                # Una posición cuenta como máximo una vez por código y estado.
                risk_checks.update(set((check['code'], check['status']) for check in checks
                    if isinstance(check, dict) and _text(check.get('code'))
                    and check.get('status') in ('blocked', 'missing', 'waiting')))
            for trigger in ('stop_loss_triggered', 'take_profit_triggered'):
                if evidence.get(trigger) is True:
                    price_triggers[trigger] += 1
    per_mint = []
    for mint, trades in sorted(mints.items()):
        net = sum(p['pnl_micro'] for p in trades)
        per_mint.append({'mint': mint, 'closed_count': len(trades),
                         'wins': sum(p['pnl_micro'] > 0 for p in trades),
                         'losses': sum(p['pnl_micro'] < 0 for p in trades),
                         'realized_pnl_micro': net, 'realized_pnl_usdc': net / 1000000})
    by_pnl = sorted(per_mint, key=lambda item: (item['realized_pnl_micro'], item['mint']))
    best, worst = (by_pnl[-1], by_pnl[0]) if by_pnl else (None, None)
    entry_days = sorted({_utc(p['opened_at'])[:10] for p in closed})
    closing_days = sorted({_utc(p['closed_at'])[:10] for p in closed})
    assumed_costs = 2 * len(closed) * fixed_micro if fixed_micro is not None else None
    result.update(
        wins=wins, losses=losses, flat=len(closed)-wins-losses,
        win_rate_pct=100*wins/len(closed) if closed else None,
        gross_profit_usdc=gross_profit/1000000, gross_loss_usdc=gross_loss/1000000,
        profit_factor=gross_profit/gross_loss if gross_loss else None,
        profit_factor_unavailable_reason=None if gross_loss else
            ('no_closed_trades' if not closed else 'no_realized_losses_in_window'),
        unique_mints=len(mints), repeated_mints_count=sum(len(trades) > 1 for trades in mints.values()),
        repeat_mint_trades_count=sum(len(trades)-1 for trades in mints.values()),
        closed_trade_entry_days_utc=entry_days, unique_entry_days=len(entry_days),
        closing_days_utc=closing_days, unique_closing_days=len(closing_days),
        median_hold_seconds=statistics.median(p['closed_at']-p['opened_at'] for p in closed) if closed else None,
        assumed_fixed_costs_usdc=assumed_costs/1000000 if assumed_costs is not None else None,
        assumed_fixed_cost_per_side_usdc=fixed_micro/1000000 if fixed_micro is not None else None,
        fixed_cost_policy_available=fixed_micro is not None,
        exit_reason_counts=dict(sorted(reasons.items())),
        exit_evidence={'recorded_closed_count': len(closed)-missing_evidence-invalid_evidence,
                       'missing_closed_count': missing_evidence, 'invalid_closed_count': invalid_evidence,
                       'kind_counts': dict(sorted(evidence_kinds.items())),
                       'check_counts': [{'code': code, 'status': status, 'closed_count': count}
                                        for (code, status), count in sorted(risk_checks.items())],
                       'price_trigger_counts': dict(sorted(price_triggers.items()))},
        per_mint=per_mint, best_mint=best, worst_mint=worst,
        net_without_best_mint_usdc=(result['realized_pnl_micro']-best['realized_pnl_micro'])/1000000
            if best else None)
    return result


def performance_report(store, since, until):
    """Read-only report; preserves an existing caller transaction and never calls providers.

    A closed trade belongs to the interval containing its close, even when opened
    before since. Positions closing at until remain outstanding just before that
    exclusive boundary. Entry provenance is read from the saved observation; no
    current scanner version or current policy substitutes missing historical data.
    """
    window = _window(since, until)
    db = store.db
    own_snapshot = not db.in_transaction
    if own_snapshot:
        db.execute('BEGIN')
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        active_profiles = ('portfolio_config' in tables
                           and db.execute('SELECT 1 FROM portfolio_config WHERE id=1').fetchone() is not None)
        profile_tables = [(profile, 'paper_'+profile) for profile in PROFILE_IDS
                          if 'paper_'+profile+'_positions' in tables
                          and (active_profiles or ('paper_'+profile+'_account' in tables
                               and db.execute('SELECT 1 FROM paper_'+profile+'_account WHERE id=1').fetchone()))]
        if active_profiles and len(profile_tables) != len(PROFILE_IDS):
            raise ValueError('Faltan carteras del plan activo; no se calcula un resultado parcial')
        # Never combine an archived legacy account with the active profile experiment.
        ledgers = profile_tables if active_profiles or profile_tables else (
            [('legacy', 'paper')] if 'paper_positions' in tables else [])
        profiles, all_rows = {}, []
        for profile, prefix in ledgers:
            position_table, account_table = prefix+'_positions', prefix+'_account'
            columns = {row[1] for row in db.execute('PRAGMA table_info('+position_table+')')}
            evidence = 'p.exit_evidence' if 'exit_evidence' in columns else 'NULL'
            policy_row = (db.execute('SELECT policy FROM '+account_table+' WHERE id=1').fetchone()
                          if account_table in tables else None)
            policy = _object(policy_row[0]) if policy_row else None
            try:
                fixed_micro = micro_usdc(policy['fixed_cost_usdc_per_side']) if policy else None
            except (KeyError, ValueError):
                fixed_micro = None
            rows = [dict(row) for row in db.execute('''SELECT p.id,p.mint,p.opened_at,p.closed_at,
                p.state,p.pnl_micro,p.exit_reason,'''+evidence+''' AS exit_evidence,o.payload
                FROM '''+position_table+''' p LEFT JOIN observations o ON p.observation_id=o.id
                WHERE p.opened_at < ? AND (p.closed_at IS NULL OR p.closed_at >= ?)
                ORDER BY p.opened_at,p.id''', (until, since))]
            if any(p['closed_at'] is not None and p['closed_at'] < p['opened_at'] for p in rows):
                raise ValueError('Posición con cierre anterior a su apertura')
            cohorts = defaultdict(list)
            for position in rows:
                entry = _object(position['payload']) or {}
                cohorts[(_text(entry.get('scanner_version')), _text(entry.get('profile_plan_hash')))].append(position)
            cohort_reports = []
            for (version, plan_hash), positions in sorted(cohorts.items(), key=lambda item: tuple(v or '' for v in item[0])):
                cohort_reports.append({'profile': profile, 'scanner_version': version,
                    'profile_plan_hash': plan_hash, 'entry_provenance_complete': version is not None
                        and (profile == 'legacy' or plan_hash is not None),
                    **_metrics(positions, since, until, fixed_micro)})
            accounting, _ = _accounting(rows, since, until)
            profiles[profile] = {'accounting': accounting, 'cohorts': cohort_reports}
            all_rows.extend(rows)
        accounting, _ = _accounting(all_rows, since, until)
        return {'schema': 1, 'mode': 'paper_only', 'window': window,
                'scope': 'profile_ledgers' if profile_tables else 'legacy_ledger' if ledgers else 'no_ledger',
                'excluded_archived_ledgers': ['paper'] if profile_tables and 'paper_positions' in tables else [],
                'accounting': accounting, 'profiles': profiles,
                'limitations': [
                    'Cotizaciones de simulación; estos resultados no demuestran rentabilidad ejecutable.',
                    'El agregado es contabilidad; comparar rendimiento por perfil, versión de entrada y plan.',
                    'Solo se suman cierres en [since, until); no se reconstruye equity histórico ni retorno anual.',
                    'Las operaciones repetidas de una moneda y los perfiles simultáneos no son muestras independientes.',
                    'Los costes fijos son el supuesto por ambos lados de cada cierre, ya incluido en el PnL; no restarlo otra vez. '
                    'El valor de salida se limita a cero si no alcanza para cubrir el supuesto completo.',
                    'net_without_best_mint_usdc es una sensibilidad retrospectiva, no una estrategia seleccionable.',
                    'La evidencia de salida ausente en operaciones antiguas no se reconstruye con datos posteriores.']}
    finally:
        if own_snapshot:
            db.rollback()
