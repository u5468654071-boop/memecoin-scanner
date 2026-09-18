import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from telegram_notifications import Notifications, TelegramClient, TelegramError, main
from service_health import heartbeat


NOW = 1789745000
TOKEN = 'So11111111111111111111111111111111111111112'


class FakeClient:
    def __init__(self):
        self.sent = []
        self.error = None

    def call(self, method, payload=None):
        return {'id': 123, 'username': 'fixture_bot', 'is_bot': True} if method == 'getMe' else {'url': ''}

    def send(self, chat_id, text):
        if self.error:
            raise self.error
        self.sent.append((chat_id, text))
        return len(self.sent)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.n = Notifications(self.root)
        self.client = FakeClient()
        self.link = self.n.pair(self.client, NOW)
        self.nonce = self.link.split('start=')[1]
        heartbeat(self.root/'scanner.heartbeat.json', 'waiting', NOW)
        heartbeat(self.root/'paper.heartbeat.json', 'waiting', NOW)
        self.source = sqlite3.connect(self.root/'scanner.sqlite3')
        self.source.execute('''CREATE TABLE paper_positions (id INTEGER PRIMARY KEY, mint TEXT,
            opened_at REAL, closed_at REAL, cost_micro INTEGER, state TEXT, pnl_micro INTEGER,
            exit_reason TEXT, last_error TEXT)''')

    def tearDown(self):
        self.source.close()
        self.n.db.close()
        self.temp.cleanup()

    def update(self, nonce=None, chat=789, kind='private', at=NOW+2, ident=1):
        return {'update_id': ident, 'message': {'date': at, 'chat': {'id': chat, 'type': kind},
            'from': {'id': chat, 'is_bot': False}, 'text': '/start '+(self.nonce if nonce is None else nonce)}}

    def pair(self):
        self.assertTrue(self.n.accept_updates([self.update()], NOW+3))

    def position(self, opened=NOW+4, closed=None):
        with self.source:
            self.source.execute('INSERT INTO paper_positions VALUES (1,?,?,?,?,?,?,?,NULL)',
                (TOKEN, opened, closed, 25050000, 'closed' if closed else 'open', 1000000 if closed else None,
                 'objetivo observado' if closed else None))

    def test_pair_requires_fresh_nonce_private_sender_and_is_one_use(self):
        self.assertFalse(self.n.accept_updates([self.update('wrong')], NOW+3))
        self.assertFalse(self.n.accept_updates([self.update(kind='group', ident=2)], NOW+3))
        self.assertFalse(self.n.accept_updates([self.update(at=NOW-20, ident=3)], NOW+3))
        impostor = self.update(ident=4)
        impostor['message']['from']['id'] = 999
        self.assertFalse(self.n.accept_updates([impostor], NOW+3))
        self.assertTrue(self.n.accept_updates([self.update(ident=5)], NOW+3))
        self.assertIsNone(self.n.binding()['nonce_hash'])
        self.assertFalse(self.n.accept_updates([self.update(chat=999, ident=6)], NOW+4))
        self.assertEqual(self.n.binding()['chat_id'], 789)
        with self.assertRaises(ValueError):
            self.n.pair(self.client, NOW+5)

    def test_pair_expired_or_replayed_update_rejected(self):
        self.n.accept_updates([self.update('wrong', ident=10)], NOW+3)
        self.assertFalse(self.n.accept_updates([self.update(ident=9)], NOW+3))
        self.assertFalse(self.n.accept_updates([self.update(ident=11)], NOW+3601))
        self.assertFalse(self.n.status()['paired'])

    def test_unpaired_never_collects_or_sends(self):
        self.position()
        self.n.collect(NOW+5)
        self.assertFalse(self.n.deliver_one(self.client, NOW+5))
        self.assertEqual(self.n.status()['pending'], 0)

    def test_open_and_close_between_polls_survive_restart_without_duplicates(self):
        self.pair()
        self.position(closed=NOW+8)
        self.n.collect(NOW+10)
        self.n.db.close()
        self.n = Notifications(self.root)
        self.n.collect(NOW+20)
        self.assertEqual(self.n.status()['pending'], 3)
        for i in range(3):
            self.assertTrue(self.n.deliver_one(self.client, NOW+20+i))
        self.n.collect(NOW+30)
        self.assertFalse(self.n.deliver_one(self.client, NOW+30))
        self.assertEqual(len(self.client.sent), 3)
        self.assertTrue(all(chat == 789 and text.startswith('SIMULACIÓN') for chat, text in self.client.sent))
        self.assertIn('+1.00 USDC', self.client.sent[-1][1])

    def test_does_not_send_old_closed_history(self):
        self.pair()
        self.position(opened=NOW-100, closed=NOW-50)
        self.n.collect(NOW+10)
        self.assertEqual(self.n.status()['pending'], 1)

    def test_failure_retry_after_persists_and_preserves_order(self):
        self.pair()
        self.position()
        self.n.collect(NOW+10)
        self.client.error = TelegramError(429, 300)
        self.assertFalse(self.n.deliver_one(self.client, NOW+10))
        self.n.db.close()
        self.n = Notifications(self.root)
        self.client.error = None
        self.assertFalse(self.n.deliver_one(self.client, NOW+309))
        self.assertTrue(self.n.deliver_one(self.client, NOW+310))
        self.assertIn('Telegram conectado', self.client.sent[0][1])
        self.assertEqual(self.n.status()['pending'], 1)

    def test_outage_and_recovery_report_once_per_transition(self):
        self.pair()
        self.position()
        with self.source:
            self.source.execute("UPDATE paper_positions SET last_error='no route'")
        self.n.collect(NOW+10)
        self.n.collect(NOW+20)
        self.assertEqual(self.n.status()['pending'], 3)
        with self.source:
            self.source.execute('UPDATE paper_positions SET last_error=NULL')
        self.n.collect(NOW+30)
        self.n.collect(NOW+31)
        self.assertEqual(self.n.status()['pending'], 4)
        self.n.collect(NOW+1000)
        self.n.collect(NOW+1001)
        self.assertEqual(self.n.status()['pending'], 6)
        heartbeat(self.root/'paper.heartbeat.json', 'waiting', NOW+1002)
        self.n.collect(NOW+1003)
        self.n.collect(NOW+1004)
        self.assertEqual(self.n.status()['pending'], 7)

    def test_status_is_offline_and_hides_destination(self):
        self.pair()
        with patch('sys.stdout', new_callable=io.StringIO) as output, patch('telegram_notifications.TelegramClient') as client:
            self.assertEqual(main(['status', '--data-dir', str(self.root)]), 0)
            client.assert_not_called()
            self.assertNotIn('789', output.getvalue())
            self.assertTrue(json.loads(output.getvalue())['paired'])

    def test_collect_transaction_rolls_back_on_failure(self):
        self.pair()
        self.position(closed=NOW+8)
        self.n.db.execute("CREATE TRIGGER fail_close BEFORE INSERT ON outbox WHEN NEW.event_key='close:1' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.n.collect(NOW+10)
        self.assertEqual(self.n.status()['pending'], 1)
        self.n.db.execute('DROP TRIGGER fail_close')
        self.n.collect(NOW+11)
        self.assertEqual(self.n.status()['pending'], 3)


class ClientTests(unittest.TestCase):
    def test_network_error_never_exposes_secret_url(self):
        with patch('telegram_notifications.build_opener') as build:
            client = TelegramClient('123:'+'x'*30)
            build.return_value.open.side_effect = URLError('https://api.telegram.org/bot123:'+'x'*30)
            with self.assertRaises(TelegramError) as exc:
                client.call('getMe')
            self.assertNotIn('xxx', str(exc.exception))
            self.assertNotIn('https', str(exc.exception))

    def test_retry_after_from_http_error(self):
        with patch('telegram_notifications.build_opener') as build:
            client = TelegramClient('123:'+'x'*30)
            build.return_value.open.side_effect = HTTPError('redacted', 429, 'rate limit', {},
                io.BytesIO(json.dumps({'ok': False, 'error_code': 429, 'parameters': {'retry_after': 600}}).encode()))
            with self.assertRaises(TelegramError) as exc:
                client.call('getMe')
            self.assertEqual(exc.exception.retry_after, 600)

    def test_send_checks_recipient_and_disables_paid_broadcasts(self):
        client = TelegramClient('123:'+'x'*30)
        with patch.object(client, 'call', return_value={'message_id': 9, 'chat': {'id': 789}}) as call:
            self.assertEqual(client.send(789, 'SIMULACIÓN'), 9)
            self.assertFalse(call.call_args[0][1]['allow_paid_broadcast'])
            with self.assertRaises(ValueError):
                client.send(-100, 'message')
            call.return_value['chat']['id'] = 999
            with self.assertRaises(TelegramError):
                client.send(789, 'SIMULACIÓN')


if __name__ == '__main__':
    unittest.main()
