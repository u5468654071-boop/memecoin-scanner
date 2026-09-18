import copy
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from engine import Engine
from memecoin_scanner import utc_string
from observation_store import ObservationStore
from paper_trading import PaperLedger, PaperPolicy
from profile_plan import ProfilePlan, PROFILE_IDS
from profile_portfolio import ProfilePortfolio, active_plan
from providers import USDC, Jupiter
from server import main
from telegram_notifications import Notifications
from test_v05 import enriched, NOW, TOKEN, OWNER1, OWNER2, exit_quote, mint_response, report, organic
from test_paper import FakeJupiter
from test_telegram import FakeClient
from test_scanner import pair, rug

PLAN_PATH = Path(__file__).resolve().parents[1] / 'profiles.json'


def source_row(plan, now=NOW, history=(), **changes):
    row = enriched(now)
    row.update(changes)
    row['exit_quotes'] = [{**exit_quote(now), 'amount_usdc': size} for size in plan.sizes]
    row['profile_plan_hash'] = plan.digest
    row['profiles'] = plan.evaluate(row, history, now)
    row['quality_pass'] = any(p['quality_pass'] for p in row['profiles'].values())
    order = {'candidate': 0, 'observing': 1, 'insufficient_data': 2, 'rejected': 3}
    row['state'] = min(row['profiles'].values(), key=lambda p: order[p['state']])['state']
    return row


def confirmed(plan, now=NOW, **changes):
    history = []
    for age in (600, 450, 300, 150, 0):
        history.append(source_row(plan, now-age, history, **changes))
    return history[-1]


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.plan = ProfilePlan.load(PLAN_PATH)

    def test_complete_data_confirms_all_without_changing_raw_observation(self):
        row = confirmed(self.plan)
        self.assertTrue(all(r['quality_pass'] for r in row['profiles'].values()))
        self.assertEqual([r['exit_quotes'][0]['amount_usdc'] for r in row['profiles'].values()], [50,25,10])
        self.assertEqual(row['organic']['organic_score'], 60)
        self.assertEqual(sum(p['paper']['initial_usdc'] for p in self.plan.profiles), 1000)

    def test_earlier_entry_only_aggressive_then_balanced(self):
        history = []
        for at in (NOW-180, NOW-120, NOW):
            history.append(source_row(self.plan, at, history))
        self.assertEqual(history[1]['profiles']['aggressive']['state'], 'candidate')
        self.assertEqual(history[1]['profiles']['balanced']['state'], 'observing')
        self.assertEqual(history[-1]['profiles']['balanced']['state'], 'candidate')
        self.assertEqual(history[-1]['profiles']['conservative']['state'], 'observing')

    def test_liquidity_age_and_concentration_thresholds_separate_profiles(self):
        for change in ({'liquidity_usd':30000}, {'age_hours':0.1},
                       {'holders':{'status':'ok','top1_pct_lower_bound':18,'top10_pct_lower_bound':50}}):
            with self.subTest(change=change):
                row = confirmed(self.plan, **change)
                self.assertTrue(row['profiles']['aggressive']['quality_pass'])
                self.assertFalse(row['profiles']['balanced']['quality_pass'])
                self.assertFalse(row['profiles']['conservative']['quality_pass'])

    def test_all_profiles_keep_critical_checks(self):
        for change in ({'mint_check':{'status':'blocked','reasons':['freeze activa']}},
                       {'networks':{'status':'unavailable'}}, {'rugged':True},
                       {'organic':{**organic(),'flagged_suspicious':True}},
                       {'holders':{'status':'incomplete'}}, {'pool_check':{'status':'incomplete'}}):
            with self.subTest(change=change):
                self.assertFalse(any(p['quality_pass'] for p in confirmed(self.plan, **change)['profiles'].values()))

    def test_cached_missing_or_different_plan_history_does_not_confirm(self):
        history = [source_row(self.plan, NOW-age) for age in (600,450,300,150)]
        for row in history:
            row['organic']['updated_at'] = NOW
        current = source_row(self.plan, NOW, history)
        self.assertFalse(any(p['quality_pass'] for p in current['profiles'].values()))
        for row in history:
            row['profile_plan_hash'] = 'old-experiment'
        current = source_row(self.plan, NOW, history)
        self.assertFalse(any(p['quality_pass'] for p in current['profiles'].values()))

    def test_quote_failure_for_one_size_does_not_reject_other_profiles(self):
        row = confirmed(self.plan)
        history = [source_row(self.plan, NOW-age) for age in (600,450,300,150)]
        row['exit_quotes'][0]['status'] = 'unavailable'
        decisions = self.plan.evaluate(row, history, NOW)
        self.assertFalse(decisions['conservative']['quality_pass'])
        self.assertTrue(decisions['balanced']['quality_pass'])
        self.assertTrue(decisions['aggressive']['quality_pass'])

    def test_preflight_saves_quotes_only_when_static_filters_fail(self):
        self.assertEqual(self.plan.sizes_to_quote(enriched(), NOW), [50,25,10])
        row = enriched()
        row['liquidity_usd'] = 30000
        self.assertEqual(self.plan.sizes_to_quote(row, NOW), [10])
        row['mint_check'] = {'status':'blocked','reasons':['authority']}
        self.assertEqual(self.plan.sizes_to_quote(row, NOW), [])

    def test_invalid_plan_budget_duplicate_profile_nan_and_disabled_guard(self):
        for mutate in (lambda p:p.update(total_initial_usdc=999),
                       lambda p:p['profiles'][1].update(id='conservative'),
                       lambda p:p['profiles'][2]['market'].update(min_lp_locked_pct=0),
                       lambda p:p['profiles'][0]['enhanced'].update(min_samples=1),
                       lambda p:p['profiles'][0]['enhanced'].update(max_owner_pct=float('nan')),
                       lambda p:p.update(max_token_exposure_usdc=10)):
            raw = copy.deepcopy(self.plan.data)
            mutate(raw)
            with self.assertRaises(ValueError):
                ProfilePlan(raw)

    def test_organic_discovery_is_deduplicated_and_not_an_approval(self):
        transport = Mock(jupiter_key='fixture')
        transport.get.return_value = [{'id':TOKEN}, {'id':TOKEN}, {'id':'invalid'}, {'id':OWNER1}]
        self.assertEqual(Jupiter(transport).discover_organic(3), [TOKEN,OWNER1])
        self.assertIn('/tokens/v2/toporganicscore/5m?limit=3', transport.get.call_args[0][0])

    def test_engine_shares_evidence_and_reports_each_profile(self):
        with tempfile.TemporaryDirectory() as tmp, ObservationStore(Path(tmp)/'db') as store:
            transport = Mock(jupiter_key='fixture')
            transport.get.return_value = report()
            transport.rpc.return_value = mint_response()
            engine = Engine(store, transport, sizes=self.plan.sizes, profile_plan=self.plan)
            with patch('memecoin_scanner.get_pairs_for_token', return_value=[pair()]), \
                 patch('memecoin_scanner.get_rugcheck_summary', return_value=rug()), \
                 patch('engine.holder_evidence', return_value={'status':'ok','top1_pct_lower_bound':1,'top10_pct_lower_bound':5}) as holders:
                for index, at in enumerate((NOW,NOW+150,NOW+300,NOW+450,NOW+600)):
                    engine.jupiter.token = Mock(return_value=organic(at))
                    engine.jupiter.round_trip = Mock(side_effect=lambda mint,size: {**exit_quote(at),'amount_usdc':size})
                    row = engine.analyze('solana',TOKEN,now=at)
                    store.record_enriched(row,str(index))
                self.assertTrue(all(p['quality_pass'] for p in row['profiles'].values()))
                self.assertEqual(holders.call_count,5)
                self.assertEqual(transport.rpc.call_count,5)
                self.assertEqual(engine.jupiter.token.call_count,1)
                self.assertEqual(engine.jupiter.round_trip.call_count,3)
                selectors={g['selector'] for g in store.report_enhanced(NOW+601)['exit_quote_comparisons']}
                self.assertTrue(set(PROFILE_IDS).issubset(selectors))

    def test_future_measurement_batch_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp, ObservationStore(Path(tmp)/'db') as store:
            for index in range(4):
                store.record_enriched(confirmed(self.plan,NOW+index),str(index))
            jup=SizedJupiter(lambda:NOW+3605)
            store.evaluate_exits(jup,now=NOW+3605,max_tasks=2)
            self.assertEqual(len(jup.calls),2)
            count=store.db.execute("SELECT count(*) FROM exit_outcomes_v5 WHERE checked_at IS NOT NULL").fetchone()[0]
            self.assertEqual(count,2)


class SizedJupiter(FakeJupiter):
    def quote(self, src, dst, amount):
        self.calls.append((src,dst,str(amount)))
        if self.fail:
            raise RuntimeError('no route')
        # Identical decimals for synthetic assets: size-proportional indicative price.
        return {'input_mint':src,'output_mint':dst,'in_amount':str(amount),
                'out_amount':str(int(int(amount)*self.returned/25000000)), 'received_at':self.clock()}

    def round_trip(self, mint, size):
        amount = int(size*1e6)
        buy = {'input_mint':USDC,'output_mint':mint,'in_amount':str(amount),
               'out_amount':str(amount),'received_at':self.clock()}
        sell = self.quote(mint, USDC, str(amount))
        return {'status':'quoted','amount_usdc':size,'round_trip_loss_pct':0,'buy':buy,'sell':sell,'received_at':self.clock()}


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ObservationStore(self.root/'scanner.sqlite3')
        self.plan = ProfilePlan.load(PLAN_PATH)
        self.portfolio = ProfilePortfolio(self.store, self.plan)
        self.now = NOW
        self.jup = SizedJupiter(lambda:self.now)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def signal(self, mint=TOKEN):
        row = confirmed(self.plan, self.now, base_address=mint)
        return self.store.record_enriched(row, mint+str(self.now))

    def test_initial_allocation_and_restart_are_idempotent(self):
        for _ in range(2):
            portfolio = ProfilePortfolio(self.store,self.plan)
            result = portfolio.report(self.now)
            self.assertEqual(result['cash_usdc'],1000)
            self.assertEqual([p['cash_usdc'] for p in result['profiles'].values()], [600,300,100])
        with self.assertRaises(ValueError):
            PaperLedger(self.store,PaperPolicy())
        self.assertEqual(active_plan(self.store).digest,self.plan.digest)

    def test_changed_policy_cannot_silently_reset_accounts(self):
        raw = copy.deepcopy(self.plan.data)
        raw['profiles'][0]['paper']['order_usdc'] = 49
        with self.assertRaises(ValueError):
            ProfilePortfolio(self.store,ProfilePlan(raw))
        self.assertEqual(self.portfolio.report()['cash_usdc'],1000)

    def test_global_exposure_and_losses_apply_across_profiles(self):
        self.signal()
        self.portfolio.tick(self.jup,lambda:self.now)
        self.signal(OWNER1)
        self.portfolio.tick(self.jup,lambda:self.now)
        self.signal(OWNER2)
        # Use a lower hypothetical global cap to check the shared guard at a boundary.
        self.portfolio.plan.data['max_total_exposure_usdc']=130
        self.assertFalse(self.portfolio.allows('balanced',OWNER2,self.now))
        with self.store.db:
            self.store.db.execute('UPDATE paper_conservative_positions SET mark_micro=cost_micro-16000000')
            self.store.db.execute('UPDATE paper_aggressive_positions SET mark_micro=cost_micro+20000000')
        self.assertGreaterEqual(self.portfolio.losses(self.now),32000000)
        self.assertFalse(self.portfolio.allows('balanced',OWNER2,self.now))

    def test_manual_pause_and_wrong_scanner_plan_fail_closed(self):
        self.assertIn('entradas pausadas manualmente',self.portfolio.report(self.now,paused=True)['entry_blockers'])
        with patch('sys.stderr',new_callable=io.StringIO),patch('scanner_v05.main') as scan:
            self.assertEqual(main(['scan','--data-dir',str(self.root)]),2)
            scan.assert_not_called()

    def test_profile_signal_is_separate_and_global_token_limit_applies(self):
        self.signal()
        result = self.portfolio.tick(self.jup,lambda:self.now)
        self.assertEqual([p['profile'] for p in result['open_positions']],['conservative','aggressive'])
        self.assertAlmostEqual(result['cash_usdc'],939.9)
        self.assertEqual(result['profiles']['balanced']['cash_usdc'],300)
        again = self.portfolio.tick(self.jup,lambda:self.now)
        self.assertEqual(len(again['open_positions']),2)
        self.assertLessEqual(sum(p['cost_micro'] for p in again['open_positions']),70000000)

    def test_old_and_forged_profile_hash_do_not_open(self):
        row = confirmed(self.plan)
        row['profile_plan_hash'] = 'different-plan'
        self.store.record_enriched(row,'different')
        self.assertFalse(self.portfolio.tick(self.jup,lambda:self.now)['open_positions'])

    def test_profile_rejection_not_overridden_by_another_candidate(self):
        row = confirmed(self.plan,liquidity_usd=30000)
        self.store.record_enriched(row,'less_liquid')
        result = self.portfolio.tick(self.jup,lambda:self.now)
        self.assertEqual([p['profile'] for p in result['open_positions']],['aggressive'])

    def test_pause_keeps_exit_processing_for_every_profile(self):
        self.signal()
        self.portfolio.tick(self.jup,lambda:self.now)
        self.now += 10
        self.jup.returned = 10000000
        result = self.portfolio.tick(self.jup,lambda:self.now,paused=True)
        self.assertEqual(result['closed_positions'],2)
        self.assertFalse(result['open_positions'])
        self.assertTrue(result['entry_blockers'])
        self.assertLess(result['cash_usdc'],1000)
        self.assertGreater(result['profiles']['conservative']['max_observed_drawdown_pct'],0)

    def test_missing_exit_route_blocks_all_new_entries_and_preserves_cash(self):
        self.signal()
        before = self.portfolio.tick(self.jup,lambda:self.now)
        self.now += 10
        self.signal(OWNER1)
        self.jup.fail = True
        after = self.portfolio.tick(self.jup,lambda:self.now,close_all=True)
        self.assertEqual(before['cash_usdc'],after['cash_usdc'])
        self.assertEqual(len(after['open_positions']),2)
        self.assertIsNone(after['equity_usdc'])
        self.assertTrue(after['entry_blockers'])
        self.jup.fail = False
        self.now += 10
        closed = self.portfolio.tick(self.jup,lambda:self.now,close_all=True)
        self.assertFalse(closed['open_positions'])
        self.assertEqual(closed['closed_positions'],2)

    def test_all_exits_before_any_entry(self):
        events=[]
        with patch.object(self.portfolio.ledgers['conservative'],'refresh_positions',side_effect=lambda *a:events.append('exit-c')), \
             patch.object(self.portfolio.ledgers['balanced'],'refresh_positions',side_effect=lambda *a:events.append('exit-b')), \
             patch.object(self.portfolio.ledgers['aggressive'],'refresh_positions',side_effect=lambda *a:events.append('exit-a')), \
             patch.object(self.portfolio.ledgers['conservative'],'enter_positions',side_effect=lambda *a:events.append('entry-c')), \
             patch.object(self.portfolio.ledgers['balanced'],'enter_positions',side_effect=lambda *a:events.append('entry-b')), \
             patch.object(self.portfolio.ledgers['aggressive'],'enter_positions',side_effect=lambda *a:events.append('entry-a')):
            self.portfolio.tick(self.jup,lambda:self.now)
        self.assertEqual(events[:3],['exit-c','exit-b','exit-a'])

    def test_guard_is_checked_again_after_network(self):
        oid = self.signal()
        ledger = self.portfolio.ledgers['conservative']
        calls=[]
        def guard(*args):
            calls.append(args)
            return len(calls)==1
        self.assertFalse(ledger.try_open(oid,self.jup,lambda:self.now,entry_guard=guard))
        self.assertEqual(ledger.report()['cash_usdc'],600)

    def test_atomic_debit_rolls_back_profile_position(self):
        oid = self.signal()
        self.store.db.execute("CREATE TRIGGER fail_debit BEFORE UPDATE ON paper_conservative_account BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.portfolio.ledgers['conservative'].try_open(oid,self.jup,lambda:self.now)
        self.assertFalse(self.portfolio.open_positions())
        self.assertEqual(self.portfolio.report()['cash_usdc'],1000)

    def test_profile_invalidation_only_closes_affected_profile(self):
        self.signal()
        self.portfolio.tick(self.jup,lambda:self.now)
        self.now += 10
        row = confirmed(self.plan,self.now,liquidity_usd=30000)
        self.store.record_enriched(row,'invalidation')
        result=self.portfolio.tick(self.jup,lambda:self.now,paused=True)
        self.assertEqual(result['profiles']['conservative']['closed_positions'],1)
        self.assertEqual(len(result['profiles']['aggressive']['open_positions']),1)

    def test_notification_profile_ids_do_not_collide_and_activation_is_once(self):
        n=Notifications(self.root)
        client=FakeClient()
        try:
            link=n.pair(client,self.now-2)
            nonce=link.split('start=')[1]
            self.assertTrue(n.accept_updates([{'update_id':1,'message':{'date':int(self.now-1),'chat':{'id':789,'type':'private'},
                'from':{'id':789,'is_bot':False},'text':'/start '+nonce}}],self.now))
            self.signal()
            self.portfolio.tick(self.jup,lambda:self.now)
            n.collect(self.now)
            n.collect(self.now+1)
            keys=[r[0] for r in n.db.execute('SELECT event_key FROM outbox')]
            self.assertIn('open:conservative:1',keys)
            self.assertIn('open:aggressive:1',keys)
            self.assertEqual(sum(k.startswith('profiles:') for k in keys),1)
            msg=n.db.execute("SELECT text FROM outbox WHERE event_key='open:aggressive:1'").fetchone()[0]
            self.assertIn('Perfil: Agresivo',msg)
        finally:
            n.db.close()

    def test_report_pause_close_and_resume_find_profile_positions(self):
        self.signal()
        self.portfolio.tick(self.jup,lambda:self.now)
        with patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(main(['report','--data-dir',str(self.root)]),0)
            self.assertEqual(len(json.loads(output.getvalue())['profiles']),3)
        with patch('sys.stdout',new_callable=io.StringIO),patch('sys.stderr',new_callable=io.StringIO):
            self.assertEqual(main(['close-all','--data-dir',str(self.root)]),0)
            self.assertEqual(main(['resume','--data-dir',str(self.root)]),2)
            self.portfolio.tick(self.jup,lambda:self.now,close_all=True)
            self.assertEqual(main(['resume','--data-dir',str(self.root)]),0)

    def test_followup_queue_prioritizes_confirmation_over_unseen_stream(self):
        self.store.note_token('solana',TOKEN,NOW-100)
        with self.store.db:
            self.store.db.execute("UPDATE tokens_v5 SET state='observing',last_scanned=? WHERE mint=?",(NOW-61,TOKEN))
        self.store.note_token('solana',OWNER1,NOW-1000)
        self.store.note_token('solana',OWNER2,NOW-900)
        self.assertEqual(self.store.due_tokens('solana',2,now=NOW,prioritize_observing=True),[TOKEN,OWNER1])
        self.assertEqual(self.store.due_tokens('solana',2,now=NOW),[OWNER1,OWNER2])


class MigrationTests(unittest.TestCase):
    def test_untraded_legacy_is_preserved_and_not_added_to_total(self):
        with tempfile.TemporaryDirectory() as tmp, ObservationStore(Path(tmp)/'db') as store:
            PaperLedger(store,PaperPolicy())
            created=store.db.execute('SELECT created_at FROM paper_account').fetchone()[0]
            portfolio=ProfilePortfolio(store,ProfilePlan.load(PLAN_PATH))
            self.assertEqual(portfolio.report()['cash_usdc'],1000)
            self.assertEqual(store.db.execute('SELECT created_at FROM paper_account').fetchone()[0],created)

    def test_migration_with_legacy_activity_fails_without_creating_funds(self):
        with tempfile.TemporaryDirectory() as tmp, ObservationStore(Path(tmp)/'db') as store:
            PaperLedger(store,PaperPolicy())
            with store.db:
                store.db.execute('UPDATE paper_account SET cash_micro=900000000')
            with self.assertRaises(ValueError):
                ProfilePortfolio(store,ProfilePlan.load(PLAN_PATH))
            self.assertIsNone(active_plan(store))
            self.assertEqual(store.db.execute('SELECT count(*) FROM paper_conservative_account').fetchone()[0],0)


if __name__=='__main__':
    unittest.main()
