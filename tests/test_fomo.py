import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from discovery import DiscoveryQueue
from fomo_source import MAX_BYTES, capture_from_snapshots, import_snapshot, parse_snapshot
from fomo_receive import receive
from memecoin_scanner import utc_string
from observation_store import ObservationStore
from profile_plan import ProfilePlan
from scanner_v05 import main
from test_profiles import PLAN_PATH
from test_v05 import NOW, TOKEN, OWNER1


def batch(at=NOW, mint=TOKEN, view='trending'):
    return {'schema':1, 'source':'fomo_browser', 'captured_at':utc_string(at),
            'tokens':[{'mint':mint, 'view':view, 'url':'https://fomo.family/tokens/solana/'+mint}]}


def encode(data):
    return json.dumps(data).encode()


class ContractTests(unittest.TestCase):
    def test_missing_fields_market_claims_and_foreign_urls_rejected(self):
        samples = []
        for key in batch():
            item = batch()
            del item[key]
            samples.append(item)
        item = batch()
        item['quality_pass'] = True
        samples.append(item)
        for url in ('https://example.com/'+TOKEN,
                    'https://fomo.family/tokens/solana/'+TOKEN+'?token=secret',
                    'https://fomo.family/tokens/solana/'+OWNER1):
            item = batch()
            item['tokens'][0]['url'] = url
            samples.append(item)
        item = batch(mint='0x'+'1'*40)
        samples.append(item)
        for sample in samples:
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                parse_snapshot(encode(sample), NOW)

    def test_corrupt_deep_duplicate_and_large_json_are_bounded_errors(self):
        for raw in (b'\xff', b'['*2000+b']'*2000, b' '*MAX_BYTES+b' ',
                    encode(batch()).replace(b'"schema": 1', b'"schema": 2,"schema":1')):
            with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
                parse_snapshot(raw,NOW)

    def test_freshness_is_capture_time_never_import_time(self):
        for at in (NOW-301,NOW+31):
            with self.assertRaises(ValueError):
                parse_snapshot(encode(batch(at)),NOW)
        for date in ('2026-09-19T00:00:00',float('nan'),None,{}):
            item = batch()
            item['captured_at'] = date
            with self.assertRaises(ValueError):
                parse_snapshot(encode(item),NOW)

    def test_duplicates_and_order_do_not_change_identity(self):
        item = batch()
        item['tokens'] += batch(mint=OWNER1)['tokens']
        _,_,digest = parse_snapshot(encode(item),NOW)
        item['tokens'] = list(reversed(item['tokens'])) + item['tokens']
        self.assertEqual(parse_snapshot(encode(item),NOW)[2],digest)

    def test_limits_apply_before_import(self):
        item = batch()
        item['tokens'] *= 91
        with self.assertRaisesRegex(ValueError,'invalid_token_count'):
            parse_snapshot(encode(item),NOW)
        alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
        item['tokens'] = [batch(mint=ch+'1'*31)['tokens'][0] for ch in alphabet[:31]]
        with self.assertRaisesRegex(ValueError,'view_limit'):
            parse_snapshot(encode(item),NOW)

    def test_rendered_panel_excludes_ticker_foreign_chain_and_detail(self):
        snapshot = (f'- link "ticker":\n  - /url: /tokens/solana/{OWNER1}\n'
                    '- button "Bonding"\n'
                    f'- link "selected":\n  - /url: /tokens/solana/{TOKEN}\n'
                    '- link "other chain":\n  - /url: /tokens/base/0x111\n'
                    '- separator "Cambiar el tamaño del panel de descubrimiento"\n'
                    f'- link "detail":\n  - /url: /tokens/solana/{OWNER1}\n')
        captures = [{'view':'trending','captured_at':utc_string(NOW),'snapshot':snapshot},
                    {'view':'graduated','captured_at':utc_string(NOW+20),'snapshot':snapshot}]
        with patch('fomo_source.time.time',return_value=NOW+20):
            result = capture_from_snapshots(captures)
            self.assertEqual(result['captured_at'],utc_string(NOW))
            self.assertEqual({t['mint'] for t in result['tokens']},{TOKEN})
            with self.assertRaises(ValueError):
                capture_from_snapshots(captures[:1]+captures[:1])
            with self.assertRaisesRegex(ValueError,'panel_missing'):
                capture_from_snapshots([{**captures[0],'snapshot':'- button "Iniciar sesión"'}])


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root/'scanner.sqlite3'
        self.path = self.root/'inbox.json'
        self.store = ObservationStore(self.db)
        self.plan = ProfilePlan.load(PLAN_PATH)
        self.queue = DiscoveryQueue(self.store,self.plan)

    def tearDown(self):
        self.store.__exit__()
        self.tmp.cleanup()

    def ingest(self, data=None, now=NOW):
        self.path.write_bytes(encode(batch() if data is None else data))
        return import_snapshot(self.path,self.store,self.queue,now)

    def row(self):
        return dict(self.store.db.execute('SELECT * FROM discovery_v8 WHERE mint=?',(TOKEN,)).fetchone())

    def test_import_queues_identity_only_without_approval_or_observation(self):
        item = batch()
        item['tokens'] += batch(view='graduated')['tokens']
        result = self.ingest(item)
        self.assertEqual(result['new_to_queue'],1)
        self.assertEqual(json.loads(self.row()['sources']),['fomo_graduated','fomo_trending'])
        self.assertFalse(self.queue.select(3,NOW))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],0)

    def test_replay_restart_and_new_batch_preserve_cooldowns(self):
        self.ingest()
        with self.store.db:
            self.store.db.execute('UPDATE discovery_v8 SET next_full=?,next_probe=?',(NOW+900,NOW+200))
        with ObservationStore(self.db) as other:
            result = import_snapshot(self.path,other,DiscoveryQueue(other,self.plan),NOW+50)
        self.assertEqual(result['state'],'current')
        self.assertEqual(self.row()['last_seen'],NOW)
        result = self.ingest(batch(NOW+60),NOW+60)
        self.assertEqual(result['batches'],2)
        self.assertEqual(result['new_to_queue'],0)
        self.assertEqual(self.row()['next_full'],NOW+900)
        self.assertEqual(self.row()['next_probe'],NOW+200)

    def test_old_conflicting_stale_and_partial_bad_batches_leave_queue_unchanged(self):
        self.ingest()
        before = self.row()
        self.assertEqual(self.ingest(batch(NOW-10))['state'],'out_of_order')
        self.assertEqual(self.ingest(batch(mint=OWNER1))['reason'],'conflicting_capture')
        self.assertEqual(self.ingest(now=NOW+301)['state'],'stale')
        item = batch(NOW+20)
        item['tokens'] += [{'mint':OWNER1,'view':'trending','url':'wrong'}]
        self.assertEqual(self.ingest(item,NOW+20)['state'],'invalid')
        self.assertEqual(before,self.row())
        self.assertEqual(self.store.db.execute('SELECT batches FROM fomo_source_v1').fetchone()[0],1)

    def test_optional_source_missing_does_not_stop_scanner(self):
        self.assertEqual(import_snapshot(None,self.store,self.queue)['state'],'disabled')
        self.assertEqual(import_snapshot(self.path,self.store,self.queue)['state'],'awaiting_capture')

    def test_receiver_preserves_last_good_capture_on_rejected_input(self):
        result = receive(encode(batch()),self.path,NOW)
        self.assertTrue(result['accepted'])
        before = self.path.read_bytes()
        for item in (batch(NOW-1),batch(mint=OWNER1)):
            with self.assertRaises(ValueError):
                receive(encode(item),self.path,NOW)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(receive(encode(batch()),self.path,NOW)['state'],'unchanged')
        self.assertEqual(receive(encode(batch(NOW+600)),self.path,NOW+600)['state'],'received')

    def test_scanner_runs_normal_prescreen_for_fomo_and_rejects_missing_liquidity(self):
        self.path.write_bytes(encode(batch()))
        with patch('scanner_v05.Transport'), patch('scanner_v05.Engine') as engine, \
             patch('memecoin_scanner.gather_candidates',return_value=({},[])), \
             patch('discovery.batch_pairs',return_value={TOKEN:[]}), \
             patch('scanner_v05.time.time',return_value=NOW), patch('builtins.print'):
            engine.return_value.jupiter.enabled = True
            engine.return_value.jupiter.prefetch.return_value = {TOKEN:{'status':'unavailable'}}
            args = ['--profiles',str(PLAN_PATH),'--candidates','','--db',str(self.db),
                    '--fomo-inbox',str(self.path),'--json-output',str(self.root/'report.json'),
                    '--log',str(self.root/'log.csv')]
            self.assertEqual(main(args),0)
            engine.return_value.jupiter.prefetch.assert_called_once()
            engine.return_value.analyze.assert_not_called()
        result = json.loads((self.root/'report.json').read_text())
        self.assertEqual(result['fomo_source']['state'],'imported')
        self.assertEqual(self.row()['stage'],'deferred')


if __name__ == '__main__':
    unittest.main()
