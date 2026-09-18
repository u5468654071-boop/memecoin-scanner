"""Captura solo creación/migración gratuitas de PumpPortal, sin suscripciones de pago."""
from __future__ import annotations

import asyncio
import json
import time

from memecoin_scanner import number, obj, valid_address
from observation_store import ObservationStore

STREAM_URL = 'wss://pumpportal.fun/api/data'


def normalize_event(data, now=None):
    now = time.time() if now is None else now
    if not isinstance(data, dict) or data.get('txType') not in ('create', 'migrate'):
        return None
    if not valid_address('solana', data.get('mint')):
        return None
    event_at = number(data.get('blockTime'), 0)
    if event_at is not None and event_at > now + 30:
        return None
    signature = data.get('signature')
    if not isinstance(signature, str):
        signature = None
    # Conservar solo los campos necesarios, sin metadata ni URLs de promotores.
    payload = {key: data.get(key) for key in ('mint', 'txType', 'signature', 'blockTime', 'pool', 'traderPublicKey')}
    return {'source': 'pumpportal', 'mint': data['mint'], 'kind': data['txType'],
            'signature': signature, 'event_at': event_at, 'received_at': now, 'payload': payload}


async def collect(db_path, duration=60, stop_event=None):
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        raise RuntimeError('Para el stream instala requirements-stream.txt en un entorno virtual') from None
    deadline = time.monotonic() + duration if duration else float('inf')
    count, failures = 0, 0
    with ObservationStore(db_path) as store:
        # Un cierre anterior no significa que los eventos perdidos se hayan recuperado.
        gap = store.open_gap('inicio/reinicio del stream; intervalo previo sin replay garantizado')
        try:
            while time.monotonic() < deadline and not (stop_event and stop_event.is_set()):
                try:
                    async with connect(STREAM_URL, open_timeout=10, close_timeout=3, ping_interval=20,
                                       max_size=262144, max_queue=64, proxy=None) as websocket:
                        for method in ('subscribeNewToken', 'subscribeMigration'):
                            await websocket.send(json.dumps({'method': method}))
                        store.close_gap(gap)
                        gap = None
                        failures = 0
                        while time.monotonic() < deadline and not (stop_event and stop_event.is_set()):
                            try:
                                message = await asyncio.wait_for(websocket.recv(), timeout=min(1.0, max(0.01, deadline - time.monotonic())))
                            except asyncio.TimeoutError:
                                continue
                            try:
                                data = json.loads(message)
                                event = normalize_event(data)
                                if event:
                                    count += int(store.ingest_event(event))
                                elif isinstance(data, dict) and data.get('errors'):
                                    raise RuntimeError('Suscripción al stream rechazada')
                            except (ValueError, TypeError):
                                # Mensaje inválido no implica una creación válida ni un crash.
                                continue
                except Exception as exc:
                    # Nunca imprimir contenido del proveedor ni secretos en URLs.
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    failures += 1
                    if gap is None:
                        gap = store.open_gap('desconexión: ' + type(exc).__name__)
                    delay = min(30, 2**min(failures, 5), max(0, deadline - time.monotonic()))
                    # Revisar parada cada segundo incluso durante backoff.
                    for _ in range(int(delay) + 1):
                        if time.monotonic() >= deadline or (stop_event and stop_event.is_set()):
                            break
                        await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
        finally:
            if gap is None:
                store.open_gap('captura detenida; eventos futuros no observados')
    return count
