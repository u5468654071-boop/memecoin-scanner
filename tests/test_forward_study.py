import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forward_study import ForwardStudy, STUDY, SIGNAL_STUDY_ID
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

    def enroll(self, mint=TOKEN, rejected=False, stale=False):
        row = confirmed(self.plan,self.now,base_address=mint)
        if rejected:
            row['quality_pass'],row['state'] = False,'rejected'
            for p in row['profiles'].values():
                p.update(quality_pass=False,state='rejected')
                p['decision_checks']['blockers'] = ['fixture risk']
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
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM research_entries_v8').fetchone()[0],2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM research_outcomes_v8').fetchone()[0],6)

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
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM research_outcomes_v8').fetchone()[0],3)
        self.assertFalse(self.enroll())

    def test_missing_exit_is_retried_within_window_then_marked_unavailable(self):
        self.enroll()
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
            self.assertTrue(self.enroll())
            self.assertTrue(self.enroll(OWNER1))
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
        self.assertEqual([e['study'] for e in entries],[STUDY['id'],SIGNAL_STUDY_ID])
        self.assertFalse(any(p['selected'] for p in json.loads(entries[0]['labels']).values()))
        self.assertTrue(any(p['selected'] for p in json.loads(entries[1]['labels']).values()))
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


if __name__=='__main__':
    unittest.main()
