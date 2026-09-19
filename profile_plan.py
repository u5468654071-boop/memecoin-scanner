"""Tres hipótesis de investigación sobre una misma observación de mercado."""
import hashlib
import json
import math
from dataclasses import asdict

from engine import EnhancedPolicy, decide, trajectory
from memecoin_scanner import Policy
from paper_trading import PaperPolicy, micro_usdc

PROFILE_IDS = ('conservative', 'balanced', 'aggressive')
LABELS = dict(zip(PROFILE_IDS, ('Conservador', 'Equilibrado', 'Agresivo')))
DECISION_FIELDS = ('policy', 'enhanced_policy', 'trajectory', 'exit_quotes', 'state', 'quality_pass',
                   'decision_checks', 'decision_reasons', 'quality_fail_reasons', 'research_score',
                   'dimensions', 'selection_evidence', 'invalidates_if', 'profile_id', 'profile_plan_hash', 'market_checks')


class ProfilePlan:
    def __init__(self, raw):
        try:
            if set(raw) != {'schema', 'total_initial_usdc', 'max_total_exposure_usdc',
                            'max_token_exposure_usdc', 'daily_loss_limit_usdc', 'profiles'} or type(raw['schema']) is not int or raw['schema'] != 1:
                raise ValueError()
            if tuple(p['id'] for p in raw['profiles']) != PROFILE_IDS:
                raise ValueError()
            profiles = []
            for item in raw['profiles']:
                if set(item) != {'id', 'label', 'market', 'enhanced', 'paper'} or item['label'] != LABELS[item['id']]:
                    raise ValueError()
                market, enhanced, paper = Policy(**item['market']), EnhancedPolicy(**item['enhanced']), PaperPolicy(**item['paper']).validate()
                for key, val in {**asdict(market), **asdict(enhanced)}.items():
                    if type(val) not in (float, int) or not math.isfinite(val) or (val < 0 and key != 'min_price_change_h1'):
                        raise ValueError()
                if (not 0 <= market.min_age_hours <= market.max_age_hours <= 720
                        or market.min_liquidity < 20000 or not 80 <= market.min_lp_locked_pct <= 100
                        or not 0 <= market.max_rugcheck_score <= 30 or market.min_sells_h1 < 5
                        or market.min_txns_h1 < 30 or market.min_volume_h1 < 1000
                        or market.min_price_change_h1 >= market.max_price_change_h1
                        or not 20 <= enhanced.min_organic_score <= 100
                        or not 0 < enhanced.max_owner_pct <= 20 or not 0 < enhanced.max_top10_pct <= 60
                        or enhanced.max_owner_pct > enhanced.max_top10_pct
                        or not 0 < enhanced.max_network_pct <= 30 or not 0 < enhanced.max_round_trip_loss_pct <= 5
                        or type(enhanced.min_samples) is not int or not 2 <= enhanced.min_samples <= 31
                        or not 60 <= enhanced.min_observation_seconds <= 1800
                        or not 60 <= enhanced.data_max_age_seconds <= 300
                        or not 0 < enhanced.max_liquidity_drop_pct <= 20
                        or enhanced.min_organic_buyers_5m < 5):
                    raise ValueError()
                profiles.append({'id': item['id'], 'label': item['label'], 'market': asdict(market),
                                 'enhanced': asdict(enhanced), 'paper': asdict(paper)})
            amounts = {k: micro_usdc(raw[k]) for k in ('total_initial_usdc', 'max_total_exposure_usdc',
                                                      'max_token_exposure_usdc', 'daily_loss_limit_usdc')}
            if (sum(micro_usdc(p['paper']['initial_usdc']) for p in profiles) != amounts['total_initial_usdc']
                    or not 0 < amounts['daily_loss_limit_usdc'] <= amounts['total_initial_usdc']
                    or not 0 < amounts['max_token_exposure_usdc'] <= amounts['max_total_exposure_usdc'] <= amounts['total_initial_usdc']
                    or any(micro_usdc(p['paper']['order_usdc']) + micro_usdc(p['paper']['fixed_cost_usdc_per_side'])
                           > amounts['max_token_exposure_usdc'] for p in profiles)):
                raise ValueError()
            self.data = {**raw, 'profiles': profiles}
            self.encoded = json.dumps(self.data, sort_keys=True, allow_nan=False)
            self.digest = hashlib.sha256(self.encoded.encode()).hexdigest()
            self.profiles = profiles
        except (ValueError, KeyError, TypeError, OverflowError):
            raise ValueError('Plan de perfiles inválido: revisar distribución, límites y filtros mínimos') from None

    @classmethod
    def load(cls, path):
        try:
            return cls(json.loads(path.read_text(encoding='utf-8')))
        except json.JSONDecodeError:
            raise ValueError('JSON del plan de perfiles inválido') from None

    @property
    def sizes(self):
        return [p['paper']['order_usdc'] for p in self.profiles]

    def evaluate(self, row, history, now):
        decisions = {}
        for profile in self.profiles:
            ident = profile['id']
            policy = EnhancedPolicy(**profile['enhanced'])
            current = {**row, 'policy': profile['market'], 'enhanced_policy': profile['enhanced'],
                       'profile_id': ident, 'profile_plan_hash': self.digest,
                       'exit_quotes': [q for q in row.get('exit_quotes', []) if q['amount_usdc'] == profile['paper']['order_usdc']]}
            old = [{**r, **r['profiles'][ident]} for r in history
                   if r.get('profile_plan_hash') == self.digest and ident in r.get('profiles', {})]
            current['trajectory'] = trajectory(current, old, policy, now)
            decide(current, policy)
            decisions[ident] = {key: current[key] for key in DECISION_FIELDS}
        return decisions

    def sizes_to_quote(self, row, now):
        if row['lifecycle']['phase'] in ('detected', 'bonding_curve', 'unknown'):
            return []
        decisions = self.evaluate({**row, 'exit_quotes': []}, [], now)
        quote_missing = 'no hay cotización de ida y vuelta utilizable para todos los tamaños'
        return [p['paper']['order_usdc'] for p in self.profiles
                if not decisions[p['id']]['decision_checks']['blockers']
                and not any(c['status']=='waiting' for c in decisions[p['id']]['market_checks'])
                and not set(decisions[p['id']]['decision_checks']['missing']) - {quote_missing}]
