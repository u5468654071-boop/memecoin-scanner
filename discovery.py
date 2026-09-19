"""Preselección barata y seguimiento persistente; nunca aprueba una compra."""
import collections
import json
import time

import memecoin_scanner as legacy
from providers import batch_pairs, timestamp
from version import SCANNER_VERSION


def create_queue(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS discovery_v8 (
            chain TEXT NOT NULL, mint TEXT NOT NULL, sources TEXT NOT NULL,
            first_seen REAL NOT NULL, last_seen REAL NOT NULL,
            stage TEXT NOT NULL DEFAULT 'pending', reason_codes TEXT NOT NULL DEFAULT '[]',
            last_probe REAL, next_probe REAL NOT NULL DEFAULT 0,
            last_full REAL, next_full REAL NOT NULL DEFAULT 0,
            confirm_until REAL, priority REAL NOT NULL DEFAULT 0, evidence TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(chain,mint)
        );
        CREATE INDEX IF NOT EXISTS discovery_probe ON discovery_v8(chain,next_probe,last_probe);
        CREATE INDEX IF NOT EXISTS discovery_full ON discovery_v8(chain,stage,next_full);
    ''')


def screen(mint, pairs, organic, now, minimum_liquidity, maximum_age, minimum_age, profiles=None):
    # La curva inicial se conserva en eventos; el motor de entrada opera pools DEX.
    tradable = [p for p in pairs if str(p.get('dexId', '')).lower() != 'pumpfun']
    pair = legacy.select_pair(tradable, 'solana', mint)
    reasons, evidence = [], {'organic_status': organic.get('status')}
    retry = 180
    if pair is None:
        return {'stage': 'deferred', 'reasons': ['awaiting_dex_pool'], 'retry': 180,
                'priority': 0, 'evidence': evidence}
    row = legacy.score_market(pair, now)
    age = row.get('age_hours')
    first_pool = organic.get('first_pool_at')
    if first_pool is not None and 0 <= first_pool <= now:
        age = max(age if age is not None else 0, (now-first_pool)/3600)
    evidence.update(pair_address=row.get('pair_address'), age_hours=age,
                    liquidity_usd=row.get('liquidity_usd'), organic_score=organic.get('organic_score'))
    if row.get('quote_address') not in legacy.QUOTE_MINTS['solana']:
        reasons.append('unsupported_quote_asset')
    if row.get('price_usd') is None:
        reasons.append('price_missing')
    if age is None:
        reasons.append('age_missing')
    elif age > maximum_age:
        reasons.append('outside_profile_age')
        retry = 3600
    elif age < minimum_age:
        reasons.append('waiting_minimum_age')
        retry = max(60, (minimum_age-age)*3600)
    liquidity = row.get('liquidity_usd')
    if liquidity is None:
        reasons.append('liquidity_missing')
    elif liquidity < minimum_liquidity:
        reasons.append('liquidity_below_all_profiles')
    if organic.get('status') != 'ok':
        reasons.append('organic_' + str(organic.get('status', 'missing')))
    if organic.get('flagged_suspicious'):
        reasons.append('jupiter_suspicious')
        retry = max(retry, 900)
    if profiles:
        score = legacy.number(organic.get('organic_score'), 0, 100)
        buyers = legacy.number(legacy.obj(legacy.obj(organic.get('windows')).get('5m')).get('numOrganicBuyers'), 0)
        failures, compatible = {}, []
        for profile in profiles:
            market, enhanced = profile['market'], profile['enhanced']
            failed = []
            if age is None or not market['min_age_hours'] <= age <= market['max_age_hours']:
                failed.append('age')
            if liquidity is None or liquidity < market['min_liquidity']:
                failed.append('liquidity')
            if score is None or score < enhanced['min_organic_score']:
                failed.append('organic_score')
            if buyers is None or buyers < enhanced['min_organic_buyers_5m']:
                failed.append('organic_buyers')
            failures[profile['id']] = failed
            if not failed:
                compatible.append(profile['id'])
        evidence.update(organic_buyers_5m=buyers, compatible_profiles=compatible, profile_failures=failures)
        if not compatible:
            reasons.append('no_compatible_profile')
            if score is None:
                reasons.append('organic_score_missing')
            elif score < min(p['enhanced']['min_organic_score'] for p in profiles):
                reasons.append('organic_score_below_all_profiles')
            if buyers is None:
                reasons.append('organic_buyers_missing')
            elif buyers < min(p['enhanced']['min_organic_buyers_5m'] for p in profiles):
                reasons.append('organic_buyers_below_all_profiles')
    # Solo ordenar la investigación, nunca estimar probabilidad de beneficio.
    priority = (organic.get('organic_score') or 0) + min(20, (liquidity or 0)/50000)
    return {'stage': 'deferred' if reasons else 'ready', 'reasons': reasons,
            'retry': retry, 'priority': priority, 'evidence': evidence}


class DiscoveryQueue:
    def __init__(self, store, plan, interval=60):
        self.store, self.db, self.plan, self.interval = store, store.db, plan, interval

    def offer(self, provenance, now=None):
        now = time.time() if now is None else now
        with self.db:
            for mint, sources in provenance.items():
                if not legacy.valid_address('solana', mint):
                    continue
                old = self.db.execute('SELECT sources FROM discovery_v8 WHERE chain=? AND mint=?', ('solana',mint)).fetchone()
                sources = sorted(set(sources) | (set(json.loads(old[0])) if old else set()))
                self.db.execute('''INSERT INTO discovery_v8 (chain,mint,sources,first_seen,last_seen)
                    VALUES ('solana',?,?,?,?) ON CONFLICT(chain,mint) DO UPDATE SET
                    sources=excluded.sources,last_seen=MAX(last_seen,excluded.last_seen)''',
                    (mint,json.dumps(sources),now,now))

    def import_migrations(self, now=None):
        now = time.time() if now is None else now
        # Los eventos create solos no entran en la cola de análisis profundo.
        events = self.db.execute('''SELECT mint,MAX(received_at) AS at FROM events_v5
            WHERE chain='solana' AND kind='migrate' AND received_at>=? GROUP BY mint''', (now-86400,)).fetchall()
        for event in events:
            self.offer({event['mint']: ['pumpportal_migration']}, event['at'])
            with self.db:
                self.db.execute('''UPDATE discovery_v8 SET next_probe=MIN(next_probe,?)
                    WHERE chain='solana' AND mint=? AND last_probe<?''', (now,event['mint'],event['at']))

    def refresh(self, jupiter, transport, now=None, limit=30):
        clock = (lambda: now) if now is not None else time.time
        at = clock()
        # Una recuperación del stream puede aportar cientos de migraciones antiguas.
        # Reservar capacidad a las listas de mercado para que ese backlog no las bloquee.
        query = '''SELECT mint FROM discovery_v8 WHERE chain='solana'
            AND last_seen>=? AND next_probe<=? AND (stage!='confirm' OR confirm_until<?)
            AND (sources='["pumpportal_migration"]')={}
            AND (sources LIKE '%jupiter_market_%')={}
            AND (last_probe IS NULL)={}
            ORDER BY COALESCE(last_probe,0), (sources LIKE '%jupiter_market_%') DESC,
                     first_seen DESC,mint LIMIT ?'''
        def interleave(first, second):
            merged = []
            for index in range(max(len(first),len(second))):
                for rows in (first,second):
                    if index < len(rows):
                        merged.append(rows[index])
            return merged
        def fair_lane(migration, market):
            new = self.db.execute(query.format(migration,market,1),(at-86400,at,at,limit)).fetchall()
            due = self.db.execute(query.format(migration,market,0),(at-86400,at,at,limit)).fetchall()
            # Alternar revisitas y nuevas: una llegada continua no bloquea ninguna.
            return interleave(due,new)
        # De 20 huecos de listas, reservar 10 al universo de actividad; ceder sobrantes.
        feeds = interleave(fair_lane(0,1),fair_lane(0,0))
        migrations = fair_lane(1,0)
        feed_quota = max(1,limit*2//3)
        chosen = {r['mint']:r for r in feeds[:feed_quota]+migrations[:limit-feed_quota]}
        for row in feeds+migrations:
            if len(chosen)>=limit:
                break
            chosen.setdefault(row['mint'],row)
        pending = list(chosen.values())
        mints = [r['mint'] for r in pending]
        if not mints:
            return {'probed': 0, 'ready': 0, 'errors': []}
        errors = []
        try:
            organic = jupiter.prefetch(mints, now=now)
        except RuntimeError as exc:
            organic = {mint: {'status':'error'} for mint in mints}
            errors.append(str(exc))
        try:
            pairs = batch_pairs(transport, 'solana', mints)
        except RuntimeError as exc:
            pairs = {mint: [] for mint in mints}
            errors.append(str(exc))
        minimum_liquidity = min(p['market']['min_liquidity'] for p in self.plan.profiles)
        maximum_age = max(p['market']['max_age_hours'] for p in self.plan.profiles)
        minimum_age = min(p['market']['min_age_hours'] for p in self.plan.profiles)
        ready = 0
        at = clock()
        with self.db:
            for mint in mints:
                result = screen(mint, pairs[mint], organic[mint], at, minimum_liquidity, maximum_age, minimum_age,
                                self.plan.profiles)
                ready += result['stage'] == 'ready'
                if errors:
                    result['reasons'].append('provider_error')
                self.db.execute('''UPDATE discovery_v8 SET stage=?,reason_codes=?,last_probe=?,next_probe=?,
                    priority=?,evidence=? WHERE chain='solana' AND mint=?''',
                    (result['stage'],json.dumps(result['reasons']),at,at+result['retry'],result['priority'],
                     json.dumps(result['evidence'],allow_nan=False),mint))
        return {'probed': len(mints), 'ready': ready, 'errors': errors}

    def select(self, limit, now=None):
        now = time.time() if now is None else now
        selected = {}
        held_mints = set()
        # La valoración de salidas sigue en el servicio paper; aquí se refresca su evidencia.
        for profile in self.plan.profiles:
            table = 'paper_' + profile['id'] + '_positions'
            if not self.db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                continue
            for pos in self.db.execute('SELECT mint FROM '+table+" WHERE state!='closed'"):
                held_mints.add(pos['mint'])
                self.offer({pos['mint']: ['open_position']}, now)
        held = [self.db.execute("SELECT * FROM discovery_v8 WHERE chain='solana' AND mint=?", (mint,)).fetchone()
                for mint in held_mints]
        held = sorted((row for row in held if (row['last_full'] or 0)<=now-self.interval),
                      key=lambda row:(row['last_full'] or 0,row['mint']))
        for row in held[:limit]:
            selected[row['mint']] = ['open_position']
        follow = self.db.execute('''SELECT * FROM discovery_v8 WHERE chain='solana' AND stage='confirm'
            AND confirm_until>=? AND next_full<=? ORDER BY COALESCE(last_full,0),mint''', (now,now)).fetchall()
        fresh = self.db.execute('''SELECT * FROM discovery_v8 WHERE chain='solana' AND stage IN ('ready','confirm')
            AND next_full<=? AND last_probe>=? AND (stage='ready' OR confirm_until<?)
            ORDER BY COALESCE(last_full,0),priority DESC,first_seen,mint''', (now,now-300,now)).fetchall()
        # Un hueco para explorar; el resto para confirmar. Si no hay novedad, usar el lote completo.
        quota = limit-1 if fresh and limit > 1 else limit
        for row in follow:
            if len(selected) >= quota:
                break
            selected.setdefault(row['mint'], ['confirmation'] + json.loads(row['sources']))
        for row in fresh + follow:
            if len(selected) >= limit:
                break
            selected.setdefault(row['mint'], ['screened_watchlist'] + json.loads(row['sources']))
        return selected

    def analyzed(self, row, now=None):
        now = timestamp(row['scanned_at']) if now is None else now
        self.offer({row['base_address']: row.get('sources', [])}, now)
        old = self.db.execute("SELECT * FROM discovery_v8 WHERE chain='solana' AND mint=?", (row['base_address'],)).fetchone()
        state = row['state']
        confirming = state in ('candidate','observing')
        until = old['confirm_until'] if old['stage'] == 'confirm' else now+1800
        if state == 'candidate':
            until = now+1800
        if confirming and until is not None and until > now:
            stage, delay = 'confirm', self.interval
        else:
            stage = 'ready'
            delay = 900 if state == 'rejected' else 180
        with self.db:
            self.db.execute('''UPDATE discovery_v8 SET stage=?,last_full=?,next_full=?,next_probe=?,
                confirm_until=? WHERE chain='solana' AND mint=?''',
                (stage,now,now+delay,now+(self.interval if stage=='confirm' else min(delay,300)),until,row['base_address']))

    def report(self, now=None):
        now = time.time() if now is None else now
        entries = self.db.execute("SELECT * FROM discovery_v8 WHERE last_seen>=?", (now-86400,)).fetchall()
        return {'active_24h':len(entries), 'stages':dict(collections.Counter(r['stage'] for r in entries)),
                'screening_reasons':dict(collections.Counter(reason for r in entries for reason in json.loads(r['reason_codes']))),
                'ready_and_due':sum(r['stage']=='ready' and r['next_full']<=now and
                                    r['last_probe'] is not None and now-300<=r['last_probe']<=now for r in entries),
                'ready_with_expired_probe':sum(r['stage']=='ready' and (r['last_probe'] or 0)<now-300 for r in entries),
                'active_confirmations':sum(r['stage']=='confirm' and (r['confirm_until'] or 0)>=now for r in entries),
                'source_counts':dict(collections.Counter(s for r in entries for s in json.loads(r['sources']))),
                'scope':'Preselección; ready no es una aprobación de entrada.'}


def coverage_report(store, now=None, hours=24):
    now = time.time() if now is None else now
    # Límite explícito para que el informe no crezca indefinidamente con el historial.
    data = store.db.execute('''SELECT token,payload FROM observations WHERE observed_at>=?
        ORDER BY id DESC LIMIT 5000''', (now-hours*3600,))
    counts, networks = collections.Counter(), collections.Counter()
    dimensions = {key:collections.Counter() for key in ('mint_check','pool_check','holders','networks','organic','trajectory')}
    profiles = {ident:{'states':collections.Counter(),'market_failures':collections.Counter(),
                      'decision_blockers':collections.Counter(),'quote_statuses':collections.Counter()}
                for ident in ('conservative','balanced','aggressive')}
    total, observations = 0, 0
    for record in data:
        total += 1
        row = json.loads(record['payload'])
        if row.get('scanner_version')!=SCANNER_VERSION:
            continue
        observations += 1
        counts[row['base_address']] += 1
        for key,counter in dimensions.items():
            counter[legacy.obj(row.get(key)).get('status','missing')] += 1
        networks.update(row.get('networks',{}).get('reason_codes',[]))
        for ident,group in profiles.items():
            decision = row.get('profiles',{}).get(ident)
            if decision:
                group['states'][decision['state']] += 1
                group['market_failures'].update(c['code']+':'+c['status'] for c in decision.get('market_checks',[]))
                group['decision_blockers'].update(decision.get('decision_checks',{}).get('blockers',[]))
                group['quote_statuses'].update(q.get('status','missing') for q in decision.get('exit_quotes',[]))
    return {'scanner_version':SCANNER_VERSION, 'hours':hours, 'observations':observations, 'unique_tokens':len(counts),
            'tokens_with_revisits':sum(n>1 for n in counts.values()), 'dimensions':dimensions, 'profiles':profiles,
            'network_reason_codes':dict(networks),
            'observation_limit':5000, 'window_may_be_truncated':total==5000,
            'profitability_proven':False}
