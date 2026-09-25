import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from forward_study import ForwardStudy, STUDY, SIGNAL_STUDY_ID, PROFILE_SIGNAL_STUDIES
from observation_store import ObservationStore
from profile_plan import ProfilePlan
from profile_portfolio import ProfilePortfolio
from providers import USDC
from test_profiles import PLAN_PATH, confirmed, SizedJupiter
from test_v05 import NOW, TOKEN, OWNER1, OWNER2
from memecoin_scanner import utc_string


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ObservationStore(Path(self.tmp.name)/'db')
        self.plan = ProfilePlan.load(PLAN_PATH)
        self.portfolio = ProfilePortfolio(self.store,self.plan)
        self.study = ForwardStudy(self.store)
        self.now = NOW
        self.clock = lambda:self.now
        self.jupiter = SizedJupiter(self.clock)
        self.serial = 0

    def tearDown(self):
        self.store.__exit__()
        self.tmp.cleanup()

    def enroll(self, mint=TOKEN, rejected=False, stale=False, selected=None):
        row = confirmed(self.plan,self.now,base_address=mint)
        if rejected:
            row['quality_pass'],row['state'] = False,'rejected'
            for p in row['profiles'].values():
                p.update(quality_pass=False,state='rejected')
                p['decision_checks']['blockers'] = ['fixture risk']
        if selected is not None:
            for name,p in row['profiles'].items():
                p.update(quality_pass=name in selected,state='candidate' if name in selected else 'observing')
            row['quality_pass'] = bool(selected)
        if stale:
            row['scanned_at'] = utc_string(self.now-61)
        self.serial += 1
        oid = self.store.record_enriched(row,str(self.serial))
        with patch.object(self.study,'sampled',return_value=True):
            return self.study.enroll(row,oid,self.jupiter,self.clock)

    def test_accepted_and_rejected_are_recorded_without_debiting_any_portfolio(self):
        self.assertTrue(self.enroll(rejected=True))
        self.assertTrue(self.enroll(OWNER1))
        self.assertEqual(self.portfolio.report(self.now)['cash_usdc'],1000)
        self.assertFalse(self.portfolio.open_positions())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM research_entries_v8').fetchone()[0],5)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM research_outcomes_v8').fetchone()[0],15)
        self.assertEqual(len(self.jupiter.calls),4)  # Dos snapshots, no ocho cotizaciones por snapshot.

    def test_restart_does_not_duplicate_initial_quotes_or_samples(self):
        self.enroll()
        calls = len(self.jupiter.calls)
        self.study = ForwardStudy(self.store)
        self.assertFalse(self.enroll())
        self.assertEqual(len(self.jupiter.calls),calls)

    def test_costs_apply_to_exact_quantity_and_measured_exit(self):
        self.enroll()
        entry = self.store.db.execute('SELECT * FROM research_entries_v8').fetchone()
        self.assertEqual(entry['quantity_raw'],'9950000')
        self.assertEqual(entry['cost_usdc'],10.05)
        self.assertEqual(self.jupiter.calls[-1],(TOKEN,USDC,'9950000'))
        self.now += 3600
        self.study.evaluate(self.jupiter,self.clock)
        out = self.store.db.execute('SELECT * FROM research_outcomes_v8 WHERE horizon=1').fetchone()
        expected = ((9.95*.995-.05)/10.05-1)*100
        self.assertAlmostEqual(out['return_pct'],expected)
        self.assertEqual(out['status'],'quoted')
        groups = self.study.report(self.now)['groups']
        self.assertTrue(any(g['selector']=='all' and g['horizon_hours']==1 and g['coverage_pct']==100 for g in groups))

    def test_entry_route_failure_remains_in_denominator(self):
        self.jupiter.fail = True
        self.enroll()
        self.now += 4*3600+1
        group = next(g for g in self.study.report(self.now)['groups'] if g['selector']=='all' and g['horizon_hours']==1)
        self.assertEqual(group['states'],{'untrackable':1})
        self.assertEqual(group['coverage_pct'],0)
        self.assertIsNone(group['mean_return_pct_observed_only'])
        self.assertFalse(self.enroll())

    def test_crash_after_reservation_keeps_all_horizons_visible(self):
        with patch.object(self.jupiter,'quote',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.enroll()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM research_outcomes_v8').fetchone()[0],12)
        self.assertFalse(self.enroll())

    def test_missing_exit_is_retried_within_window_then_marked_unavailable(self):
        self.enroll(rejected=True)
        self.now += 3600
        self.jupiter.fail = True
        self.study.evaluate(self.jupiter,self.clock)
        calls = len(self.jupiter.calls)
        self.now += 10
        self.study.evaluate(self.jupiter,self.clock)
        self.assertEqual(len(self.jupiter.calls),calls)
        self.now += STUDY['deadline_seconds']
        self.study.evaluate(self.jupiter,self.clock)
        out = self.store.db.execute('SELECT * FROM research_outcomes_v8 WHERE horizon=1').fetchone()
        self.assertEqual(out['status'],'unavailable')
        self.assertIsNone(out['return_pct'])

    def test_no_late_quote_can_be_used_as_historical_price(self):
        self.enroll()
        calls = len(self.jupiter.calls)
        self.now += 3600+STUDY['deadline_seconds']+1
        self.study.evaluate(self.jupiter,self.clock)
        self.assertEqual(len(self.jupiter.calls),calls)
        self.assertEqual(self.store.db.execute('SELECT status FROM research_outcomes_v8 WHERE horizon=1').fetchone()[0],'missed')

    def test_response_crossing_deadline_is_not_a_valid_result(self):
        self.enroll()
        self.now += 3600+STUDY['deadline_seconds']-1
        original = self.jupiter.quote
        def slow(*args):
            self.now += 2
            return original(*args)
        with patch.object(self.jupiter,'quote',side_effect=slow):
            self.study.evaluate(self.jupiter,self.clock)
        row = self.store.db.execute('SELECT * FROM research_outcomes_v8 WHERE horizon=1').fetchone()
        self.assertEqual(row['status'],'missed')
        self.assertIsNone(row['return_pct'])

    def test_daily_cap_and_task_limit_are_enforced(self):
        with patch.dict(STUDY,{'daily_sample_cap':2}):
            self.assertTrue(self.enroll(rejected=True))
            self.assertTrue(self.enroll(OWNER1,rejected=True))
            calls = len(self.jupiter.calls)
            self.assertFalse(self.enroll(OWNER2))
            self.assertEqual(calls,len(self.jupiter.calls))
        self.now += 3600
        self.study.evaluate(self.jupiter,self.clock,limit=1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM research_outcomes_v8 WHERE status='quoted'").fetchone()[0],1)

    def test_stale_initial_observation_is_recorded_but_not_quoted(self):
        self.enroll(stale=True)
        self.assertFalse(self.jupiter.calls)
        self.assertEqual(self.store.db.execute('SELECT status FROM research_entries_v8').fetchone()[0],'unavailable')

    def test_sampling_is_independent_of_price_or_outcome_and_is_stable(self):
        first = self.study.sampled(TOKEN,self.plan.digest)
        self.assertEqual(first,ForwardStudy(self.store).sampled(TOKEN,self.plan.digest))
        self.assertIsInstance(first,bool)

    def test_later_confirmation_has_new_entry_without_relabelling_first_observation(self):
        self.enroll(rejected=True)
        self.now += 180
        self.assertTrue(self.enroll())
        entries = self.store.db.execute('SELECT * FROM research_entries_v8 ORDER BY id').fetchall()
        self.assertEqual([e['study'] for e in entries],[STUDY['id'],*PROFILE_SIGNAL_STUDIES.values()])
        self.assertFalse(any(p['selected'] for p in json.loads(entries[0]['labels']).values()))
        for entry in entries[1:]:
            self.assertTrue(all(p['selected'] for p in json.loads(entry['labels']).values()))
        calls = len(self.jupiter.calls)
        self.study = ForwardStudy(self.store)
        self.assertFalse(self.enroll())
        self.assertEqual(len(self.jupiter.calls),calls)
        self.now += 3600
        self.study.evaluate(self.jupiter,self.clock)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM research_outcomes_v8 WHERE status='quoted'").fetchone()[0],2)
        self.assertEqual(self.portfolio.report(self.now)['cash_usdc'],1000)

    def test_confirmed_followup_obeys_shared_daily_cap(self):
        self.enroll(rejected=True)
        with patch.dict(STUDY,{'daily_sample_cap':1}):
            calls = len(self.jupiter.calls)
            self.assertFalse(self.enroll())
            self.assertEqual(len(self.jupiter.calls),calls)

    def test_each_profile_gets_its_own_first_confirmation_price_without_relabelling(self):
        self.enroll(rejected=True)
        self.now += 60
        self.enroll(selected=['aggressive'])
        self.now += 180
        self.jupiter.returned = 50000000
        self.enroll(selected=['aggressive','balanced'])
        self.now += 420
        self.jupiter.returned = 75000000
        self.enroll(selected=['aggressive','balanced','conservative'])
        entries = {row['study']:dict(row) for row in self.store.db.execute('SELECT * FROM research_entries_v8')}
        self.assertEqual(len(entries),4)
        self.assertEqual(entries[STUDY['id']]['sampled_at'],NOW)
        for name,at,quantity in (('aggressive',NOW+60,'9950000'),
                                 ('balanced',NOW+240,'19900000'),('conservative',NOW+660,'29850000')):
            entry = entries[PROFILE_SIGNAL_STUDIES[name]]
            self.assertEqual(entry['sampled_at'],at)
            self.assertEqual(entry['quantity_raw'],quantity)
            self.assertEqual(list(json.loads(entry['labels'])),[name])
        self.assertFalse(any(p['selected'] for p in json.loads(entries[STUDY['id']]['labels']).values()))
        calls = len(self.jupiter.calls)
        self.assertFalse(self.enroll())
        self.assertEqual(len(self.jupiter.calls),calls)

    def test_legacy_confirmation_is_preserved_and_evaluated_with_original_assumptions(self):
        self.enroll(rejected=True)
        self.now += 60
        self.enroll(selected=['aggressive'])
        with self.store.db:
            self.store.db.execute('UPDATE research_entries_v8 SET study=? WHERE study=?',
                                 (SIGNAL_STUDY_ID,PROFILE_SIGNAL_STUDIES['aggressive']))
        original = dict(self.store.db.execute('SELECT * FROM research_entries_v8 WHERE study=?',(SIGNAL_STUDY_ID,)).fetchone())
        self.now += 180
        self.assertTrue(self.enroll(selected=['balanced']))
        self.assertEqual(dict(self.store.db.execute('SELECT * FROM research_entries_v8 WHERE study=?',
                                                   (SIGNAL_STUDY_ID,)).fetchone()),original)
        self.now = NOW+3661
        with patch.dict(STUDY,{'slippage_bps_per_side':9999,'fixed_cost_usdc_per_side':999}):
            self.study.evaluate(self.jupiter,self.clock,limit=10)
        out = self.store.db.execute('SELECT * FROM research_outcomes_v8 WHERE entry_id=? AND horizon=1',
                                    (original['id'],)).fetchone()
        expected = ((9.95*.995-.05)/10.05-1)*100
        self.assertAlmostEqual(out['return_pct'],expected)
        self.assertEqual(out['status'],'quoted')

    def test_reporting_exposes_outlier_concentration_without_discarding_it(self):
        self.enroll(rejected=True)
        self.enroll(OWNER1,rejected=True)
        entries = self.store.db.execute('SELECT id FROM research_entries_v8 ORDER BY id').fetchall()
        with self.store.db:
            for entry,result in zip(entries,(3320.0,-57.0)):
                self.store.db.execute("UPDATE research_outcomes_v8 SET status='quoted',return_pct=? WHERE entry_id=? AND horizon=1",
                                     (result,entry['id']))
        group = next(g for g in self.study.report(NOW+3601)['groups'] if g['selector']=='all' and g['horizon_hours']==1)
        self.assertEqual(group['observed_count'],2)
        self.assertEqual(group['unique_mints'],2)
        self.assertEqual(group['sampled_utc_days'],1)
        self.assertEqual(group['max_return_pct_observed_only'],3320)
        self.assertEqual(group['min_return_pct_observed_only'],-57)
        self.assertEqual(group['mean_return_pct_observed_only'],1631.5)
        self.assertEqual(group['largest_mint_share_positive_returns_pct'],100)
        self.assertEqual(group['leave_best_mint_out']['mean_return_pct_observed_only'],-57)
        self.assertTrue(group['insufficient_evidence'])

    def test_missing_exit_sensitivity_excludes_pending_future_and_failed_entries(self):
        self.enroll(rejected=True)
        self.enroll(OWNER1,rejected=True)
        self.jupiter.fail = True
        self.enroll(OWNER2,rejected=True)
        entries = self.store.db.execute('SELECT id FROM research_entries_v8 ORDER BY id').fetchall()
        with self.store.db:
            self.store.db.execute("UPDATE research_outcomes_v8 SET status='quoted',return_pct=50 WHERE entry_id=? AND horizon=1",(entries[0]['id'],))
        pending_group = next(g for g in self.study.report(NOW+3601)['groups'] if g['selector']=='all' and g['horizon_hours']==1)
        self.assertEqual(pending_group['missing_exit_loss_sensitivity']['imputed_terminal_exits'],0)
        self.assertEqual(pending_group['missing_exit_loss_sensitivity']['mean_return_pct'],50)
        groups = self.study.report(NOW+3600+STUDY['deadline_seconds']+1)['groups']
        group = next(g for g in groups if g['selector']=='all' and g['horizon_hours']==1)
        scenario = group['missing_exit_loss_sensitivity']
        self.assertTrue(scenario['hypothetical'])
        self.assertEqual(scenario['imputed_terminal_exits'],1)
        self.assertEqual(scenario['included_observations'],2)
        self.assertEqual(scenario['mean_return_pct'],-25)
        self.assertEqual(group['mean_return_pct_observed_only'],50)
        self.assertEqual(group['quoted_entry_count'],2)
        future = next(g for g in groups if g['selector']=='all' and g['horizon_hours']==4)
        self.assertEqual(future['missing_exit_loss_sensitivity']['imputed_terminal_exits'],0)
        self.assertIsNone(future['missing_exit_loss_sensitivity']['mean_return_pct'])

    def test_partially_reserved_profile_studies_do_not_exceed_shared_cap(self):
        with patch.dict(STUDY,{'daily_sample_cap':2}):
            self.enroll()
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM research_entries_v8').fetchone()[0],2)
            self.assertEqual(len(self.jupiter.calls),2)
            self.assertFalse(self.enroll())

    def test_concurrent_connections_cannot_reserve_the_same_last_daily_slot(self):
        # Force the old failure mode: the first reader holds its count of zero
        # while the second attempts enrollment. With a write lock before COUNT,
        # that second reader must wait until the first reservation commits.
        first_counted, second_counted = Event(), Event()
        rows = []
        for serial,mint in enumerate((TOKEN,OWNER1),1):
            row = confirmed(self.plan,self.now,base_address=mint)
            row['quality_pass'] = False
            for profile in row['profiles'].values():
                profile.update(quality_pass=False,state='observing')
            rows.append((row,self.store.record_enriched(row,str(serial))))
        path = Path(self.tmp.name)/'db'

        class CapturedCount:
            def __init__(self, row):
                self.row = row
            def fetchone(self):
                return self.row

        class CoordinatedConnection:
            def __init__(self, connection, first):
                self.connection, self.first = connection, first
            def __enter__(self):
                self.connection.__enter__()
                return self
            def __exit__(self, *args):
                return self.connection.__exit__(*args)
            def execute(self, sql, parameters=()):
                cursor = self.connection.execute(sql,parameters)
                if sql.startswith('SELECT count(*) FROM research_entries_v8'):
                    count = cursor.fetchone()
                    if self.first:
                        first_counted.set()
                        second_counted.wait(timeout=1)
                    else:
                        second_counted.set()
                    return CapturedCount(count)
                return cursor

        def enroll(index):
            if index:
                self.assertTrue(first_counted.wait(timeout=5))
            db = sqlite3.connect(path,timeout=5)
            db.row_factory = sqlite3.Row
            try:
                study = ForwardStudy(SimpleNamespace(db=CoordinatedConnection(db,index==0)))
                jupiter = SizedJupiter(self.clock)
                row,oid = rows[index]
                with patch.object(study,'sampled',return_value=True):
                    return study.enroll(row,oid,jupiter,self.clock),len(jupiter.calls)
            finally:
                db.close()

        with patch.dict(STUDY,{'daily_sample_cap':1}), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(enroll,(0,1)))
        self.assertEqual(results,[(True,2),(False,0)])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM research_entries_v8').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM research_outcomes_v8').fetchone()[0],3)


if __name__=='__main__':
    unittest.main()
