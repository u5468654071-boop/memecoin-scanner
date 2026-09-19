import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from discovery import DiscoveryQueue, coverage_report, screen
from engine import EnhancedPolicy, decide
from observation_store import ObservationStore
from providers import Jupiter, batch_pairs, network_evidence, timestamp
from profile_plan import ProfilePlan
from profile_portfolio import ProfilePortfolio
from scanner_v05 import main
from test_profiles import PLAN_PATH, confirmed, SizedJupiter
from test_v05 import NOW, TOKEN, OWNER1, OWNER2, ACCOUNT1, organic, enriched, report
from test_scanner import pair
from memecoin_scanner import utc_string


def token_info(mint=TOKEN, at=NOW):
    return {'id':mint,'updatedAt':utc_string(at),'decimals':6,'organicScore':60,
            'stats5m':{'numOrganicBuyers':25},'firstPool':{'createdAt':utc_string(NOW-7200)},'audit':{}}


class BatchingTests(unittest.TestCase):
    def test_market_lists_filter_known_old_tokens_before_limit_and_keep_identity(self):
        transport = Mock(jupiter_key='fixture')
        old = token_info(OWNER1)
        old['firstPool']['createdAt'] = utc_string(NOW-169*3600)
        future = token_info(OWNER2)
        future['firstPool']['createdAt'] = utc_string(NOW+60)
        transport.get.return_value = [old,future,token_info(),token_info(),{'id':'invalid'}]
        jup = Jupiter(transport)
        self.assertEqual(jup.discover_market(1,'traded',now=NOW),[TOKEN])
        self.assertEqual(transport.get.call_args.args[0],'https://api.jup.ag/tokens/v2/toptraded/5m?limit=100')
        self.assertEqual(jup.discover_market(1,'trending',now=NOW),[TOKEN])
        self.assertIn('toptrending/1h',transport.get.call_args.args[0])
        with self.assertRaises(ValueError):
            jup.discover_market(1,'execute')

    def test_invalid_market_response_is_a_visible_error(self):
        transport = Mock(jupiter_key='fixture')
        transport.get.return_value = {'error':'unavailable'}
        with self.assertRaises(RuntimeError):
            Jupiter(transport).discover_market(30,'traded')

    def test_nanosecond_provider_timestamp_is_not_reported_as_missing(self):
        self.assertEqual(timestamp('2026-09-18T20:10:21.034234634Z'),timestamp('2026-09-18T20:10:21.034234Z'))
        self.assertIsNone(timestamp('2026-09-18T20:10:21.034234634'))

    def test_batch_resolves_exact_id_and_does_not_refresh_age_from_cache(self):
        transport = Mock(jupiter_key='fixture')
        transport.get.return_value = [token_info(),token_info(OWNER1)]
        jup = Jupiter(transport)
        batch = jup.prefetch([TOKEN,OWNER1,OWNER2],now=NOW)
        self.assertEqual(batch[OWNER2]['status'],'unavailable')
        self.assertEqual(jup.token(TOKEN,now=NOW+20)['received_at'],NOW)
        self.assertEqual(transport.get.call_count,1)
        self.assertEqual(jup.token(TOKEN,now=NOW+301)['status'],'stale')
        self.assertEqual(transport.get.call_count,2)

    def test_duplicate_and_unrelated_batch_results_cannot_approve(self):
        transport = Mock(jupiter_key='fixture')
        transport.get.return_value = [token_info(),token_info(),token_info(OWNER1)]
        jup = Jupiter(transport)
        self.assertEqual(jup.prefetch([TOKEN,OWNER2],now=NOW)[TOKEN]['status'],'unavailable')
        self.assertEqual(jup.token(OWNER2,now=NOW)['status'],'unavailable')

    def test_batch_query_never_sends_more_than_100_or_invalid_addresses(self):
        transport = Mock(jupiter_key='fixture')
        jup = Jupiter(transport)
        for mints in ([],[TOKEN,'bad']):
            with self.assertRaises(ValueError):
                jup.prefetch(mints)
        transport.get.assert_not_called()

    def test_pair_batch_rejects_quote_side_and_foreign_chain(self):
        transport = Mock()
        good = pair()
        wrong = copy.deepcopy(good)
        wrong['chainId'] = 'base'
        quote_side = copy.deepcopy(good)
        quote_side['baseToken']['address'] = OWNER1
        quote_side['quoteToken']['address'] = TOKEN
        transport.get.return_value = [wrong,quote_side,good]
        self.assertEqual(batch_pairs(transport,'solana',[TOKEN])[TOKEN],[good])


class ScreeningTests(unittest.TestCase):
    def test_prescreen_must_match_one_whole_profile_not_a_mix_of_thresholds(self):
        plan = ProfilePlan.load(PLAN_PATH)
        data = organic()
        data.update(organic_score=30,first_pool_at=NOW-100*3600)
        result = screen(TOKEN,[pair()],data,NOW,20000,168,2/60,plan.profiles)
        self.assertEqual(result['stage'],'deferred')
        self.assertEqual(result['evidence']['compatible_profiles'],[])
        self.assertIn('no_compatible_profile',result['reasons'])
        data['first_pool_at'] = NOW-2*3600
        self.assertEqual(screen(TOKEN,[pair()],data,NOW,20000,168,2/60,plan.profiles)['evidence']['compatible_profiles'],['aggressive'])

    def test_missing_buyers_wait_and_recover_when_provider_reports_activity(self):
        plan = ProfilePlan.load(PLAN_PATH)
        data = organic()
        del data['windows']['5m']['numOrganicBuyers']
        result = screen(TOKEN,[pair()],data,NOW,20000,168,2/60,plan.profiles)
        self.assertIn('organic_buyers_missing',result['reasons'])
        self.assertEqual(result['stage'],'deferred')
        data['windows']['5m']['numOrganicBuyers'] = 25
        self.assertEqual(screen(TOKEN,[pair()],data,NOW,20000,168,2/60,plan.profiles)['stage'],'ready')

    def test_network_amount_over_supply_is_not_clamped_into_approval(self):
        data = report()
        data['insiderNetworks'] = [{'id':'fixture','size':2,'tokenAmount':int(data['token']['supply'])+1}]
        result = network_evidence(data)
        self.assertEqual(result['status'],'incomplete')
        self.assertIsNone(result['max_group_supply_pct'])
        self.assertEqual(result['reason_codes'],['network_amount_exceeds_supply'])

    def screen(self, pairs, data=None):
        return screen(TOKEN,pairs,organic() if data is None else data,NOW,20000,168,2/60)

    def test_pool_with_valid_data_is_only_ready_for_analysis(self):
        result = self.screen([pair()])
        self.assertEqual(result['stage'],'ready')
        self.assertNotIn('quality_pass',result)

    def test_new_curve_never_qualifies_as_dex_even_with_liquidity(self):
        curve = pair()
        curve['dexId'] = 'pumpfun'
        self.assertEqual(self.screen([curve])['reasons'],['awaiting_dex_pool'])

    def test_missing_or_stale_data_never_becomes_eligible(self):
        for data in ({'status':'unavailable'},{**organic(),'status':'stale'}, {**organic(),'flagged_suspicious':True}):
            self.assertEqual(self.screen([pair()],data)['stage'],'deferred')
        p = pair()
        p.pop('liquidity')
        self.assertIn('liquidity_missing',self.screen([p])['reasons'])

    def test_first_pool_age_cannot_be_reset_by_a_new_pair(self):
        data = organic()
        data['first_pool_at'] = NOW-200*3600
        self.assertIn('outside_profile_age',self.screen([pair()],data)['reasons'])

    def test_market_failure_classified_independently_of_missing_fields(self):
        row = enriched()
        row.update(price_change_h1=None,liquidity_usd=1000)
        result = decide(row,EnhancedPolicy())
        checks = {c['code']:c['status'] for c in result['market_checks']}
        self.assertEqual(checks['liquidity'],'blocked')
        self.assertEqual(checks['price_change'],'missing')
        self.assertEqual(result['state'],'rejected')

    def test_young_age_waits_but_does_not_pass_and_does_not_quote_too_early(self):
        plan = ProfilePlan.load(PLAN_PATH)
        row = enriched()
        row['age_hours'] = 0.01
        self.assertEqual(plan.sizes_to_quote(row,NOW),[])
        result = plan.evaluate(row,[],NOW)['aggressive']
        self.assertFalse(result['quality_pass'])
        self.assertIn({'code':'age','status':'waiting','reason':'edad del par desconocida o fuera del intervalo'},result['market_checks'])

    def test_network_diagnostics_do_not_treat_absence_as_empty(self):
        r = report()
        del r['insiderNetworks']
        self.assertEqual(network_evidence(r)['reason_codes'],['networks_not_reported'])
        self.assertEqual(network_evidence(r)['status'],'unavailable')
        self.assertEqual(network_evidence(report())['status'],'reported')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'scanner.sqlite3'
        self.store = ObservationStore(self.path)
        self.plan = ProfilePlan.load(PLAN_PATH)
        self.queue = DiscoveryQueue(self.store,self.plan)

    def tearDown(self):
        self.store.__exit__()
        self.tmp.cleanup()

    def ready(self, mints, at=NOW):
        self.queue.offer({mint:['jupiter_recent'] for mint in mints},at)
        with self.store.db:
            self.store.db.execute("UPDATE discovery_v8 SET stage='ready',last_probe=?",(at,))

    def test_stream_creation_is_recorded_but_only_migration_enters_deep_queue(self):
        event = {'source':'pumpportal','mint':TOKEN,'kind':'create','received_at':NOW,'payload':{}}
        self.store.ingest_event(event)
        self.queue.import_migrations(NOW)
        self.assertEqual(self.queue.report(NOW)['active_24h'],0)
        self.store.ingest_event({**event,'kind':'migrate','received_at':NOW+1})
        self.queue.import_migrations(NOW+1)
        self.assertEqual(self.queue.report(NOW+1)['active_24h'],1)

    def test_confirmation_has_capacity_and_leaves_space_for_discovery(self):
        self.ready([TOKEN,OWNER1,OWNER2,ACCOUNT1])
        for mint in (TOKEN,OWNER1,OWNER2):
            self.queue.analyzed({**enriched(NOW-61),'base_address':mint,'state':'observing'},NOW-61)
        chosen = self.queue.select(3,NOW)
        self.assertEqual(len(chosen),3)
        self.assertIn(ACCOUNT1,chosen)
        self.assertEqual(sum('confirmation' in src for src in chosen.values()),2)

    def test_restart_preserves_cooldown_and_old_stream_cannot_starve_ready(self):
        self.ready([TOKEN])
        self.queue.analyzed({**enriched(),'state':'rejected'},NOW)
        self.queue.offer({TOKEN:['boosted']},NOW+1)
        self.assertFalse(self.queue.select(3,NOW+1))
        with ObservationStore(self.path) as other:
            self.assertFalse(DiscoveryQueue(other,self.plan).select(3,NOW+1))
        self.ready([OWNER1],NOW+1)
        self.assertIn(OWNER1,self.queue.select(3,NOW+1))

    def test_confirmation_window_expires_and_no_invented_freshness(self):
        self.ready([TOKEN],NOW-1900)
        self.queue.analyzed({**enriched(NOW-1900),'state':'observing'},NOW-1900)
        self.assertFalse(self.queue.select(3,NOW))

    def test_batch_failure_keeps_candidates_deferred_and_bounded(self):
        self.queue.offer({TOKEN:['boosted'],OWNER1:['profiles']},NOW)
        jup = Mock()
        jup.prefetch.side_effect = RuntimeError('jupiter: HTTP 429')
        transport = Mock()
        transport.get.side_effect = RuntimeError('dexscreener: HTTP 500')
        result = self.queue.refresh(jup,transport,now=NOW)
        self.assertEqual(result['probed'],2)
        self.assertEqual(len(result['errors']),2)
        self.assertFalse(self.queue.select(3,NOW))
        self.assertEqual(self.queue.refresh(jup,transport,now=NOW+1)['probed'],0)

    def test_migration_backlog_cannot_starve_current_market_lists(self):
        self.queue.offer({OWNER2:['pumpportal_migration']},NOW-900)
        self.queue.offer({ACCOUNT1:['pumpportal_migration']},NOW-300)
        self.queue.offer({TOKEN:['jupiter_recent'],OWNER1:['jupiter_organic']},NOW)
        jup = Mock()
        jup.prefetch.side_effect = lambda mints,now:{m:{'status':'unavailable'} for m in mints}
        transport = Mock()
        transport.get.return_value = []
        result = self.queue.refresh(jup,transport,now=NOW,limit=3)
        self.assertEqual(result['probed'],3)
        self.assertEqual(set(jup.prefetch.call_args.args[0]),{TOKEN,OWNER1,ACCOUNT1})

    def test_unused_screening_capacity_is_available_to_other_lane(self):
        self.queue.offer({m:['pumpportal_migration'] for m in (TOKEN,OWNER1,OWNER2)},NOW)
        jup = Mock()
        jup.prefetch.side_effect = lambda mints,now:{m:{'status':'unavailable'} for m in mints}
        transport = Mock()
        transport.get.return_value = []
        self.assertEqual(self.queue.refresh(jup,transport,now=NOW,limit=3)['probed'],3)

    def test_arrival_stream_and_due_revisits_both_receive_screening_capacity(self):
        self.queue.offer({TOKEN:['jupiter_recent']},NOW-500)
        with self.store.db:
            self.store.db.execute('UPDATE discovery_v8 SET last_probe=?,next_probe=?',(NOW-400,NOW-220))
        self.queue.offer({OWNER1:['jupiter_market_traded'],OWNER2:['jupiter_recent']},NOW)
        jup = Mock()
        jup.prefetch.side_effect = lambda mints,now:{m:{'status':'unavailable'} for m in mints}
        transport = Mock()
        transport.get.return_value = []
        self.queue.refresh(jup,transport,now=NOW,limit=2)
        self.assertEqual(set(jup.prefetch.call_args.args[0]),{TOKEN,OWNER1})

    def test_ready_report_excludes_expired_evidence_and_deep_cooldown(self):
        self.ready([TOKEN,OWNER1,OWNER2])
        with self.store.db:
            self.store.db.execute('UPDATE discovery_v8 SET last_probe=? WHERE mint=?',(NOW-301,OWNER1))
            self.store.db.execute('UPDATE discovery_v8 SET next_full=? WHERE mint=?',(NOW+60,OWNER2))
        result = self.queue.report(NOW)
        self.assertEqual(result['ready_and_due'],1)
        self.assertEqual(result['ready_with_expired_probe'],1)
        self.assertEqual(list(self.queue.select(3,NOW)),[TOKEN])

    def test_coverage_separates_skipped_quotes_from_route_errors(self):
        row = confirmed(self.plan)
        row['profiles']['aggressive']['exit_quotes'][0]['status'] = 'not_requested'
        row['profiles']['balanced']['exit_quotes'][0]['status'] = 'unavailable'
        row['profiles']['balanced']['decision_checks']['blockers'] = ['fixture risk']
        self.store.record_enriched(row,'quotes')
        profiles = coverage_report(self.store,now=NOW)['profiles']
        self.assertEqual(profiles['aggressive']['quote_statuses'],{'not_requested':1})
        self.assertEqual(profiles['balanced']['quote_statuses'],{'unavailable':1})
        self.assertEqual(profiles['balanced']['decision_blockers'],{'fixture risk':1})

    def test_open_position_gets_followup_even_if_screening_is_deferred(self):
        portfolio = ProfilePortfolio(self.store,self.plan)
        oid = self.store.record_enriched(confirmed(self.plan),'fixture')
        portfolio.tick(SizedJupiter(lambda:NOW),lambda:NOW)
        self.queue.offer({TOKEN:['boosted']},NOW)
        with self.store.db:
            self.store.db.execute("UPDATE discovery_v8 SET stage='deferred',next_full=?",(NOW+999,))
        self.assertIn(TOKEN,self.queue.select(3,NOW))
        portfolio.tick(SizedJupiter(lambda:NOW+1),lambda:NOW+1,close_all=True)
        self.assertNotIn(TOKEN,self.queue.select(3,NOW+2))

    def test_report_separates_versions_and_counts_revisits(self):
        row = confirmed(self.plan)
        self.store.record_enriched(row,'one')
        self.store.record_enriched({**row,'scanned_at':utc_string(NOW+61)},'two')
        self.store.record_enriched({**row,'scanner_version':'0.7.0'},'old')
        report = coverage_report(self.store,now=NOW+62)
        self.assertEqual(report['observations'],2)
        self.assertEqual(report['unique_tokens'],1)
        self.assertEqual(report['tokens_with_revisits'],1)

    def test_scan_no_ready_tokens_is_valid_idle_cycle_without_deep_calls(self):
        path = Path(self.tmp.name)
        with patch('scanner_v05.Transport') as transport, patch('scanner_v05.Engine') as engine, \
             patch('memecoin_scanner.gather_candidates',return_value=({},[])), \
             patch('builtins.print'):
            engine.return_value.jupiter.enabled = True
            code = main(['--profiles',str(PLAN_PATH),'--candidates','','--db',str(self.path),
                         '--json-output',str(path/'report.json'),'--log',str(path/'log.csv')])
            self.assertEqual(code,0)
            engine.return_value.analyze.assert_not_called()
            transport.return_value.get.assert_not_called()

    def test_scan_persists_screened_observation_and_revisits_after_restart(self):
        path = Path(self.tmp.name)
        args = ['--profiles',str(PLAN_PATH),'--candidates','profiles','--db',str(self.path),
                '--json-output',str(path/'report.json'),'--log',str(path/'log.csv')]
        with patch('scanner_v05.Transport'), patch('scanner_v05.Engine') as engine, \
             patch('memecoin_scanner.gather_candidates',return_value=({TOKEN:['profiles']},[])), \
             patch('discovery.batch_pairs',return_value={TOKEN:[pair()]}), \
             patch('scanner_v05.ForwardStudy.enroll'), patch('scanner_v05.time.time',return_value=NOW) as clock, \
             patch('builtins.print'):
            engine.return_value.jupiter.enabled = True
            engine.return_value.jupiter.prefetch.return_value = {TOKEN:organic()}
            row = confirmed(self.plan)
            row.update(state='observing',quality_pass=False)
            engine.return_value.analyze.return_value = row
            self.assertEqual(main(args),0)
            saved = json.loads((path/'report.json').read_text())
            self.assertEqual(saved['coverage']['observations'],1)
            self.assertEqual(saved['discovery']['stages'],{'confirm':1})
            clock.return_value = NOW+61
            engine.return_value.analyze.return_value = {**row,'scanned_at':utc_string(NOW+61)}
            self.assertEqual(main(args),0)
            saved = json.loads((path/'report.json').read_text())
            self.assertEqual(saved['coverage']['tokens_with_revisits'],1)
            self.assertIn('confirmation',engine.return_value.analyze.call_args.args[2])
            self.assertEqual(engine.return_value.jupiter.prefetch.call_count,1)

    def test_evaluate_mode_runs_forward_study_without_new_scans(self):
        with patch('scanner_v05.Transport'), patch('scanner_v05.Engine') as engine, \
             patch('scanner_v05.ForwardStudy.evaluate') as evaluate, patch('builtins.print'):
            self.assertEqual(main(['--profiles',str(PLAN_PATH),'--evaluate','--db',str(self.path)]),0)
            evaluate.assert_called_once_with(engine.return_value.jupiter)
            engine.return_value.analyze.assert_not_called()


if __name__=='__main__':
    unittest.main()
