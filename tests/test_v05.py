import copy
import io
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import memecoin_scanner as s
from engine import Engine, EnhancedPolicy, decide, trajectory
from event_stream import normalize_event
from observation_store import ObservationStore
from providers import (Jupiter, TOKEN_2022, TOKEN_PROGRAM, Transport, USDC, holder_evidence,
                       inspect_mint, integer, network_evidence, pool_evidence)
from scanner_v05 import main
from version import SCANNER_VERSION
from test_scanner import NOW, TOKEN, QUOTE, analyze, pair, rug

OWNER1 = 'FDh33VL9aCa8a3U7ySsknMvq5CyfB9e684gdxBMEUHZX'
OWNER2 = '2bDQvnvwd7rNbm4o3JNdN6z9c7MVzp4yqHJPNXPiSTNK'
ACCOUNT1 = '6JETXVHFmWFu95A6UmzJPMa8MLNzsoA1Wd6qrWT4TcP2'
ACCOUNT2 = 'HVVAKKU6QBq5WjaDhwXmxdBshH7w9d5nXcsuzV1KC6x7'


def mint_response():
    return {'context': {'slot': 100}, 'value': {'owner': TOKEN_PROGRAM, 'data': {'parsed': {
        'type': 'mint', 'info': {'mintAuthority': None, 'freezeAuthority': None, 'supply': '1000000000',
                                'decimals': 6, 'isInitialized': True}}}}}


def report():
    return {'mint': TOKEN, 'rugged': False, 'token': {'supply': 1000000000}, 'insiderNetworks': [],
            'markets': [{'pubkey': 'pool-original', 'mintA': TOKEN, 'mintB': QUOTE,
                         'marketType': 'test_fixture', 'lp': {'lpLockedPct': 99}}]}


def organic(now=NOW):
    return {'status': 'ok', 'organic_score': 60.0, 'decimals': 6, 'updated_at': now, 'received_at': now,
            'windows': {'5m': {'numOrganicBuyers': 25.0}}, 'first_pool_at': NOW - 7200,
            'flagged_suspicious': False}


def exit_quote(now=NOW):
    return {'status': 'quoted', 'amount_usdc': 100.0, 'round_trip_loss_pct': 1.0, 'received_at': now,
            'buy': {'out_amount': '1000000', 'received_at': now},
            'sell': {'out_amount': '99000000', 'received_at': now}}


def enriched(now=NOW):
    row = analyze()
    row.update(scanner_version=SCANNER_VERSION, scanned_at=s.utc_string(now), baseline_v04_pass=True,
               simple_baseline_pass=True, enhanced_policy=asdict(EnhancedPolicy()),
               mint_check={'status': 'ok'}, pool_check={'status': 'reported', 'locked_pct': 99},
               holders={'status': 'ok', 'top1_pct_lower_bound': 1, 'top10_pct_lower_bound': 5},
               networks={'status': 'reported', 'max_group_supply_pct': 0}, organic=organic(now),
               trajectory={'status': 'sustained', 'reasons': []}, exit_quotes=[exit_quote(now)],
               lifecycle={'phase': 'established'}, rugged=False)
    row['policy']['min_age_hours'] = 0
    return decide(row, EnhancedPolicy())


class MintTests(unittest.TestCase):
    def test_revoked_authorities_pass(self):
        self.assertEqual(inspect_mint(mint_response())['status'], 'ok')

    def test_missing_authority_is_not_revoked(self):
        for key in ('mintAuthority', 'freezeAuthority'):
            data = mint_response()
            del data['value']['data']['parsed']['info'][key]
            self.assertEqual(inspect_mint(data)['status'], 'incomplete')

    def test_active_authority_blocks(self):
        data = mint_response()
        data['value']['data']['parsed']['info']['freezeAuthority'] = OWNER1
        self.assertEqual(inspect_mint(data)['status'], 'blocked')

    def test_token_2022_requires_explicit_extensions(self):
        data = mint_response()
        data['value']['owner'] = TOKEN_2022
        self.assertEqual(inspect_mint(data)['status'], 'incomplete')
        data['value']['data']['parsed']['info']['extensions'] = [{'extension': 'permanentDelegate'}]
        self.assertEqual(inspect_mint(data)['status'], 'unsupported')
        data['value']['data']['parsed']['info']['extensions'] = [{'extension': 'tokenMetadata'}]
        self.assertEqual(inspect_mint(data)['status'], 'ok')

    def test_wrong_program_and_malformed_supply_never_pass(self):
        data = mint_response()
        data['value']['owner'] = OWNER1
        self.assertEqual(inspect_mint(data)['status'], 'incomplete')
        for amount in (None, 'NaN', 1.2, True, '18446744073709551616'):
            data = mint_response()
            data['value']['data']['parsed']['info']['supply'] = amount
            self.assertNotEqual(inspect_mint(data)['status'], 'ok')

    def test_raw_integer_precision(self):
        self.assertEqual(integer('18446744073709551615'), 18446744073709551615)
        self.assertIsNone(integer(123.0))
        self.assertIsNone(integer('1e9'))


class PoolHolderTests(unittest.TestCase):
    def test_lp_must_match_exact_pool_and_both_mints(self):
        row = analyze()
        self.assertEqual(pool_evidence(report(), row)['status'], 'reported')
        bad = report()
        bad['markets'][0]['pubkey'] = 'other'
        self.assertEqual(pool_evidence(bad, row)['status'], 'incomplete')
        bad = report()
        bad['markets'][0]['mintB'] = OWNER1
        self.assertEqual(pool_evidence(bad, row)['status'], 'incomplete')

    def account(self, owner, mint=TOKEN, amount='10000000'):
        return {'owner': TOKEN_PROGRAM, 'data': {'parsed': {'type': 'account', 'info': {
            'mint': mint, 'owner': owner, 'tokenAmount': {'amount': amount}}}}}

    def test_accounts_resolve_to_one_owner_without_double_counting(self):
        rpc = Mock(side_effect=[{'value': [{'address': ACCOUNT1}, {'address': ACCOUNT2}]},
            {'context': {'slot': 101}, 'value': [self.account(OWNER1), self.account(OWNER1)]}])
        result = holder_evidence(rpc, TOKEN, '1000000000', 100, report())
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['owners']), 1)
        self.assertEqual(result['top1_pct_lower_bound'], 2)
        self.assertEqual(rpc.call_args[0][1][1]['minContextSlot'], 100)

    def test_pool_reserve_excluded_by_account_not_all_same_owner(self):
        data = report()
        data['markets'][0]['liquidityA'] = ACCOUNT1
        rpc = Mock(side_effect=[{'value': [{'address': ACCOUNT1}, {'address': ACCOUNT2}]},
            {'context': {'slot': 101}, 'value': [self.account(OWNER1), self.account(OWNER1)]}])
        result = holder_evidence(rpc, TOKEN, '1000000000', 100, data)
        self.assertEqual(result['top1_pct_lower_bound'], 1)
        self.assertEqual(result['excluded_accounts'], [ACCOUNT1])

    def test_unresolved_or_wrong_mint_is_incomplete(self):
        rpc = Mock(side_effect=[{'value': [{'address': ACCOUNT1}]},
                               {'context': {'slot': 101}, 'value': [self.account(OWNER1, mint=QUOTE)]}])
        self.assertEqual(holder_evidence(rpc, TOKEN, '1000000000', 100, report())['status'], 'incomplete')

    def test_linked_groups_not_summed(self):
        data = report()
        data['insiderNetworks'] = [{'id': 'a', 'size': 3, 'tokenAmount': 100000000},
                                   {'id': 'b', 'size': 4, 'tokenAmount': 200000000}]
        result = network_evidence(data)
        self.assertEqual(result['max_group_supply_pct'], 20)
        self.assertFalse(result['independently_verified'])

    def test_missing_networks_not_empty_networks(self):
        data = report()
        del data['insiderNetworks']
        self.assertEqual(network_evidence(data)['status'], 'unavailable')
        self.assertEqual(network_evidence(report())['status'], 'reported')


class JupiterTests(unittest.TestCase):
    def setUp(self):
        self.transport = Mock(jupiter_key='test-not-a-real-key')
        self.jup = Jupiter(self.transport)

    def token_payload(self):
        return [{'id': TOKEN, 'decimals': 6, 'updatedAt': s.utc_string(NOW), 'organicScore': 60,
                 'stats5m': {'numOrganicBuyers': 20}, 'audit': {}}]

    def test_missing_key_never_calls_network(self):
        self.transport.jupiter_key = ''
        self.assertEqual(self.jup.token(TOKEN)['status'], 'not_configured')
        self.assertEqual(self.jup.round_trip(TOKEN, 100)['status'], 'not_configured')
        self.transport.get.assert_not_called()

    def test_token_identity_and_staleness(self):
        data = self.token_payload()
        self.transport.get.return_value = data
        self.assertEqual(self.jup.token(TOKEN, now=NOW)['status'], 'ok')
        self.assertEqual(self.jup.token(TOKEN, now=NOW + 301)['status'], 'stale')
        data[0]['id'] = QUOTE
        self.assertEqual(self.jup.token(TOKEN, now=NOW)['status'], 'unavailable')

    def test_missing_updated_at_and_score_not_trusted(self):
        data = self.token_payload()
        del data[0]['updatedAt']
        self.transport.get.return_value = data
        self.assertEqual(self.jup.token(TOKEN, now=NOW)['status'], 'stale')
        data = self.token_payload()
        data[0]['organicScore'] = 'NaN'
        self.transport.get.return_value = data
        self.assertEqual(self.jup.token(TOKEN, now=NOW)['status'], 'incomplete')

    def test_suspicious_flag_uses_presence_as_documented_by_jupiter(self):
        # Jupiter indica expresamente comprobar presencia, incluso si el valor es false/null.
        for value in (True, False, None):
            with self.subTest(value=value):
                data = self.token_payload()
                data[0]['audit'] = {'isSus': value}
                self.transport.get.return_value = data
                self.assertTrue(self.jup.token(TOKEN, now=NOW)['flagged_suspicious'])

    def test_quote_request_has_no_wallet_or_execute(self):
        self.transport.get.return_value = {'inputMint': USDC, 'outputMint': TOKEN,
            'inAmount': '100000000', 'outAmount': '12345678901234567', 'transaction': None}
        result = self.jup.quote(USDC, TOKEN, '100000000')
        self.assertEqual(result['out_amount'], '12345678901234567')
        url = self.transport.get.call_args[0][0]
        self.assertIn('/swap/v2/order?', url)
        self.assertNotIn('taker', url)
        self.assertNotIn('execute', url)

    def test_mismatched_quote_and_error_rejected(self):
        base = {'inputMint': USDC, 'outputMint': TOKEN, 'inAmount': '100000000', 'outAmount': '1000'}
        for key, value in [('outputMint', QUOTE), ('inAmount', '1'), ('errorCode', 1),
                           ('outAmount', '0'), ('transaction', 'unsigned-unexpected')]:
            with self.subTest(key=key):
                data = dict(base)
                data[key] = value
                self.transport.get.return_value = data
                with self.assertRaises(RuntimeError):
                    self.jup.quote(USDC, TOKEN, '100000000')

    def test_round_trip_uses_actual_token_quantity(self):
        self.transport.get.side_effect = [
            {'inputMint': USDC, 'outputMint': TOKEN, 'inAmount': '100000000', 'outAmount': '333333333'},
            {'inputMint': TOKEN, 'outputMint': USDC, 'inAmount': '333333333', 'outAmount': '97000000'}]
        result = self.jup.round_trip(TOKEN, 100)
        self.assertAlmostEqual(result['round_trip_loss_pct'], 3)
        self.assertIn('amount=333333333', self.transport.get.call_args[0][0])


class DecisionTests(unittest.TestCase):
    def test_complete_sustained_candidate(self):
        row = enriched()
        self.assertEqual(row['state'], 'candidate', row['decision_reasons'])
        self.assertIsNone(row['dimensions']['probability_of_profit'])

    def test_danger_never_offset_by_organic_score(self):
        row = enriched()
        row['has_danger_flag'] = True
        row['organic']['organic_score'] = 100
        self.assertEqual(decide(row, EnhancedPolicy())['state'], 'rejected')

    def test_missing_key_prevents_candidate(self):
        row = enriched()
        row['organic'] = {'status': 'not_configured'}
        self.assertEqual(decide(row, EnhancedPolicy())['state'], 'insufficient_data')

    def test_pool_missing_is_insufficient_not_fraud(self):
        row = enriched()
        row['pool_check'] = {'status': 'incomplete', 'locked_pct': None}
        self.assertEqual(decide(row, EnhancedPolicy())['state'], 'insufficient_data')

    def test_early_phase_is_watch_only(self):
        row = enriched()
        row['lifecycle']['phase'] = 'bonding_curve'
        self.assertEqual(decide(row, EnhancedPolicy())['state'], 'observing')

    def test_excessive_round_trip_and_group_concentration_block(self):
        row = enriched()
        row['exit_quotes'][0]['round_trip_loss_pct'] = 10
        self.assertEqual(decide(row, EnhancedPolicy())['state'], 'rejected')
        row = enriched()
        row['networks']['max_group_supply_pct'] = 80
        self.assertEqual(decide(row, EnhancedPolicy())['state'], 'rejected')

    def test_three_spaced_updates_required(self):
        policy = EnhancedPolicy()
        current = enriched(NOW + 200)
        history = [enriched(NOW + 90), enriched(NOW)]
        self.assertEqual(trajectory(current, history, policy, NOW + 200)['status'], 'sustained')
        self.assertEqual(trajectory(current, [enriched(NOW + 190)] * 10, policy, NOW + 200)['status'], 'warming_up')

    def test_cached_updates_do_not_count_as_new_activity(self):
        current = enriched(NOW + 200)
        old1, old2 = enriched(NOW + 90), enriched(NOW)
        old1['organic']['updated_at'] = NOW
        old2['organic']['updated_at'] = NOW
        current['organic']['updated_at'] = NOW
        self.assertEqual(trajectory(current, [old1, old2], EnhancedPolicy(), NOW + 200)['status'], 'incomplete')

    def test_future_and_other_pool_snapshots_excluded(self):
        old = enriched(NOW)
        old['pair_address'] = 'different-pool'
        current = enriched(NOW + 200)
        result = trajectory(current, [old, enriched(NOW + 300)], EnhancedPolicy(), NOW + 200)
        self.assertEqual(result['status'], 'warming_up')

    def test_deterioration_blocks_and_windows_not_summed(self):
        current = enriched(NOW + 200)
        current['liquidity_usd'] = 50000
        result = trajectory(current, [enriched(NOW + 90), enriched(NOW)], EnhancedPolicy(), NOW + 200)
        self.assertEqual(result['status'], 'deteriorating')
        self.assertEqual(result['organic_buyers_change'], 0)

    def test_incomplete_older_sample_cannot_extend_confirmation_span(self):
        current = enriched(NOW + 300)
        invalid = enriched(NOW)
        invalid['organic'] = {'status': 'error'}
        result = trajectory(current, [enriched(NOW + 230), enriched(NOW + 160), invalid],
                            EnhancedPolicy(), NOW + 300)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['span_seconds'], 140)

    def test_liquidity_loss_from_intermediate_peak_blocks(self):
        current, peak, oldest = enriched(NOW + 200), enriched(NOW + 90), enriched(NOW)
        current['liquidity_usd'] = oldest['liquidity_usd'] = 100000
        peak['liquidity_usd'] = 200000
        result = trajectory(current, [peak, oldest], EnhancedPolicy(), NOW + 200)
        self.assertEqual(result['status'], 'deteriorating')
        self.assertEqual(result['liquidity_change_pct'], 0)
        self.assertEqual(result['liquidity_drawdown_pct'], 50)

    def test_long_gap_is_not_sustained_activity(self):
        result = trajectory(enriched(NOW + 1000), [enriched(NOW + 90), enriched(NOW)],
                            EnhancedPolicy(), NOW + 1000)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['samples'], 1)

    def test_historical_low_organic_score_is_not_sustained(self):
        old = enriched(NOW)
        old['organic']['organic_score'] = 0
        result = trajectory(enriched(NOW + 200), [enriched(NOW + 90), old],
                            EnhancedPolicy(), NOW + 200)
        self.assertEqual(result['status'], 'deteriorating')

    def test_stale_historical_payload_is_not_confirmation(self):
        old = enriched(NOW)
        old['organic']['updated_at'] = NOW - 301
        result = trajectory(enriched(NOW + 200), [enriched(NOW + 90), old],
                            EnhancedPolicy(), NOW + 200)
        self.assertEqual(result['status'], 'incomplete')

    def test_other_version_or_token_cannot_confirm(self):
        for key, value in [('scanner_version', '0.5.0'), ('base_address', QUOTE), ('chain', 'base')]:
            with self.subTest(key=key):
                old = enriched(NOW)
                old[key] = value
                result = trajectory(enriched(NOW + 200), [enriched(NOW + 90), old],
                                    EnhancedPolicy(), NOW + 200)
                self.assertEqual(result['status'], 'warming_up')


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.db'
        self.store = ObservationStore(self.path)

    def tearDown(self):
        self.store.__exit__()
        self.tmp.cleanup()

    def test_reports_separate_enhanced_policies_and_versions(self):
        original = enriched()
        changed_policy, changed_version = copy.deepcopy(original), copy.deepcopy(original)
        changed_policy['enhanced_policy']['min_organic_score'] = 40
        changed_version['scanner_version'] = '0.5.0'
        for index, row in enumerate((original, changed_policy, changed_version)):
            self.store.record_enriched(row, str(index))
        market = [g for g in self.store.report(NOW)['groups'] if g['horizon_hours'] == 1]
        exits = [g for g in self.store.report_enhanced(NOW)['exit_quote_comparisons']
                 if g['horizon_hours'] == 1 and g['selector'] == 'v05']
        self.assertEqual(len(market), 3)
        self.assertEqual(len(exits), 3)
        self.assertTrue(all(g['observations'] == 1 for g in market + exits))

    def test_reports_separate_requested_size_sets(self):
        one_size, two_sizes = enriched(), enriched()
        additional = exit_quote()
        additional['amount_usdc'] = 250
        two_sizes['exit_quotes'].append(additional)
        self.store.record_enriched(one_size, 'one')
        self.store.record_enriched(two_sizes, 'two')
        market = [g for g in self.store.report(NOW)['groups'] if g['horizon_hours'] == 1]
        exits = [g for g in self.store.report_enhanced(NOW)['exit_quote_comparisons']
                 if g['horizon_hours'] == 1 and g['selector'] == 'v05' and g['size_usdc'] == 100]
        self.assertEqual(len(market), 2)
        self.assertEqual(len(exits), 2)

    def test_event_dedup_and_unknown_creation_time(self):
        event = normalize_event({'mint': TOKEN, 'txType': 'create', 'signature': 'test'}, now=NOW)
        self.assertTrue(self.store.ingest_event(event))
        self.assertFalse(self.store.ingest_event(event))
        row = {'chain': 'solana', 'base_address': TOKEN}
        lifecycle = self.store.lifecycle(row, {}, NOW)
        self.assertEqual(lifecycle['phase'], 'detected')
        self.assertIsNone(lifecycle['created_at'])

    def test_future_or_trade_events_rejected(self):
        self.assertIsNone(normalize_event({'mint': TOKEN, 'txType': 'buy'}, NOW))
        self.assertIsNone(normalize_event({'mint': TOKEN, 'txType': 'create', 'blockTime': NOW + 1000}, NOW))
        self.assertIsNone(normalize_event({'mint': 'bad', 'txType': 'create'}, NOW))

    def test_migration_does_not_reset_creation_or_first_pool(self):
        for kind, at in [('create', NOW - 10000), ('migrate', NOW - 10)]:
            self.store.ingest_event(normalize_event({'mint': TOKEN, 'txType': kind, 'blockTime': at}, now=NOW))
        row = analyze()
        first = self.store.lifecycle(row, {}, NOW)
        row['age_hours'] = 0.01
        row['pair_address'] = 'new-pool'
        second = self.store.lifecycle(row, {}, NOW + 20)
        self.assertEqual(first['created_at'], second['created_at'])
        self.assertEqual(first['first_pool_at'], second['first_pool_at'])
        self.assertEqual(second['phase'], 'recent_migration')

    def test_daily_budget_survives_second_connection_and_resets_next_day(self):
        self.store.reserve_call('jupiter', 1, NOW)
        with ObservationStore(self.path) as another:
            with self.assertRaises(RuntimeError):
                another.reserve_call('jupiter', 1, NOW)
            another.reserve_call('jupiter', 1, NOW + 86400)

    def test_alert_expiry_invalidation_and_idempotent_record(self):
        row = enriched()
        oid = self.store.record_enriched(row, 'run1')
        self.assertEqual(self.store.record_enriched(row, 'run1'), oid)
        self.assertEqual(len(self.store.alerts(now=NOW)), 1)
        self.assertFalse(self.store.alerts(now=NOW + 301)[0]['active'])
        next_row = enriched(NOW + 90)
        self.store.record_enriched(next_row, 'run2')
        self.assertEqual(len(self.store.alerts(now=NOW + 90)), 1)
        invalid = enriched(NOW + 200)
        invalid['state'], invalid['quality_pass'] = 'rejected', False
        self.store.record_enriched(invalid, 'run3')
        alerts = self.store.alerts(now=NOW + 200)
        self.assertEqual(alerts[0]['kind'], 'invalidated')
        self.assertFalse(alerts[1]['active'])

    def test_restart_preserves_queue_and_history(self):
        self.store.record_enriched(enriched(), 'run')
        with ObservationStore(self.path) as second:
            self.assertEqual(second.due_tokens('solana', 5, now=NOW + 70), [TOKEN])
            self.assertEqual(len(second.history('solana', TOKEN, NOW + 70)), 1)

    def test_exit_eval_token_scope_and_cost_scenario(self):
        self.store.record_enriched(enriched(), 'run')
        jup = Mock(enabled=True)
        jup.quote.return_value = {'out_amount': '110000000'}
        self.store.evaluate_exits(jup, now=NOW + 3601)
        record = self.store.db.execute('SELECT * FROM exit_outcomes_v5 WHERE horizon=1').fetchone()
        self.assertEqual(record['status'], 'quoted')
        self.assertAlmostEqual(record['quoted_return_pct'], 10)
        self.assertAlmostEqual(record['stressed_return_pct'], 8.8)
        jup.quote.assert_called_once_with(TOKEN, USDC, '1000000')
        groups = self.store.report_enhanced(now=NOW + 3601)['exit_quote_comparisons']
        self.assertEqual({g['selector'] for g in groups}, {'v05', 'v04_baseline', 'liquidity_activity_baseline'})

    def test_missing_exit_not_counted_as_zero_or_dropped(self):
        self.store.record_enriched(enriched(), 'run')
        jup = Mock(enabled=True)
        jup.quote.side_effect = RuntimeError('no route')
        self.store.evaluate_exits(jup, now=NOW + 3601)
        self.store.evaluate_exits(jup, now=NOW + 5000)
        record = self.store.db.execute('SELECT * FROM exit_outcomes_v5 WHERE horizon=1').fetchone()
        self.assertEqual(record['status'], 'missed')
        self.assertIsNone(record['quoted_return_pct'])

    def test_rejected_tokens_backoff_even_if_rediscovered(self):
        row = enriched()
        row['state'], row['quality_pass'] = 'rejected', False
        self.store.record_enriched(row, 'run')
        self.store.note_token('solana', TOKEN, NOW + 70)
        self.assertFalse(self.store.is_due('solana', TOKEN, NOW + 70))
        self.assertEqual(self.store.due_tokens('solana', 5, now=NOW + 70), [])
        self.assertEqual(self.store.due_tokens('solana', 5, now=NOW + 901), [TOKEN])

    def test_failure_rolls_back_observation_alerts_and_outcomes(self):
        row = enriched()
        row['exit_quotes'] = [exit_quote(), exit_quote()]  # Fuerza UNIQUE en la segunda inserción.
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_enriched(row, 'run')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM observations').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM alerts_v5').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outcomes').fetchone()[0], 0)

    def test_gap_end_is_not_claimed_recovery(self):
        gap = self.store.open_gap('disconnected', NOW)
        self.store.close_gap(gap, NOW + 5)
        item = self.store.report_enhanced()['stream_gaps'][0]
        self.assertEqual(item['recovered'], 0)
        self.assertEqual(item['ended_at'], NOW + 5)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ObservationStore(Path(self.tmp.name) / 'db')
        self.transport = Transport(self.store, 10, rpc_url='https://rpc.example.invalid/secret-value', jupiter_key='test-key')

    def tearDown(self):
        self.store.__exit__()
        self.tmp.cleanup()

    def test_trade_endpoints_and_write_rpc_rejected(self):
        for url in ('https://api.jup.ag/swap/v2/execute', 'https://api.jup.ag/tokens/v2/verify', 'https://evil.test'):
            with self.assertRaises(RuntimeError):
                self.transport.get(url)
        with self.assertRaises(RuntimeError):
            self.transport.rpc('sendTransaction', [])

    def test_rpc_error_does_not_leak_url(self):
        self.transport.opener.open = Mock(side_effect=HTTPError('secret-value', 403, 'secret-value', {}, None))
        with self.assertRaises(RuntimeError) as raised:
            self.transport.rpc('getAccountInfo', [TOKEN])
        self.assertNotIn('secret', str(raised.exception))
        self.assertIn('HTTP 403', str(raised.exception))

    def test_long_retry_after_blocks_following_requests(self):
        self.transport.opener.open = Mock(side_effect=HTTPError('url', 429, 'busy', {'Retry-After': '120'}, None))
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self.transport.get('https://api.jup.ag/tokens/v2/recent')
        self.assertEqual(self.transport.opener.open.call_count, 1)

    def test_api_key_sent_only_to_jupiter(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'[]'
        self.transport.opener.open = Mock(return_value=response)
        self.transport.get('https://api.dexscreener.com/test')
        request = self.transport.opener.open.call_args[0][0]
        self.assertNotIn('X-api-key', request.headers)
        self.transport.get('https://api.jup.ag/tokens/v2/recent')
        request = self.transport.opener.open.call_args[0][0]
        self.assertEqual(request.headers['X-api-key'], 'test-key')


class IntegrationTests(unittest.TestCase):
    def test_old_buy_quote_cannot_be_refreshed_by_a_recent_sell(self):
        with tempfile.TemporaryDirectory() as tmp, ObservationStore(Path(tmp) / 'db') as store:
            transport = Mock(jupiter_key='fixture')
            transport.get.return_value = report()
            transport.rpc.return_value = mint_response()
            engine = Engine(store, transport)
            engine.jupiter.token = Mock(return_value=organic())
            quote = exit_quote()
            quote['buy']['received_at'] = NOW - 31
            engine.jupiter.round_trip = Mock(return_value=quote)
            with patch.object(s, 'get_pairs_for_token', return_value=[pair()]), \
                    patch.object(s, 'get_rugcheck_summary', return_value=rug()), \
                    patch('engine.holder_evidence', return_value={'status': 'ok', 'top1_pct_lower_bound': 1,
                                                                 'top10_pct_lower_bound': 5, 'owners': []}):
                row = engine.analyze('solana', TOKEN, now=NOW)
            self.assertEqual(row['exit_quotes'][0]['status'], 'stale')
            self.assertEqual(row['state'], 'insufficient_data')

    def test_engine_persists_three_observations_then_candidate(self):
        with tempfile.TemporaryDirectory() as tmp, ObservationStore(Path(tmp) / 'db') as store:
            transport = Mock(jupiter_key='fixture')
            transport.get.return_value = report()
            engine = Engine(store, transport)
            with patch.object(s, 'get_pairs_for_token', return_value=[pair()]), \
                    patch.object(s, 'get_rugcheck_summary', return_value=rug()), \
                    patch('engine.holder_evidence', return_value={'status': 'ok', 'top1_pct_lower_bound': 1,
                                                                 'top10_pct_lower_bound': 5, 'owners': []}):
                transport.rpc.return_value = mint_response()
                for index, at in enumerate((NOW, NOW + 90, NOW + 200)):
                    engine.jupiter.token = Mock(return_value=organic(at))
                    engine.jupiter.round_trip = Mock(return_value=exit_quote(at))
                    row = engine.analyze('solana', TOKEN, now=at)
                    store.record_enriched(row, str(index))
                self.assertTrue(row['quality_pass'], row['decision_reasons'])
                self.assertEqual(store.alerts(now=NOW + 200)[0]['kind'], 'candidate')

    def test_scan_lock_rejects_overlap_and_releases_on_close(self):
        from run_lock import ScanLock
        with tempfile.TemporaryDirectory() as tmp:
            first, second = ScanLock(Path(tmp) / 'db'), ScanLock(Path(tmp) / 'db')
            first.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
            finally:
                first.close()
            second.acquire()
            second.close()

    def test_watch_stops_after_requested_cycles_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('scanner_v05.Engine.analyze', side_effect=[enriched(), enriched(NOW + 90)]) as analyze_mock, \
                    patch('scanner_v05.Transport') as transport, patch('scanner_v05.time.sleep'), \
                    patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
                transport.return_value.jupiter_key = ''
                result = main(['--tokens', TOKEN, '--watch', '--cycles', '2', '--db', str(root / 'db'),
                               '--log', str(root / 'log.csv'), '--json-output', str(root / 'scan.json')])
            self.assertEqual(result, 0)
            self.assertEqual(analyze_mock.call_count, 2)
            with ObservationStore(root / 'db') as store:
                self.assertEqual(store.db.execute('SELECT count(*) FROM observations').fetchone()[0], 2)

    def test_cli_invalid_parameters_before_network(self):
        for args in (['--sizes-usdc', 'NaN'], ['--min-samples', '1'], ['--with-stream'],
                     ['--candidates', 'unknown'], ['--interval', '1'], ['--exit-stress-bps', '10001'],
                     ['--min-samples', '32'], ['--min-observation-seconds', '1801'],
                     ['--data-max-age-seconds', '59'], ['--sizes-usdc', '0.0000001']):
            with self.subTest(args=args), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
                main(args)

    def test_report_and_alerts_use_no_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('scanner_v05.Transport', side_effect=AssertionError('unexpected network')), \
                    patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(main(['--report', '--db', str(Path(tmp) / 'db')]), 0)
                self.assertEqual(main(['--alerts', '--db', str(Path(tmp) / 'db')]), 0)

    def test_no_configured_jupiter_still_records_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'scan.json'
            with patch('scanner_v05.Engine.analyze', return_value=enriched()), \
                    patch('scanner_v05.Transport') as transport, \
                    patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
                transport.return_value.jupiter_key = ''
                code = main(['--tokens', TOKEN, '--db', str(Path(tmp) / 'db'), '--log', str(Path(tmp) / 'scan.csv'),
                             '--json-output', str(output)])
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(output.read_text())['configuration']['jupiter_configured'])


if __name__ == '__main__':
    unittest.main()
