import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from observation_store import ObservationStore
from paper_events import create_events, record_event
from profile_plan import ProfilePlan
from profile_portfolio import ProfilePortfolio
from shadow_quarantine import ShadowQuarantine, shadow_report, RULE
from test_profiles import PLAN_PATH
from test_v05 import NOW, TOKEN, OWNER1
from version import SCANNER_VERSION


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ObservationStore(Path(self.tmp.name)/'state.db')
        self.db = self.store.db
        self.plan = ProfilePlan.load(PLAN_PATH)
        self.portfolio = ProfilePortfolio(self.store,self.plan)
        create_events(self.db)
        self.shadow = ShadowQuarantine(self.store)
        self.now, self.serial = NOW, 0

    def tearDown(self):
        self.store.__exit__()
        self.tmp.cleanup()

    def activate(self):
        report = self.shadow.activate_if_flat(self.now)
        self.assertEqual(report['status'],'active')
        return report

    def open(self, profile='balanced', mint=TOKEN, cost=None):
        self.serial += 1
        self.now += 1
        cost = cost if cost is not None else {'conservative':50050000,'balanced':25050000,'aggressive':10050000}[profile]
        prefix = 'paper_'+profile
        with self.db:
            observation = self.db.execute('''INSERT INTO observations
                (run_id,observed_at,chain,token,eligible,payload) VALUES (?,?,?, ?,1,?)''',
                (str(self.serial),self.now,'solana',mint,json.dumps({'scanner_version':SCANNER_VERSION,'profile_plan_hash':self.plan.digest}))).lastrowid
            pid = self.db.execute('''INSERT INTO '''+prefix+'''_positions
                (observation_id,mint,opened_at,quantity_raw,cost_micro,state,entry_quote,mark_micro,mark_at,peak_micro)
                VALUES (?,?,?,'1000000',?,'open','{}',?,?,?)''', (observation,mint,self.now,cost,cost,self.now,cost)).lastrowid
            self.db.execute('UPDATE '+prefix+'_account SET cash_micro=cash_micro-? WHERE id=1', (cost,))
            record_event(self.db,'open',profile,pid,mint,self.now,{
                'scanner_version':SCANNER_VERSION,'profile_plan_hash':self.plan.digest,
                'observation_id':observation,'cost_micro':cost,'quantity_raw':'1000000'})
        return profile,pid

    def close(self, key, pnl):
        self.now += 1
        profile,pid = key
        prefix = 'paper_'+profile
        row = self.db.execute('SELECT * FROM '+prefix+'_positions WHERE id=?',(pid,)).fetchone()
        proceeds = row['cost_micro']+pnl
        with self.db:
            self.db.execute('''UPDATE '''+prefix+'''_positions SET state='closed',closed_at=?,mark_micro=?,
                pnl_micro=?,exit_reason='fixture',exit_quote='{}' WHERE id=?''', (self.now,proceeds,pnl,pid))
            self.db.execute('UPDATE '+prefix+'_account SET cash_micro=cash_micro+? WHERE id=1',(proceeds,))
            record_event(self.db,'close',profile,pid,row['mint'],self.now,{
                'scanner_version':SCANNER_VERSION,'profile_plan_hash':self.plan.digest,
                'observation_id':row['observation_id'],'cost_micro':row['cost_micro'],
                'quantity_raw':row['quantity_raw'],'proceeds_micro':proceeds,'pnl_micro':pnl,
                'exit_reason':'fixture'})

    def trigger(self):
        self.close(self.open('balanced'),-1000000)
        self.close(self.open('aggressive'),-500000)
        self.shadow.sync(self.now)
        return self.now

    def test_initialization_does_not_activate_and_read_only_report_creates_nothing(self):
        self.assertEqual(self.shadow.report(self.now)['status'],'waiting_for_flat_start')
        self.assertEqual(self.shadow.sync(self.now)['status'],'waiting_for_flat_start')
        before = self.db.total_changes
        self.db.execute('PRAGMA query_only=ON')
        self.assertEqual(shadow_report(self.store,self.now)['status'],'waiting_for_flat_start')
        self.assertEqual(self.db.total_changes,before)
        self.assertFalse(self.db.in_transaction)
        self.db.execute('PRAGMA query_only=OFF')

    def test_start_waits_for_flat_and_uses_equal_actual_cash_excluding_all_history(self):
        old = self.open()
        waiting = self.shadow.activate_if_flat(self.now)
        self.assertFalse(waiting['active'])
        self.assertIn('sin posiciones',waiting['waiting_reason'])
        self.close(old,-1000000)
        before = self.db.execute('SELECT count(*) FROM paper_event_journal').fetchone()[0]
        report = self.activate()
        self.assertEqual(report['start_seq'],before)
        for arm in ('control','treatment'):
            self.assertEqual(report['arms'][arm]['initial_usdc'],999)
            self.assertEqual(report['arms'][arm]['closed_count'],0)
        self.close(self.open(),-1000000)
        report = self.shadow.sync(self.now)
        self.assertEqual(report['quarantine_count'],0)  # The pre-start loss is excluded.
        restarted = ShadowQuarantine(self.store).activate_if_flat(self.now)
        self.assertEqual(restarted['started_at'],report['started_at'])
        self.assertEqual(restarted['start_seq'],report['start_seq'])

    def test_shared_two_loss_quarantine_tracks_both_avoided_loss_and_forgone_gain(self):
        self.activate()
        trigger = self.trigger()
        loss = self.open('balanced')
        self.close(loss,-2000000)
        gain = self.open('aggressive')
        self.close(gain,3000000)
        report = self.shadow.sync(self.now)
        self.assertEqual(report['quarantine_count'],1)
        self.assertEqual(report['active_quarantines'][0]['until_at'],trigger+86400)
        self.assertEqual(report['arms']['control']['closed_count'],4)
        self.assertEqual(report['arms']['treatment']['closed_count'],2)
        self.assertEqual(report['skipped']['quarantine']['loss_avoided_usdc'],2)
        self.assertEqual(report['skipped']['quarantine']['gain_forgone_usdc'],3)
        self.assertEqual(report['realized_difference_usdc'],-1)
        self.assertEqual(report['arms']['treatment']['realized_pnl_usdc'],-1.5)
        # No provider object or provider method is involved in any overlay operation.
        self.assertFalse(report['profitability_proven'])
        again = ShadowQuarantine(self.store).sync(self.now)
        self.assertEqual(again['arms'],report['arms'])
        self.assertEqual(again['quarantine_count'],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM shadow_quarantine_positions').fetchone()[0],4)

    def test_loss_window_excludes_exact_lower_boundary_and_quarantine_ends_exactly(self):
        self.activate()
        self.close(self.open(),-1)
        first_at = self.now
        self.now = first_at+86400-2
        self.close(self.open('aggressive'),-1)
        report = self.shadow.sync(self.now)
        self.assertEqual(self.now,first_at+86400)
        self.assertEqual(report['quarantine_count'],0)
        self.close(self.open(),-1)
        report = self.shadow.sync(self.now)
        until = report['active_quarantines'][0]['until_at']
        self.now = until-1
        key = self.open()  # Exactly until: accepted.
        report = self.shadow.sync(self.now)
        row = self.db.execute('SELECT accepted FROM shadow_quarantine_positions WHERE profile=? AND position_id=?',key).fetchone()
        self.assertEqual(row['accepted'],1)
        self.assertFalse(report['active_quarantines'])

    def test_profit_does_not_reset_window_and_active_quarantine_is_not_extended(self):
        self.activate()
        self.close(self.open(),-1)
        self.close(self.open('aggressive'),1)
        preexisting = self.open('conservative')
        self.close(self.open('aggressive'),-1)
        trigger = self.now
        self.shadow.sync(self.now)
        self.now += 60
        self.close(preexisting,-1)
        report = self.shadow.sync(self.now)
        self.assertEqual(report['quarantine_count'],1)
        self.assertEqual(report['active_quarantines'][0]['until_at'],trigger+86400)

    def test_missed_gains_can_make_treatment_cash_insufficient_and_are_separate(self):
        with self.db:
            self.db.execute('UPDATE paper_balanced_account SET cash_micro=60000000')
        self.activate()
        self.close(self.open(),-5000000)
        self.close(self.open(),-5000000)
        self.shadow.sync(self.now)
        self.close(self.open(),100000000)  # Skipped win: control grows, treatment does not.
        self.shadow.sync(self.now)
        self.close(self.open(mint=OWNER1),-25050000)  # Accepted by both: treatment now has 24.95.
        self.shadow.sync(self.now)
        # Both reasons apply; lack of cash must not inflate quarantine's benefit.
        self.close(self.open(),1000000)
        report = self.shadow.sync(self.now)
        self.assertEqual(report['skipped']['insufficient_cash']['openings_skipped'],1)
        self.assertEqual(report['skipped']['insufficient_cash']['gain_forgone_usdc'],1)
        self.assertEqual(report['skipped']['quarantine']['gain_forgone_usdc'],100)
        self.assertAlmostEqual(report['arms']['treatment']['profiles']['balanced']['cash_usdc'],24.95)

    def test_marks_require_exact_identity_freshness_and_no_pending_events(self):
        self.activate()
        key = self.open()
        report = self.shadow.report(self.now)
        self.assertFalse(report['comparison_current'])
        self.assertIsNone(report['arms']['control']['equity_usdc'])
        report = self.shadow.sync(self.now)
        self.assertEqual(report['arms']['control']['equity_usdc'],1000)
        report = self.shadow.report(self.now+121)
        self.assertIsNone(report['arms']['control']['equity_usdc'])
        self.assertIsNone(report['arms']['treatment']['equity_usdc'])
        with self.db:
            self.db.execute('UPDATE paper_balanced_positions SET mint=? WHERE id=?',(OWNER1,key[1]))
        self.assertIsNone(self.shadow.report(self.now)['arms']['control']['equity_usdc'])

    def test_incompatible_plan_or_version_never_processes_or_resets(self):
        self.activate()
        self.open()
        with patch('shadow_quarantine.SCANNER_VERSION','future-version'):
            report = self.shadow.sync(self.now)
            self.assertEqual(report['status'],'incompatible')
            self.assertEqual(report['last_seq'],report['start_seq'])
        with self.db:
            self.db.execute("UPDATE portfolio_config SET digest='different'")
        report = self.shadow.activate_if_flat(self.now)
        self.assertEqual(report['status'],'incompatible')
        self.assertEqual(report['last_seq'],report['start_seq'])

    def test_invalid_close_rolls_back_whole_batch_and_cursor(self):
        self.activate()
        key = self.open()
        self.close(key,-1)
        row = self.db.execute("SELECT seq,payload FROM paper_event_journal WHERE kind='close'").fetchone()
        payload = json.loads(row['payload'])
        payload['pnl_micro'] = 100  # Does not equal proceeds minus cost.
        with self.db:
            self.db.execute('UPDATE paper_event_journal SET payload=? WHERE seq=?',(json.dumps(payload),row['seq']))
        report = self.shadow.sync(self.now)
        self.assertEqual(report['status'],'error')
        self.assertEqual(report['last_seq'],report['start_seq'])
        self.assertEqual(report['arms']['control']['cash_usdc'],1000)
        self.assertEqual(self.db.execute('SELECT count(*) FROM shadow_quarantine_positions').fetchone()[0],0)
        self.assertFalse(self.db.in_transaction)

    def test_source_journal_gap_is_sticky_across_recovery_and_restart(self):
        self.activate()
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS paper_journal_health
                (id INTEGER PRIMARY KEY,healthy INTEGER,error_type TEXT,happened_at REAL)''')
            self.db.execute("INSERT OR REPLACE INTO paper_journal_health VALUES (1,0,'fixture',?)",(self.now,))
        report = self.shadow.sync(self.now)
        self.assertEqual(report['incompatibility'],'source_journal_gap')
        with self.db:
            self.db.execute('UPDATE paper_journal_health SET healthy=1')
        report = ShadowQuarantine(self.store).sync(self.now)
        self.assertEqual(report['incompatibility'],'source_journal_gap')
        self.assertFalse(report['comparison_current'])

    def test_bounded_sync_exposes_backlog_until_all_events_are_applied(self):
        self.activate()
        self.close(self.open(),1)
        with patch('shadow_quarantine.MAX_EVENTS_PER_SYNC',1):
            report = self.shadow.sync(self.now)
            self.assertEqual(report['pending_events'],1)
            self.assertFalse(report['comparison_current'])
            report = self.shadow.sync(self.now)
            self.assertEqual(report['pending_events'],0)
            self.assertTrue(report['comparison_current'])
        self.assertAlmostEqual(report['arms']['control']['cash_usdc'],1000.000001)

    def test_read_only_report_preserves_callers_transaction(self):
        self.activate()
        self.db.execute('UPDATE shadow_quarantine_accounts SET cash_micro=cash_micro')
        shadow_report(SimpleNamespace(db=self.db),self.now)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()


if __name__ == '__main__':
    unittest.main()
