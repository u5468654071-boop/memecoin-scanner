"""Fomo browser observations become discovery leads, never market evidence or orders."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from memecoin_scanner import valid_address
from providers import timestamp

MAX_BYTES = 131072
MAX_TOKENS = 90
MAX_AGE_SECONDS = 300
VIEWS = ('trending', 'graduated', 'most_held')


def create_source(db):
    db.execute('''CREATE TABLE IF NOT EXISTS fomo_source_v1 (
        source TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, captured_at REAL NOT NULL,
        received_at REAL NOT NULL, token_count INTEGER NOT NULL, batches INTEGER NOT NULL
    )''')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def parse_snapshot(raw, now=None, max_age=MAX_AGE_SECONDS):
    """Strict, bounded contract; the caller supplies public rendered token links only."""
    now = time.time() if now is None else now
    if len(raw) > MAX_BYTES:
        raise ValueError('snapshot_too_large')
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid_json') from None
    if (not isinstance(data, dict) or set(data) != {'schema', 'source', 'captured_at', 'tokens'}
            or type(data['schema']) is not int or data['schema'] != 1 or data['source'] != 'fomo_browser'):
        raise ValueError('invalid_schema')
    captured = timestamp(data['captured_at'])
    if captured is None or captured > now + 30:
        raise ValueError('invalid_capture_time')
    if max_age is not None and now - captured > max_age:
        raise ValueError('stale_capture')
    tokens = data['tokens']
    if not isinstance(tokens, list) or not 1 <= len(tokens) <= MAX_TOKENS:
        raise ValueError('invalid_token_count')
    seen, counts, normalized = set(), {v: 0 for v in VIEWS}, []
    for token in tokens:
        if (not isinstance(token, dict) or set(token) != {'mint', 'url', 'view'}
                or token['view'] not in VIEWS or not valid_address('solana', token['mint'])
                or not isinstance(token['url'], str)):
            raise ValueError('invalid_token')
        # No redirects, arbitrary URLs, credential query strings or token aliases.
        expected = 'https://fomo.family/tokens/solana/' + token['mint']
        if token['url'] != expected:
            raise ValueError('invalid_token_url')
        identity = (token['mint'], token['view'])
        if identity not in seen:
            counts[token['view']] += 1
            if counts[token['view']] > 30:
                raise ValueError('view_limit_exceeded')
            normalized.append(token)
            seen.add(identity)
    data = {**data, 'tokens': sorted(normalized, key=lambda t: (t['mint'], t['view']))}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return data, captured, digest


def import_snapshot(path, store, queue, now=None):
    """Idempotent import. Replays never refresh discovery or bypass its cooldowns."""
    now = time.time() if now is None else now
    status = {'enabled':path is not None, 'state':'disabled', 'imported':0,
              'scope':'Discovery only; browser capture time is not first appearance on Fomo.'}
    if path is None:
        return status
    prior = store.db.execute("SELECT * FROM fomo_source_v1 WHERE source='fomo_browser'").fetchone()
    if prior:
        status.update(last_imported_capture_at=prior['captured_at'],
                      last_received_at=prior['received_at'], token_count=prior['token_count'],
                      batches=prior['batches'], age_seconds=max(0,now-prior['captured_at']))
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        data, captured, digest = parse_snapshot(raw, now)
    except FileNotFoundError:
        return {**status, 'state':'awaiting_capture'}
    except OSError:
        return {**status, 'state':'unreadable'}
    except ValueError as exc:
        return {**status, 'state':'stale' if str(exc)=='stale_capture' else 'invalid', 'reason':str(exc)}
    status.update(snapshot_id=digest, captured_at=captured, age_seconds=max(0,now-captured))
    if prior and captured < prior['captured_at']:
        return {**status, 'state':'out_of_order'}
    if prior and digest == prior['snapshot_id']:
        return {**status, 'state':'current'}
    if prior and captured == prior['captured_at']:
        return {**status, 'state':'invalid', 'reason':'conflicting_capture'}
    provenance = {}
    for token in data['tokens']:
        provenance.setdefault(token['mint'], []).append('fomo_' + token['view'])
    known = sum(bool(store.db.execute("SELECT 1 FROM discovery_v8 WHERE chain='solana' AND mint=?", (mint,)).fetchone())
                for mint in provenance)
    # Queue/token writes are individually idempotent. If interrupted before the receipt,
    # retrying uses the same captured_at and cannot make old data look newly observed.
    queue.offer(provenance, captured)
    for mint in provenance:
        store.note_token('solana', mint, captured)
    with store.db:
        store.db.execute('''INSERT INTO fomo_source_v1 VALUES ('fomo_browser',?,?,?,?,1)
            ON CONFLICT(source) DO UPDATE SET snapshot_id=excluded.snapshot_id,
            captured_at=excluded.captured_at, received_at=excluded.received_at,
            token_count=excluded.token_count, batches=fomo_source_v1.batches+1''',
            (digest,captured,now,len(provenance)))
    return {**status, 'state':'imported', 'imported':len(provenance),
            'new_to_queue':len(provenance)-known, 'token_count':len(provenance),
            'last_imported_capture_at':captured, 'last_received_at':now,
            'batches':(prior['batches'] if prior else 0)+1}


def capture_from_snapshots(captures):
    """Convert bounded, rendered discovery panels from the supported browser tool.

    Input items contain view, captured_at and snapshot. No browser/session files are read.
    Only token links between the discovery tabs and its panel separator are considered.
    """
    import re
    if not isinstance(captures,list) or not 1 <= len(captures) <= len(VIEWS):
        raise ValueError('invalid_capture_count')
    entries, dates, views = [], [], set()
    for capture in captures:
        if not isinstance(capture,dict) or set(capture) != {'view','captured_at','snapshot'}:
            raise ValueError('invalid_capture')
        view, snapshot = capture['view'], capture['snapshot']
        at = timestamp(capture['captured_at'])
        if (view not in VIEWS or view in views or at is None or not isinstance(snapshot,str)
                or len(snapshot.encode()) > 1000000):
            raise ValueError('invalid_capture')
        # Both markers must exist: sign-in screens and unrelated pages fail closed.
        if '- button "Bonding"' not in snapshot or '- separator "Cambiar el tamaño del panel de descubrimiento"' not in snapshot:
            raise ValueError('discovery_panel_missing')
        panel = snapshot.split('- button "Bonding"',1)[1].split('- separator "Cambiar el tamaño del panel de descubrimiento"',1)[0]
        mints = list(dict.fromkeys(re.findall(r'^  - /url: /tokens/solana/([A-Za-z0-9]+)$', panel, re.M)))[:30]
        if not mints:
            raise ValueError('no_solana_tokens_in_capture')
        entries.extend({'mint':mint, 'url':'https://fomo.family/tokens/solana/'+mint, 'view':view} for mint in mints)
        dates.append((at,capture['captured_at']))
        views.add(view)
    result = {'schema':1, 'source':'fomo_browser', 'captured_at':min(dates)[1], 'tokens':entries}
    # Bounds/identity checked again at receipt time on the server.
    parse_snapshot(json.dumps(result).encode())
    return result


def main(argv=None):
    import argparse
    from service_health import write_json
    parser = argparse.ArgumentParser(description='Prepare or validate Fomo browser captures; no orders or credentials')
    parser.add_argument('command', choices=('prepare','validate'))
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            if args.output is None:
                parser.error('prepare necesita --output')
            with args.input.open('rb') as stream:
                raw = stream.read(3000001)
            if len(raw) > 3000000:
                raise ValueError('capture_too_large')
            data = capture_from_snapshots(json.loads(raw))
            write_json(args.output,data)
        else:
            with args.input.open('rb') as stream:
                data, _, _ = parse_snapshot(stream.read(MAX_BYTES+1))
        print(json.dumps({'valid':True,'captured_at':data['captured_at'],
                          'unique_tokens':len({t['mint'] for t in data['tokens']})}))
        return 0
    except (ValueError, OSError, RecursionError) as exc:
        print(json.dumps({'valid':False,'reason':str(exc) if isinstance(exc,ValueError) else 'file_error'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
