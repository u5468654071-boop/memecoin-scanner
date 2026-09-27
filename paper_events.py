"""Ordered paper-trade events; recorded atomically with the source account.

These events do not place orders. A consumer can compare prospective overlays
without additional quotes or reconstructing decisions from future outcomes.
"""
import json
import math
import sqlite3


def create_events(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS paper_event_journal (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK(kind IN ('open','close')),
            profile TEXT NOT NULL CHECK(profile IN ('conservative','balanced','aggressive')),
            position_id INTEGER NOT NULL,
            mint TEXT NOT NULL,
            happened_at REAL NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(profile,position_id,kind)
        );
        CREATE TABLE IF NOT EXISTS paper_journal_health (
            id INTEGER PRIMARY KEY CHECK(id=1), healthy INTEGER NOT NULL,
            error_type TEXT, happened_at REAL
        );
        INSERT OR IGNORE INTO paper_journal_health VALUES (1,1,NULL,NULL);
    ''')


def record_event(db, kind, profile, position_id, mint, happened_at, payload):
    # The caller owns the transaction. Never commit independently of the trade.
    if not db.in_transaction:
        raise RuntimeError('El evento necesita la transacción de la operación ficticia')
    if (kind not in ('open', 'close') or profile not in ('conservative', 'balanced', 'aggressive')
            or type(position_id) is not int or position_id < 1
            or not isinstance(mint, str) or not mint
            or isinstance(happened_at, bool) or not isinstance(happened_at, (int, float))
            or not math.isfinite(happened_at) or happened_at < 0 or not isinstance(payload, dict)):
        raise ValueError('Evento de operación ficticia inválido')
    db.execute('''INSERT INTO paper_event_journal
        (kind,profile,position_id,mint,happened_at,payload) VALUES (?,?,?,?,?,?)''',
        (kind, profile, position_id, mint, happened_at,
         json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)))


def record_event_safely(db, kind, profile, position_id, mint, happened_at, payload):
    """Keep the control running, but permanently flag any missing study event.

    A savepoint isolates only the event insert. The gap flag is committed with
    the source trade, so consumers cannot silently evaluate incomplete history.
    If the database cannot even persist that flag, the outer trade rolls back.
    """
    if not db.in_transaction:
        raise RuntimeError('El evento necesita la transacción de la operación ficticia')
    db.execute('SAVEPOINT paper_journal_event')
    try:
        record_event(db, kind, profile, position_id, mint, happened_at, payload)
    except (sqlite3.Error, ValueError, TypeError, RuntimeError) as exc:
        db.execute('ROLLBACK TO paper_journal_event')
        db.execute('RELEASE paper_journal_event')
        db.execute('UPDATE paper_journal_health SET healthy=0,error_type=?,happened_at=? WHERE id=1',
                   (type(exc).__name__, happened_at))
        return False
    db.execute('RELEASE paper_journal_event')
    return True
