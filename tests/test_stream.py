import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from event_stream import collect
from observation_store import ObservationStore
from test_scanner import TOKEN


@unittest.skipUnless(importlib.util.find_spec('websockets'), 'dependencia opcional del stream no instalada')
class StreamIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_free_subscriptions_persist_deduplicated_events(self):
        from websockets.asyncio.server import serve
        subscriptions = []
        event = {'mint': TOKEN, 'txType': 'create', 'signature': 'fixture-create'}

        async def handler(websocket):
            subscriptions.extend([json.loads(await websocket.recv()), json.loads(await websocket.recv())])
            await websocket.send('not-json')
            await websocket.send(json.dumps({'message': 'subscription confirmed'}))
            await websocket.send(json.dumps(event))
            await websocket.send(json.dumps(event))
            await websocket.send(json.dumps(dict(event, txType='buy')))
            await asyncio.sleep(0.4)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'db'
            async with serve(handler, '127.0.0.1', 0) as server:
                port = server.sockets[0].getsockname()[1]
                with patch('event_stream.STREAM_URL', f'ws://127.0.0.1:{port}'):
                    count = await collect(db, duration=0.25)
            self.assertEqual(count, 1)
            self.assertEqual(subscriptions, [{'method': 'subscribeNewToken'}, {'method': 'subscribeMigration'}])
            with ObservationStore(db) as store:
                self.assertEqual(store.db.execute('SELECT count(*) FROM events_v5').fetchone()[0], 1)
                self.assertEqual(store.due_tokens('solana', 10), [TOKEN])
                self.assertTrue(store.report_enhanced()['stream_gaps'])
