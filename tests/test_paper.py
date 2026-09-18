import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from observation_store import ObservationStore
from paper_trading import PaperLedger, PaperPolicy, micro_usdc
from providers import USDC
from server import main
from service_health import heartbeat, healthy
from test_scanner import NOW, TOKEN, QUOTE
from test_v05 import enriched, OWNER1


class FakeJupiter:
    enabled = True

    def __init__(self, clock):
        self.clock, self.returned = clock, 25000000
        self.fail = False
        self.calls = []

    def quote(self, src, dst, amount):
        self.calls.append((src, dst, str(amount)))
        if self.fail:
            raise RuntimeError('no route')
        return {'input_mint': src, 'output_mint': dst, 'in_amount': str(amount),
                'out_amount': str(self.returned), 'received_at': self.clock()}

    def round_trip(self, mint, size):
        buy = self.quote(USDC, mint, micro_usdc(size))
        buy['out_amount'] = '1000000'
        sell = self.quote(mint, USDC, buy['out_amount'])
        return {'status': 'quoted', 'amount_usdc': size, 'buy': buy, 'sell': sell,
                'round_trip_loss_pct': 0, 'received_at': self.clock()}


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.db'
        self.store = ObservationStore(self.path)
        self.policy = PaperPolicy()
        self.ledger = PaperLedger(self.store, self.policy)
        self.now = NOW
        self.clock = lambda: self.now
        self.jupiter = FakeJupiter(self.clock)
        self.serial = 0

    def tearDown(self):
        self.store.__exit__()
        self.tmp.cleanup()

    def signal(self, mint=TOKEN, **overrides):
        row = enriched(self.now)
        row['base_address'] = mint
        row['exit_quotes'][0]['amount_usdc'] = 25
        row.update(overrides)
        self.serial += 1
        return self.store.record_enriched(row, str(self.serial))

    def open(self, signal=None, **kwargs):
        return self.ledger.try_open(signal or self.signal(), self.jupiter, self.clock, **kwargs)

    def test_entry_uses_exact_quantity_and_charges_assumptions_once(self):
        self.assertTrue(self.open())
        position = self.ledger.open_positions()[0]
        self.assertEqual(position['quantity_raw'], '995000')
        self.assertEqual(self.jupiter.calls[-1], (TOKEN, USDC, '995000'))
        self.assertEqual(position['mark_micro'], 24825000)
        report = self.ledger.report(self.now)
        self.assertEqual(report['cash_usdc'], 974.95)
        self.assertEqual(report['equity_usdc'], 999.775)

    def test_restart_keeps_balance_policy_and_unique_entry(self):
        signal = self.signal()
        self.assertTrue(self.open(signal))
        with ObservationStore(self.path) as second:
            ledger = PaperLedger(second, self.policy)
            self.assertEqual(ledger.report(self.now)['cash_usdc'], 974.95)
            self.assertFalse(ledger.try_open(signal, self.jupiter, self.clock))
            with self.assertRaises(ValueError):
                PaperLedger(second, replace(self.policy, order_usdc=10))
            self.assertEqual(ledger.report(self.now)['cash_usdc'], 974.95)

    def test_newer_invalid_observation_blocks_older_eligible_signal(self):
        signal = self.signal()
        self.now += 1
        self.signal(quality_pass=False, state='rejected')
        self.assertFalse(self.open(signal))
        self.assertFalse(self.jupiter.calls)

    def test_future_stale_and_other_version_signals_block(self):
        signal = self.signal()
        for delta in (-1, 61):
            self.now = NOW + delta
            self.assertFalse(self.open(signal))
        self.now = NOW
        self.assertFalse(self.open(self.signal(scanner_version='0.5.1')))

    def test_missing_jupiter_and_missing_or_wrong_size_evidence_block(self):
        self.jupiter.enabled = False
        self.assertFalse(self.open())
        self.jupiter.enabled = True
        self.assertFalse(self.open(self.signal(exit_quotes=[])))
        quote = enriched()['exit_quotes']
        self.assertFalse(self.open(self.signal(exit_quotes=quote)))

    def test_malformed_stale_wrong_mint_and_wrong_amount_quotes_block(self):
        original = self.jupiter.quote
        for changed in ({'received_at': NOW-31}, {'received_at': NOW+1}, {'out_amount': '1e9'},
                        {'input_mint': QUOTE}, {'in_amount': '1'}, {'out_amount': True}):
            with self.subTest(changed=changed):
                self.jupiter.quote = lambda *args: dict(original(*args), **changed)
                self.assertFalse(self.open())
        self.assertEqual(self.ledger.report(self.now)['cash_usdc'], 1000)

    def test_buy_can_expire_while_obtaining_initial_exit_mark(self):
        original = self.jupiter.quote
        def delayed(src, dst, amount):
            if str(amount) == '995000':
                self.now += 31
            return original(src, dst, amount)
        self.jupiter.quote = delayed
        self.assertFalse(self.open())

    def test_pause_arriving_during_network_prevents_entry(self):
        paused = [False]
        original = self.jupiter.quote
        def quote(*args):
            paused[0] = True
            return original(*args)
        self.jupiter.quote = quote
        self.assertFalse(self.open(paused=lambda: paused[0]))

    def test_max_positions_and_same_mint_dedup(self):
        self.assertTrue(self.open())
        self.assertFalse(self.open())
        self.assertTrue(self.open(self.signal(QUOTE)))
        self.assertTrue(self.open(self.signal(OWNER1)))
        self.assertIn('máximo de posiciones ficticias', self.ledger.entry_blockers(self.now))
        self.assertEqual(len(self.ledger.open_positions()), 3)

    def test_insufficient_cash_never_goes_negative(self):
        with self.store.db:
            self.store.db.execute('UPDATE paper_account SET cash_micro=25049999')
        self.assertFalse(self.open())
        self.assertEqual(self.ledger.report(self.now)['cash_usdc'], 25.049999)

    def test_stop_loss_closes_at_observed_quote_not_trigger_price(self):
        self.open()
        self.now += 30
        self.jupiter.returned = 10000000
        self.ledger.refresh_positions(self.jupiter, self.clock)
        report = self.ledger.report(self.now)
        self.assertEqual(report['cash_usdc'], 984.85)
        self.assertEqual(report['realized_pnl_usdc'], -15.15)
        self.assertEqual(report['recent_closed'][0]['exit_reason'], 'stop-loss observado')
        self.ledger.refresh_positions(self.jupiter, self.clock)
        self.assertEqual(self.ledger.report(self.now)['cash_usdc'], 984.85)

    def test_take_profit_and_reentry_cooldown_survive_restart(self):
        self.open()
        self.now += 1
        self.jupiter.returned = 40000000
        self.ledger.refresh_positions(self.jupiter, self.clock)
        self.assertIn('objetivo', self.ledger.report(self.now)['recent_closed'][0]['exit_reason'])
        self.jupiter.returned = 25000000
        self.assertFalse(self.open())
        self.now += 3601
        self.assertTrue(self.open())

    def test_trailing_exit_uses_highest_observed_net_value(self):
        self.open()
        self.jupiter.returned = 30000000
        self.ledger.refresh_positions(self.jupiter, self.clock)
        self.assertEqual(len(self.ledger.open_positions()), 1)
        self.jupiter.returned = 26500000
        self.ledger.refresh_positions(self.jupiter, self.clock)
        self.assertIn('retroceso', self.ledger.report(self.now)['recent_closed'][0]['exit_reason'])

    def test_time_limit_with_missing_route_keeps_cash_locked_until_recovery(self):
        self.open()
        self.now += 3601
        self.jupiter.fail = True
        self.ledger.refresh_positions(self.jupiter, self.clock)
        report = self.ledger.report(self.now)
        self.assertEqual(report['cash_usdc'], 974.95)
        self.assertEqual(report['closed_positions'], 0)
        self.assertIsNone(report['equity_usdc'])
        self.assertTrue(report['entry_blockers'])
        self.jupiter.fail = False
        self.ledger.refresh_positions(self.jupiter, self.clock)
        self.assertEqual(self.ledger.report(self.now)['closed_positions'], 1)
        self.assertEqual(self.ledger.report(self.now)['recent_closed'][0]['exit_reason'], 'tiempo máximo')

    def test_new_invalidation_exits_and_manual_pause_does_not_disable_exits(self):
        self.open()
        self.now += 1
        self.signal(quality_pass=False, state='insufficient_data')
        self.ledger.tick(self.jupiter, self.clock, paused=True)
        self.assertEqual(self.ledger.report(self.now)['recent_closed'][0]['exit_reason'], 'candidato invalidado')

    def test_close_all_does_not_reopen_another_candidate(self):
        self.open()
        self.signal(QUOTE)
        report = self.ledger.tick(self.jupiter, self.clock, close_all=True)
        self.assertEqual(report['closed_positions'], 1)
        self.assertFalse(report['open_positions'])

    def test_daily_loss_counts_losers_without_offsetting_gains(self):
        self.ledger.policy = replace(self.policy, daily_loss_limit_usdc=10)
        self.open()
        self.jupiter.returned = 10000000
        self.ledger.refresh_positions(self.jupiter, self.clock)
        self.assertIn('límite de pérdidas ficticias del día alcanzado', self.ledger.entry_blockers(self.now))
        self.assertFalse(self.open(self.signal(QUOTE)))
        self.now += 86400
        self.assertNotIn('límite de pérdidas ficticias del día alcanzado', self.ledger.entry_blockers(self.now))

    def test_open_losses_and_stale_marks_block_entries(self):
        self.ledger.policy = replace(self.policy, daily_loss_limit_usdc=1)
        self.open()
        self.jupiter.returned = 23000000
        self.ledger.refresh_positions(self.jupiter, self.clock)
        self.assertTrue(self.ledger.open_positions())
        self.assertIn('límite de pérdidas ficticias del día alcanzado', self.ledger.entry_blockers(self.now))
        self.now += 121
        self.assertIsNone(self.ledger.report(self.now)['equity_usdc'])

    def test_entry_and_exit_accounting_roll_back_together(self):
        self.store.db.execute('''CREATE TRIGGER reject_cash BEFORE UPDATE OF cash_micro ON paper_account
            WHEN NEW.cash_micro != OLD.cash_micro BEGIN SELECT RAISE(ABORT, 'fixture'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.open()
        self.assertFalse(self.ledger.open_positions())
        self.assertEqual(self.ledger.report(self.now)['cash_usdc'], 1000)
        self.store.db.execute('DROP TRIGGER reject_cash')
        self.open()
        self.store.db.execute('''CREATE TRIGGER reject_cash BEFORE UPDATE OF cash_micro ON paper_account
            BEGIN SELECT RAISE(ABORT, 'fixture'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.refresh_positions(self.jupiter, self.clock, close_all=True)
        self.assertEqual(len(self.ledger.open_positions()), 1)
        self.assertEqual(self.ledger.report(self.now)['cash_usdc'], 974.95)

    def test_policy_rejects_nan_bool_fractional_raw_money_and_unknown_keys(self):
        for updates in ({'initial_usdc': float('nan')}, {'max_positions': True}, {'slippage_bps': 10000},
                        {'order_usdc': 0.0000001}, {'order_usdc': 1000}, {'quote_max_age_seconds': 31}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                replace(self.policy, **updates).validate()
        path = Path(self.tmp.name) / 'bad.json'
        path.write_text('{"real_trading": true}')
        with self.assertRaises(ValueError):
            PaperPolicy.load(path)


class ServerTests(unittest.TestCase):
    def test_persistent_pacing_across_connections_and_wait_does_not_spend_quota(self):
        with tempfile.TemporaryDirectory() as tmp, ObservationStore(Path(tmp)/'db') as first, ObservationStore(Path(tmp)/'db') as second:
            self.assertEqual(first.reserve_call('jupiter', 5, NOW, min_interval=1.1), 0)
            self.assertAlmostEqual(second.reserve_call('jupiter', 5, NOW, min_interval=1.1), 1.1, places=5)
            self.assertEqual(first.db.execute('SELECT calls FROM usage_v5').fetchone()[0], 1)
            first.set_provider_cooldown('jupiter', NOW+120)
            with self.assertRaises(RuntimeError):
                second.reserve_call('jupiter', 5, NOW+2, min_interval=1.1)
            self.assertEqual(second.reserve_call('jupiter', 5, NOW+121, min_interval=1.1), 0)

    def test_scanner_lower_budget_leaves_shared_allowance_for_paper(self):
        with tempfile.TemporaryDirectory() as tmp, ObservationStore(Path(tmp)/'db') as store:
            store.reserve_call('jupiter', 1, NOW)
            with self.assertRaises(RuntimeError):
                store.reserve_call('jupiter', 1, NOW)
            store.reserve_call('jupiter', 2, NOW)
            with self.assertRaises(RuntimeError):
                store.reserve_call('jupiter', 2, NOW)

    def test_health_distinguishes_stopped_stale_future_and_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'health.json'
            self.assertFalse(healthy(path, now=NOW))
            heartbeat(path, 'waiting', NOW)
            self.assertTrue(healthy(path, now=NOW))
            self.assertFalse(healthy(path, now=NOW+901))
            self.assertFalse(healthy(path, now=NOW-1))
            heartbeat(path, 'stopped', NOW)
            self.assertFalse(healthy(path, now=NOW))
            path.write_text('{broken')
            self.assertFalse(healthy(path, now=NOW))

    def test_paper_bounded_run_and_restart_preserve_account_without_network(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'JUPITER_API_KEY': 'fixture'}), \
                patch('server.Transport') as transport, patch('sys.stdout', new_callable=io.StringIO):
            args = ['paper', '--data-dir', tmp, '--cycles', '1']
            self.assertEqual(main(args), 0)
            with ObservationStore(Path(tmp)/'scanner.sqlite3') as store:
                with store.db:
                    store.db.execute('UPDATE paper_account SET cash_micro=900000000')
            self.assertEqual(main(args), 0)
            data = json.loads((Path(tmp)/'paper-report.json').read_text())
            self.assertEqual(data['cash_usdc'], 900)
            transport.return_value.get.assert_not_called()

    def test_report_controls_backup_and_missing_key_are_offline(self):
        with tempfile.TemporaryDirectory() as tmp, patch('server.Transport', side_effect=AssertionError('network')), \
                patch.dict('os.environ', {'JUPITER_API_KEY': ''}), patch('sys.stdout', new_callable=io.StringIO), \
                patch('sys.stderr', new_callable=io.StringIO):
            for command in ('report', 'pause', 'close-all', 'resume'):
                self.assertEqual(main([command, '--data-dir', tmp]), 0)
            self.assertEqual(main(['paper', '--data-dir', tmp, '--cycles', '1']), 2)
            destination = Path(tmp)/'snapshot.sqlite3'
            self.assertEqual(main(['backup', '--data-dir', tmp, '--backup-to', str(destination)]), 0)
            with sqlite3.connect(destination) as copied:
                self.assertEqual(copied.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            with self.assertRaises(SystemExit):
                main(['backup', '--data-dir', tmp, '--backup-to', str(destination)])

    def test_scan_uses_same_size_and_leaves_api_reserve(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'JUPITER_API_KEY': 'fixture'}), \
                patch('scanner_v05.main', return_value=0) as scan:
            self.assertEqual(main(['scan', '--data-dir', tmp, '--cycles', '1']), 0)
            args = scan.call_args[0][0]
            self.assertEqual(args[args.index('--sizes-usdc')+1], '25')
            self.assertEqual(args[args.index('--daily-api-limit')+1], '15000')


if __name__ == '__main__':
    unittest.main()
