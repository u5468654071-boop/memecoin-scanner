import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from performance import performance_report


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.store = SimpleNamespace(db=self.db)
        self.db.execute('CREATE TABLE observations (id INTEGER PRIMARY KEY,payload TEXT)')
        self.serial = 0

    def tearDown(self):
        self.db.close()

    def ledger(self, profile='legacy', cost=0.05, evidence=False):
        prefix = 'paper' if profile == 'legacy' else 'paper_'+profile
        self.db.execute('CREATE TABLE '+prefix+'_account (id INTEGER PRIMARY KEY,policy TEXT)')
        self.db.execute('INSERT INTO '+prefix+'_account VALUES (1,?)',
                        (json.dumps({'fixed_cost_usdc_per_side': cost}),))
        self.db.execute('CREATE TABLE '+prefix+'''_positions (id INTEGER PRIMARY KEY,
            observation_id INTEGER,mint TEXT,opened_at REAL,closed_at REAL,state TEXT,
            pnl_micro INTEGER,exit_reason TEXT'''+(',exit_evidence TEXT' if evidence else '')+')')
        self.db.commit()
        return prefix

    def position(self, prefix, opened, closed, pnl, mint='A', version='0.10.0', plan='plan-a',
                 reason='objetivo observado', evidence=None):
        self.serial += 1
        self.db.execute('INSERT INTO observations VALUES (?,?)',
            (self.serial, json.dumps({'scanner_version': version, 'profile_plan_hash': plan})))
        columns = 'observation_id,mint,opened_at,closed_at,state,pnl_micro,exit_reason'
        values = [self.serial, mint, opened, closed, 'open' if closed is None else 'closed', pnl, reason]
        if evidence is not None:
            columns += ',exit_evidence'
            values.append(json.dumps(evidence))
        cursor = self.db.execute('INSERT INTO '+prefix+'_positions ('+columns+') VALUES ('+
                                ','.join('?' for _ in values)+')', values)
        self.db.commit()
        return cursor.lastrowid

    def test_interval_boundaries_and_outstanding_reconstructed_without_future_pnl(self):
        prefix = self.ledger()
        self.position(prefix, 1, 99, 9000000)       # closed before interval
        self.position(prefix, 1, 100, 2000000)     # included at since, old opening
        self.position(prefix, 100, 199, -500000)   # included
        self.position(prefix, 101, 200, 99000000)  # excluded PnL, outstanding at end
        self.position(prefix, 102, 201, 88000000)  # same
        self.position(prefix, 103, None, None)    # still open
        self.position(prefix, 200, None, None)    # future opening excluded
        report = performance_report(self.store, 100, 200)
        self.assertEqual(report['accounting'], {'opened_count': 4, 'closed_count': 2,
            'open_at_end_count': 3, 'realized_pnl_micro': 1500000, 'realized_pnl_usdc': 1.5})
        cohort = report['profiles']['legacy']['cohorts'][0]
        self.assertEqual(cohort['profit_factor'], 4)
        self.assertEqual(cohort['assumed_fixed_costs_usdc'], 0.2)
        self.assertEqual(cohort['median_hold_seconds'], 99)
        self.assertNotIn('equity_usdc', report)

    def test_profile_identity_and_version_and_plan_cohorts_never_merge(self):
        balanced, aggressive = self.ledger('balanced'), self.ledger('aggressive')
        self.assertEqual(self.position(balanced, 100, 110, 1000000), 1)
        self.assertEqual(self.position(aggressive, 100, 110, -2000000), 1)
        self.position(balanced, 110, 120, -3000000, version='0.11.0')
        self.position(balanced, 120, 130, 4000000, version='0.11.0', plan='plan-b')
        report = performance_report(self.store, 100, 200)
        self.assertEqual(report['accounting']['closed_count'], 4)
        self.assertEqual(report['accounting']['realized_pnl_micro'], 0)
        cohorts = report['profiles']['balanced']['cohorts']
        self.assertEqual([(c['scanner_version'], c['profile_plan_hash']) for c in cohorts],
                         [('0.10.0', 'plan-a'), ('0.11.0', 'plan-a'), ('0.11.0', 'plan-b')])
        self.assertEqual([c['closed_count'] for c in cohorts], [1, 1, 1])
        self.assertNotIn('profit_factor', report['accounting'])

    def test_repeat_mint_concentration_integer_accounting_and_zero_loss_denominator(self):
        prefix = self.ledger(cost=0.000001)
        self.position(prefix, 100, 110, 1)
        self.position(prefix, 111, 120, 2)
        self.position(prefix, 121, 130, 3, mint='B')
        self.position(prefix, 131, 140, 0, mint='C')
        cohort = performance_report(self.store, 100, 200)['profiles']['legacy']['cohorts'][0]
        self.assertEqual(cohort['realized_pnl_micro'], 6)
        self.assertEqual(cohort['realized_pnl_usdc'], 0.000006)
        self.assertEqual(cohort['assumed_fixed_costs_usdc'], 0.000008)
        self.assertIsNone(cohort['profit_factor'])
        self.assertEqual(cohort['profit_factor_unavailable_reason'], 'no_realized_losses_in_window')
        self.assertEqual((cohort['wins'], cohort['losses'], cohort['flat']), (3, 0, 1))
        self.assertEqual((cohort['unique_mints'], cohort['repeated_mints_count'], cohort['repeat_mint_trades_count']), (3, 1, 1))
        self.assertEqual(cohort['net_without_best_mint_usdc'], 0.000003)

    def test_utc_days_refer_to_closed_trades_and_empty_interval_is_explicit(self):
        prefix = self.ledger()
        self.position(prefix, 86390, 86410, 1)
        self.position(prefix, 86420, 172810, -1)
        cohort = performance_report(self.store, 86400, 172820)['profiles']['legacy']['cohorts'][0]
        self.assertEqual(cohort['closed_trade_entry_days_utc'], ['1970-01-01', '1970-01-02'])
        self.assertEqual(cohort['closing_days_utc'], ['1970-01-02', '1970-01-03'])
        empty = performance_report(self.store, 200000, 200001)
        self.assertEqual(empty['profiles']['legacy']['cohorts'], [])
        self.assertEqual(empty['accounting']['closed_count'], 0)

    def test_exit_evidence_counts_known_reasons_and_leaves_legacy_missing(self):
        prefix = self.ledger(evidence=True)
        check = {'code': 'liquidity', 'status': 'blocked', 'reason': 'liquidez insuficiente'}
        self.position(prefix, 1, 100, -1, reason='riesgo de posición', evidence={
            'kind': 'position_risk', 'checks': [check, check], 'stop_loss_triggered': True})
        self.position(prefix, 2, 101, 1)
        cohort = performance_report(self.store, 100, 200)['profiles']['legacy']['cohorts'][0]
        self.assertEqual(cohort['exit_reason_counts'], {'objetivo observado': 1, 'riesgo de posición': 1})
        self.assertEqual(cohort['exit_evidence'], {'recorded_closed_count': 1, 'missing_closed_count': 1,
            'invalid_closed_count': 0, 'kind_counts': {'position_risk': 1},
            'check_counts': [{'code': 'liquidity', 'status': 'blocked', 'closed_count': 1}],
            'price_trigger_counts': {'stop_loss_triggered': 1}})

    def test_active_profiles_exclude_archived_legacy_and_incomplete_plan_fails(self):
        legacy = self.ledger()
        self.position(legacy, 1, 100, 99000000)
        for profile in ('conservative', 'balanced', 'aggressive'):
            prefix = self.ledger(profile)
            self.position(prefix, 1, 100, 1000000)
        self.db.execute('CREATE TABLE portfolio_config (id INTEGER PRIMARY KEY)')
        self.db.execute('INSERT INTO portfolio_config VALUES (1)')
        self.db.commit()
        report = performance_report(self.store, 100, 200)
        self.assertEqual(report['accounting']['realized_pnl_usdc'], 3)
        self.assertEqual(report['excluded_archived_ledgers'], ['paper'])
        self.db.execute('DROP TABLE paper_aggressive_positions')
        with self.assertRaises(ValueError):
            performance_report(self.store, 100, 200)

    def test_empty_profiles_are_visible_and_failed_migration_does_not_hide_legacy(self):
        legacy = self.ledger()
        self.position(legacy, 1, 100, 1)
        self.ledger('conservative')
        report = performance_report(self.store, 100, 200)
        self.assertEqual(report['profiles']['conservative']['accounting']['closed_count'], 0)
        self.assertEqual(report['profiles']['conservative']['cohorts'], [])
        # A rejected migration can leave empty profile schemas, but no accounts.
        self.db.execute('DELETE FROM paper_conservative_account')
        self.db.commit()
        report = performance_report(self.store, 100, 200)
        self.assertEqual(report['scope'], 'legacy_ledger')
        self.assertEqual(report['accounting']['closed_count'], 1)

    def test_missing_entry_and_policy_remain_unknown_without_losing_accounting(self):
        prefix = self.ledger()
        self.position(prefix, 1, 100, -500000, version=None, plan=None)
        self.db.execute('UPDATE paper_account SET policy=?', ('invalid',))
        self.db.commit()
        cohort = performance_report(self.store, 100, 200)['profiles']['legacy']['cohorts'][0]
        self.assertFalse(cohort['entry_provenance_complete'])
        self.assertIsNone(cohort['scanner_version'])
        self.assertIsNone(cohort['assumed_fixed_costs_usdc'])
        self.assertEqual(cohort['realized_pnl_usdc'], -0.5)

    def test_read_only_query_and_caller_transaction_preserved(self):
        prefix = self.ledger()
        self.position(prefix, 1, 100, 1)
        self.db.execute('PRAGMA query_only=ON')
        before = self.db.total_changes
        performance_report(self.store, 100, 200)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(before, self.db.total_changes)
        self.db.execute('PRAGMA query_only=OFF')
        self.db.execute('UPDATE paper_positions SET pnl_micro=3')
        performance_report(self.store, 100, 200)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT pnl_micro FROM paper_positions').fetchone()[0], 1)

    def test_multiple_ledgers_use_one_consistent_read_snapshot(self):
        for profile in ('balanced', 'aggressive'):
            prefix = self.ledger(profile)
            self.position(prefix, 1, 100, 1000000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'performance.sqlite3'
            reader = sqlite3.connect(path)
            reader.row_factory = sqlite3.Row
            self.db.backup(reader)
            reader.execute('PRAGMA journal_mode=WAL')
            writer = sqlite3.connect(path)
            changed = []
            def during_read(sql):
                if 'FROM paper_aggressive_positions p' in sql and not changed:
                    writer.execute('UPDATE paper_aggressive_positions SET pnl_micro=9000000')
                    writer.commit()
                    changed.append(True)
            reader.set_trace_callback(during_read)
            try:
                report = performance_report(SimpleNamespace(db=reader), 100, 200)
                self.assertEqual(changed, [True])
                self.assertEqual(report['accounting']['realized_pnl_usdc'], 2)
                self.assertEqual(writer.execute('SELECT pnl_micro FROM paper_aggressive_positions').fetchone()[0], 9000000)
            finally:
                writer.close()
                reader.close()

    def test_invalid_bounds_and_corrupt_closed_pnl_fail_explicitly(self):
        for since, until in ((1, 1), (2, 1), (True, 2), (0, float('nan')), (0, float('inf')),
                             ('0', 1), (0, 10**1000), (0, 1e20)):
            with self.subTest(since=since, until=until), self.assertRaises(ValueError):
                performance_report(self.store, since, until)
        prefix = self.ledger()
        self.position(prefix, 1, 100, None)
        with self.assertRaises(ValueError):
            performance_report(self.store, 100, 200)
        self.assertFalse(self.db.in_transaction)


if __name__ == '__main__':
    unittest.main()
