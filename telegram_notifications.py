"""Avisos de simulación: destinatario vinculado, cola persistente y red aislada."""
import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from memecoin_scanner import valid_address
from run_lock import ScanLock
from service_health import heartbeat, healthy


class TelegramError(RuntimeError):
    def __init__(self, code=0, retry_after=0):
        self.code = code if type(code) is int else 0
        self.retry_after = retry_after if type(retry_after) is int and retry_after > 0 else 0
        super().__init__('Telegram: error HTTP/API ' + str(self.code) if self.code else 'Telegram: conexión o respuesta inválida')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TelegramClient:
    def __init__(self, token, opener=None):
        if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,100}', token or ''):
            raise ValueError('Falta TELEGRAM_BOT_TOKEN o tiene formato inválido')
        self._token = token
        self.opener = opener or build_opener(NoRedirect())

    def call(self, method, payload=None):
        if method not in ('getMe', 'getWebhookInfo', 'getUpdates', 'sendMessage'):
            raise ValueError('Método de Telegram no permitido')
        request = Request('https://api.telegram.org/bot' + self._token + '/' + method,
                          data=json.dumps(payload or {}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        try:
            try:
                response = self.opener.open(request, timeout=15)
            except HTTPError as exc:
                response = exc
            with response:
                raw = response.read(1000001)
                if len(raw) > 1000000:
                    raise TelegramError()
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise TelegramError()
                if response.code != 200 or result.get('ok') is not True:
                    parameters = result.get('parameters') or {}
                    raise TelegramError(result.get('error_code', response.code),
                                        parameters.get('retry_after', 0) if isinstance(parameters, dict) else 0)
                return result['result']
        except TelegramError:
            raise
        except (OSError, URLError, ValueError, KeyError, TypeError):
            # Nunca incluir el error de urllib: su URL contiene el token.
            raise TelegramError() from None

    def send(self, chat_id, text):
        if type(chat_id) is not int or chat_id <= 0:
            raise ValueError('Se requiere un chat privado vinculado')
        result = self.call('sendMessage', {'chat_id': chat_id, 'text': text,
                            'link_preview_options': {'is_disabled': True}, 'allow_paid_broadcast': False})
        if (not isinstance(result, dict) or type(result.get('message_id')) is not int
                or result.get('chat', {}).get('id') != chat_id):
            raise TelegramError()
        return result['message_id']


def utc(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


class Notifications:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'notifications.sqlite3', timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS binding (
                id INTEGER PRIMARY KEY CHECK(id=1), bot_id INTEGER NOT NULL, username TEXT NOT NULL,
                nonce_hash TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                chat_id INTEGER, paired_at REAL, offset INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, text TEXT NOT NULL,
                created_at REAL NOT NULL, sent_at REAL, message_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS transitions (
                key TEXT PRIMARY KEY, state TEXT NOT NULL, revision INTEGER NOT NULL
            );
        ''')

    def binding(self):
        return self.db.execute('SELECT * FROM binding WHERE id=1').fetchone()

    def pair(self, client, now):
        existing = self.binding()
        if existing and existing['chat_id']:
            raise ValueError('Este servicio ya tiene un chat vinculado; no se cambia el destinatario automáticamente')
        me = client.call('getMe')
        if (not isinstance(me, dict) or me.get('is_bot') is not True or type(me.get('id')) is not int
                or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', me.get('username', ''))):
            raise TelegramError()
        if client.call('getWebhookInfo').get('url'):
            raise ValueError('El bot tiene un webhook activo. Usa un bot dedicado para estos avisos')
        nonce = secrets.token_urlsafe(24)
        with self.db:
            self.db.execute('''INSERT OR REPLACE INTO binding
                (id,bot_id,username,nonce_hash,created_at,expires_at) VALUES (1,?,?,?,?,?)''',
                (me['id'], me['username'], hashlib.sha256(nonce.encode()).hexdigest(), now, now+3600))
        return 'https://t.me/' + me['username'] + '?start=' + nonce

    def queue(self, key, text, now):
        self.db.execute('INSERT OR IGNORE INTO outbox(event_key,text,created_at) VALUES (?,?,?)',
                        (key, text + '\nReferencia: ' + key, now))

    def accept_updates(self, updates, now):
        binding = self.binding()
        if not binding or binding['chat_id'] or now > binding['expires_at']:
            return False
        with self.db:
            for update in updates:
                if not isinstance(update, dict) or type(update.get('update_id')) is not int:
                    continue
                if update['update_id'] < binding['offset']:
                    continue
                self.db.execute('UPDATE binding SET offset=MAX(offset,?) WHERE id=1', (update['update_id']+1,))
                msg = update.get('message') or {}
                chat, sender = msg.get('chat') or {}, msg.get('from') or {}
                text, date = msg.get('text', ''), msg.get('date')
                if (chat.get('type') != 'private' or type(chat.get('id')) is not int or chat['id'] <= 0
                        or sender.get('id') != chat['id'] or sender.get('is_bot') is not False
                        or type(date) is not int or not binding['created_at']-1 <= date <= now+5
                        or not isinstance(text, str) or not text.startswith('/start ')):
                    continue
                digest = hashlib.sha256(text[7:].encode()).hexdigest()
                if not hmac.compare_digest(digest, binding['nonce_hash']):
                    continue
                self.db.execute('UPDATE binding SET chat_id=?,paired_at=?,nonce_hash=NULL WHERE id=1', (chat['id'], now))
                self.queue('connected', 'SIMULACIÓN · Telegram conectado\nRecibirás aperturas, cierres, incidencias de valoración y fallos de los servicios.\nEl bot utiliza dinero ficticio y no ejecuta compras reales.', now)
                return True
        return False

    def transition(self, key, state, text, now):
        previous = self.db.execute('SELECT * FROM transitions WHERE key=?', (key,)).fetchone()
        if previous and previous['state'] == state:
            return
        revision = previous['revision']+1 if previous else 1
        self.db.execute('INSERT OR REPLACE INTO transitions VALUES (?,?,?)', (key, state, revision))
        if text:
            self.queue(key + ':' + str(revision), text, now)

    def collect(self, now=None):
        health_time = now
        now = time.time() if now is None else now
        binding = self.binding()
        if not binding or not binding['chat_id']:
            return
        # Leer la cartera sin crearla ni modificarla; la cola tiene su propia base.
        ledger = self.root / 'scanner.sqlite3'
        positions = []
        portfolio = None
        if ledger.exists():
            source = sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
            source.row_factory = sqlite3.Row
            try:
                if source.execute("SELECT 1 FROM sqlite_master WHERE name='paper_positions'").fetchone():
                    positions = source.execute('''SELECT * FROM paper_positions
                        WHERE state='open' OR opened_at>=? OR closed_at>=? ORDER BY opened_at,id''',
                        (binding['created_at'], binding['created_at'])).fetchall()
                    positions = [dict(p) for p in positions]
                if source.execute("SELECT 1 FROM sqlite_master WHERE name='portfolio_config'").fetchone():
                    config = source.execute('SELECT digest,plan FROM portfolio_config WHERE id=1').fetchone()
                    if config:
                        portfolio = (config['digest'], json.loads(config['plan']))
                        for ident, label in (('conservative', 'Conservador'), ('balanced', 'Equilibrado'), ('aggressive', 'Agresivo')):
                            rows = source.execute(f'''SELECT * FROM paper_{ident}_positions
                                WHERE state='open' OR opened_at>=? OR closed_at>=? ORDER BY opened_at,id''',
                                (binding['created_at'], binding['created_at'])).fetchall()
                            positions.extend({**dict(p), 'profile': ident, 'profile_label': label} for p in rows)
            finally:
                source.close()
        with self.db:
            if portfolio:
                digest, plan = portfolio
                amounts = '\n'.join(p['label'] + ': %.0f USDC virtuales; %.2f por entrada' %
                                      (p['paper']['initial_usdc'], p['paper']['order_usdc']) for p in plan['profiles'])
                self.queue('profiles:' + digest[:12], 'SIMULACIÓN · TRES PERFILES ACTIVOS\n' + amounts
                           + '\nLos resultados se contabilizan por separado. Se aplican límites conjuntos de exposición.', now)
            for pos in positions:
                ident = (pos['profile'] + ':' if pos.get('profile') else '') + str(pos['id'])
                mint = pos['mint'] if valid_address('solana', pos['mint']) else 'dirección inválida'
                label = ('Perfil: ' + pos['profile_label'] + '\n' if pos.get('profile') else '') + 'Token: ' + mint + '\nPosición: ' + ident
                if pos['state'] == 'open' or pos['opened_at'] >= binding['created_at']:
                    self.queue('open:' + ident, 'SIMULACIÓN · APERTURA\n' + label
                        + '\nCoste ficticio: %.2f USDC\nMotivo: filtros de entrada y cotización aceptados.\n' % (pos['cost_micro']/1e6)
                        + utc(pos['opened_at']), now)
                if pos['state'] == 'closed':
                    allowed = ('cierre manual solicitado', 'tiempo máximo', 'candidato invalidado',
                               'stop-loss observado', 'objetivo observado', 'retroceso desde máximo observado')
                    reason = pos['exit_reason'] if pos['exit_reason'] in allowed else 'regla de salida'
                    self.queue('close:' + ident, 'SIMULACIÓN · CIERRE\n' + label
                        + '\nResultado neto ficticio: %+.2f USDC\nMotivo: %s\n' % ((pos['pnl_micro'] or 0)/1e6, reason)
                        + utc(pos['closed_at']), now)
                    continue
                failed = bool(pos['last_error'] or pos['exit_reason'])
                previous = self.db.execute('SELECT state FROM transitions WHERE key=?', ('valuation:'+ident,)).fetchone()
                text = ('SIMULACIÓN · SALIDA/VALORACIÓN PENDIENTE\n' + label
                        + '\nNo hay una cotización de salida utilizable. El saldo sigue comprometido.') if failed else (
                            'SIMULACIÓN · VALORACIÓN RECUPERADA\n' + label if previous and previous['state'] == 'failed' else None)
                self.transition('valuation:'+ident, 'failed' if failed else 'ok', text, now)
            for service in ('scanner', 'paper'):
                # En producción, healthy toma la hora DESPUÉS de leer el archivo.
                # Otra tarea puede publicar un heartbeat durante la lectura del ledger.
                ok = healthy(self.root / (service+'.heartbeat.json'), now=health_time)
                if not ok and now-binding['paired_at'] < 180:
                    continue
                previous = self.db.execute('SELECT state FROM transitions WHERE key=?', ('service:'+service,)).fetchone()
                text = ('SIMULACIÓN · SERVICIO SIN PROGRESO\nServicio: ' + service
                        + '\nRevisar el VPS: detenido o sin progreso reciente.') if not ok else (
                            'SIMULACIÓN · SERVICIO RECUPERADO\nServicio: ' + service if previous and previous['state'] == 'failed' else None)
                self.transition('service:'+service, 'ok' if ok else 'failed', text, now)

    def deliver_one(self, client, now):
        binding = self.binding()
        if not binding or not binding['chat_id']:
            return False
        row = self.db.execute('SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT 1').fetchone()
        if not row or row['next_attempt'] > now:
            return False
        try:
            message_id = client.send(binding['chat_id'], row['text'])
        except TelegramError as exc:
            delay = max(exc.retry_after, min(3600, 5*2**min(row['attempts'], 10)))
            with self.db:
                self.db.execute('UPDATE outbox SET attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?',
                                (now+delay, str(exc), row['id']))
            return False
        with self.db:
            self.db.execute('UPDATE outbox SET sent_at=?,message_id=?,attempts=attempts+1,last_error=NULL WHERE id=?',
                            (now, message_id, row['id']))
        return True

    def status(self):
        binding = self.binding()
        pending = self.db.execute('SELECT last_error FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT 1').fetchone()
        return {'configured': binding is not None, 'paired': bool(binding and binding['chat_id']),
                'pending': self.db.execute('SELECT count(*) FROM outbox WHERE sent_at IS NULL').fetchone()[0],
                'sent': self.db.execute('SELECT count(*) FROM outbox WHERE sent_at IS NOT NULL').fetchone()[0],
                'delivery_error': bool(pending and pending['last_error'])}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Telegram: avisos de simulación, sin órdenes ni control remoto')
    parser.add_argument('command', choices=('pair', 'run', 'status', 'health'))
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('DATA_DIR', 'data')))
    parser.add_argument('--cycles', type=int, default=0)
    args = parser.parse_args(argv)
    if args.cycles < 0:
        parser.error('cycles debe ser >=0')
    path = args.data_dir / 'telegram.heartbeat.json'
    if args.command == 'health':
        return 0 if healthy(path, max_age=120) else 1
    lock = ScanLock(args.data_dir / 'notifications.sqlite3')
    notifications = None
    acquired = False
    try:
        if args.command != 'status':
            lock.acquire()
            acquired = True
        notifications = Notifications(args.data_dir)
        if args.command == 'status':
            print(json.dumps(notifications.status()))
            return 0
        client = TelegramClient(os.environ.get('TELEGRAM_BOT_TOKEN', '').strip())
        if args.command == 'pair':
            print(notifications.pair(client, time.time()))
            return 0
        binding = notifications.binding()
        if not binding:
            raise ValueError('Ejecuta primero el comando pair y abre su enlace privado')
        if client.call('getMe').get('id') != binding['bot_id']:
            raise ValueError('El token corresponde a otro bot; se conserva el destinatario existente')
        cycle = 0
        while True:
            now = time.time()
            binding = notifications.binding()
            wait = 5
            try:
                if not binding['chat_id'] and now <= binding['expires_at']:
                    updates = client.call('getUpdates', {'offset': binding['offset'], 'timeout': 5,
                                                         'allowed_updates': ['message'], 'limit': 100})
                    if not isinstance(updates, list):
                        raise TelegramError()
                    notifications.accept_updates(updates, time.time())
                notifications.collect()
                notifications.deliver_one(client, time.time())
                heartbeat(path, 'paired' if notifications.binding()['chat_id'] else 'awaiting_pair')
            except TelegramError as exc:
                wait = max(5, exc.retry_after)
                print(str(exc), flush=True)
                heartbeat(path, 'connection_error')
            cycle += 1
            if args.cycles and cycle >= args.cycles:
                return 0
            time.sleep(wait)
    except KeyboardInterrupt:
        return 0
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc) if isinstance(exc, (RuntimeError, ValueError)) else 'Error local de notificaciones: '+type(exc).__name__)
        return 2
    finally:
        if notifications:
            notifications.db.close()
        lock.close()
        if acquired and args.command == 'run':
            heartbeat(path, 'stopped')


if __name__ == '__main__':
    def stop(_signal, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    raise SystemExit(main())
