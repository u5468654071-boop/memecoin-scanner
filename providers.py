"""Adaptadores de solo lectura. No hay endpoints de firma ni envío de operaciones."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from memecoin_scanner import NoRedirect, number, obj, valid_address

USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
RPC_METHODS = {'getAccountInfo', 'getTokenLargestAccounts', 'getMultipleAccounts', 'getTokenSupply'}


def integer(value, minimum=0, maximum=2**64 - 1):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        if isinstance(value, str) and (not value or not value.isascii() or not value.isdigit()):
            return None
        n = int(value)
        return n if minimum <= n <= maximum else None
    except (ValueError, OverflowError):
        return None


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        # Tokens V2 emite fracciones de nanosegundo; Python 3.9 solo acepta 3/6 dígitos.
        value = re.sub(r'\.(\d{1,9})(?=Z$|[+-]\d{2}:\d{2}$)',
                       lambda m:'.'+m[1][:6].ljust(6,'0'),value)
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.timestamp() if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


class Transport:
    def __init__(self, store, daily_limit=2000, rpc_url=None, jupiter_key=None, timeout=10):
        self.store, self.daily_limit = store, daily_limit
        self.rpc_url = rpc_url or os.environ.get('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com')
        self.jupiter_key = jupiter_key if jupiter_key is not None else os.environ.get('JUPITER_API_KEY', '')
        parsed = urlsplit(self.rpc_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('SOLANA_RPC_URL debe ser HTTPS y no contener credenciales de usuario')
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def get(self, url, quiet_404=False):
        return self.request(url, quiet_404=quiet_404)

    def request(self, url, payload=None, quiet_404=False):
        parsed = urlsplit(url)
        rpc = url == self.rpc_url and payload is not None
        if rpc:
            provider = 'solana_rpc'
            if obj(payload).get('method') not in RPC_METHODS:
                raise RuntimeError('Método RPC no permitido')
        elif (parsed.scheme == 'https' and not parsed.username and not parsed.password
              and parsed.port in (None, 443) and payload is None):
            provider = {'api.dexscreener.com': 'dexscreener', 'api.rugcheck.xyz': 'rugcheck',
                        'api.jup.ag': 'jupiter'}.get(parsed.hostname)
            if provider == 'jupiter' and parsed.path not in ('/tokens/v2/search', '/tokens/v2/recent', '/tokens/v2/toporganicscore/5m', '/swap/v2/order'):
                raise RuntimeError('Endpoint Jupiter no permitido')
        else:
            provider = None
        if not provider:
            raise RuntimeError('Endpoint de lectura no permitido')
        if provider == 'jupiter' and not self.jupiter_key:
            raise RuntimeError('Jupiter sin configurar: falta JUPITER_API_KEY')
        headers = {'Accept': 'application/json', 'User-Agent': 'memecoin-scanner/0.5'}
        if provider == 'jupiter':
            headers['x-api-key'] = self.jupiter_key
        raw = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
        if raw is not None:
            headers['Content-Type'] = 'application/json'
        error = 'sin respuesta'
        for attempt in range(3):
            while True:
                wait = self.store.reserve_call(provider, self.daily_limit, min_interval=1.1)
                if wait <= 0:
                    break
                if wait > 30:
                    raise RuntimeError(provider + ': reloj o intervalo pendiente; reintentar más tarde')
                time.sleep(wait)
            delay = 2**attempt
            try:
                with self.opener.open(Request(url, data=raw, headers=headers), timeout=self.timeout) as response:
                    data = response.read(8_000_001)
                if len(data) > 8_000_000:
                    raise RuntimeError(provider + ': respuesta demasiado grande')
                return json.loads(data.decode())
            except HTTPError as exc:
                if exc.code == 404 and quiet_404:
                    return None
                error = 'HTTP ' + str(exc.code)
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    break
                retry = exc.headers.get('Retry-After') if exc.headers else None
                seconds = number(retry, 0)
                if retry and seconds is None:
                    try:
                        seconds = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
                if seconds is not None and seconds > 30:
                    self.store.set_provider_cooldown(provider, time.time() + seconds)
                    break
                delay = max(delay, seconds or 0)
                if exc.code == 429:
                    self.store.set_provider_cooldown(provider, time.time() + delay)
            except (URLError, OSError, ValueError) as exc:
                error = type(exc).__name__  # Nunca serializar URLs de RPC o claves.
            if attempt < 2:
                time.sleep(delay)
        raise RuntimeError(provider + ': ' + error)

    def rpc(self, method, params):
        data = self.request(self.rpc_url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        if not isinstance(data, dict) or data.get('error') or 'result' not in data:
            raise RuntimeError('solana_rpc: respuesta inválida o error JSON-RPC')
        return data['result']


def inspect_mint(response):
    """Interpretación conservadora del jsonParsed de Solana; desconocido nunca es revocado."""
    result = {'status': 'incomplete', 'reasons': [], 'slot': obj(obj(response).get('context')).get('slot')}
    value = obj(obj(response).get('value'))
    parsed = obj(obj(value.get('data')).get('parsed'))
    info = obj(parsed.get('info'))
    program = value.get('owner')
    result['program'] = program
    required = {'mintAuthority', 'freezeAuthority', 'decimals', 'supply', 'isInitialized'}
    if program not in (TOKEN_PROGRAM, TOKEN_2022) or parsed.get('type') != 'mint' or not required <= info.keys():
        result['reasons'].append('mint o programa no interpretado')
        return result
    decimals, supply = integer(info['decimals'], 0, 18), integer(info['supply'], 1)
    result.update(decimals=decimals, supply_raw=str(supply) if supply else None,
                  mint_authority=info['mintAuthority'], freeze_authority=info['freezeAuthority'])
    if decimals is None or supply is None or info['isInitialized'] is not True:
        result['reasons'].append('supply/decimales/estado inválidos')
        return result
    if info['mintAuthority'] is not None:
        result['reasons'].append('autoridad de emisión activa')
    if info['freezeAuthority'] is not None:
        result['reasons'].append('autoridad de congelación activa')
    extensions = info.get('extensions', [])
    if program == TOKEN_2022 and 'extensions' not in info:
        result['reasons'].append('lista de extensiones Token-2022 ausente')
        return result
    if not isinstance(extensions, list):
        result['reasons'].append('extensiones no interpretadas')
        return result
    # Metadatos no alteran transferencias. Cualquier otra extensión exige implementación explícita.
    metadata_only = {'metadataPointer', 'tokenMetadata', 'groupPointer', 'groupMemberPointer',
                     'tokenGroup', 'tokenGroupMember'}
    names = [obj(e).get('extension') for e in extensions]
    result['extensions'] = names
    unknown = [n for n in names if n not in metadata_only]
    if unknown:
        result['reasons'].append('extensiones que requieren revisión: ' + ', '.join(str(n) for n in unknown))
        result['status'] = 'unsupported'
    elif result['reasons']:
        result['status'] = 'blocked'
    elif result['slot'] is None:
        result['reasons'].append('slot RPC ausente')
    else:
        result['status'] = 'ok'
    return result


def pool_evidence(report, row):
    """Asociar el pool exacto al informe; nunca heredar un bloqueo de otro mercado."""
    result = {'status': 'incomplete', 'source': 'rugcheck_full_report', 'independently_verified': False,
              'pool_address': row.get('pair_address'), 'locked_pct': None, 'reserve_accounts': []}
    if not isinstance(report, dict) or report.get('mint') != row['base_address']:
        return result
    markets = report.get('markets')
    if not isinstance(markets, list):
        return result
    for market in markets:
        market = obj(market)
        if market.get('pubkey') != row.get('pair_address'):
            continue
        if not all(isinstance(a, str) for a in (market.get('mintA'), market.get('mintB'), row.get('quote_address'))):
            return result
        if {market['mintA'], market['mintB']} != {row['base_address'], row['quote_address']}:
            return result
        lp = obj(market.get('lp'))
        result.update(market_type=market.get('marketType'), locked_pct=number(lp.get('lpLockedPct'), 0, 100),
                      reserve_accounts=[a for a in (market.get('liquidityA'), market.get('liquidityB'))
                                        if valid_address('solana', a)])
        # El porcentaje es reportado, no un bloqueo probado criptográficamente.
        result['status'] = 'reported' if result['locked_pct'] is not None else 'incomplete'
        return result
    return result


def holder_evidence(rpc, mint, supply_raw, slot, report):
    """Resuelve cuentas de token a propietarios; los porcentajes son límites inferiores."""
    result = {'status': 'incomplete', 'owners': [], 'excluded_accounts': [],
              'coverage': 'muestra de las 20 mayores cuentas, no censo completo', 'reasons': []}
    largest = rpc('getTokenLargestAccounts', [mint, {'commitment': 'confirmed'}])
    accounts = obj(largest).get('value')
    if not isinstance(accounts, list) or not accounts:
        result['reasons'].append('sin muestra de holders')
        return result
    addresses = [obj(a).get('address') for a in accounts]
    if any(not valid_address('solana', a) for a in addresses) or len(set(addresses)) != len(addresses):
        return result
    response = rpc('getMultipleAccounts', [addresses, {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                                                       'minContextSlot': slot}])
    values = obj(response).get('value')
    if not isinstance(values, list) or len(values) != len(addresses):
        return result
    reserves = set()
    for market in obj(report).get('markets') or []:
        market = obj(market)
        for token_key, reserve_key in (('mintA', 'liquidityA'), ('mintB', 'liquidityB')):
            if market.get(token_key) == mint:
                reserves.add(market.get(reserve_key))
    owners = {}
    total = integer(supply_raw, 1)
    if total is None:
        return result
    for address, value in zip(addresses, values):
        value = obj(value)
        parsed = obj(obj(value.get('data')).get('parsed'))
        info = obj(parsed.get('info'))
        amount = integer(obj(info.get('tokenAmount')).get('amount'))
        owner = info.get('owner')
        if (value.get('owner') not in (TOKEN_PROGRAM, TOKEN_2022) or parsed.get('type') != 'account'
                or info.get('mint') != mint or amount is None or not valid_address('solana', owner)):
            result['reasons'].append('cuenta de token sin resolver')
            return result
        if address in reserves:
            result['excluded_accounts'].append(address)
            continue
        owners[owner] = owners.get(owner, 0) + amount
    if sum(owners.values()) > total:
        result['reasons'].append('supply y saldos inconsistentes')
        return result
    result['owners'] = [{'owner': owner, 'amount_raw': str(amount), 'supply_pct_lower_bound': amount / total * 100}
                        for owner, amount in sorted(owners.items(), key=lambda item: item[1], reverse=True)]
    result['top1_pct_lower_bound'] = result['owners'][0]['supply_pct_lower_bound'] if owners else 0
    result['top10_pct_lower_bound'] = sum(o['supply_pct_lower_bound'] for o in result['owners'][:10])
    result['slot'] = obj(response.get('context')).get('slot')
    result['status'] = 'ok' if result['slot'] is not None else 'incomplete'
    return result


def network_evidence(report):
    """Usa grupos que reporta RugCheck, sin atribuir identidades ni sumar grupos solapados."""
    result = {'status': 'unavailable', 'source': 'rugcheck_insider_networks', 'groups': [],
              'max_group_supply_pct': None, 'independently_verified': False, 'reason_codes': []}
    networks = obj(report).get('insiderNetworks')
    supply = integer(obj(obj(report).get('token')).get('supply'), 1)
    if not isinstance(networks, list):
        result['reason_codes'].append('networks_not_reported')
    if supply is None:
        result['reason_codes'].append('supply_not_reported')
    if result['reason_codes']:
        return result
    for network in networks:
        network = obj(network)
        amount = integer(network.get('tokenAmount'))
        size = integer(network.get('size'), 1, 100000000)
        if amount is None or size is None or amount > supply:
            result['status'] = 'incomplete'
            result['reason_codes'].append('malformed_network')
            return result
        result['groups'].append({'id': str(network.get('id', 'unknown')), 'type': str(network.get('type', 'unknown')),
                                 'size': size, 'supply_pct': amount / supply * 100,
                                 'interpretation': 'relación reportada; no prueba mismo dueño ni fraude'})
    result['max_group_supply_pct'] = max((g['supply_pct'] for g in result['groups']), default=0)
    result['status'] = 'reported'
    return result


class Jupiter:
    def __init__(self, transport):
        self.transport = transport
        self.prefetched = {}

    @property
    def enabled(self):
        return bool(self.transport.jupiter_key)

    def discover(self, limit):
        data = self.transport.get('https://api.jup.ag/tokens/v2/recent')
        if not isinstance(data, list):
            raise RuntimeError('Jupiter: lista inválida')
        return [item['id'] for item in data if isinstance(item, dict) and valid_address('solana', item.get('id'))][:limit]

    def discover_organic(self, limit):
        data = self.transport.get('https://api.jup.ag/tokens/v2/toporganicscore/5m?' + urlencode({'limit': limit}))
        if not isinstance(data, list):
            raise RuntimeError('Jupiter: lista de actividad orgánica inválida')
        return list(dict.fromkeys(item['id'] for item in data
                    if isinstance(item, dict) and valid_address('solana', item.get('id'))))[:limit]

    def token(self, mint, now=None, max_age=300):
        now = time.time() if now is None else now
        if not self.enabled:
            return {'status': 'not_configured'}
        cached = self.prefetched.get(mint)
        if cached and 0 <= now - cached[0] <= 30:
            return self.parse_token(cached[1], now, max_age, received_at=cached[0])
        data = self.transport.get('https://api.jup.ag/tokens/v2/search?' + urlencode({'query': mint}))
        match = next((r for r in data if isinstance(r, dict) and r.get('id') == mint), None) if isinstance(data, list) else None
        return self.parse_token(match, now, max_age)

    def prefetch(self, mints, now=None, max_age=300):
        """Un lote de hasta 100 identidades exactas; nunca sustituir mints ausentes."""
        mints = list(dict.fromkeys(mints))
        if not mints or len(mints) > 100 or any(not valid_address('solana', mint) for mint in mints):
            raise ValueError('Lote Jupiter inválido')
        if not self.enabled:
            return {mint: {'status': 'not_configured'} for mint in mints}
        data = self.transport.get('https://api.jup.ag/tokens/v2/search?' + urlencode({'query': ','.join(mints)}))
        if not isinstance(data, list):
            raise RuntimeError('Jupiter: lote inválido')
        received = time.time() if now is None else now
        matches, duplicates = {}, set()
        for item in data:
            if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'] in mints:
                if item['id'] in matches:
                    duplicates.add(item['id'])
                matches[item['id']] = item
        self.prefetched = {mint: (received, matches.get(mint) if mint not in duplicates else None) for mint in mints}
        return {mint: self.parse_token(item, received, max_age) for mint, (_, item) in self.prefetched.items()}

    @staticmethod
    def parse_token(match, now, max_age=300, received_at=None):
        if match is None:
            return {'status': 'unavailable'}
        updated = timestamp(match.get('updatedAt'))
        score = number(match.get('organicScore'), 0, 100)
        result = {'status': 'ok', 'source': 'jupiter', 'organic_score': score, 'updated_at': updated,
                  'received_at': now if received_at is None else received_at, 'decimals': integer(match.get('decimals'), 0, 18),
                  'holder_count': number(match.get('holderCount'), 0), 'windows': {},
                  'first_pool_at': timestamp(obj(match.get('firstPool')).get('createdAt')),
                  'flagged_suspicious': 'isSus' in obj(match.get('audit'))}
        for window in ('5m', '1h', '6h', '24h'):
            stats = obj(match.get('stats' + window))
            result['windows'][window] = {field: number(stats.get(field), 0 if field != 'numNetBuyers' else None)
                                        for field in ('numTraders', 'numOrganicBuyers', 'numNetBuyers',
                                                      'buyOrganicVolume', 'sellOrganicVolume')}
        if updated is None or updated > now + 30 or now - updated > max_age:
            result['status'] = 'stale'
            result['reason_code'] = ('timestamp_missing' if updated is None else
                                     ('timestamp_in_future' if updated > now + 30 else 'timestamp_expired'))
        elif score is None or result['decimals'] is None:
            result['status'] = 'incomplete'
        return result

    def quote(self, input_mint, output_mint, amount):
        if not valid_address('solana', input_mint) or not valid_address('solana', output_mint):
            raise RuntimeError('Jupiter: mint inválido')
        amount = integer(amount, 1)
        if amount is None:
            raise RuntimeError('Jupiter: cantidad inválida')
        data = self.transport.get('https://api.jup.ag/swap/v2/order?' + urlencode(
            {'inputMint': input_mint, 'outputMint': output_mint, 'amount': str(amount)}))
        if (not isinstance(data, dict) or data.get('inputMint') != input_mint or data.get('outputMint') != output_mint
                or integer(data.get('inAmount'), 1) != amount or integer(data.get('outAmount'), 1) is None
                or data.get('errorCode') is not None or data.get('error') or data.get('transaction') not in (None, '')):
            raise RuntimeError('Jupiter: cotización ausente, identidad incorrecta o ruta no utilizable')
        return {'input_mint': input_mint, 'output_mint': output_mint, 'in_amount': str(amount),
                'out_amount': str(integer(data['outAmount'], 1)), 'router': data.get('router'),
                'fee_bps_reported': number(data.get('feeBps'), 0), 'received_at': time.time()}

    def round_trip(self, mint, amount_usdc, max_age=30):
        if not self.enabled:
            return {'status': 'not_configured', 'amount_usdc': amount_usdc}
        try:
            scaled = Decimal(str(amount_usdc)) * 1000000
            if not scaled.is_finite() or scaled != scaled.to_integral_value() or scaled <= 0:
                raise ValueError
            raw = str(int(scaled))
        except (InvalidOperation, ValueError):
            raise RuntimeError('Cantidad USDC inválida')
        buy = self.quote(USDC, mint, raw)
        sell = self.quote(mint, USDC, buy['out_amount'])
        received = time.time()
        if received - buy['received_at'] > max_age:
            return {'status': 'stale', 'amount_usdc': amount_usdc}
        returned = Decimal(sell['out_amount']) / 1000000
        return {'status': 'quoted', 'amount_usdc': float(Decimal(raw) / 1000000),
                'buy': buy, 'sell': sell, 'received_at': received,
                'round_trip_loss_pct': float((1 - returned / Decimal(str(amount_usdc))) * 100),
                'returned_usdc': float(returned),
                'limitations': 'Cotizaciones independientes sin ejecutar; no incluyen necesariamente gas, MEV ni impacto propio.'}


def batch_pairs(transport, chain, mints):
    """Preselección DEX (máximo 30 mints). No sustituye la lectura del pool al analizar."""
    mints = list(dict.fromkeys(mints))
    if not mints or len(mints) > 30 or any(not valid_address(chain, mint) for mint in mints):
        raise ValueError('Lote de pares inválido')
    data = transport.get('https://api.dexscreener.com/tokens/v1/' + chain + '/' + ','.join(mints))
    if not isinstance(data, list):
        raise RuntimeError('DexScreener: lote de pares inválido')
    grouped = {mint: [] for mint in mints}
    for pair in data:
        if not isinstance(pair, dict) or pair.get('chainId') != chain:
            continue
        mint = obj(pair.get('baseToken')).get('address')
        if isinstance(mint, str) and mint in grouped:
            grouped[mint].append(pair)
    return grouped
