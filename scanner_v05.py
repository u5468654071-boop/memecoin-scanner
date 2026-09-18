#!/usr/bin/env python3
"""CLI de investigación continua y eventos; sin transacciones ni mensajes externos."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import threading
import time
import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import memecoin_scanner as legacy
from engine import Engine, EnhancedPolicy
from observation_store import ObservationStore
from providers import Transport
from run_lock import ScanLock
from version import SCANNER_VERSION


def emit_result(path, result):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def positive_float(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('debe ser un número finito mayor que cero')
    return result


def build_parser():
    parser = argparse.ArgumentParser(description=f'Memecoin Scanner v{SCANNER_VERSION}: fases, seguridad, actividad y salida indicativa')
    parser.add_argument('--version', action='version', version=SCANNER_VERSION)
    parser.add_argument('--chain', choices=('solana', 'base', 'ethereum'), default='solana')
    parser.add_argument('--tokens', help='Direcciones separadas por coma')
    parser.add_argument('--candidates', default='boosted,profiles', help='boosted,profiles,jupiter; vacío para solo pendientes')
    parser.add_argument('--search', action='append', default=[])
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--max-tokens', type=int, default=20)
    parser.add_argument('--top', type=int, default=10)
    parser.add_argument('--sizes-usdc', default='100', help='Tamaños de cotización, p.ej. 50,100,250; máximo tres')
    parser.add_argument('--daily-api-limit', type=int, default=2000, help='Solicitudes por proveedor/día UTC, reintentos incluidos')
    parser.add_argument('--db', type=Path, default=Path('data/scanner.sqlite3'))
    parser.add_argument('--log', type=Path, default=Path('data/scan_log_v05.csv'))
    parser.add_argument('--json-output', type=Path, default=Path('data/latest_v05.json'))
    parser.add_argument('--quality-filter', action='store_true', help='Compatibilidad: el filtro está siempre activo')
    for key, value in asdict(legacy.Policy()).items():
        parser.add_argument('--' + key.replace('_', '-'), type=float, default=value,
                            help='Solo referencia v0.4; v0.5 exige trayectoria temporal' if key == 'min_age_hours' else None)
    for key, value in asdict(EnhancedPolicy()).items():
        parser.add_argument('--' + key.replace('_', '-'), type=type(value), default=value)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--report', action='store_true', help='Informe local sin red')
    mode.add_argument('--evaluate', action='store_true', help='Actualiza resultados vencidos, sin escaneo nuevo')
    mode.add_argument('--alerts', action='store_true', help='Muestra alertas locales y su caducidad, sin red')
    mode.add_argument('--stream', action='store_true', help='Captura creación/migración de PumpPortal; no escanea')
    parser.add_argument('--stream-seconds', type=int, default=60, help='Duración de captura aislada')
    parser.add_argument('--watch', action='store_true', help='Bucle en primer plano: evaluar pendientes y escanear')
    parser.add_argument('--with-stream', action='store_true', help='Añade captura en paralelo durante --watch')
    parser.add_argument('--interval', type=positive_float, default=60)
    parser.add_argument('--cycles', type=int, default=0, help='0 = hasta Ctrl+C cuando se usa --watch')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100 or not 1 <= args.max_tokens <= 500 or args.top < 1 or args.daily_api_limit < 1:
        parser.error('limit 1–100, max-tokens 1–500, top y daily-api-limit positivos')
    if args.cycles < 0 or args.stream_seconds < 1 or args.interval < 10:
        parser.error('cycles >=0, stream-seconds >=1 e interval >=10')
    if args.with_stream and not args.watch:
        parser.error('--with-stream requiere --watch')
    if (args.stream or args.with_stream) and args.chain != 'solana':
        parser.error('PumpPortal solo se admite para Solana')
    if args.watch and (args.stream or args.report or args.evaluate or args.alerts):
        parser.error('--watch no se combina con report/evaluate/alerts/stream; usa --with-stream')
    sources = list(dict.fromkeys(s.strip() for s in args.candidates.split(',') if s.strip()))
    if any(s not in ('boosted', 'profiles', 'jupiter') for s in sources):
        parser.error('fuentes admitidas: boosted,profiles,jupiter')
    policy = legacy.Policy(**{k: getattr(args, k) for k in asdict(legacy.Policy())})
    enhanced = EnhancedPolicy(**{k: getattr(args, k) for k in asdict(EnhancedPolicy())})
    for k, value in {**asdict(policy), **asdict(enhanced)}.items():
        if not math.isfinite(value) or (k != 'min_price_change_h1' and value < 0):
            parser.error('umbrales inválidos')
    if (policy.min_age_hours > policy.max_age_hours or policy.min_liquidity <= 0 or policy.min_lp_locked_pct > 100
            or policy.max_rugcheck_score > 100 or policy.min_price_change_h1 > policy.max_price_change_h1
            or not 0 <= enhanced.min_organic_score <= 100 or enhanced.min_samples < 2
            or not 60 <= enhanced.min_observation_seconds <= 1800 or enhanced.data_max_age_seconds < 60
            or enhanced.min_samples > 31
            or enhanced.exit_stress_bps > 10000
            or any(getattr(enhanced, k) > 100 for k in ('max_owner_pct','max_top10_pct','max_network_pct','max_liquidity_drop_pct'))):
        parser.error('intervalos de política inválidos')
    try:
        sizes = list(dict.fromkeys(positive_float(v.strip()) for v in args.sizes_usdc.split(',')))
        if (not 1 <= len(sizes) <= 3 or any(s > 1000000 for s in sizes)
                or any(Decimal(str(s)) * 1000000 != (Decimal(str(s)) * 1000000).to_integral_value() for s in sizes)):
            raise ValueError
    except (ValueError, argparse.ArgumentTypeError):
        parser.error('sizes-usdc: entre uno y tres tamaños positivos, máximo 1.000.000 USDC y seis decimales')
    manual = None
    if args.tokens is not None:
        manual = list(dict.fromkeys(t.strip() for t in args.tokens.split(',') if t.strip()))
        if not manual or any(not legacy.valid_address(args.chain, t) for t in manual):
            parser.error('dirección de token inválida')
    if args.stream:
        from event_stream import collect
        try:
            count = asyncio.run(collect(args.db, args.stream_seconds))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f'Eventos guardados: {count}. Los huecos de conexión se conservan en --report.')
        return 0 if count else 2
    stop, worker = threading.Event(), None
    original_client = legacy.CLIENT
    scan_lock = None
    try:
        with ObservationStore(args.db) as store:
            if args.report:
                print(json.dumps({'market': store.report(), 'enhanced': store.report_enhanced()}, ensure_ascii=False, indent=2))
                return 0
            if args.alerts:
                print(json.dumps(store.alerts(), ensure_ascii=False, indent=2))
                return 0
            scan_lock = ScanLock(args.db)
            try:
                scan_lock.acquire()
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            try:
                transport = Transport(store, args.daily_api_limit)
            except ValueError as exc:
                parser.error(str(exc))
            legacy.CLIENT = transport
            engine = Engine(store, transport, policy, enhanced, sizes)
            if args.evaluate:
                store.evaluate_due(transport.get)
                store.evaluate_exits(engine.jupiter)
                print(json.dumps(store.report_enhanced(), ensure_ascii=False, indent=2))
                return 0
            if not engine.jupiter.enabled:
                print('Jupiter sin configurar: se recopilará evidencia; no habrá candidatos validados.', file=sys.stderr)
            if args.with_stream:
                try:
                    import websockets  # noqa: F401
                except ImportError:
                    parser.error('instala requirements-stream.txt para --with-stream')
                from event_stream import collect
                def capture():
                    try:
                        asyncio.run(collect(args.db, duration=0, stop_event=stop))
                    except Exception as exc:
                        print('Stream detenido: ' + type(exc).__name__, file=sys.stderr)
                worker = threading.Thread(target=capture, name='pumpportal-read-only', daemon=True)
                worker.start()
            cycle, code = 0, 0
            while True:
                start = time.monotonic()
                # El seguimiento se atiende antes del siguiente lote de análisis.
                store.evaluate_due(transport.get)
                store.evaluate_exits(engine.jupiter)
                errors = []
                if manual is not None:
                    provenance = {mint: ['manual'] for mint in manual}
                else:
                    found, errors = legacy.gather_candidates(args.chain, [s for s in sources if s != 'jupiter'], args.limit, args.search)
                    if 'jupiter' in sources and args.chain == 'solana':
                        try:
                            for mint in engine.jupiter.discover(args.limit):
                                found.setdefault(mint, []).append('jupiter_recent')
                        except RuntimeError as exc:
                            errors.append(str(exc))
                    for mint in found:
                        store.note_token(args.chain, mint)
                    # Reservar al menos la mitad de cada lote a la cola más antigua.
                    pending = store.due_tokens(args.chain, args.max_tokens, interval=args.interval)
                    provenance = {}
                    queues = [(mint, ['watchlist']) for mint in pending]
                    fresh = [(mint, source) for mint, source in found.items()
                             if store.is_due(args.chain, mint, interval=args.interval)]
                    for i in range(max(len(queues), len(fresh))):
                        for source in (queues, fresh):
                            if i < len(source):
                                mint, origin = source[i]
                                provenance.setdefault(mint, [])
                                provenance[mint] = list(dict.fromkeys(provenance[mint] + origin))
                run_id, rows = uuid.uuid4().hex, []
                for mint, origin in list(provenance.items())[:args.max_tokens]:
                    print('Analizando ' + mint, file=sys.stderr)
                    row = engine.analyze(args.chain, mint, origin)
                    store.record_enriched(row, run_id)
                    rows.append(row)
                    # Evitar que un escaneo largo abandone todos los plazos de evaluación.
                    store.evaluate_due(transport.get)
                    store.evaluate_exits(engine.jupiter)
                rows.sort(key=lambda r: (not r['quality_pass'], -(r['research_score'] or 0), r['base_address']))
                legacy.log_results(rows, args.log)
                result = {'scanner_version': SCANNER_VERSION, 'run_id': run_id, 'source_errors': errors,
                          'rows': rows, 'alerts': store.alerts(), 'configuration': {
                              'jupiter_configured': engine.jupiter.enabled, 'sizes_usdc': sizes,
                              'daily_api_limit_per_provider': args.daily_api_limit}}
                emit_result(args.json_output, result)
                candidates = [r for r in rows if r['quality_pass']]
                print(f'\n{len(candidates)}/{len(rows)} candidatos. Prioridad de investigación, no probabilidad de beneficio.')
                for row in rows[:args.top]:
                    symbol = legacy.terminal_text(row.get('base_symbol') or row['base_address'])
                    reasons = '; '.join(row['decision_reasons'][:3]) or 'comprobaciones actuales superadas'
                    priority = f"{row['research_score']:.1f}" if row['research_score'] is not None else '—'
                    print(f"{row['state']:18} {symbol[:20]:20} prioridad {priority:>5} | {row['lifecycle']['phase']:18} {reasons}")
                print(f'JSON: {args.json_output} | Historial: {args.db}')
                code = 0 if rows and any(r['analysis_status'] == 'ok' for r in rows) else 2
                cycle += 1
                if not args.watch or (args.cycles and cycle >= args.cycles):
                    break
                time.sleep(max(0, args.interval - (time.monotonic() - start)))
            return code
    except KeyboardInterrupt:
        print('\nDetenido; historial y eventos conservados.')
        return 0
    finally:
        stop.set()
        if worker:
            worker.join(timeout=15)
        legacy.CLIENT = original_client
        if scan_lock:
            scan_lock.close()


if __name__ == '__main__':
    sys.exit(main())
