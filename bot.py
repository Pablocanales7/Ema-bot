# EMA Bot v10.3 + Momentum Direccional + Bugfix
import requests
import json
import os
import hmac
import hashlib
import time
from datetime import datetime, timezone, timedelta
import signal
import sys

# ──────────────────────────────────────────────────────────
# FEES BINANCE FUTURES TAKER (market orders) Binance VIP0
# ──────────────────────────────────────────────────────────
FEE_RATE = 0.0005
FEE_ROUNDTRIP = 0.0010
FEE_BUFFER_PCT = 0.10

cycle_count = 0
REPORT_EVERY_N_CYCLES = 10

running = True

def signal_handler(sig, frame):
    global running
    print('\\n[SIGNAL] Recibida señal de terminación. Cerrando...')
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

CHECK_INTERVAL_WITH_POSITIONS = 60
CHECK_INTERVAL_NO_POSITIONS = 600
CHECK_INTERVAL_LOCKED = int(os.environ.get('CHECK_INTERVAL_LOCKED', '15'))

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT = os.environ['TELEGRAM_CHAT_ID']
API_KEY = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '')
AUTO_TRADE = os.environ.get('AUTO_TRADE', 'false').lower() == 'true'
TRADE_AMOUNT = float(os.environ.get('TRADE_AMOUNT', '10'))
TRADE_PCT = float(os.environ.get('TRADE_PCT', '0'))
LEVERAGE = int(os.environ.get('LEVERAGE', '10'))
MAX_TRADES_DAY = int(os.environ.get('MAX_DAILY_TRADES', '20'))
MAX_OPEN_POS = int(os.environ.get('MAX_OPEN_POSITIONS', '4'))
MAX_ALT_POS = int(os.environ.get('MAX_ALT_POSITIONS', '3'))
RSI_MIN = float(os.environ.get('RSI_ENTRY_MIN', '52'))
RSI_MAX = float(os.environ.get('RSI_ENTRY_MAX', '48'))
TRAIL_MULT = float(os.environ.get('TRAIL_ATR_MULT', '0.6'))
ADX_MIN = float(os.environ.get('ADX_MIN', '18'))
DAILY_LOSS_LIMIT = float(os.environ.get('DAILY_LOSS_LIMIT', '-15'))
SL_COOLDOWN_HOURS = int(os.environ.get('SL_COOLDOWN_HOURS', '1'))
VOL_MULT = float(os.environ.get('VOL_MULT', '1.2'))
USE_MTF = os.environ.get('USE_MTF', 'false').lower() == 'true'
USE_VOLUME_FILTER = os.environ.get('USE_VOLUME_FILTER', 'false').lower() == 'true'
USE_BTC_FILTER = os.environ.get('USE_BTC_FILTER', 'false').lower() == 'true'
RECV_WINDOW = int(os.environ.get('RECV_WINDOW', '10000'))
MAX_CANDLES_LATE = int(os.environ.get('MAX_CANDLES_LATE', '2'))
USE_MOMENTUM = os.environ.get('USE_MOMENTUM_DETECTION', 'true').lower() == 'true'
MOMENTUM_THRESHOLD = float(os.environ.get('MOMENTUM_THRESHOLD', '0.009'))
MOMENTUM_ADX_MIN = float(os.environ.get('MOMENTUM_ADX_MIN', '16.5'))
TIMEFRAME_HOURS = int(os.environ.get('TIMEFRAME_HOURS', '1'))
MIN_MACD_STRENGTH = float(os.environ.get('MIN_MACD_STRENGTH', '15'))
MACD_WEAKENING_THRESHOLD = float(os.environ.get('MACD_WEAKENING_THRESHOLD', '0.35'))
MIN_PROFIT_MACD_EXIT = 0.3
LOCK_START_USD = float(os.environ.get('LOCK_START_USD', '0.45'))   # desde qué PnL empezar a asegurar
LOCK_RATIO = float(os.environ.get('LOCK_RATIO', '0.75'))          # % del máximo PnL a asegurar (0.8 = 80%)
PROFIT_TARGET_USD = float(os.environ.get('PROFIT_TARGET_USD', '10'))

PAIRS = [
    {'symbol': 'BTCUSDT',  'fsym': 'BTC',  'dec': 3},
    {'symbol': 'ETHUSDT',  'fsym': 'ETH',  'dec': 3},
    {'symbol': 'SOLUSDT',  'fsym': 'SOL',  'dec': 1},
    {'symbol': 'XRPUSDT',  'fsym': 'XRP',  'dec': 1},
    {'symbol': 'DOGEUSDT', 'fsym': 'DOGE', 'dec': 0},
    {'symbol': 'BNBUSDT',  'fsym': 'BNB',  'dec': 2},
]

STATE_FILE = 'state.json'
EMPTY_STATE = {
    'last_signal': None,
    'position': 'FLAT',
    'entry_price': None,
    'entry_qty': None,
    'initial_sl': None,
    'tp_target': None,
    'trailing_sl': None,
    'partial_closed': False,
    'trades_today': 0,
    'trades_date': None,
    'session_pnl': 0.0,
    'last_sl_time': None,
    'trade_amount_used': None,
    'signal': None,
    'price': None,
    'rsi': None,
    'ema21': None,
    'ema89': None,
    'atr': None,
    'adx': None,
    'velas_cruce': None,
    'max_pnl_usd': 0.0,          # NUEVO: mejor PnL visto en esta posición
}

SIGNAL_LABEL = {
    'BUY': 'SENAL LONG',
    'SELL': 'SENAL SHORT',
    'LONG_ACTIVE': 'LARGO ACTIVO',
    'SHORT_ACTIVE': 'CORTO ACTIVO',
    'WAIT_RSI': 'ESPERAR RSI LONG',
    'WAIT_RSI_SHORT': 'ESPERAR RSI SHORT',
    'CLOSE': 'CERRAR LONG',
    'WAIT': 'SIN SENAL',
}

# ── State dinámico ─────────────────────────────────────────────────────────────
_state_cache = {}
_balance_cache = None

def load_state():
    global _state_cache
    if _state_cache:
        return _state_cache
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                _state_cache = json.load(f)
            print(f'[State] Cargado desde disco ({STATE_FILE})')
        except Exception as e:
            print(f'[State] ⚠️ Error al leer: {e} — iniciando vacío')
            _state_cache = {}
    else:
        _state_cache = {}
    return _state_cache

def _build_config():
    return {
        'leverage': LEVERAGE, 'trade_amount': TRADE_AMOUNT, 'trade_pct': TRADE_PCT,
        'rsi_min': RSI_MIN, 'rsi_max': RSI_MAX, 'trail_mult': TRAIL_MULT,
        'adx_min': ADX_MIN, 'daily_loss_limit': DAILY_LOSS_LIMIT,
        'sl_cooldown_hours': SL_COOLDOWN_HOURS, 'vol_mult': VOL_MULT,
        'max_trades': MAX_TRADES_DAY, 'max_open_pos': MAX_OPEN_POS,
        'max_alt_pos': MAX_ALT_POS, 'use_mtf': USE_MTF,
        'use_btc_filter': USE_BTC_FILTER, 'timeframe_hours': TIMEFRAME_HOURS,
        'use_momentum': USE_MOMENTUM, 'momentum_threshold': MOMENTUM_THRESHOLD,
        'momentum_adx_min': MOMENTUM_ADX_MIN,
        'lock_start_usd': LOCK_START_USD,
        'lock_ratio': LOCK_RATIO,
    }

def _update_live_pnl(state):
    for p in PAIRS:
        ps = state.get(p['symbol'], {})
        pos = ps.get('position', 'FLAT')
        entry = ps.get('entry_price')

        unrealized = ps.get('unrealized_pnl_binance')
        if pos in ('LONG', 'SHORT') and unrealized is not None:
            ps['live_pnl_pct'] = 0.0
            ps['live_pnl_usd'] = round(float(unrealized), 2)
        else:
            price = ps.get('price')
            if pos in ('LONG', 'SHORT') and entry and price:
                pct = (price - entry) / entry * 100 if pos == 'LONG' else (entry - price) / entry * 100
                ta = ps.get('trade_amount_used') or TRADE_AMOUNT
                ps['live_pnl_pct'] = round(pct, 3)
                ps['live_pnl_usd'] = round(ta * LEVERAGE * pct / 100, 2)
            else:
                ps['live_pnl_pct'] = 0.0
                ps['live_pnl_usd'] = 0.0

def save_state(state, now_str, balance=None, reason=''):
    global _state_cache, _balance_cache
    _state_cache = state
    if isinstance(balance, dict):
        _balance_cache = balance
    state['last_run'] = now_str
    state['auto_trade'] = AUTO_TRADE
    if isinstance(_balance_cache, dict):
        state['balance'] = _balance_cache
    state['config'] = _build_config()
    _update_live_pnl(state)
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
        if reason:
            print(f'[State] 💾 {reason}')
    except Exception as e:
        print(f'[State] ⚠️ Error al guardar: {e}')

def save_state_now(state, now_str, reason):
    print(f'[State] 🔴 Crítico: {reason}')
    save_state(state, now_str, reason=reason)

def get_pair_state(state, symbol):
    if symbol not in state:
        state[symbol] = dict(EMPTY_STATE)
    return state[symbol]

def count_open_positions(state):
    return sum(1 for p in PAIRS if state.get(p['symbol'], {}).get('position') in ('LONG', 'SHORT'))

def count_alt_positions(state):
    return sum(1 for p in PAIRS if p['fsym'] != 'BTC' and state.get(p['symbol'], {}).get('position') in ('LONG', 'SHORT'))

def has_locked_positions(state):
    for p in PAIRS:
        ps = state.get(p['symbol'], {})
        if ps.get('position') in ('LONG', 'SHORT'):
            if (ps.get('max_pnl_usd', 0.0) or 0.0) >= LOCK_START_USD:
                return True
    return False
# ── Binance API ────────────────────────────────────────────────────────────────

def _sign(params):
    return hmac.new(API_SECRET.encode(), params.encode(), hashlib.sha256).hexdigest()

def get_futures_balance():
    if not API_KEY:
        return None
    try:
        ts = int(time.time() * 1000)
        params = f'timestamp={ts}&recvWindow={RECV_WINDOW}'
        sig = _sign(params)
        r = requests.get(
            f'https://fapi.binance.com/fapi/v2/account?{params}&signature={sig}',
            headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
        r.raise_for_status()
        acc = r.json()
        result = {
            'total': round(float(acc.get('totalWalletBalance', 0)), 2),
            'available': round(float(acc.get('availableBalance', 0)), 2),
            'margin': round(float(acc.get('totalInitialMargin', 0)), 2),
            'unrealized_pnl': round(float(acc.get('totalUnrealizedProfit', 0)), 2),
            'margin_pct': 0.0,
        }
        if result['total'] > 0:
            result['margin_pct'] = round(result['margin'] / result['total'] * 100, 1)
        print(f" [Balance] Total: ${result['total']} | Libre: ${result['available']} | Margen: {result['margin_pct']}%")
        return result
    except Exception as exc:
        print(f' [Balance] Error: {exc}')
        return None

def sync_positions_with_binance(state):
    if not API_KEY:
        print("[SYNC] Sin API")
        return state
    try:
        ts = int(time.time() * 1000)
        params = f'timestamp={ts}&recvWindow={RECV_WINDOW}'
        sig = _sign(params)
        r = requests.get(
            f'https://fapi.binance.com/fapi/v2/positionRisk?{params}&signature={sig}',
            headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
        positions = r.json()
        print("[SYNC] Binance positions...")
        for p in positions:
            sym = p['symbol']
            if sym.endswith('USDT') and any(pair['symbol'] == sym for pair in PAIRS):
                size = float(p['positionAmt'])
                if abs(size) > 0.001:
                    side = 'LONG' if size > 0 else 'SHORT'
                    if sym not in state:
                        state[sym] = {**EMPTY_STATE}
                    ps = state[sym]
                    if ps.get('position') != side:
                        print(f"[SYNC] {side} {sym} qty={size}")
                        ps.update({
                            'position': side,
                            'entry_price': float(p['entryPrice']),
                            'entry_qty': abs(size),
                            'unrealized_pnl_binance': float(p.get('unRealizedProfit', 0)),
    })
                else:
                    if sym in state and state[sym].get('position') in ('LONG', 'SHORT'):
                        print(f"[SYNC] Closed {sym}")
                        state[sym].update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None})
        print("[SYNC] OK")
    except Exception as e:
        print(f"[SYNC] {e}")
    return state

def resolve_trade_amount(balance):
    bal_num = balance['available'] if isinstance(balance, dict) else balance
    if TRADE_PCT > 0 and bal_num is not None:
        ta = round(bal_num * TRADE_PCT, 2)
        print(f' [Sizing] Dinámico: ${ta}')
        return ta
    return TRADE_AMOUNT

def set_leverage_binance(symbol):
    if not API_KEY:
        return
    try:
        ts = int(time.time() * 1000)
        p = f'symbol={symbol}&leverage={LEVERAGE}&timestamp={ts}&recvWindow={RECV_WINDOW}'
        r = requests.post(
            f'https://fapi.binance.com/fapi/v1/leverage?{p}&signature={_sign(p)}',
            headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
        data = r.json()
        if 'code' in data and data['code'] < 0:
            print(f' [Leverage] Error {symbol}: {data}')
        else:
            print(f' [Leverage] {symbol} → {LEVERAGE}x OK')
    except Exception as exc:
        print(f' [Leverage] Excepción {symbol}: {exc}')

def market_order(symbol, side, qty):
    if not API_KEY:
        return {}
    try:
        ts = int(time.time() * 1000)
        p = f'symbol={symbol}&side={side}&type=MARKET&quantity={qty}&timestamp={ts}&recvWindow={RECV_WINDOW}'
        r = requests.post(
            f'https://fapi.binance.com/fapi/v1/order?{p}&signature={_sign(p)}',
            headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
        data = r.json()
        print(f' [Order DEBUG] Response: {data}')
        if 'code' in data and data['code'] < 0:
            print(f' [Order] Error {symbol} {side}: {data}')
        return data
    except Exception as exc:
        print(f' [Order] Excepción {symbol}: {exc}')
        return {}

# ── Datos de mercado ───────────────────────────────────────────────────────────

def fetchcandles(symbol, aggregate=1, limit=200):
    url = f"https://min-api.cryptocompare.com/data/v2/histohour?fsym={symbol}&tsym=USDT&limit={limit}&aggregate={aggregate}&e=Binance"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if 'Data' not in data or 'Data' not in data['Data']:
            print(f"⚠️ [{symbol}] API sin datos válidos. Reintentando en próximo ciclo.")
            return [], [], [], []
        raw = data['Data']['Data']
        if len(raw) < 50:
            print(f"⚠️ [{symbol}] Datos insuficientes ({len(raw)} velas)")
            return [], [], [], []
        return (
            [float(c['close']) for c in raw],
            [float(c['high']) for c in raw],
            [float(c['low']) for c in raw],
            [float(c['volumeto']) for c in raw]
        )
    except Exception as e:
        print(f"❌ [{symbol}] Error fetchcandles: {e}")
        return [], [], [], []

def fetchdailycandles(symbol, limit=100):
    url = f"https://min-api.cryptocompare.com/data/v2/histoday?fsym={symbol}&tsym=USDT&limit={limit}&e=Binance"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if 'Data' not in data or 'Data' not in data['Data']:
            print(f"⚠️ [{symbol}] API diaria sin datos válidos")
            return []
        raw = data['Data']['Data']
        return [float(c['close']) for c in raw]
    except Exception as e:
        print(f"❌ [{symbol}] Error fetchdailycandles: {e}")
        return []

# ── Indicadores ────────────────────────────────────────────────────────────────

def calc_ema(data, period):
    k = 2 / (period + 1)
    result = [None] * (period - 1)
    result.append(sum(data[:period]) / period)
    for i in range(period, len(data)):
        result.append(data[i] * k + result[-1] * (1 - k))
    return result

def calc_rsi(data, period=14):
    result = [None] * period
    avg_gain = avg_loss = 0.0
    for i in range(1, period + 1):
        diff = data[i] - data[i - 1]
        if diff > 0:
            avg_gain += diff
        else:
            avg_loss += abs(diff)
    avg_gain /= period
    avg_loss /= period
    result.append(100 - 100 / (1 + avg_gain / (avg_loss or 1e-9)))
    for i in range(period + 1, len(data)):
        diff = data[i] - data[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period
        result.append(100 - 100 / (1 + avg_gain / (avg_loss or 1e-9)))
    return result

def calc_atr(highs, lows, closes, period=14):
    tr = [None]
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    avg = sum(tr[1:period+1]) / period
    result = [None] * period
    result.append(avg)
    for i in range(period + 1, len(tr)):
        avg = (avg * (period - 1) + tr[i]) / period
        result.append(avg)
    return result

def calc_adx(highs, lows, closes, period=14):
    n = len(closes)
    tr, pdm, ndm = [], [], []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = highs[i] - highs[i-1], lows[i-1] - lows[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn >= up and dn > 0 else 0.0)
    if len(tr) < period * 2:
        return [None] * n
    def wilder(arr):
        res = [sum(arr[:period]) / period]
        for v in arr[period:]:
            res.append(res[-1] * (period - 1) / period + v / period)
        return res

    satr, spdm, sndm = wilder(tr), wilder(pdm), wilder(ndm)
    dx = []
    for a, p_, nd in zip(satr, spdm, sndm):
        pdi = 100 * p_ / (a or 1e-9)
        ndi = 100 * nd / (a or 1e-9)
        dx.append(100 * abs(pdi - ndi) / ((pdi + ndi) or 1e-9))
    adx_vals = [sum(dx[:period]) / period]
    for v in dx[period:]:
        adx_vals.append(adx_vals[-1] * (period - 1) / period + v / period)
    pad = n - len(adx_vals)
    return [None] * pad + adx_vals

def compute_macd(closes, fast=12, slow=26, signal=9):
    closes = list(closes)
    if len(closes) < slow + signal:
        return None, None, None
    def ema_calc(data, period):
        k = 2 / (period + 1)
        ema = [sum(data[:period]) / period]
        for price in data[period:]:
            ema.append(price * k + ema[-1] * (1 - k))
        return ema
    ema_fast = ema_calc(closes, fast)
    ema_slow = ema_calc(closes, slow)
    min_len = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[i + (len(ema_fast) - min_len)] - ema_slow[i + (len(ema_slow) - min_len)] for i in range(min_len)]
    signal_line = ema_calc(macd_line, signal)
    min_len2 = min(len(macd_line), len(signal_line))
    histogram = [macd_line[i + (len(macd_line) - min_len2)] - signal_line[i] for i in range(min_len2)]
    return macd_line[-1], signal_line[-1], histogram[-1]

def macd_exit_signal(closes, position):
    macd, sig, hist = compute_macd(closes)
    if macd is None:
        return False
    if position == 'LONG':
        return macd < sig and hist < 0
    if position == 'SHORT':
        return macd > sig and hist > 0
    return False

def macd_weakening(closes, position):
    if len(closes) < 40:
        return False, 0, 0
    _, _, hist_now = compute_macd(closes)
    _, _, hist_prev = compute_macd(closes[:-1])
    if None in (hist_now, hist_prev) or hist_prev == 0:
        return False, 0, 0
    if position == 'LONG' and hist_now > 0 and hist_prev > 0:
        ratio = hist_now / hist_prev
        return ratio <= MACD_WEAKENING_THRESHOLD, round(hist_now, 2), round(hist_prev, 2)
    if position == 'SHORT' and hist_now < 0 and hist_prev < 0:
        ratio = hist_now / hist_prev
        return ratio <= MACD_WEAKENING_THRESHOLD, round(hist_now, 2), round(hist_prev, 2)
    return False, 0, 0

# ── Señales ────────────────────────────────────────────────────────────────────

def get_signal(ema21, ema89, rsi_vals, rsi_min, rsi_max):
    if len(ema21) < 2:
        return 'WAIT'
    e21, e89, p21, p89, rsi = ema21[-1], ema89[-1], ema21[-2], ema89[-2], rsi_vals[-1]
    if None in (e21, e89, p21, p89, rsi):
        return 'WAIT'
    bull, was = e21 > e89, p21 > p89
    if not was and bull:
        return 'BUY' if rsi > rsi_min else 'WAIT_RSI'
    if was and not bull:
        return 'SELL' if rsi < rsi_max else 'WAIT_RSI_SHORT'
    if bull:
        return 'LONG_ACTIVE' if rsi > rsi_min else 'WAIT_RSI'
    return 'SHORT_ACTIVE' if rsi < rsi_max else 'WAIT_RSI_SHORT'

def velas_desde_cruce(ema21, ema89, max_look=10):
    for i in range(1, min(max_look + 1, len(ema21) - 1)):
        a21, a89, b21, b89 = ema21[-i], ema89[-i], ema21[-i-1], ema89[-i-1]
        if None in (a21, a89, b21, b89):
            continue
        if (b21 <= b89 and a21 > a89) or (b21 >= b89 and a21 < a89):
            return i - 1
    return max_look + 1

def btc_is_bullish():
    try:
        closes, highs, lows, vols = fetchcandles('BTC')
        if not closes or len(closes) < 100:
            print(" [BTC] Sin datos, asumiendo alcista")
            return True
        e21 = calc_ema(closes, 21)
        e89 = calc_ema(closes, 89)
        bull = (e21[-1] or 0) > (e89[-1] or 0)
        print(f" [BTC] {'ALCISTA' if bull else 'BAJISTA'}")
        return bull
    except Exception as exc:
        print(f" [BTC] Error: {exc}")
        return True

def pair_daily_is_bullish(fsym):
    try:
        closes = fetchdailycandles(fsym)
        if not closes or len(closes) < 100:
            print(f" [1D {fsym}] Sin datos")
            return True
        e21 = calc_ema(closes, 21)
        e89 = calc_ema(closes, 89)
        bull = (e21[-1] or 0) > (e89[-1] or 0)
        print(f" [1D {fsym}] {'ALCISTA' if bull else 'BAJISTA'}")
        return bull
    except Exception as exc:
        print(f" [1D {fsym}] Error: {exc}")
        return True

def volume_confirmed(vols):
    avg = sum(vols[:-1]) / max(len(vols) - 1, 1)
    last = vols[-1]
    ok = last >= avg * VOL_MULT
    ratio = round(last / (avg or 1e-9), 2)
    print(f' [Vol] {ratio}x promedio — {"✅" if ok else "⚠️ bajo"}')
    return ok, ratio

def daily_loss_exceeded(state, today):
    total_pnl = sum(
        state.get(p['symbol'], {}).get('session_pnl', 0.0)
        for p in PAIRS if state.get(p['symbol'], {}).get('trades_date') == today
    )
    if total_pnl <= DAILY_LOSS_LIMIT:
        print(f' [DAILY LOSS] PnL: ${round(total_pnl, 2)} ≤ límite ${DAILY_LOSS_LIMIT}')
        return True, total_pnl
    return False, total_pnl

def sl_cooldown_active(ps, now):
    last_sl = ps.get('last_sl_time')
    if not last_sl:
        return False, 0
    try:
        sl_time = datetime.fromisoformat(last_sl)
        hours_since = (now - sl_time).total_seconds() / 3600
        if hours_since < SL_COOLDOWN_HOURS:
            return True, round(SL_COOLDOWN_HOURS - hours_since, 1)
    except Exception:
        pass
    return False, 0

def should_close_profit(position, entry, price):
    if not entry:
        return False
    if position == 'LONG':
        pnl_pct = (price - entry) / entry * 100
    elif position == 'SHORT':
        pnl_pct = (entry - price) / entry * 100
    else:
        return False
    pnl_usd = TRADE_AMOUNT * LEVERAGE * pnl_pct / 100
    return pnl_usd >= PROFIT_TARGET_USD

def calc_pnl_net(pos, entry, price, trade_amount, leverage):
    if not entry or entry <= 0:
        return 0.0, 0.0
    if pos == 'LONG':
        pnlpct_gross = (price - entry) / entry * 100
    else:
        pnlpct_gross = (entry - price) / entry * 100
    pnlpct_net = pnlpct_gross - FEE_BUFFER_PCT
    pnl_usd = trade_amount * leverage * pnlpct_net / 100
    return pnlpct_net, pnl_usd

# ── Telegram ───────────────────────────────────────────────────────────────────

def escape_html(text):
    text = str(text)
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def build_msg(parts):
    safe_parts = [escape_html(p) for p in parts if p is not None]
    return '\n'.join(safe_parts)

def send_msg(text):
    try:
        if not text or len(text.strip()) == 0:
            print('[Telegram] Mensaje vacío, no enviado')
            return
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        payload = {'chat_id': TELEGRAM_CHAT, 'text': text, 'parse_mode': 'HTML'}
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print('[Telegram] Mensaje enviado OK en HTML')
            return
        print(f'[Telegram] Error HTML {response.status_code}: {response.text}')
        payload_plain = {'chat_id': TELEGRAM_CHAT, 'text': text}
        response2 = requests.post(url, json=payload_plain, timeout=10)
        if response2.status_code == 200:
            print('[Telegram] Mensaje enviado OK en texto plano')
            return
        print(f'[Telegram] Error final {response2.status_code}: {response2.text}')
    except Exception as exc:
        print(f'[Telegram] Excepción: {exc}')

def send_session_report(state, now_str, balance, today):
    lines = [f'📊 REPORTE — {now_str}']
    total_pnl = 0.0
    for p in PAIRS:
        ps = state.get(p['symbol'], {})
        pos = ps.get('position', 'FLAT')
        sig = ps.get('signal', 'WAIT')
        price = ps.get('price', 0)
        rsi = ps.get('rsi', 0)
        adx = ps.get('adx', 0)
        pnl = ps.get('session_pnl', 0.0)
        if ps.get('trades_date') == today:
            total_pnl += pnl
        icon = '🟢' if pos == 'LONG' else '🔴' if pos == 'SHORT' else '⚪'
        s = '+' if pnl >= 0 else ''
        live_str = f' | {"🟩" if ps.get("live_pnl_usd",0)>=0 else "🟥"}{ps.get("live_pnl_pct",0):+.2f}%/${ps.get("live_pnl_usd",0):+.2f}' if pos in ('LONG','SHORT') else ''
        lines.append(f'{icon} {p["fsym"]}: ${price} | RSI:{rsi} | ADX:{adx} | {sig}{live_str} | Sess:{s}{round(pnl, 2)} | {ps.get("last_update","")}')
    s = '+' if total_pnl >= 0 else ''
    lines.append(f'📊 PnL hoy: {s}{round(total_pnl, 2)} USDT')
    lines.append(f'💰 Límite diario: {DAILY_LOSS_LIMIT}')
    if isinstance(balance, dict):
        lines.append(f'💰 Balance: ${balance["total"]} | Libre: ${balance["available"]} | Margen: {balance.get("margin_pct", 0)}%')
    send_msg(build_msg(lines))

# ── Órdenes ────────────────────────────────────────────────────────────────────

def open_long(pair, ps, price, sl, tp, ta):
    sym, fsym, dec = pair['symbol'], pair['fsym'], pair['dec']
    qty = round(ta * LEVERAGE / price, dec)
    halfway = price + (tp - price) * 0.5
    set_leverage_binance(sym)
    res = market_order(sym, 'BUY', qty)
    oid = res.get('orderId')
    if oid:
        ps.update({
            'position': 'LONG', 'entry_price': price, 'entry_qty': qty,
            'initial_sl': sl, 'tp_target': tp, 'trailing_sl': sl,
            'partial_closed': False, 'trades_today': ps.get('trades_today', 0) + 1,
            'trade_amount_used': ta
        })
        send_msg(build_msg([
            f'🟢 LONG abierto — {fsym}/USDT',
            f' Qty: {qty} @ ${round(price, 2)}',
            f' Monto: ${ta} | Lev: {LEVERAGE}x',
            f' TP: ${round(tp, 2)} | SL: ${round(sl, 2)}',
            f' 50% TP: ${round(halfway, 2)} | ID: {oid}'
        ]))
        _now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        save_state_now(_state_cache, _now, f'LONG abierto {fsym} @ {round(price,2)}')
    else:
        send_msg(f"❌ ERROR BUY {fsym}: {res.get('msg', str(res))}")

def close_position(pair, ps, price, reason, partial=False):
    sym, fsym, dec = pair['symbol'], pair['fsym'], pair['dec']
    entry = ps['entry_price'] or price
    qty = ps.get('entry_qty') or 0
    ta = ps.get('trade_amount_used') or TRADE_AMOUNT
    factor = 0.5 if partial else (0.5 if ps['partial_closed'] else 1.0)
    sell_qty = round(qty * factor, dec)
    if sell_qty <= 0:
        return
    set_leverage_binance(sym)
    res = market_order(sym, 'SELL', sell_qty)
    oid = res.get('orderId')
    pnl_pct = (price - entry) / entry * 100
    pnl_u = ta * factor * LEVERAGE * pnl_pct / 100
    ps['session_pnl'] = ps.get('session_pnl', 0) + pnl_u
    if oid:
        send_msg(build_msg([
            f'LONG cerrado — {fsym}/USDT',
            f' Razón: {reason}',
            f' SELL {sell_qty} @ ${round(price, 2)}',
            f' PnL: {round(pnl_u, 2)} USDT',
            f' ID: {oid}'
        ]))
    if not partial:
        ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                   'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                   'partial_closed': False, 'trade_amount_used': None,'max_pnl_usd': 0.0,})
    _now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    save_state_now(_state_cache, _now, f'LONG cerrado {pair["fsym"]}')

def open_short(pair, ps, price, sl, tp, ta):
    sym, fsym, dec = pair['symbol'], pair['fsym'], pair['dec']
    qty = round(ta * LEVERAGE / price, dec)
    halfway = price - (price - tp) * 0.5
    set_leverage_binance(sym)
    res = market_order(sym, 'SELL', qty)
    oid = res.get('orderId')
    if oid:
        ps.update({
            'position': 'SHORT', 'entry_price': price, 'entry_qty': qty,
            'initial_sl': sl, 'tp_target': tp, 'trailing_sl': sl,
            'partial_closed': False, 'trades_today': ps.get('trades_today', 0) + 1,
            'trade_amount_used': ta
        })
        send_msg(build_msg([
            f'🔴 SHORT abierto — {fsym}/USDT',
            f' Qty: {qty} @ ${round(price, 2)}',
            f' Monto: ${ta} | Lev: {LEVERAGE}x',
            f' TP: ${round(tp, 2)} | SL: ${round(sl, 2)}',
            f' 50% TP: ${round(halfway, 2)} | ID: {oid}'
        ]))
        _now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        save_state_now(_state_cache, _now, f'SHORT abierto {fsym} @ {round(price,2)}')
    else:
        send_msg(f"❌ ERROR SHORT {fsym}: {res.get('msg', str(res))}")

def close_short(pair, ps, price, reason, partial=False):
    sym, fsym, dec = pair['symbol'], pair['fsym'], pair['dec']
    entry = ps['entry_price'] or price
    qty = ps.get('entry_qty') or 0
    ta = ps.get('trade_amount_used') or TRADE_AMOUNT
    factor = 0.5 if partial else (0.5 if ps['partial_closed'] else 1.0)
    buy_qty = round(qty * factor, dec)
    if buy_qty <= 0:
        return
    set_leverage_binance(sym)
    res = market_order(sym, 'BUY', buy_qty)
    oid = res.get('orderId')
    pnl_pct = (entry - price) / entry * 100
    pnl_u = ta * factor * LEVERAGE * pnl_pct / 100
    ps['session_pnl'] = ps.get('session_pnl', 0) + pnl_u
    if oid:
        send_msg(build_msg([
            f'SHORT cerrado — {fsym}/USDT',
            f' Razón: {reason}',
            f' BUY {buy_qty} @ ${round(price, 2)}',
            f' PnL: {round(pnl_u, 2)} USDT',
            f' ID: {oid}'
        ]))
    if not partial:
        ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                   'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                   'partial_closed': False, 'trade_amount_used': None,'max_pnl_usd': 0.0,})
    _now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    save_state_now(_state_cache, _now, f'SHORT cerrado {fsym} — {reason}')

# ── Gestión de posiciones ──────────────────────────────────────────────────────

def _maybe_update_momentum_block(ps, pair, position, adx_now):
    if not USE_MOMENTUM or adx_now < MOMENTUM_ADX_MIN:
        return False
    closes, _, _, _ = fetchcandles(pair['fsym'])
    if len(closes) < 5:
        return False
    price_change_pct = abs(closes[-1] - closes[-4]) / closes[-4]
    if price_change_pct < MOMENTUM_THRESHOLD:
        return False

    sig = ps.get('signal', '')
    if position == 'LONG' and ('SELL' in sig or 'WAIT_RSI_SHORT' in sig):
        print(f"🛡️ MOMENTUM: Protegiendo LONG {pair['fsym']} (ADX:{adx_now} > {MOMENTUM_ADX_MIN})")
        return True
    if position == 'SHORT' and ('BUY' in sig or 'WAIT_RSI' in sig):
        print(f"🛡️ MOMENTUM: Protegiendo SHORT {pair['fsym']} (ADX:{adx_now} > {MOMENTUM_ADX_MIN})")
        return True
    return False

def manage_open(pair, ps, price, atr, now_str, now_dt):
    entry, tp, trail = ps.get('entry_price'), ps.get('tp_target'), ps.get('trailing_sl')
    partial = ps.get('partial_closed', False)
    if None in (entry, tp, trail) or atr is None or atr <= 0:
        return {'closed': False, 'action': None}

    ta = ps.get('trade_amount_used') or TRADE_AMOUNT
    pnl_pct = (price - entry) / entry * 100
    pnl_u = ta * LEVERAGE * pnl_pct / 100

    adx_now = ps.get('adx', 0.0) or 0.0
    if _maybe_update_momentum_block(ps, pair, 'LONG', adx_now):
        return {'closed': False, 'action': 'momentum_block'}

    max_pnl = ps.get('max_pnl_usd', 0.0)
    if pnl_u > max_pnl:
        max_pnl = pnl_u
        ps['max_pnl_usd'] = max_pnl

    if max_pnl >= LOCK_START_USD:
        lock_pnl = max_pnl * LOCK_RATIO
        lock_pct = lock_pnl * 100 / (ta * LEVERAGE)
        sl_lock_price = entry * (1 + lock_pct / 100)
        ps['trailing_sl'] = max(ps['trailing_sl'], sl_lock_price)
        trail = ps['trailing_sl']
        print(f"🔒 Escalera LONG {pair['fsym']}: max_pnl={max_pnl:.2f} lock={lock_pnl:.2f} SL_lock={sl_lock_price:.2f}")

    new_trail_atr = price - atr * TRAIL_MULT
    if new_trail_atr > ps['trailing_sl']:
        ps['trailing_sl'] = new_trail_atr
        trail = new_trail_atr

    halfway = entry + (tp - entry) * 0.5
    if not partial and price >= halfway:
        ps['partial_closed'] = True
        ps['trailing_sl'] = max(ps['trailing_sl'], entry)
        send_msg(build_msg([
            f'📍 50% TP LONG — {pair["fsym"]}/USDT',
            f' Precio: ${round(price, 2)} | SL → BE: ${round(entry, 2)}'
        ]))
        if AUTO_TRADE and API_KEY:
            close_position(pair, ps, price, '50% TP', partial=True)
        return {'closed': True, 'action': 'partial_tp'}

    if price <= ps['trailing_sl']:
        ps['last_sl_time'] = now_dt.isoformat()
        send_msg(build_msg([
            f'🛑 TRAILING SL LONG — {pair["fsym"]}/USDT',
            f' Precio: ${round(price, 2)} | Trail: ${round(ps["trailing_sl"], 2)}'
        ]))
        if AUTO_TRADE and API_KEY:
            close_position(pair, ps, price, 'Trailing SL')
        else:
            ps.update({
                'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                'partial_closed': False, 'trade_amount_used': None,
                'max_pnl_usd': 0.0,
            })
        return {'closed': True, 'action': 'trailing_sl'}

    return {'closed': False, 'action': None}

def manage_short(pair, ps, price, atr, now_str, now_dt):
    entry, tp, trail = ps.get('entry_price'), ps.get('tp_target'), ps.get('trailing_sl')
    partial = ps.get('partial_closed', False)
    if None in (entry, tp, trail) or atr is None or atr <= 0:
        return {'closed': False, 'action': None}

    ta = ps.get('trade_amount_used') or TRADE_AMOUNT
    pnl_pct = (entry - price) / entry * 100
    pnl_u = ta * LEVERAGE * pnl_pct / 100

    adx_now = ps.get('adx', 0.0) or 0.0
    if _maybe_update_momentum_block(ps, pair, 'SHORT', adx_now):
        return {'closed': False, 'action': 'momentum_block'}

    max_pnl = ps.get('max_pnl_usd', 0.0)
    if pnl_u > max_pnl:
        max_pnl = pnl_u
        ps['max_pnl_usd'] = max_pnl

    if max_pnl >= LOCK_START_USD:
        lock_pnl = max_pnl * LOCK_RATIO
        lock_pct = lock_pnl * 100 / (ta * LEVERAGE)
        sl_lock_price = entry * (1 - lock_pct / 100)
        ps['trailing_sl'] = min(ps['trailing_sl'], sl_lock_price)
        trail = ps['trailing_sl']
        print(f"🔒 Escalera SHORT {pair['fsym']}: max_pnl={max_pnl:.2f} lock={lock_pnl:.2f} SL_lock={sl_lock_price:.2f}")

    new_trail_atr = price + atr * TRAIL_MULT
    if new_trail_atr < ps['trailing_sl']:
        ps['trailing_sl'] = new_trail_atr
        trail = new_trail_atr

    halfway = entry - (entry - tp) * 0.5
    if not partial and price <= halfway:
        ps['partial_closed'] = True
        ps['trailing_sl'] = min(ps['trailing_sl'], entry)
        send_msg(build_msg([
            f'📍 50% TP SHORT — {pair["fsym"]}/USDT',
            f' Precio: ${round(price, 2)} | SL → BE: ${round(entry, 2)}'
        ]))
        if AUTO_TRADE and API_KEY:
            close_short(pair, ps, price, '50% TP', partial=True)
        return {'closed': True, 'action': 'partial_tp'}

    if price >= ps['trailing_sl']:
        ps['last_sl_time'] = now_dt.isoformat()
        send_msg(build_msg([
            f'🛑 TRAILING SL SHORT — {pair["fsym"]}/USDT',
            f' Precio: ${round(price, 2)} | Trail: ${round(ps["trailing_sl"], 2)}'
        ]))
        if AUTO_TRADE and API_KEY:
            close_short(pair, ps, price, 'Trailing SL')
        else:
            ps.update({
                'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                'partial_closed': False, 'trade_amount_used': None,
                'max_pnl_usd': 0.0,
            })
        return {'closed': True, 'action': 'trailing_sl'}

    return {'closed': False, 'action': None}

# ── Cierres por BTC ────────────────────────────────────────────────────────────

def check_market_reversal_exits(state, btc_bull_now, btc_bull_prev, now_str, now_dt):
    if btc_bull_now == btc_bull_prev:
        return
    print(f' [REVERSIÓN BTC] {"BAJISTA→ALCISTA" if btc_bull_now else "ALCISTA→BAJISTA"}')
    for pair in PAIRS:
        ps = state.get(pair['symbol'], {})
        pos = ps.get('position', 'FLAT')
        if pos == 'FLAT':
            continue
        entry = ps.get('entry_price')
        if not entry:
            continue
        try:
            closes, highs, lows, vols = fetchcandles(pair['fsym'])
            if not closes:
                print(f' [REVERSIÓN] Sin datos para {pair["fsym"]}, saltando')
                continue
            price = closes[-1]
        except Exception as exc:
            print(f' [REVERSIÓN] Error obteniendo precio {pair["fsym"]}: {exc}')
            continue
        if pos == 'LONG':
            pnl_pct = (price - entry) / entry * 100
        else:
            pnl_pct = (entry - price) / entry * 100
        ta = ps.get('trade_amount_used') or TRADE_AMOUNT
        pnl_u = ta * LEVERAGE * pnl_pct / 100
        if btc_bull_now and pos == 'SHORT' and pnl_pct > 0:
            send_msg(build_msg([
                f'🔄 CAMBIO DE TENDENCIA — {pair["fsym"]}/USDT',
                f'📊 BTC cambió a ALCISTA → Cerrando SHORT en ganancia',
                f'💵 Entrada: ${round(entry, 2)} | Actual: ${round(price, 2)}',
                f'💰 PnL: +{round(pnl_pct, 2)}% (+${round(pnl_u, 2)} USDT)',
                f'⏰ {now_str}',
            ]))
            if AUTO_TRADE and API_KEY:
                close_short(pair, ps, price, 'BTC alcista - ganancia asegurada')
            else:
                ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                           'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                           'partial_closed': False, 'trade_amount_used': None,
                           'signal': 'WAIT', 'last_signal': 'WAIT'})
        elif not btc_bull_now and pos == 'LONG' and pnl_pct > 0:
            send_msg(build_msg([
                f'🔄 CAMBIO DE TENDENCIA — {pair["fsym"]}/USDT',
                f'📊 BTC cambió a BAJISTA → Cerrando LONG en ganancia',
                f'💵 Entrada: ${round(entry, 2)} | Actual: ${round(price, 2)}',
                f'💰 PnL: +{round(pnl_pct, 2)}% (+${round(pnl_u, 2)} USDT)',
                f'⏰ {now_str}',
            ]))
            if AUTO_TRADE and API_KEY:
                close_position(pair, ps, price, 'BTC bajista - ganancia asegurada')
            else:
                ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                           'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                           'partial_closed': False, 'trade_amount_used': None})
        elif pnl_pct <= 0:
            print(f' [REVERSIÓN] Manteniendo {pos} {pair["fsym"]} en pérdida ({round(pnl_pct, 2)}%)')
        else:
            print(f' [REVERSIÓN] Manteniendo {pos} {pair["fsym"]} (alineado con nueva tendencia)')

def check_btc_long_signal_exits(state, btc_signal, now_str, now_dt):
    if btc_signal not in ('BUY', 'LONG_ACTIVE'):
        return
    print(f' [BTC SIGNAL EXIT] Señal BTC: {btc_signal} → revisar SHORTs en alts')
    for pair in PAIRS:
        fsym, sym = pair['fsym'], pair['symbol']
        ps = state.get(sym, {})
        pos = ps.get('position', 'FLAT')
        if fsym == 'BTC' or pos != 'SHORT':
            continue
        entry = ps.get('entry_price')
        if not entry:
            continue
        try:
            closes, highs, lows, vols = fetchcandles(fsym)
            if not closes:
                continue
            price = closes[-1]
        except Exception as exc:
            print(f' [BTC SIGNAL EXIT] Error obteniendo precio {fsym}: {exc}')
            continue
        pnl_pct = (entry - price) / entry * 100
        ta = ps.get('trade_amount_used') or TRADE_AMOUNT
        pnl_u = ta * LEVERAGE * pnl_pct / 100
        if pnl_pct <= 0:
            continue
        send_msg(build_msg([
            f'🔄 BTC SEÑAL LONG — CIERRE SHORT {fsym}/USDT',
            '📊 BTC con señal alcista → cerrando SHORT en ganancia',
            f'💵 Entrada: ${round(entry, 2)} | Actual: ${round(price, 2)}',
            f'💰 PnL: +{round(pnl_pct, 2)}% (+${round(pnl_u, 2)} USDT)',
            f'⏰ {now_str}',
        ]))
        if AUTO_TRADE and API_KEY:
            close_short(pair, ps, price, 'BTC señal LONG - ganancia asegurada')
        else:
            ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                       'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                       'partial_closed': False, 'trade_amount_used': None})

def check_btc_short_signal_exits(state, btc_signal, now_str, now_dt):
    if btc_signal not in ('SELL', 'SHORT_ACTIVE'):
        return
    print(f' [BTC SIGNAL EXIT] Señal BTC: {btc_signal} → revisar LONGs en alts')
    for pair in PAIRS:
        fsym, sym = pair['fsym'], pair['symbol']
        ps = state.get(sym, {})
        pos = ps.get('position', 'FLAT')
        if fsym == 'BTC' or pos != 'LONG':
            continue
        entry = ps.get('entry_price')
        if not entry:
            continue
        try:
            closes, highs, lows, vols = fetchcandles(fsym)
            if not closes:
                continue
            price = closes[-1]
        except Exception as exc:
            print(f' [BTC SIGNAL EXIT] Error obteniendo precio {fsym}: {exc}')
            continue
        pnl_pct = (price - entry) / entry * 100
        ta = ps.get('trade_amount_used') or TRADE_AMOUNT
        pnl_u = ta * LEVERAGE * pnl_pct / 100
        if pnl_pct <= 0:
            continue
        send_msg(build_msg([
            f'🔄 BTC SEÑAL SHORT — CIERRE LONG {fsym}/USDT',
            '📊 BTC con señal bajista → cerrando LONG en ganancia',
            f'💵 Entrada: ${round(entry, 2)} | Actual: ${round(price, 2)}',
            f'💰 PnL: +{round(pnl_pct, 2)}% (+${round(pnl_u, 2)} USDT)',
            f'⏰ {now_str}',
        ]))
        if AUTO_TRADE and API_KEY:
            close_position(pair, ps, price, 'BTC señal SHORT - ganancia asegurada')
        else:
            ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                       'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                       'partial_closed': False, 'trade_amount_used': None})

def is_momentum_exhausted(closes, e21, position='LONG'):
    if len(closes) < 10 or e21[-1] is None:
        return False
    move_8 = (closes[-1] - closes[-9]) / closes[-9]
    move_3 = (closes[-1] - closes[-4]) / closes[-4]
    dist_ema21 = (closes[-1] - e21[-1]) / e21[-1]
    if position == 'LONG':
        if move_8 > 0.025 and move_3 < MOMENTUM_THRESHOLD:
            print(f'  🚫 [MOMENTUM AGOTADO LONG] swing8={round(move_8*100,2)}% move3={round(move_3*100,2)}%')
            return True
        if dist_ema21 > 0.02:
            print(f'  🚫 [SOBREEXTENDIDO LONG] {round(dist_ema21*100,2)}% sobre EMA21')
            return True
    if position == 'SHORT':
        if move_8 < -0.025 and move_3 > -MOMENTUM_THRESHOLD:
            print(f'  🚫 [MOMENTUM AGOTADO SHORT] swing8={round(move_8*100,2)}% move3={round(move_3*100,2)}%')
            return True
        if dist_ema21 < -0.02:
            print(f'  🚫 [SOBREEXTENDIDO SHORT] {round(dist_ema21*100,2)}% bajo EMA21')
            return True
    return False

# ── Proceso principal por par ──────────────────────────────────────────────────

def process_pair(pair, ps, today, now_str, now_dt, btc_bull, balance, state):
    sym, fsym = pair['symbol'], pair['fsym']

    if ps.get('trades_date') != today:
        ps['trades_today'] = 0
        ps['trades_date'] = today

    closes, highs, lows, vols = fetchcandles(fsym)
    if not closes or len(closes) < 100:
        print(f"⚠️ [{sym}] Sin datos suficientes, saltando...")
        return ps

    price = closes[-1]
    e21 = calc_ema(closes, 21)
    e89 = calc_ema(closes, 89)
    rsi14 = calc_rsi(closes, 14)
    atr14 = calc_atr(highs, lows, closes, 14)
    adx14 = calc_adx(highs, lows, closes, 14)

    le21 = e21[-1] or 0.0
    le89 = e89[-1] or 0.0
    lr = rsi14[-1] or 0.0
    la = atr14[-1] or 0.0
    la_adx = adx14[-1] if adx14 and adx14[-1] is not None else 0.0

    momentum_detected = False
    if USE_MOMENTUM and len(closes) >= 5:
        price_change_pct = abs(closes[-1] - closes[-4]) / closes[-4]
        if price_change_pct >= MOMENTUM_THRESHOLD and la_adx >= MOMENTUM_ADX_MIN:
            momentum_detected = True
            print(f' ⚡ [MOMENTUM] {round(price_change_pct * 100, 2)}% | ADX:{round(la_adx, 1)}')

    sl_long = price - la * 0.65
    tp_long = price + la * 2.5
    sl_short = price + la * 0.65
    tp_short = price - la * 2.5

    sig = get_signal(e21, e89, rsi14, RSI_MIN, RSI_MAX)
    vcr = velas_desde_cruce(e21, e89)

    macd = sigmacd = hist = None
    ok_long = False
    ok_short = False
    macd, sigmacd, hist = compute_macd(closes)
    if macd is not None and sigmacd is not None and hist is not None:
        macd_dir = 'ALCISTA' if macd > sigmacd else 'BAJISTA'
        strength = abs(macd - sigmacd)
        ok_long = macd > sigmacd and strength >= MIN_MACD_STRENGTH
        ok_short = macd < sigmacd and strength >= MIN_MACD_STRENGTH
        print(f' [{sym}] MACD {macd_dir} | macd={round(macd, 2)} fuerza={round(strength, 2)} long={ok_long} short={ok_short}')

    now_time = datetime.now(timezone.utc).strftime('%H:%M UTC')
    ps.update({
        'signal': sig,
        'price': round(price, 2),
        'rsi': round(lr, 1),
        'ema21': round(le21, 2),
        'ema89': round(le89, 2),
        'atr': round(la, 2),
        'adx': round(la_adx, 1),
        'velas_cruce': vcr,
        'last_update': now_time,
    })

    pos = ps.get('position', 'FLAT')
    print(f' [{sym}] {sig} | ${round(price, 2)} | RSI:{round(lr, 1)} | ADX:{round(la_adx, 1)} | pos:{pos}')
    if pos in ('LONG', 'SHORT') and ps.get('entry_price'):
        print(f' [{sym}] 📈 PnL live: {ps.get("live_pnl_pct", 0):+.2f}% (${ps.get("live_pnl_usd", 0):+.2f})')

    action = {'closed': False, 'action': None}

    if pos == 'LONG':
        action = manage_open(pair, ps, price, la, now_str, now_dt)
    elif pos == 'SHORT':
        action = manage_short(pair, ps, price, la, now_str, now_dt)

    if action.get('closed'):
        ps['last_signal'] = sig
        return ps


    if sig in ('SELL', 'WAIT_RSI_SHORT') and ps.get('position') == 'LONG':
        ep = ps.get('entry_price') or price
        ta_u = ps.get('trade_amount_used') or TRADE_AMOUNT
        pnl_u = ta_u * LEVERAGE * (price - ep) / ep
        send_msg(build_msg([
            f'🔴 CRUCE BAJISTA — {fsym}/USDT 4H',
            '❌ CERRAR POSICIÓN LONG',
            f'📊 Precio: ${round(price, 2)} | PnL est.: {round(pnl_u, 2)} USDT'
        ]))
        if AUTO_TRADE and API_KEY:
            close_position(pair, ps, price, 'Cruce bajista')
        else:
            ps.update({
                'position': 'FLAT',
                'entry_price': None,
                'entry_qty': None,
                'initial_sl': None,
                'tp_target': None,
                'trailing_sl': None,
                'partial_closed': False,
                'trade_amount_used': None
            })

    if sig in ('BUY', 'WAIT_RSI') and ps.get('position') == 'SHORT':
        ep = ps.get('entry_price') or price
        ta_u = ps.get('trade_amount_used') or TRADE_AMOUNT
        pnl_u = ta_u * LEVERAGE * (ep - price) / ep
        send_msg(build_msg([
            f'🟢 CRUCE ALCISTA — {fsym}/USDT 4H',
            '❌ CERRAR POSICIÓN SHORT',
            f'📊 Precio: ${round(price, 2)} | PnL est.: {round(pnl_u, 2)} USDT'
        ]))
        if AUTO_TRADE and API_KEY:
            close_short(pair, ps, price, 'Cruce alcista')
        else:
            ps.update({
                'position': 'FLAT',
                'entry_price': None,
                'entry_qty': None,
                'initial_sl': None,
                'tp_target': None,
                'trailing_sl': None,
                'partial_closed': False,
                'trade_amount_used': None
            })

    pos = ps.get('position', 'FLAT')
    if sig == ps.get('last_signal') and pos != 'FLAT':
        return ps

    ta_now = resolve_trade_amount(balance)
    btc_ok_long = True if (not USE_BTC_FILTER or momentum_detected) else (btc_bull or fsym == 'BTC')
    btc_ok_short = True if (not USE_BTC_FILTER or momentum_detected) else ((not btc_bull) or fsym == 'BTC')

    halfway_l = price + (tp_long - price) * 0.5
    halfway_s = price - (price - tp_short) * 0.5
    sl_pct_l = round((sl_long - price) / price * 100, 1)
    tp_pct_l = round((tp_long - price) / price * 100, 1)
    sl_pct_s = round((sl_short - price) / price * 100, 1)
    tp_pct_s = round((tp_short - price) / price * 100, 1)
    pnl_sl_l = round(ta_now * LEVERAGE * abs(sl_pct_l) / 100, 2)
    pnl_tp_l = round(ta_now * LEVERAGE * tp_pct_l / 100, 2)
    pnl_sl_s = round(ta_now * LEVERAGE * abs(sl_pct_s) / 100, 2)
    pnl_tp_s = round(ta_now * LEVERAGE * abs(tp_pct_s) / 100, 2)

    parts = None

    if sig == 'BUY':
        dec_txt = '✅ ABRIR LONG' if btc_ok_long else '⛔ NO OPERAR — BTC filtrado'
        parts = [
            f'🟢 SEÑAL LONG — {fsym}/USDT 4H', '', dec_txt, '',
            f'📊 ${round(price, 2)} | EMA21: ${round(le21, 2)} | EMA89: ${round(le89, 2)}',
            f'📐 RSI: {round(lr, 1)} (mín {RSI_MIN}) | ADX: {round(la_adx, 1)}', '',
            '━━━ SETUP ━━━',
            f'🛑 SL: ${round(sl_long, 2)} ({sl_pct_l}%) → -${pnl_sl_l}',
            f'🎯 TP: ${round(tp_long, 2)} (+{tp_pct_l}%) → +${pnl_tp_l}',
            f'📍 50%: ${round(halfway_l, 2)} | 🔄 Trailing activo',
            f'⚡ Lev: {LEVERAGE}x | ${ta_now} | R:R 1:2 | {now_str}'
        ]
    elif sig == 'SELL':
        dec_txt = '✅ ABRIR SHORT' if btc_ok_short else '⛔ NO OPERAR — BTC filtrado'
        parts = [
            f'🔴 SEÑAL SHORT — {fsym}/USDT 4H', '', dec_txt, '',
            f'📊 ${round(price, 2)} | EMA21: ${round(le21, 2)} | EMA89: ${round(le89, 2)}',
            f'📐 RSI: {round(lr, 1)} (máx {RSI_MAX}) | ADX: {round(la_adx, 1)}', '',
            '━━━ SETUP ━━━',
            f'🛑 SL: ${round(sl_short, 2)} (+{sl_pct_s}%) → -${pnl_sl_s}',
            f'🎯 TP: ${round(tp_short, 2)} ({tp_pct_s}%) → +${pnl_tp_s}',
            f'📍 50%: ${round(halfway_s, 2)} | 🔄 Trailing activo',
            f'⚡ Lev: {LEVERAGE}x | ${ta_now} | R:R 1:2 | {now_str}'
        ]
    elif sig == 'WAIT_RSI':
        falta = round(RSI_MIN - lr, 1)
        parts = [
            f'🟡 CRUCE ALCISTA — {fsym}/USDT 4H',
            '⏳ Esperar RSI',
            f'📐 RSI: {round(lr, 1)} (faltan +{falta} para >{RSI_MIN})',
            f'🕯 Velas desde cruce: {vcr} | ADX: {round(la_adx, 1)}',
            f'🎯 TP ref: ${round(tp_long, 2)} | 🛑 SL ref: ${round(sl_long, 2)}',
            f'⏰ {now_str}'
        ]
    elif sig == 'WAIT_RSI_SHORT':
        falta = round(lr - RSI_MAX, 1)
        parts = [
            f'🟠 CRUCE BAJISTA — {fsym}/USDT 4H',
            '⏳ Esperar RSI',
            f'📐 RSI: {round(lr, 1)} (sobran {falta} para <{RSI_MAX})',
            f'🕯 Velas desde cruce: {vcr} | ADX: {round(la_adx, 1)}',
            f'🎯 TP ref: ${round(tp_short, 2)} | 🛑 SL ref: ${round(sl_short, 2)}',
            f'⏰ {now_str}'
        ]
    elif sig == 'LONG_ACTIVE':
        dec_txt = f'⚡ Entrada tardía válida ({vcr} velas)' if vcr <= MAX_CANDLES_LATE else f'⏸ Cruce antiguo ({vcr} velas)'
        parts = [
            f'🔵 LARGO ACTIVO — {fsym}/USDT 4H',
            dec_txt,
            f'📊 ${round(price, 2)} | RSI: {round(lr, 1)} | ADX: {round(la_adx, 1)}',
            f'🎯 TP ref: ${round(tp_long, 2)} | 🛑 SL ref: ${round(sl_long, 2)}',
            f'⏰ {now_str}'
        ]
    elif sig == 'SHORT_ACTIVE':
        dec_txt = f'⚡ Entrada tardía válida ({vcr} velas)' if vcr <= MAX_CANDLES_LATE else f'⏸ Cruce antiguo ({vcr} velas)'
        parts = [
            f'🟠 CORTO ACTIVO — {fsym}/USDT 4H',
            dec_txt,
            f'📊 ${round(price, 2)} | RSI: {round(lr, 1)} | ADX: {round(la_adx, 1)}',
            f'🎯 TP ref: ${round(tp_short, 2)} | 🛑 SL ref: ${round(sl_short, 2)}',
            f'⏰ {now_str}'
        ]

    if parts is None:
        ps['last_signal'] = sig
        return ps


    late_long = sig == 'LONG_ACTIVE' and vcr <= MAX_CANDLES_LATE
    late_short = sig == 'SHORT_ACTIVE' and vcr <= MAX_CANDLES_LATE

    pos = ps.get('position', 'FLAT')
    if momentum_detected:
        want_long = sig in ('BUY', 'LONG_ACTIVE', 'WAIT_RSI') and pos == 'FLAT' and closes[-1] > closes[-4]
        want_short = sig in ('SELL', 'SHORT_ACTIVE', 'WAIT_RSI_SHORT') and pos == 'FLAT' and closes[-1] < closes[-4]
    else:
        want_long = (sig == 'BUY' or sig == 'LONG_ACTIVE' or late_long) and pos == 'FLAT'
        want_short = (sig == 'SELL' or sig == 'SHORT_ACTIVE' or late_short) and pos == 'FLAT'

    if AUTO_TRADE and API_KEY and (want_long or want_short):
        cooldown_active, _ = sl_cooldown_active(ps, now_dt)
        if cooldown_active:
            ps['last_signal'] = sig
            return ps

        if la_adx < ADX_MIN and not (momentum_detected and la_adx >= MOMENTUM_ADX_MIN):
            ps['last_signal'] = sig
            return ps

        if USE_MTF:
            daily_bull = pair_daily_is_bullish(fsym)
            if want_long and not daily_bull:
                ps['last_signal'] = sig
                return ps
            if want_short and daily_bull:
                ps['last_signal'] = sig
                return ps

        if USE_VOLUME_FILTER:
            vol_ok, _ = volume_confirmed(vols)
            if not vol_ok:
                ps['last_signal'] = sig
                return ps

        if USE_BTC_FILTER and not momentum_detected:
            if want_long and not btc_ok_long:
                ps['last_signal'] = sig
                return ps
            if want_short and not btc_ok_short:
                ps['last_signal'] = sig
                return ps

        if count_open_positions(state) >= MAX_OPEN_POS:
            ps['last_signal'] = sig
            return ps

        if fsym != 'BTC' and count_alt_positions(state) >= MAX_ALT_POS:
            ps['last_signal'] = sig
            return ps

        if ps.get('trades_today', 0) >= MAX_TRADES_DAY:
            ps['last_signal'] = sig
            return ps

        exceeded, _ = daily_loss_exceeded(state, today)
        if exceeded:
            ps['last_signal'] = sig
            return ps


        if want_long:
            if momentum_detected and is_momentum_exhausted(closes, e21, 'LONG'):
                ps['last_signal'] = sig
                return ps
            open_long(pair, ps, price, sl_long, tp_long, ta_now)
        elif want_short:
            if momentum_detected and is_momentum_exhausted(closes, e21, 'SHORT'):
                ps['last_signal'] = sig
                return ps
            open_short(pair, ps, price, sl_short, tp_short, ta_now)

    ps['last_signal'] = sig
    return ps

# ── Loop principal ─────────────────────────────────────────────────────────────

def run_bot_cycle():
    global cycle_count
    now_dt = datetime.now(timezone.utc)
    today = now_dt.strftime('%Y-%m-%d')
    now_str = now_dt.strftime('%Y-%m-%d %H:%M UTC')
    sizing = f'{TRADE_PCT * 100:.1f}%' if TRADE_PCT > 0 else f'${TRADE_AMOUNT}'

    print('=' * 55)
    print(f' EMA Bot v10.3 + Momentum Direccional | {now_str}')
    print(f' Sizing: {sizing} | Lev: {LEVERAGE}x')
    print(f' RSI LONG>{RSI_MIN} SHORT<{RSI_MAX} | ADX>{ADX_MIN}')
    print(f' MaxPos: {MAX_OPEN_POS} | MaxAlts: {MAX_ALT_POS} | MaxTrades: {MAX_TRADES_DAY}')
    print(f' DailyLoss: ${DAILY_LOSS_LIMIT} | SL Cooldown: {SL_COOLDOWN_HOURS}h')
    print(f' MTF: {USE_MTF} | VolFilter: {USE_VOLUME_FILTER}')
    print('=' * 55)

    state = load_state()
    state = sync_positions_with_binance(state)
    balance = get_futures_balance() if API_KEY else None
    btc_signal = None

    loss_exceeded, daily_pnl = daily_loss_exceeded(state, today)
    if loss_exceeded:
        send_msg(build_msg([
            '🛑 STOP DIARIO ACTIVADO',
            f'PnL del día: {round(daily_pnl, 2)}',
            f'Límite: {DAILY_LOSS_LIMIT}',
            f'Bot bloqueado hasta mañana. {now_str}'
        ]))
        save_state(state, now_str, balance)
        return False

    print('[Filtro BTC 1H]')
    btc_bull = btc_is_bullish()
    open_now = count_open_positions(state)
    print(f'[Posiciones abiertas: {open_now}/{MAX_OPEN_POS}]')

    for pair in PAIRS:
        sym = pair['symbol']
        fsym = pair['fsym']
        print(f'\n-> {sym}')
        ps = get_pair_state(state, sym)
        if ps.get('position') == 'FLAT' and open_now >= MAX_OPEN_POS:
            print(' [SALTADO MAX_OPEN_POSITIONS]')
            state[sym] = ps
            continue
        try:
            state[sym] = process_pair(pair, ps, today, now_str, now_dt, btc_bull, balance, state)
            if fsym == 'BTC':
                btc_signal = state[sym].get('signal')
        except Exception as exc:
            print(f' [ERROR {sym}] {exc}')
            send_msg(f'❌ Error {sym}: {exc}')
        time.sleep(2)

    # ── FIX: btc_signal desde el estado actualizado de BTC ─────────────────────
    btc_signal = state.get('BTCUSDT', {}).get('signal')
    if btc_signal:
        check_btc_long_signal_exits(state, btc_signal, now_str, now_dt)
        check_btc_short_signal_exits(state, btc_signal, now_str, now_dt)

    save_state(state, now_str, balance, reason='Fin de ciclo')
    print(f'[State] ✅ Persistido | {now_str}')
    cycle_count += 1
    if cycle_count >= REPORT_EVERY_N_CYCLES:
        send_session_report(state, now_str, balance, today)
        cycle_count = 0
        print(f'\n📊 Reporte enviado (ciclo {REPORT_EVERY_N_CYCLES})')
    else:
        print(f'\n📊 Próximo reporte en {REPORT_EVERY_N_CYCLES - cycle_count} ciclos (~{(REPORT_EVERY_N_CYCLES - cycle_count) * 3} min)')

    print(f'\n✓ Completado — {now_str}')
    has_positions = count_open_positions(state) > 0
    return has_positions

def main():
    global running
    print('=' * 70)
    print('🤖 EMA Bot v10.3 - Iniciado')
    print('=' * 70)
    print(f'Check con posiciones: {CHECK_INTERVAL_WITH_POSITIONS}s (3 min)')
    print(f'Check sin posiciones: {CHECK_INTERVAL_NO_POSITIONS}s (15 min)')
    print('=' * 70)

    while running:
        try:
            has_positions = run_bot_cycle()
            state = load_state()

            if has_locked_positions(state):
                interval = CHECK_INTERVAL_LOCKED
                next_check = datetime.now(timezone.utc) + timedelta(seconds=interval)
                print(f'\n🔒 Escalera activa. Próximo check en {interval}s ({next_check.strftime("%H:%M UTC")})')
            elif has_positions:
                interval = CHECK_INTERVAL_WITH_POSITIONS
                next_check = datetime.now(timezone.utc) + timedelta(seconds=interval)
                print(f'\n⏰ Posiciones activas. Próximo check en {interval}s ({next_check.strftime("%H:%M UTC")})')
            else:
                interval = CHECK_INTERVAL_NO_POSITIONS
                next_check = datetime.now(timezone.utc) + timedelta(seconds=interval)
                print(f'\n⏰ Sin posiciones. Próximo check en {interval}s ({next_check.strftime("%H:%M UTC")})')

            elapsed = 0
            while elapsed < interval and running:
                sleep_time = min(5, interval - elapsed)
                time.sleep(sleep_time)
                elapsed += sleep_time

        except KeyboardInterrupt:
            print('\n[KEYBOARD] Deteniendo bot...')
            running = False
            break
        except Exception as e:
            print(f'\n[ERROR EN LOOP] {e}')
            import traceback
            traceback.print_exc()
            time.sleep(60)

    print('\n🛑 Bot detenido correctamente')

if __name__ == '__main__':
    main()
