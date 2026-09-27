import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from observation_store import ObservationStore
from paper_events import record_event
from profile_plan import ProfilePlan
from profile_portfolio import ProfilePortfolio
from server import main, update_shadow
from test_paper import FakeJupiter
from test_profiles import confirmed
from test_v05 import NOW


class JournalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ObservationStore(self.root / 'scanner.sqlite3')
        self.plan = ProfilePlan.load(Path(__file__).resolve().parents[1] / 'profiles.json')
        self.portfolio = ProfilePortfolio(self.store, self.plan)
        self.ledger = self.portfolio.ledgers['balanced']
        self.now = NOW
        self.jupiter = FakeJupiter(lambda: self.now)
        self.signal = self.store.record_enriched(confirmed(self.plan, NOW), 'journal-test')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_open_close_events_match_account_and_remain_once_after_retry(self):
        self.assertTrue(self.ledger.try_open(self.signal, self.jupiter, lambda: self.now))
        first = self.store.db.execute('SELECT * FROM paper_event_journal').fetchone()
        payload = json.loads(first['payload'])
        position = self.ledger.open_positions()[0]
        self.assertEqual(first['kind'], 'open')
        self.assertEqual(first['position_id'], position['id'])
        self.assertEqual(payload['quantity_raw'], position['quantity_raw'])
        self.assertEqual(payload['cost_micro'], position['cost_micro'])
        self.assertEqual(payload['profile_plan_hash'], self.plan.digest)
        self.now += 5
        self.ledger.refresh_positions(self.jupiter, lambda: self.now, close_all=True)
        self.ledger.refresh_positions(self.jupiter, lambda: self.now, close_all=True)
        events = self.store.db.execute('SELECT * FROM paper_event_journal ORDER BY seq').fetchall()
        self.assertEqual([r['kind'] for r in events], ['open', 'close'])
        close = json.loads(events[1]['payload'])
        self.assertEqual(close['scanner_version'], payload['scanner_version'])
        self.assertEqual(close['proceeds_micro'] - close['cost_micro'], close['pnl_micro'])
        self.assertEqual(self.ledger.report(self.now)['realized_pnl_usdc'], close['pnl_micro']/1e6)

    def test_journal_failure_keeps_control_running_and_persists_gap(self):
        before = self.ledger.report(self.now)['cash_usdc']
        with patch('paper_events.record_event', side_effect=sqlite3.OperationalError('fixture')):
            self.assertTrue(self.ledger.try_open(self.signal, self.jupiter, lambda: self.now))
        self.assertEqual(len(self.ledger.open_positions()), 1)
        self.assertEqual(self.ledger.report(self.now)['cash_usdc'], before-25.05)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_event_journal').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT healthy FROM paper_journal_health').fetchone()[0], 0)

    def test_close_journal_failure_does_not_prevent_control_exit(self):
        self.ledger.try_open(self.signal, self.jupiter, lambda: self.now)
        before = self.ledger.report(self.now)['cash_usdc']
        with patch('paper_events.record_event', side_effect=sqlite3.OperationalError('fixture')):
            self.ledger.refresh_positions(self.jupiter, lambda: self.now, close_all=True)
        self.assertEqual(len(self.ledger.open_positions()), 0)
        self.assertGreater(self.ledger.report(self.now)['cash_usdc'], before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_event_journal').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT healthy FROM paper_journal_health').fetchone()[0], 0)

    def test_event_cannot_commit_independently(self):
        with self.assertRaises(RuntimeError):
            record_event(self.store.db, 'open', 'balanced', 1, 'mint', NOW, {})

    def test_study_failure_is_visible_and_does_not_prevent_source_entry(self):
        with patch('shadow_quarantine.ShadowQuarantine', side_effect=ValueError('secret-must-not-print')):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                update_shadow(self.store, self.root, NOW)
        self.assertNotIn('secret-must-not-print', stdout.getvalue())
        status = json.loads((self.root/'shadow-status.json').read_text())
        self.assertFalse(status['ok'])
        self.assertEqual(status['error_type'], 'ValueError')
        self.assertTrue(self.ledger.try_open(self.signal, self.jupiter, lambda: self.now))

    def test_observer_transaction_is_rolled_back_on_its_own_connection(self):
        self.store.db.execute('CREATE TABLE shadow_fixture_leak (value INTEGER)')
        self.store.db.commit()

        def failing_constructor(store):
            self.assertIsNot(store.db, self.store.db)
            store.db.execute('INSERT INTO shadow_fixture_leak VALUES (1)')
            raise ValueError('after-write')

        with patch('shadow_quarantine.ShadowQuarantine', side_effect=failing_constructor):
            with contextlib.redirect_stdout(io.StringIO()):
                update_shadow(self.store, self.root, NOW)
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM shadow_fixture_leak').fetchone()[0], 0)
        self.assertTrue(self.ledger.try_open(self.signal, self.jupiter, lambda: self.now))

    def test_study_incompatibility_is_not_reported_as_healthy(self):
        study = Mock()
        study.report.return_value = {'status': 'incompatible', 'incompatibility': 'source_journal_gap',
                                     'comparison_current': False}
        with patch('shadow_quarantine.ShadowQuarantine', return_value=study):
            update_shadow(self.store, self.root, NOW)
        status = json.loads((self.root/'shadow-status.json').read_text())
        self.assertFalse(status['ok'])
        self.assertEqual(status['study_status'], 'incompatible')

    def test_two_profiles_can_journal_same_position_id(self):
        self.ledger.try_open(self.signal, self.jupiter, lambda: self.now)
        self.jupiter.returned = 10000000
        self.assertTrue(self.portfolio.ledgers['aggressive'].try_open(self.signal, self.jupiter, lambda: self.now))
        events = self.store.db.execute('SELECT profile,position_id FROM paper_event_journal ORDER BY seq').fetchall()
        self.assertEqual([tuple(r) for r in events], [('balanced', 1), ('aggressive', 1)])

    def test_predeployment_position_can_close_without_an_open_journal_event(self):
        self.ledger.try_open(self.signal, self.jupiter, lambda: self.now)
        with self.store.db:
            self.store.db.execute('DELETE FROM paper_event_journal')
        self.ledger.refresh_positions(self.jupiter, lambda: self.now, close_all=True)
        event = self.store.db.execute('SELECT * FROM paper_event_journal').fetchone()
        self.assertEqual(event['kind'], 'close')
        self.assertFalse(self.ledger.open_positions())
        self.assertEqual(self.store.db.execute('SELECT healthy FROM paper_journal_health').fetchone()[0], 1)

    def test_shadow_report_cli_does_not_create_experiment_tables(self):
        before = self.store.db.execute('SELECT name FROM sqlite_master ORDER BY name').fetchall()
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(['shadow-report', '--data-dir', str(self.root)]), 0)
        self.assertIsInstance(json.loads(stdout.getvalue()), dict)
        after = self.store.db.execute('SELECT name FROM sqlite_master ORDER BY name').fetchall()
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
