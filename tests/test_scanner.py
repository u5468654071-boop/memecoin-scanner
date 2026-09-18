import copy
import csv
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import memecoin_scanner as s
from tracking import Store

NOW = 1800000000.0
TOKEN = '6rHkNb7HCtkpvdnVJsBCZHH5dw3AndqEjfmbEGhooR7t'
QUOTE = 'So11111111111111111111111111111111111111112'


def pair():
    return {
        'chainId': 'solana', 'pairAddress': 'pool-original', 'dexId': 'raydium',
        'baseToken': {'address': TOKEN, 'symbol': 'TEST'}, 'quoteToken': {'address': QUOTE},
        'priceUsd': '0.001', 'pairCreatedAt': (NOW - 7200) * 1000,
        'liquidity': {'usd': 100000}, 'fdv': 1000000,
        'volume': {'m5': 500, 'h1': 10000, 'h6': 60000, 'h24': 240000},
        'txns': {'h1': {'buys': 100, 'sells': 80}, 'h24': {'buys': 1000, 'sells': 900}},
        'priceChange': {'h1': 5},
    }


def rug():
    return {'score_normalised': 5, 'lpLockedPct': 99, 'risks': []}


def analyze(p=None, r=None, policy=None):
    with patch.object(s, 'get_pairs_for_token', return_value=[pair() if p is None else p]), \
            patch.object(s, 'get_rugcheck_summary', return_value=rug() if r is None else r):
        return s.analyze_token('solana', TOKEN, policy, now=NOW)


class SelectionTests(unittest.TestCase):
    def test_complete_candidate_passes(self):
        row = analyze()
        self.assertTrue(row['quality_pass'], row['quality_fail_reasons'])
        self.assertGreater(row['research_score'], 0)
        self.assertLessEqual(row['research_score'], 100)

    def test_missing_rugcheck_cannot_pass(self):
        with patch.object(s, 'get_rugcheck_summary', return_value=None), \
                patch.object(s, 'get_pairs_for_token', return_value=[pair()]):
            row = s.analyze_token('solana', TOKEN, now=NOW)
        self.assertFalse(row['quality_pass'])
        self.assertIsNone(row['research_score'])
        self.assertIsNone(row['combined_score'])

    def test_rugcheck_timeout_is_recorded(self):
        with patch.object(s, 'get_rugcheck_summary', side_effect=RuntimeError('timeout')), \
                patch.object(s, 'get_pairs_for_token', return_value=[pair()]):
            row = s.analyze_token('solana', TOKEN, now=NOW)
        self.assertEqual(row['rugcheck_status'], 'error')
        self.assertFalse(row['quality_pass'])

    def test_rugcheck_invalid_or_missing_fields(self):
        for field, value in [('lpLockedPct', None), ('score_normalised', None), ('risks', None),
                             ('score_normalised', 101), ('lpLockedPct', float('nan')),
                             ('risks', [{}]), ('risks', [{'name': 'x', 'level': 'unknown'}]),
                             ('error', 'not indexed')]:
            with self.subTest(field=field, value=value):
                data = rug()
                data[field] = value
                self.assertFalse(analyze(r=data)['quality_pass'])

    def test_danger_cannot_be_offset_by_low_score(self):
        data = rug()
        data['score_normalised'] = 0
        data['risks'] = [{'name': 'Freeze Authority', 'level': 'danger'}]
        self.assertFalse(analyze(r=data)['quality_pass'])

    def test_missing_liquidity_is_not_zero(self):
        for value in [None, {}, {'usd': None}, {'usd': 'NaN'}, {'usd': -1}, {'usd': True}]:
            p = pair()
            p['liquidity'] = value
            row = analyze(p=p)
            self.assertIsNone(row['liquidity_usd'])
            self.assertFalse(row['quality_pass'])

    def test_bad_numbers_never_pass(self):
        for key, value in [('priceUsd', 'NaN'), ('priceUsd', 'Infinity'), ('priceUsd', 0),
                           ('fdv', -1), ('pairCreatedAt', (NOW + 1000) * 1000),
                           ('pairCreatedAt', None), ('priceChange', {}), ('txns', {})]:
            with self.subTest(key=key, value=value):
                p = pair()
                p[key] = value
                self.assertFalse(analyze(p=p)['quality_pass'])

    def test_age_not_rounded_across_limit(self):
        p = pair()
        p['pairCreatedAt'] = (NOW - 48.001 * 3600) * 1000
        self.assertFalse(analyze(p=p)['quality_pass'])

    def test_unsupported_chain_cannot_pass(self):
        p = pair()
        p['chainId'] = 'base'
        with patch.object(s, 'get_pairs_for_token', return_value=[p]):
            row = s.analyze_token('base', TOKEN, now=NOW)
        self.assertEqual(row['rugcheck_status'], 'unsupported')
        self.assertFalse(row['quality_pass'])

    def test_wrong_chain_or_quote_side_cannot_supply_price(self):
        wrong = pair()
        wrong['baseToken']['address'] = QUOTE
        wrong['quoteToken']['address'] = TOKEN
        self.assertIsNone(s.select_pair([wrong], 'solana', TOKEN))
        wrong = pair()
        wrong['chainId'] = 'base'
        self.assertIsNone(s.select_pair([wrong], 'solana', TOKEN))

    def test_sol_addresses_are_case_sensitive(self):
        self.assertFalse(s.same_address('solana', TOKEN, TOKEN.lower()))
        self.assertTrue(s.same_address('base', '0xAB', '0xab'))

    def test_known_quote_preferred_and_spoofed_symbol_rejected(self):
        bad = pair()
        bad['liquidity']['usd'] = 1e9
        bad['quoteToken'] = {'address': 'fake', 'symbol': 'USDC'}
        good = pair()
        self.assertEqual(s.select_pair([bad, good], 'solana', TOKEN), good)
        self.assertFalse(analyze(p=bad)['quality_pass'])

    def test_no_sell_activity_rejected(self):
        p = pair()
        p['txns']['h1']['sells'] = 0
        self.assertFalse(analyze(p=p)['quality_pass'])

    def test_zero_activity_never_ranks_even_if_thresholds_relaxed(self):
        p = pair()
        p['txns']['h1'] = {'buys': 0, 'sells': 0}
        row = analyze(p=p, policy=replace(s.Policy(), min_txns_h1=0, min_sells_h1=0))
        self.assertFalse(row['quality_pass'])
        self.assertIsNone(row['research_score'])

    def test_overflow_ratio_does_not_crash_json(self):
        p = pair()
        p['fdv'] = 1e308
        p['liquidity']['usd'] = 1e-308
        row = analyze(p=p)
        self.assertFalse(row['quality_pass'])
        json.dumps(row, allow_nan=False)

    def test_extreme_turnover_and_pump_rejected(self):
        p = pair()
        p['volume']['h1'] = 2000000
        self.assertFalse(analyze(p=p)['quality_pass'])
        p = pair()
        p['priceChange']['h1'] = 200
        self.assertFalse(analyze(p=p)['quality_pass'])

    def test_websites_not_fetched_and_no_ranking_bonus(self):
        p = pair()
        p['info'] = {'websites': [{'url': 'http://127.0.0.1/private'}],
                     'socials': [{'url': 'http://169.254.169.254/'}]}
        with patch.object(s, 'http_get', side_effect=AssertionError('unexpected request')):
            self.assertEqual(analyze(p=p)['research_score'], analyze()['research_score'])

    def test_market_failure_preserves_token(self):
        with patch.object(s, 'get_pairs_for_token', side_effect=RuntimeError('offline')):
            row = s.analyze_token('solana', TOKEN, now=NOW)
        self.assertEqual(row['base_address'], TOKEN)
        self.assertFalse(row['quality_pass'])

    def test_sources_deduplicate_keep_provenance_and_continue(self):
        data = [{'chainId': 'solana', 'tokenAddress': TOKEN}] * 2
        with patch.object(s, 'http_get', side_effect=[data, data]):
            addresses, errors = s.gather_candidates('solana', ['boosted', 'profiles'], 20)
        self.assertEqual(addresses, {TOKEN: ['boosted', 'profiles']})
        self.assertFalse(errors)
        with patch.object(s, 'http_get', side_effect=[RuntimeError('403'), data]):
            addresses, errors = s.gather_candidates('solana', ['boosted', 'profiles'], 20)
        self.assertEqual(len(addresses), 1)
        self.assertEqual(len(errors), 1)


class HTTPTests(unittest.TestCase):
    def client(self):
        return s.APIClient(interval=0)

    def response(self, payload):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = payload
        return response

    def test_429_then_success_honors_retry_after(self):
        client = self.client()
        client.opener.open = Mock(side_effect=[HTTPError('url', 429, 'busy', {'Retry-After': '5'}, None),
                                              self.response(b'{"ok":true}')])
        with patch.object(s.time, 'sleep') as sleep:
            self.assertEqual(client.get(s.DEX_BASE + '/test'), {'ok': True})
        self.assertGreaterEqual(sleep.call_args[0][0], 5)
        self.assertEqual(client.opener.open.call_count, 2)

    def test_403_does_not_retry(self):
        client = self.client()
        client.opener.open = Mock(side_effect=HTTPError('url', 403, 'forbidden', {}, None))
        with patch.object(s.time, 'sleep') as sleep, self.assertRaises(RuntimeError):
            client.get(s.DEX_BASE + '/test')
        self.assertEqual(client.opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_long_retry_after_does_not_retry_early(self):
        client = self.client()
        client.opener.open = Mock(side_effect=HTTPError('url', 429, 'busy', {'Retry-After': '120'}, None))
        with self.assertRaises(RuntimeError):
            client.get(s.DEX_BASE + '/test')
        self.assertEqual(client.opener.open.call_count, 1)

    def test_404_unindexed(self):
        client = self.client()
        client.opener.open = Mock(side_effect=HTTPError('url', 404, 'missing', {}, None))
        self.assertIsNone(client.get(s.RUGCHECK_BASE + '/test', quiet_404=True))

    def test_malformed_json_retries_bounded(self):
        client = self.client()
        client.opener.open = Mock(return_value=self.response(b'not json'))
        with patch.object(s.time, 'sleep'), self.assertRaises(RuntimeError):
            client.get(s.DEX_BASE + '/test')
        self.assertEqual(client.opener.open.call_count, 3)

    def test_unknown_host_rejected(self):
        for url in ['http://api.dexscreener.com', 'https://127.0.0.1', 'https://api.dexscreener.com.evil.test']:
            with self.assertRaises(RuntimeError):
                self.client().get(url)


class PersistenceTests(unittest.TestCase):
    def test_empty_log_and_multiple_schema_changes_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'scan.csv'
            s.log_results([], path)
            self.assertFalse(path.exists())
            path.touch()
            s.log_results([analyze()], path)
            with path.open() as handle:
                self.assertEqual(next(csv.reader(handle)), s.CSV_FIELDS)
            for _ in range(2):
                path.write_text('old,header\na,b\n')
                s.log_results([analyze()], path)
            self.assertEqual(len(list(Path(tmp).glob('*_schema_*.csv'))), 2)

    def test_formula_cell_escaped(self):
        self.assertEqual(s.csv_cell(' =HYPERLINK("x")'), '\' =HYPERLINK("x")')
        self.assertEqual(s.csv_cell(-42.0), -42.0)

    def test_observations_idempotent_and_all_candidates_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / 'db') as store:
            row = analyze()
            store.record(row, 'run1')
            store.record(row, 'run1')
            rejected = copy.deepcopy(row)
            rejected['quality_pass'] = False
            store.record(rejected, 'run2')
            self.assertEqual(store.db.execute('SELECT count(*) FROM observations').fetchone()[0], 2)
            self.assertEqual(store.db.execute('SELECT count(*) FROM outcomes').fetchone()[0], 6)

    def test_outcome_uses_original_pair_and_no_future_leakage(self):
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / 'db') as store:
            store.record(analyze(), 'run')
            p = pair()
            p['priceUsd'] = '0.0015'
            fetch = Mock(return_value={'pairs': [p]})
            store.evaluate_due(fetch, now=NOW + 3599)
            fetch.assert_not_called()
            store.evaluate_due(fetch, now=NOW + 3601)
            self.assertIn('/solana/pool-original', fetch.call_args[0][0])
            item = store.db.execute('SELECT * FROM outcomes WHERE horizon=1').fetchone()
            self.assertEqual(item['status'], 'observed')
            self.assertAlmostEqual(item['return_pct'], 50)
            self.assertEqual(item['checked_at'], NOW + 3601)

    def test_late_observation_is_missed_without_fetching(self):
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / 'db') as store:
            store.record(analyze(), 'run')
            fetch = Mock()
            store.evaluate_due(fetch, now=NOW + 5000)
            fetch.assert_not_called()
            self.assertEqual(store.db.execute('SELECT status FROM outcomes WHERE horizon=1').fetchone()[0], 'missed')

    def test_disappeared_pair_counted_not_invented_as_zero_return(self):
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / 'db') as store:
            store.record(analyze(), 'run')
            store.evaluate_due(Mock(return_value={'pairs': []}), now=NOW + 3601)
            group = next(g for g in store.report(now=NOW + 3601)['groups'] if g['horizon_hours'] == 1)
            self.assertEqual(group['status_counts'], {'unavailable': 1})
            self.assertEqual(group['coverage_of_due_pct'], 0)
            self.assertIsNone(group['median_gross_return_pct_observed_only'])

    def test_wrong_base_or_pair_not_used(self):
        for field in ['pairAddress', 'baseToken']:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / 'db') as store:
                store.record(analyze(), 'run')
                p = pair()
                p[field] = {'address': 'OTHER'} if field == 'baseToken' else 'other-pool'
                store.evaluate_due(Mock(return_value={'pairs': [p]}), now=NOW + 3601)
                self.assertEqual(store.db.execute('SELECT status FROM outcomes WHERE horizon=1').fetchone()[0], 'unavailable')

    def test_zero_liquidity_is_unavailable_even_with_price(self):
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / 'db') as store:
            store.record(analyze(), 'run')
            p = pair()
            p['liquidity']['usd'] = 0
            store.evaluate_due(Mock(return_value={'pairs': [p]}), now=NOW + 3601)
            self.assertEqual(store.db.execute('SELECT status FROM outcomes WHERE horizon=1').fetchone()[0], 'unavailable')

    def test_transient_error_remains_retryable_then_completes(self):
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / 'db') as store:
            store.record(analyze(), 'run')
            store.evaluate_due(Mock(side_effect=RuntimeError('429')), now=NOW + 3601)
            self.assertEqual(store.db.execute('SELECT status FROM outcomes WHERE horizon=1').fetchone()[0], 'pending')
            store.evaluate_due(Mock(return_value={'pairs': [pair()]}), now=NOW + 3620)
            self.assertEqual(store.db.execute('SELECT status FROM outcomes WHERE horizon=1').fetchone()[0], 'observed')

    def test_changed_policy_separates_cohorts(self):
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / 'db') as store:
            store.record(analyze(), 'run1')
            store.record(analyze(policy=replace(s.Policy(), min_liquidity=30000)), 'run2')
            self.assertEqual(len(store.report()['groups']), 6)

    def test_slow_response_outside_window_is_not_a_return(self):
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / 'db') as store:
            store.record(analyze(), 'run')
            with patch('tracking.time.time', side_effect=[NOW + 3601, NOW + 3601, NOW + 5000]):
                store.evaluate_due(Mock(return_value={'pairs': [pair()]}))
            item = store.db.execute('SELECT * FROM outcomes WHERE horizon=1').fetchone()
            self.assertEqual(item['status'], 'missed')
            self.assertIsNone(item['return_pct'])

    def test_failed_initial_scan_remains_in_denominator(self):
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / 'db') as store:
            with patch.object(s, 'get_pairs_for_token', side_effect=RuntimeError('offline')):
                row = s.analyze_token('solana', TOKEN, now=NOW)
            store.record(row, 'run')
            group = next(g for g in store.report(now=NOW + 3601)['groups'] if g['horizon_hours'] == 1)
            self.assertEqual(group['status_counts'], {'untrackable': 1})
            self.assertEqual(group['due_observations'], 1)


class CLITests(unittest.TestCase):
    def test_full_offline_scan_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(s, 'get_pairs_for_token', return_value=[pair()]), \
                    patch.object(s, 'get_rugcheck_summary', return_value=rug()), \
                    patch.object(s.time, 'time', return_value=NOW), \
                    patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
                code = s.legacy_main(['--tokens', TOKEN, '--db', str(root / 'db'), '--log', str(root / 'scan.csv'),
                               '--json-output', str(root / 'scan.json')])
            self.assertEqual(code, 0)
            data = json.loads((root / 'scan.json').read_text())
            self.assertTrue(data['rows'][0]['quality_pass'])
            self.assertEqual(data['rows'][0]['sources'], ['manual'])
            self.assertTrue((root / 'db').exists())

    def test_invalid_cli_policy(self):
        for args in [['--min-age-hours', 'nan'], ['--max-tokens', '0'], ['--min-liquidity', '0'],
                     ['--min-age-hours', '80'], ['--min-lp-locked-pct', '120']]:
            with self.subTest(args=args), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
                s.legacy_main(args)

    def test_total_market_failure_returns_nonzero_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(s, 'get_pairs_for_token', side_effect=RuntimeError('offline')), \
                    patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
                code = s.legacy_main(['--tokens', TOKEN, '--db', str(root / 'db'), '--log', str(root / 'scan.csv'),
                               '--json-output', str(root / 'scan.json')])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads((root / 'scan.json').read_text())['rows'][0]['error'], 'offline')


if __name__ == '__main__':
    unittest.main()
