"""Decisión v0.5: riesgo, evidencia temporal y salida se evalúan por separado."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace

import memecoin_scanner as legacy
from providers import Jupiter, holder_evidence, inspect_mint, network_evidence, pool_evidence, timestamp
from version import SCANNER_VERSION


@dataclass(frozen=True)
class EnhancedPolicy:
    min_organic_score: float = 20
    max_owner_pct: float = 20
    max_top10_pct: float = 60
    max_network_pct: float = 30
    max_round_trip_loss_pct: float = 5
    min_observation_seconds: int = 180
    min_samples: int = 3
    min_organic_buyers_5m: int = 5
    max_liquidity_drop_pct: float = 20
    data_max_age_seconds: int = 300
    exit_stress_bps: int = 100
    fixed_cost_usdc: float = 0.10


def trajectory(row, history, policy, now):
    """Confirmar un tramo continuo de evidencia válida, espaciada y sin cachés repetidas."""
    compatible = [r for r in history if r.get('scanner_version') == SCANNER_VERSION
                  and r.get('enhanced_policy') == asdict(policy) and r.get('policy') == row.get('policy')
                  and r.get('chain') == row.get('chain') and r.get('base_address') == row.get('base_address')
                  and timestamp(r.get('scanned_at')) is not None and timestamp(r['scanned_at']) < now
                  and r.get('pair_address') == row.get('pair_address')]

    def usable(sample, at):
        organic = legacy.obj(sample.get('organic'))
        updated = legacy.number(organic.get('updated_at'), 0)
        buyers = legacy.number(legacy.obj(legacy.obj(organic.get('windows')).get('5m')).get('numOrganicBuyers'), 0)
        liquidity = legacy.number(sample.get('liquidity_usd'), 0)
        return (sample.get('analysis_status') == 'ok' and organic.get('status') == 'ok'
                and updated is not None and -30 <= at - updated <= policy.data_max_age_seconds
                and legacy.number(organic.get('organic_score'), 0, 100) is not None
                and buyers is not None and liquidity is not None and liquidity > 0)

    result = {'status': 'warming_up', 'samples': 0,
              'span_seconds': 0, 'liquidity_change_pct': None, 'liquidity_drawdown_pct': None,
              'organic_buyers_change': None, 'reasons': [],
              'window_note': 'Comparación de snapshots; ventanas 5m móviles no se suman como flujos nuevos.'}
    if not usable(row, now):
        result['status'] = 'incomplete'
        result['reasons'].append('observación actual incompleta o caducada')
        return result

    samples, last, last_update, interrupted = [row], now, row['organic']['updated_at'], False
    for old in sorted(compatible, key=lambda r: timestamp(r['scanned_at']), reverse=True):
        at = timestamp(old['scanned_at'])
        if now - at > 1800:
            break
        if last - at < 60:
            continue
        # Un hueco o una observación incompleta no sirven para alargar la confirmación.
        if last - at > policy.data_max_age_seconds or not usable(old, at):
            interrupted = True
            break
        if old['organic']['updated_at'] >= last_update:
            interrupted = True
            continue
        samples.append(old)
        last, last_update = at, old['organic']['updated_at']
        if len(samples) >= policy.min_samples and now - last >= policy.min_observation_seconds:
            break
    result.update(samples=len(samples), span_seconds=now - last)
    if len(samples) < policy.min_samples or now - last < policy.min_observation_seconds:
        result['status'] = 'incomplete' if interrupted else 'warming_up'
        result['reasons'].append('faltan observaciones válidas, distintas y continuas del mismo pool')
        return result

    liquidities = [legacy.number(r['liquidity_usd']) for r in samples]
    buyers = [legacy.number(r['organic']['windows']['5m']['numOrganicBuyers']) for r in samples]
    scores = [legacy.number(r['organic']['organic_score']) for r in samples]
    result['liquidity_change_pct'] = (liquidities[0] / liquidities[-1] - 1) * 100
    result['liquidity_drawdown_pct'] = (1 - liquidities[0] / max(liquidities)) * 100
    result['organic_buyers_change'] = buyers[0] - buyers[-1]
    if min(buyers) < policy.min_organic_buyers_5m or min(scores) < policy.min_organic_score:
        result['reasons'].append('actividad orgánica insuficiente en varias observaciones')
    if result['liquidity_drawdown_pct'] > policy.max_liquidity_drop_pct:
        result['reasons'].append('deterioro de liquidez durante la observación')
    result['status'] = 'deteriorating' if result['reasons'] else 'sustained'
    return result


def position_risk(row, policy):
    """Safety of an existing position, independent of eligibility for a new buy.

    The ledger obtains its own quote for the actual held quantity. Entry age,
    a positive price ceiling and rebuilding temporal confirmation do not by
    themselves make that position unsafe. Current missing critical data does.
    """
    checks = []

    def add(code, reason, missing=False):
        checks.append({'code': code, 'status': 'missing' if missing else 'blocked', 'reason': reason})

    if row.get('lifecycle', {}).get('phase') in (None, 'detected', 'bonding_curve', 'unknown'):
        add('lifecycle', 'fase de mercado sin validación para mantener la posición', True)
    mint = row.get('mint_check', {})
    if mint.get('status') != 'ok':
        details = mint.get('reasons')
        reason = ('; '.join(details) if isinstance(details, list) and details
                  and all(isinstance(r, str) and r for r in details)
                  else 'comprobación del mint bloqueada o incompleta')
        add('mint', reason, mint.get('status') != 'blocked')
    if row.get('has_danger_flag') is True or row.get('rugged') is True:
        add('critical_risk', 'riesgo crítico reportado por RugCheck')
    if row.get('pool_check', {}).get('status') != 'reported':
        add('pool', 'sin evidencia LP del pool exacto', True)
    holders = row.get('holders', {})
    owner = legacy.number(holders.get('top1_pct_lower_bound'), 0, 100)
    top10 = legacy.number(holders.get('top10_pct_lower_bound'), 0, 100)
    if holders.get('status') != 'ok' or owner is None or top10 is None:
        add('holders', 'propietarios de las mayores cuentas sin resolver', True)
    elif owner > policy.max_owner_pct or top10 > policy.max_top10_pct:
        add('holders', 'concentración elevada en propietarios de la muestra')
    networks = row.get('networks', {})
    group = legacy.number(networks.get('max_group_supply_pct'), 0, 100)
    if networks.get('status') != 'reported' or group is None:
        add('networks', 'informe de grupos relacionados incompleto', True)
    elif group > policy.max_network_pct:
        add('networks', 'grupo relacionado reportado por encima del umbral')
    organic = row.get('organic', {})
    score = legacy.number(organic.get('organic_score'), 0, 100)
    buyers = legacy.number(legacy.obj(legacy.obj(organic.get('windows')).get('5m')).get('numOrganicBuyers'), 0)
    if organic.get('flagged_suspicious'):
        add('suspicious', 'Jupiter marca el token como sospechoso')
    if organic.get('status') != 'ok' or score is None or buyers is None:
        add('organic_data', 'actividad orgánica ausente o caducada', True)
    elif score < policy.min_organic_score or buyers < policy.min_organic_buyers_5m:
        add('organic_activity', 'actividad orgánica actual inferior al umbral')
    path = row.get('trajectory', {})
    if path.get('status') == 'deteriorating':
        for reason in path.get('reasons') or ['deterioro de la trayectoria']:
            add('trajectory', reason)
    elif path.get('status') not in ('sustained', 'warming_up', 'incomplete'):
        add('trajectory', 'trayectoria no interpretable', True)
    for check in row['market_checks']:
        # A known age is an entry window; unknown age remains missing evidence.
        if check['code'] == 'age' and check['status'] != 'missing':
            continue
        # Preserve the downside guard, but do not sell solely for exceeding an
        # entry ceiling after a positive move. Other market-risk checks remain.
        change = legacy.number(row.get('price_change_h1'))
        if (check['code'] == 'price_change' and check['status'] == 'blocked'
                and change is not None and change > row['policy']['max_price_change_h1']):
            continue
        checks.append(dict(check))
    return {'schema': 1, 'status': 'exit' if checks else 'hold', 'checks': checks}


def decide(row, policy):
    hard, missing, wait = [], [], []
    phase = row['lifecycle']['phase']
    if phase in ('detected', 'bonding_curve', 'unknown'):
        wait.append('fase de observación; venta inicial sin analizador específico')
    mint = row['mint_check']
    if mint.get('status') == 'blocked':
        hard.extend(mint.get('reasons', []))
    elif mint.get('status') != 'ok':
        missing.append('comprobación directa del mint incompleta o extensión no soportada')
    if row.get('has_danger_flag') is True or row.get('rugged') is True:
        hard.append('riesgo crítico reportado por RugCheck')
    pool = row['pool_check']
    if pool['status'] != 'reported':
        missing.append('sin evidencia LP del pool exacto')
    elif pool['locked_pct'] < row['policy']['min_lp_locked_pct']:
        hard.append('LP reportada del pool por debajo del umbral')
    holders = row['holders']
    if holders['status'] != 'ok':
        missing.append('propietarios de las mayores cuentas sin resolver')
    elif (holders['top1_pct_lower_bound'] > policy.max_owner_pct
          or holders['top10_pct_lower_bound'] > policy.max_top10_pct):
        hard.append('concentración elevada en propietarios de la muestra')
    networks = row['networks']
    if networks['status'] != 'reported':
        missing.append('informe de grupos relacionados incompleto')
    elif networks['max_group_supply_pct'] > policy.max_network_pct:
        hard.append('grupo relacionado reportado por encima del umbral')
    organic = row['organic']
    if organic.get('flagged_suspicious'):
        hard.append('Jupiter marca el token como sospechoso')
    if organic['status'] != 'ok':
        missing.append('actividad orgánica ausente, sin configurar o caducada')
    elif organic['organic_score'] < policy.min_organic_score:
        hard.append('actividad orgánica inferior al umbral')
    if row['trajectory']['status'] in ('warming_up', 'incomplete'):
        wait.extend(row['trajectory']['reasons'])
    elif row['trajectory']['status'] != 'sustained':
        hard.extend(row['trajectory']['reasons'])
    quotes = row['exit_quotes']
    if not quotes or any(q['status'] != 'quoted' for q in quotes):
        missing.append('no hay cotización de ida y vuelta utilizable para todos los tamaños')
    elif any(q['round_trip_loss_pct'] > policy.max_round_trip_loss_pct for q in quotes):
        hard.append('coste indicativo de ida y vuelta excesivo')
    # Aplicar filtros de mercado con edad de primer pool observada y LP del pool exacto.
    check_row = dict(row)
    check_row['lp_locked_pct'] = pool.get('locked_pct')
    market_policy = legacy.Policy(**row['policy'])
    row['market_checks'] = legacy.quality_checks(check_row, market_policy)
    reasons = [check['reason'] for check in row['market_checks']]
    for check in row['market_checks']:
        {'missing': missing, 'blocked': hard, 'waiting': wait}[check['status']].append(check['reason'])
    row['decision_reasons'] = list(dict.fromkeys(hard + missing + wait))
    row['decision_checks'] = {'blockers': list(dict.fromkeys(hard)), 'missing': list(dict.fromkeys(missing)),
                              'waiting': list(dict.fromkeys(wait))}
    row['state'] = 'rejected' if hard else ('insufficient_data' if missing else ('observing' if wait else 'candidate'))
    row['quality_pass'] = row['state'] == 'candidate'
    row['position_risk'] = position_risk(row, policy)
    row['quality_fail_reasons'] = row['decision_reasons']
    scored = dict(row, quality_pass=True)
    baseline_score = legacy.rank_candidate(scored)[0] if row.get('rugcheck_status') == 'ok' and not reasons else None
    liquidity_priority = baseline_score or 0
    priority = round(0.5 * liquidity_priority + 0.5 * organic.get('organic_score', 0), 2) if row['quality_pass'] else None
    checks = [mint.get('status') == 'ok', pool['status'] == 'reported', holders['status'] == 'ok',
              networks['status'] == 'reported', organic['status'] == 'ok', row['trajectory']['status'] == 'sustained',
              bool(quotes) and all(q['status'] == 'quoted' for q in quotes)]
    row['research_score'] = priority
    row['dimensions'] = {'data_completeness_pct': round(sum(checks) / len(checks) * 100),
                         'risk_blockers': len(set(hard)), 'organic_score': organic.get('organic_score'),
                         'trajectory': row['trajectory']['status'], 'priority': priority,
                         'probability_of_profit': None}
    row['selection_evidence'] = []
    if row['quality_pass']:
        row['selection_evidence'] = [
            'Autoridades de emisión y congelación revocadas según RPC.',
            f"LP del pool exacto reportada bloqueada: {pool['locked_pct']:.1f}% (fuente externa).",
            f"Actividad orgánica: {organic['organic_score']:.1f}/100 según Jupiter.",
            'Actividad sostenida en observaciones espaciadas; concentración dentro de los umbrales.',
            f"Mayor coste indicativo de ida y vuelta: {max(q['round_trip_loss_pct'] for q in quotes):.2f}%.",
        ]
    row['invalidates_if'] = ['Caducan los datos o falta una comprobación crítica.',
                            'Aparece una autoridad activa, concentración excesiva o deterioro de liquidez.',
                            'Desaparece la cotización de salida o excede el coste permitido.']
    return row


class Engine:
    def __init__(self, store, transport, policy=None, enhanced_policy=None, sizes=(100.0,), profile_plan=None):
        self.store, self.transport = store, transport
        self.policy = policy or legacy.Policy()
        self.enhanced_policy = enhanced_policy or EnhancedPolicy()
        self.sizes = sizes
        self.profile_plan = profile_plan
        self.jupiter = Jupiter(transport)

    def analyze(self, chain, mint, sources=(), now=None):
        # Conservar v0.4 como referencia en el mismo universo de candidatos.
        row = legacy.analyze_token(chain, mint, self.policy, sources, now=now)
        row['market_received_at'] = timestamp(row['scanned_at'])
        row['baseline_v04_pass'] = row['quality_pass']
        row['baseline_v04_policy'] = asdict(self.policy)
        row['simple_baseline_pass'] = ((row.get('liquidity_usd') or 0) >= self.policy.min_liquidity
                                       and (row.get('buys_h1') or 0) + (row.get('sells_h1') or 0) >= self.policy.min_txns_h1)
        row.update(scanner_version=SCANNER_VERSION, enhanced_policy=asdict(self.enhanced_policy), provider_errors=[])
        report = {}
        if chain == 'solana':
            try:
                data = self.transport.get('https://api.rugcheck.xyz/v1/tokens/' + mint + '/report', quiet_404=True)
                if isinstance(data, dict) and data.get('mint') == mint:
                    report = data
            except RuntimeError as exc:
                row['provider_errors'].append(str(exc))
        row['rugged'] = report.get('rugged')
        row['pool_check'] = pool_evidence(report, row)
        row['networks'] = network_evidence(report)
        row['mint_check'] = {'status': 'incomplete', 'reasons': ['sin datos RPC']}
        row['holders'] = {'status': 'incomplete', 'owners': [], 'reasons': ['sin datos RPC']}
        if chain == 'solana':
            try:
                response = self.transport.rpc('getAccountInfo', [mint, {'encoding': 'jsonParsed', 'commitment': 'confirmed'}])
                row['mint_check'] = inspect_mint(response)
                if row['mint_check']['status'] == 'ok':
                    row['holders'] = holder_evidence(self.transport.rpc, mint, row['mint_check']['supply_raw'],
                                                     row['mint_check']['slot'], report)
            except RuntimeError as exc:
                row['provider_errors'].append(str(exc))
        try:
            row['organic'] = self.jupiter.token(mint, now=now, max_age=self.enhanced_policy.data_max_age_seconds) if chain == 'solana' else {'status': 'unsupported'}
        except RuntimeError as exc:
            row['organic'] = {'status': 'error'}
            row['provider_errors'].append(str(exc))
        current_time = time.time() if now is None else now
        row['lifecycle'] = self.store.lifecycle(row, row['organic'], current_time)
        row['selected_pool_age_hours'] = row.get('age_hours')
        if row['lifecycle']['first_pool_at'] is not None:
            row['age_hours'] = (current_time - row['lifecycle']['first_pool_at']) / 3600
        # Quitar el veto fijo de 30 minutos: confirmar trayectoria será obligatorio para todas las fases.
        row['policy'] = asdict(replace(self.policy, min_age_hours=0))
        row['exit_quotes'] = []
        needed = self.profile_plan.sizes_to_quote(row, current_time) if self.profile_plan else self.sizes
        for size in self.sizes:
            if size not in needed:
                row['exit_quotes'].append({'status': 'not_requested', 'amount_usdc': size,
                                          'reason': 'comprobaciones previas del perfil no superadas'})
                continue
            try:
                quote = self.jupiter.round_trip(mint, size) if chain == 'solana' else {'status': 'unsupported', 'amount_usdc': size}
            except RuntimeError as exc:
                quote = {'status': 'unavailable', 'amount_usdc': size, 'reason': str(exc)}
            row['exit_quotes'].append(quote)
        current_time = time.time() if now is None else now
        row['scanned_at'] = legacy.utc_string(current_time)
        if current_time - row['market_received_at'] > self.enhanced_policy.data_max_age_seconds:
            row['analysis_status'] = 'stale'
        if row['organic'].get('status') == 'ok' and current_time - row['organic']['updated_at'] > self.enhanced_policy.data_max_age_seconds:
            row['organic']['status'] = 'stale'
        for quote in row['exit_quotes']:
            if quote['status'] == 'quoted':
                times = [legacy.number(quote.get('received_at'))] + [
                    legacy.number(legacy.obj(quote.get(side)).get('received_at')) for side in ('buy', 'sell')]
                if any(at is None or at > current_time + 30 or current_time - at > 30 for at in times):
                    quote['status'] = 'stale'
        row['trajectory'] = trajectory(row, self.store.history(chain, mint, current_time), self.enhanced_policy, current_time)
        decide(row, self.enhanced_policy)
        if self.profile_plan:
            row['profile_plan_hash'] = self.profile_plan.digest
            row['profiles'] = self.profile_plan.evaluate(row, self.store.history(chain, mint, current_time), current_time)
            # La prioridad de seguimiento depende del perfil que esté más cerca de confirmarse.
            order = {'candidate': 0, 'observing': 1, 'insufficient_data': 2, 'rejected': 3}
            best = min(row['profiles'].values(), key=lambda p: order[p['state']])
            row['state'], row['quality_pass'] = best['state'], best['quality_pass']
            for key in ('research_score', 'decision_reasons', 'decision_checks', 'quality_fail_reasons',
                        'dimensions', 'selection_evidence', 'invalidates_if', 'position_risk'):
                row[key] = best[key]
            row['selected_profile_summary'] = best['profile_id']
        return row
