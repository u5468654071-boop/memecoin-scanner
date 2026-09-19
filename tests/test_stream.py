import asyncio
import importlib.util
import json
import tempfile
import threading
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
        stop = threading.Event()
        ingested = []
        original_ingest = ObservationStore.ingest_event

        def ingest(store, item):
            result = original_ingest(store, item)
            ingested.append(result)
            if len(ingested) == 2:
                stop.set()
            return result

        async def handler(websocket):
            subscriptions.extend([json.loads(await websocket.recv()), json.loads(await websocket.recv())])
            await websocket.send('not-json')
            await websocket.send(json.dumps({'message': 'subscription confirmed'}))
            await websocket.send(json.dumps(event))
            await websocket.send(json.dumps(event))
            await websocket.send(json.dumps(dict(event, txType='buy')))
            await websocket.wait_closed()

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'db'
            async with serve(handler, '127.0.0.1', 0) as server:
                port = server.sockets[0].getsockname()[1]
                with patch('event_stream.STREAM_URL', f'ws://127.0.0.1:{port}'), \
                        patch.object(ObservationStore, 'ingest_event', ingest):
                    # Acabar al procesar ambos mensajes, no según la velocidad del runner.
                    count = await asyncio.wait_for(collect(db, duration=0, stop_event=stop), timeout=10)
            self.assertEqual(count, 1)
            self.assertEqual(ingested, [True, False])
            self.assertEqual(subscriptions, [{'method': 'subscribeNewToken'}, {'method': 'subscribeMigration'}])
            with ObservationStore(db) as store:
                self.assertEqual(store.db.execute('SELECT count(*) FROM events_v5').fetchone()[0], 1)
                self.assertEqual(store.due_tokens('solana', 10), [TOKEN])
                self.assertTrue(store.report_enhanced()['stream_gaps'])
