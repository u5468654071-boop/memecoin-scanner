#!/usr/bin/env python3
"""Servicios para VPS. Exclusivamente simulación; sin wallet, firmas ni envíos."""
import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from observation_store import ObservationStore
from paper_trading import PaperLedger, PaperPolicy
from profile_plan import ProfilePlan
from profile_portfolio import ProfilePortfolio, active_plan
from providers import Jupiter, Transport
from run_lock import ScanLock
from service_health import heartbeat, healthy, write_json


def env_int(name, default, lower, upper):
    try:
        value = int(os.environ.get(name, str(default)))
        if not lower <= value <= upper:
            raise ValueError
        return value
    except ValueError:
        raise ValueError(name + ' fuera del intervalo permitido') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Servidor de escaneo y cartera ficticia; no opera dinero real')
    parser.add_argument('command', choices=('scan', 'paper', 'report', 'performance', 'coverage', 'health', 'pause', 'resume', 'close-all', 'backup'))
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('DATA_DIR', 'data')))
    parser.add_argument('--policy', type=Path, default=Path('paper-policy.json'))
    parser.add_argument('--profiles', type=Path, default=os.environ.get('PAPER_PROFILES_FILE'))
    parser.add_argument('--fomo-inbox', type=Path, default=os.environ.get('FOMO_INBOX_FILE'))
    parser.add_argument('--service', choices=('scanner', 'paper'), default='paper')
    parser.add_argument('--cycles', type=int, default=0, help='Solo pruebas acotadas; 0 es continuo')
    parser.add_argument('--backup-to', type=Path)
    parser.add_argument('--since', help='Inicio inclusivo ISO 8601 con zona horaria; solo performance')
    parser.add_argument('--until', help='Fin exclusivo ISO 8601 con zona horaria; solo performance')
    args = parser.parse_args(argv)
    if args.cycles < 0:
        parser.error('cycles debe ser >=0')
    if args.command != 'performance' and (args.since is not None or args.until is not None):
        parser.error('--since y --until solo se usan con performance')
    root = args.data_dir
    db_path = root / 'scanner.sqlite3'
    pause_path, close_path = root / 'PAUSE', root / 'CLOSE_ALL'
    if args.command == 'performance':
        from performance import performance_report
        from providers import timestamp
        until = time.time() if args.until is None else timestamp(args.until)
        since = until - 7 * 86400 if args.since is None and until is not None else timestamp(args.since)
        if since is None or until is None or not 0 <= since < until:
            parser.error('Intervalo inválido: usa fechas ISO 8601 con zona horaria e inicio anterior al fin')
        if not db_path.is_file():
            parser.error('performance necesita una base existente; no crea ni inicializa carteras')
        with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            result = performance_report(SimpleNamespace(db=db), since, until)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    if args.command == 'health':
        return 0 if healthy(root / (args.service + '.heartbeat.json')) else 1
    root.mkdir(parents=True, exist_ok=True)
    if args.command == 'coverage':
        from discovery import DiscoveryQueue, coverage_report
        from forward_study import ForwardStudy
        with ObservationStore(db_path) as store:
            result = {'coverage':coverage_report(store), 'discovery':DiscoveryQueue(store, None).report(),
                      'forward_study':ForwardStudy(store).report()}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    if args.command in ('pause', 'close-all'):
        pause_path.touch(mode=0o600)
        if args.command == 'close-all':
            close_path.touch(mode=0o600)
        print('Entradas ficticias pausadas. Las salidas siguen revisándose.' if args.command == 'pause'
              else 'Cierre ficticio solicitado; las posiciones sin ruta seguirán pendientes. Entradas pausadas.')
        return 0
    if args.command == 'resume':
        if close_path.exists():
            with ObservationStore(db_path) as store:
                stored_plan = active_plan(store)
                ledger = ProfilePortfolio(store, stored_plan) if stored_plan else PaperLedger(store)
                if ledger.open_positions():
                    print('Quedan posiciones pendientes del cierre manual. Consulta report antes de reanudar.', file=sys.stderr)
                    return 2
            close_path.unlink(missing_ok=True)
        pause_path.unlink(missing_ok=True)
        print('Entradas ficticias habilitadas; siguen sujetos a filtros y límites.')
        return 0
    if args.command == 'backup':
        if args.backup_to is None or not db_path.exists():
            parser.error('backup necesita una base existente y --backup-to RUTA_NUEVA')
        destination = args.backup_to
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.touch(mode=0o600, exist_ok=False)
            with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target)
        except FileExistsError:
            parser.error('El destino ya existe; se conserva sin sobrescribir')
        print('Copia SQLite consistente creada: ' + str(destination))
        return 0
    if args.command == 'report':
        with ObservationStore(db_path) as store:
            stored_plan = active_plan(store)
            if stored_plan:
                account = ProfilePortfolio(store, stored_plan).report(paused=pause_path.exists() or close_path.exists())
            else:
                account = PaperLedger(store).report()
                if account.get('initialized'):
                    account = PaperLedger(store, PaperPolicy(**account['policy'])).report(paused=pause_path.exists() or close_path.exists())
            account['services'] = {name: {'recent_progress': healthy(root / (name + '.heartbeat.json'))}
                                   for name in ('scanner', 'paper')}
            account['pause_requested'], account['close_all_requested'] = pause_path.exists(), close_path.exists()
            print(json.dumps(account, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    try:
        plan = ProfilePlan.load(args.profiles) if args.profiles else None
        policy = PaperPolicy.load(args.policy) if plan is None else None
        with ObservationStore(db_path) as store:
            persisted = active_plan(store)
            if persisted and (plan is None or plan.digest != persisted.digest):
                raise ValueError('El servicio necesita el mismo plan de perfiles que las carteras persistidas')
        daily = env_int('DAILY_API_LIMIT', 20000, 100, 1000000)
        reserve = env_int('EXIT_API_RESERVE', 5000, 1, daily-1)
        scan_interval = env_int('SCAN_INTERVAL_SECONDS', 60, 10, 300)
        paper_interval = env_int('PAPER_INTERVAL_SECONDS', 60, 10, 60)
        max_tokens = env_int('SCAN_MAX_TOKENS', 3, 1, 20)
        discovery_limit = env_int('DISCOVERY_LIMIT', 30, 1, 100)
        if not os.environ.get('JUPITER_API_KEY', '').strip():
            raise ValueError('Falta JUPITER_API_KEY en el entorno del servicio')
    except (ValueError, OSError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else 'No se puede leer el archivo de política', file=sys.stderr)
        return 2
    if args.command == 'scan':
        from scanner_v05 import main as scan
        return scan(['--watch', '--with-stream', '--interval', str(scan_interval), '--cycles', str(args.cycles),
                     '--max-tokens', str(max_tokens), '--limit', str(discovery_limit),
                     '--candidates', 'boosted,profiles,jupiter,jupiter-organic,jupiter-market' if plan else 'boosted,profiles,jupiter',
                     '--sizes-usdc', ','.join(map(str, plan.sizes)) if plan else str(policy.order_usdc),
                     '--daily-api-limit', str(daily-reserve), '--db', str(db_path),
                     '--log', str(root / 'scan_log.csv'), '--json-output', str(root / 'scanner-report.json'),
                     '--heartbeat', str(root / 'scanner.heartbeat.json')]
                    + (['--profiles', str(args.profiles)] if plan else [])
                    + (['--fomo-inbox', str(args.fomo_inbox)] if args.fomo_inbox else []))
    lock = ScanLock(str(db_path) + '.paper')
    acquired = False
    try:
        lock.acquire()
        acquired = True
        with ObservationStore(db_path) as store:
            ledger = ProfilePortfolio(store, plan) if plan else PaperLedger(store, policy)
            jupiter = Jupiter(Transport(store, daily))
            cycle = 0
            while True:
                start = time.monotonic()
                heartbeat(root / 'paper.heartbeat.json', 'valuing')
                report = ledger.tick(jupiter, paused=pause_path.exists, close_all=close_path.exists)
                report['scanner_recent_progress'] = healthy(root / 'scanner.heartbeat.json')
                write_json(root / 'paper-report.json', report)
                heartbeat(root / 'paper.heartbeat.json', 'waiting')
                print(json.dumps({'mode': 'paper_only', 'cash_usdc': report['cash_usdc'],
                                  'equity_usdc': report['equity_usdc'], 'open_positions': len(report['open_positions']),
                                  'entry_blockers': report['entry_blockers']}, ensure_ascii=False), flush=True)
                cycle += 1
                if args.cycles and cycle >= args.cycles:
                    return 0
                time.sleep(max(0, paper_interval-(time.monotonic()-start)))
    except KeyboardInterrupt:
        return 0
    except (RuntimeError, ValueError, sqlite3.Error) as exc:
        print(str(exc) if isinstance(exc, (RuntimeError, ValueError)) else 'Error SQLite: ' + type(exc).__name__, file=sys.stderr)
        return 2
    finally:
        lock.close()
        if acquired:
            heartbeat(root / 'paper.heartbeat.json', 'stopped')


def stop_service(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, stop_service)
    sys.exit(main())
