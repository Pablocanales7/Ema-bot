import requests, json, os, hmac, hashlib, time
from datetime import datetime, timezone, timedelta

# ── Configuración ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT  = os.environ['TELEGRAM_CHAT_ID']
API_KEY        = os.environ.get('BINANCE_API_KEY', '')
API_SECRET     = os.environ.get('BINANCE_API_SECRET', '')
AUTO_TRADE     = os.environ.get('AUTO_TRADE', 'false').lower() == 'true'
TRADE_AMOUNT   = float(os.environ.get('TRADE_AMOUNT', '15'))
TRADE_PCT      = float(os.environ.get('TRADE_PCT', '0'))
LEVERAGE       = int(os.environ.get('LEVERAGE', '10'))
MAX_TRADES_DAY = int(os.environ.get('MAX_DAILY_TRADES', '3'))
MAX_OPEN_POS   = int(os.environ.get('MAX_OPEN_POSITIONS', '3'))
MAX_ALT_POS    = int(os.environ.get('MAX_ALT_POSITIONS', '2'))
RSI_MIN        = float(os.environ.get('RSI_ENTRY_MIN', '53'))
RSI_MAX        = float(os.environ.get('RSI_ENTRY_MAX', '47'))
TRAIL_MULT     = float(os.environ.get('TRAIL_ATR_MULT', '1.5'))
ADX_MIN        = float(os.environ.get('ADX_MIN', '25'))
DAILY_LOSS_LIMIT   = float(os.environ.get('DAILY_LOSS_LIMIT', '-30'))
SL_COOLDOWN_HOURS  = int(os.environ.get('SL_COOLDOWN_HOURS', '12'))
VOL_MULT           = float(os.environ.get('VOL_MULT', '1.2'))
USE_MTF            = os.environ.get('USE_MTF', 'true').lower() == 'true'
USE_VOLUME_FILTER  = os.environ.get('USE_VOLUME_FILTER', 'true').lower() == 'true'
RECV_WINDOW        = int(os.environ.get('RECV_WINDOW', '10000'))   # FIX: tolerancia timestamp

PAIRS = [
    {"symbol": "BTCUSDT", "fsym": "BTC", "dec": 3},
    {"symbol": "ETHUSDT", "fsym": "ETH", "dec": 3},
    {"symbol": "SOLUSDT", "fsym": "SOL", "dec": 1},
    {"symbol": "BNBUSDT", "fsym": "BNB", "dec": 2},
]

STATE_FILE = 'state.json'
EMPTY_STATE = {
    "last_signal": None, "position": "FLAT", "entry_price": None,
    "entry_qty": None, "initial_sl": None, "tp_target": None,
    "trailing_sl": None, "partial_closed": False, "trades_today": 0,
    "trades_date": None, "session_pnl": 0.0, "last_sl_time": None,
    "trade_amount_used": None, "signal": None, "price": None,
    "rsi": None, "ema21": None, "ema89": None, "atr": None,
    "adx": None, "velas_cruce": None,
}

SIGNAL_LABEL = {
    "BUY": "SENAL LONG", "SELL": "SENAL SHORT",
    "LONG_ACTIVE": "LARGO ACTIVO", "SHORT_ACTIVE": "CORTO ACTIVO",
    "WAIT_RSI": "ESPERAR RSI LONG", "WAIT_RSI_SHORT": "ESPERAR RSI SHORT",
    "CLOSE": "CERRAR LONG", "WAIT": "SIN SENAL",
}

# ── Estado ────────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {}

def save_state(state, now_str, balance=None):
    state['last_run'] = now_str
    state['auto_trade'] = AUTO_TRADE
    if isinstance(balance, dict): state['balance'] = balance
    state['config'] = {
        'leverage': LEVERAGE, 'trade_amount': TRADE_AMOUNT, 'trade_pct': TRADE_PCT,
        'rsi_min': RSI_MIN, 'rsi_max': RSI_MAX, 'trail_mult': TRAIL_MULT,
        'adx_min': ADX_MIN, 'daily_loss_limit': DAILY_LOSS_LIMIT,
        'sl_cooldown_hours': SL_COOLDOWN_HOURS, 'vol_mult': VOL_MULT,
        'max_trades': MAX_TRADES_DAY, 'max_open_pos': MAX_OPEN_POS,
        'max_alt_pos': MAX_ALT_POS, 'use_mtf': USE_MTF,
    }
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)

def get_pair_state(state, symbol):
    if symbol not in state: state[symbol] = dict(EMPTY_STATE)
    return state[symbol]

def count_open_positions(state):
    return sum(1 for p in PAIRS if state.get(p['symbol'], {}).get('position') in ('LONG', 'SHORT'))

def count_alt_positions(state):
    return sum(1 for p in PAIRS if p['fsym'] != 'BTC'
               and state.get(p['symbol'], {}).get('position') in ('LONG', 'SHORT'))

# ── Binance API ───────────────────────────────────────────────────────────────
def _sign(params):
    return hmac.new(API_SECRET.encode(), params.encode(), hashlib.sha256).hexdigest()

def get_futures_balance():
    if not API_KEY: return None
    try:
        ts = int(time.time() * 1000)
        params = f'timestamp={ts}&recvWindow={RECV_WINDOW}'
        sig = _sign(params)
        r = requests.get(
            f'https://fapi.binance.com/fapi/v2/account?{params}&signature={sig}',
            headers={'X-MBX-APIKEY': API_KEY}, timeout=10
        )
        r.raise_for_status()
        acc = r.json()
        result = {
            'total':          round(float(acc.get('totalWalletBalance', 0)), 2),
            'available':      round(float(acc.get('availableBalance', 0)), 2),
            'margin':         round(float(acc.get('totalInitialMargin', 0)), 2),
            'unrealized_pnl': round(float(acc.get('totalUnrealizedProfit', 0)), 2),
            'margin_pct':     0.0,
        }
        if result['total'] > 0:
            result['margin_pct'] = round(result['margin'] / result['total'] * 100, 1)
        print(f" [Balance] Total: ${result['total']} | Libre: ${result['available']} | Margen: {result['margin_pct']}%")
        return result
    except Exception as exc:
        print(f' [Balance] Error: {exc}'); return None

def resolve_trade_amount(balance):
    bal_num = balance['available'] if isinstance(balance, dict) else balance
    if TRADE_PCT > 0 and bal_num is not None:
        ta = round(bal_num * TRADE_PCT, 2)
        print(f' [Sizing] Dinámico: ${ta}'); return ta
    return TRADE_AMOUNT

# FIX: try/except + recvWindow en set_leverage_binance
def set_leverage_binance(symbol):
    if not API_KEY: return
    try:
        ts = int(time.time() * 1000)
        p = f'symbol={symbol}&leverage={LEVERAGE}&timestamp={ts}&recvWindow={RECV_WINDOW}'
        r = requests.post(
            f'https://fapi.binance.com/fapi/v1/leverage?{p}&signature={_sign(p)}',
            headers={'X-MBX-APIKEY': API_KEY}, timeout=10
        )
        data = r.json()
        if 'code' in data and data['code'] < 0:
            print(f' [Leverage] Error {symbol}: {data}')
        else:
            print(f' [Leverage] {symbol} → {LEVERAGE}x OK')
    except Exception as exc:
        print(f' [Leverage] Excepción {symbol}: {exc}')

# FIX: try/except + recvWindow + raise_for_status en market_order
def market_order(symbol, side, qty):
    if not API_KEY: return {}
    try:
        ts = int(time.time() * 1000)
        p = f'symbol={symbol}&side={side}&type=MARKET&quantity={qty}&timestamp={ts}&recvWindow={RECV_WINDOW}'
        r = requests.post(
            f'https://fapi.binance.com/fapi/v1/order?{p}&signature={_sign(p)}',
            headers={'X-MBX-APIKEY': API_KEY}, timeout=10
        )
        data = r.json()
        if 'code' in data and data['code'] < 0:
            print(f' [Order] Error {symbol} {side}: {data}')
        return data
    except Exception as exc:
        print(f' [Order] Excepción {symbol}: {exc}')
        return {}

# ── Datos de mercado ──────────────────────────────────────────────────────────
def fetch_candles(symbol, aggregate=4, limit=200):
    url = (f'https://min-api.cryptocompare.com/data/v2/histohour'
           f'?fsym={symbol}&tsym=USDT&limit={limit}&aggregate={aggregate}&e=Binance')
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    raw = r.json()['Data']['Data']
    return {
        'closes': [float(c['close']) for c in raw],
        'highs':  [float(c['high'])  for c in raw],
        'lows':   [float(c['low'])   for c in raw],
        'vols':   [float(c['volumeto']) for c in raw],
    }

def fetch_daily_candles(symbol, limit=100):
    url = (f'https://min-api.cryptocompare.com/data/v2/histoday'
           f'?fsym={symbol}&tsym=USDT&limit={limit}&e=Binance')
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    raw = r.json()['Data']['Data']
    return {'closes': [float(c['close']) for c in raw]}

# ── Indicadores ───────────────────────────────────────────────────────────────
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
        if diff > 0: avg_gain += diff
        else: avg_loss += abs(diff)
    avg_gain /= period; avg_loss /= period
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
        tr.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    avg = sum(tr[1:period+1]) / period
    result = [None] * period
    result.append(avg)
    for i in range(period+1, len(tr)):
        avg = (avg * (period-1) + tr[i]) / period
        result.append(avg)
    return result

def calc_adx(highs, lows, closes, period=14):
    n = len(closes)
    tr, pdm, ndm = [], [], []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr.append(max(h-l, abs(h-pc), abs(l-pc)))
        up, dn = highs[i]-highs[i-1], lows[i-1]-lows[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn >= up and dn > 0 else 0.0)
    def wilder(arr):
        res = [sum(arr[:period]) / period]
        for v in arr[period:]: res.append(res[-1] * (period-1)/period + v/period)
        return res
    satr, spdm, sndm = wilder(tr), wilder(pdm), wilder(ndm)
    dx = []
    for a, p_, nd in zip(satr, spdm, sndm):
        pdi = 100 * p_ / (a or 1e-9)
        ndi = 100 * nd / (a or 1e-9)
        dx.append(100 * abs(pdi-ndi) / ((pdi+ndi) or 1e-9))
    adx_vals = [sum(dx[:period]) / period]
    for v in dx[period:]: adx_vals.append(adx_vals[-1] * (period-1)/period + v/period)
    pad = n - len(adx_vals)
    return [None] * pad + adx_vals

# ── Señales ───────────────────────────────────────────────────────────────────
def get_signal(ema21, ema89, rsi_vals, rsi_min, rsi_max):
    if len(ema21) < 2: return 'WAIT'
    e21, e89, p21, p89, rsi = ema21[-1], ema89[-1], ema21[-2], ema89[-2], rsi_vals[-1]
    if None in (e21, e89, p21, p89, rsi): return 'WAIT'
    bull, was = e21 > e89, p21 > p89
    if not was and bull: return 'BUY'  if rsi > rsi_min else 'WAIT_RSI'
    if was and not bull: return 'SELL' if rsi < rsi_max else 'WAIT_RSI_SHORT'
    if bull: return 'LONG_ACTIVE'  if rsi > rsi_min else 'WAIT_RSI'
    return   'SHORT_ACTIVE' if rsi < rsi_max else 'WAIT_RSI_SHORT'

def velas_desde_cruce(ema21, ema89, max_look=10):
    for i in range(1, min(max_look+1, len(ema21)-1)):
        a21, a89, b21, b89 = ema21[-i], ema89[-i], ema21[-i-1], ema89[-i-1]
        if None in (a21, a89, b21, b89): continue
        if (b21 <= b89 and a21 > a89) or (b21 >= b89 and a21 < a89): return i - 1
    return max_look + 1

# ── Filtros externos ──────────────────────────────────────────────────────────
def btc_is_bullish():
    try:
        c = fetch_candles('BTC')
        e21 = calc_ema(c['closes'], 21)
        e89 = calc_ema(c['closes'], 89)
        bull = (e21[-1] or 0) > (e89[-1] or 0)
        print(' [BTC 4H] ' + ('ALCISTA' if bull else 'BAJISTA'))
        return bull
    except Exception as exc:
        print(f' [BTC] Error: {exc}'); return True

def pair_daily_is_bullish(fsym):
    try:
        c = fetch_daily_candles(fsym)
        e21 = calc_ema(c['closes'], 21)
        e89 = calc_ema(c['closes'], 89)
        bull = (e21[-1] or 0) > (e89[-1] or 0)
        print(f' [1D {fsym}] ' + ('ALCISTA' if bull else 'BAJISTA'))
        return bull
    except Exception as exc:
        print(f' [1D {fsym}] Error: {exc}'); return True

def volume_confirmed(vols):
    avg = sum(vols[:-1]) / max(len(vols)-1, 1)
    last = vols[-1]
    ok = last >= avg * VOL_MULT
    ratio = round(last / (avg or 1e-9), 2)
    print(f' [Vol] {ratio}x promedio — {"✅" if ok else "⚠️ bajo"}')
    return ok, ratio

def daily_loss_exceeded(state, today):
    total_pnl = sum(
        state.get(p['symbol'], {}).get('session_pnl', 0.0)
        for p in PAIRS
        if state.get(p['symbol'], {}).get('trades_date') == today
    )
    if total_pnl <= DAILY_LOSS_LIMIT:
        print(f' [DAILY LOSS] PnL: ${round(total_pnl,2)} ≤ límite ${DAILY_LOSS_LIMIT}')
        return True, total_pnl
    return False, total_pnl

def sl_cooldown_active(ps, now):
    last_sl = ps.get('last_sl_time')
    if not last_sl: return False, 0
    try:
        sl_time = datetime.fromisoformat(last_sl)
        hours_since = (now - sl_time).total_seconds() / 3600
        if hours_since < SL_COOLDOWN_HOURS:
            remaining = round(SL_COOLDOWN_HOURS - hours_since, 1)
            return True, remaining
    except Exception: pass
    return False, 0

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_msg(text):
    try:
        if not text or len(text.strip()) == 0:
            print(' ⚠️ Mensaje vacío — no enviado'); return
        response = requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT, 'text': text, 'parse_mode': 'HTML'},
            timeout=10
        )
        response.raise_for_status()
    except Exception as exc:
        print(f' Telegram error: {exc}')
        print(f' Mensaje que falló ({len(text)} chars): {repr(text[:300])}')

def escape_html(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def build_msg(parts): return chr(10).join(parts)

def send_session_report(state, now_str, balance, today):
    lines = [f'📊 REPORTE — {now_str}', '']
    total_pnl = 0.0
    for p in PAIRS:
        ps  = state.get(p['symbol'], {})
        pos = ps.get('position', 'FLAT')
        sig = ps.get('signal', 'WAIT')
        price = ps.get('price', 0)
        rsi   = ps.get('rsi', 0)
        adx   = ps.get('adx', 0)
        pnl   = ps.get('session_pnl', 0.0)
        total_pnl += pnl if ps.get('trades_date') == today else 0
        icon = '🟢' if pos == 'LONG' else ('🔴' if pos == 'SHORT' else '⚪')
        s = '+' if pnl >= 0 else ''
        lines.append(f"{icon} {p['fsym']} ${price} RSI:{rsi} ADX:{adx} | {sig} | PnL:{s}{round(pnl,2)}")
    lines.append('')
    s = '+' if total_pnl >= 0 else ''
    lines.append(f'💰 PnL hoy: {s}{round(total_pnl,2)} USDT')
    lines.append(f'🚨 Límite diario: ${DAILY_LOSS_LIMIT}')
    if isinstance(balance, dict):
        lines.append(f"💼 Balance: ${balance['total']} | Libre: ${balance['available']} | Margen: {balance['margin_pct']}%")
    send_msg(build_msg(lines))

# ── Operaciones LONG ──────────────────────────────────────────────────────────
def open_long(pair, ps, price, sl, tp, ta):
    sym, fsym, dec = pair['symbol'], pair['fsym'], pair['dec']
    qty     = round(ta * LEVERAGE / price, dec)
    halfway = price + (tp - price) * 0.5
    set_leverage_binance(sym)
    res = market_order(sym, 'BUY', qty)
    oid = res.get('orderId')
    sizing = f"{round(TRADE_PCT*100,1)}% balance" if TRADE_PCT > 0 else 'fijo'
    if oid:
        ps.update({'position': 'LONG', 'entry_price': price, 'entry_qty': qty,
                   'initial_sl': sl, 'tp_target': tp, 'trailing_sl': sl,
                   'partial_closed': False, 'trades_today': ps.get('trades_today', 0) + 1,
                   'trade_amount_used': ta})
        send_msg(build_msg([
            f'🟢 LONG abierto — {fsym}/USDT',
            f' Qty: {qty} @ ${round(price,2)}',
            f' Monto: ${ta} ({sizing}) | Lev: {LEVERAGE}x',
            f' SL: ${round(sl,2)} | TP: ${round(tp,2)}',
            f' 50% TP: ${round(halfway,2)} | ID: {oid}',
        ]))
    else:
        send_msg(f"❌ ERROR BUY {fsym}: {res.get('msg', str(res))}")

def close_position(pair, ps, price, reason, partial=False):
    sym, fsym, dec = pair['symbol'], pair['fsym'], pair['dec']
    entry = ps['entry_price'] or price
    qty   = ps.get('entry_qty') or 0
    ta    = ps.get('trade_amount_used', TRADE_AMOUNT)
    factor    = 0.5 if partial else (0.5 if ps['partial_closed'] else 1.0)
    sell_qty  = round(qty * factor, dec)
    if sell_qty <= 0: return
    set_leverage_binance(sym)
    res = market_order(sym, 'SELL', sell_qty)
    oid = res.get('orderId')
    pnl_pct = (price - entry) / entry * 100
    pnl_u   = ta * factor * LEVERAGE * pnl_pct / 100
    ps['session_pnl'] = ps.get('session_pnl', 0) + pnl_u
    if oid:
        sign  = '+' if pnl_u >= 0 else ''
        label = 'Cierre parcial LONG' if partial else 'LONG cerrado'
        send_msg(build_msg([
            f'{label} — {fsym}/USDT', f' Razón: {reason}',
            f' SELL {sell_qty} @ ${round(price,2)}',
            f' PnL: {sign}{round(pnl_u,2)} USDT | Sesión: {sign}{round(ps["session_pnl"],2)} USDT',
            f' ID: {oid}',
        ]))
        if not partial:
            ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                       'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                       'partial_closed': False, 'trade_amount_used': None})
    else:
        send_msg(f"❌ ERROR SELL {fsym}: {res.get('msg', str(res))}")

def manage_open(pair, ps, price, atr, now_str, now_dt):
    entry, tp, trail = ps['entry_price'], ps['tp_target'], ps['trailing_sl']
    partial = ps['partial_closed']
    if None in (entry, tp, trail): return False
    fsym = pair['fsym']
    ta   = ps.get('trade_amount_used', TRADE_AMOUNT)
    pnl_pct  = (price - entry) / entry * 100
    pnl_u    = ta * LEVERAGE * pnl_pct / 100
    sign     = '+' if pnl_u >= 0 else ''
    new_trail = price - atr * TRAIL_MULT
    if new_trail > trail:
        ps['trailing_sl'] = new_trail; trail = new_trail
    halfway = entry + (tp - entry) * 0.5
    if not partial and price >= halfway:
        ps['partial_closed'] = True
        ps['trailing_sl']    = max(trail, entry)
        send_msg(build_msg([
            f'📍 50% TP LONG — {fsym}/USDT',
            f' Precio: ${round(price,2)} | SL → BE: ${round(entry,2)}',
            f' PnL: {sign}{round(pnl_u,2)} USDT | {now_str}',
        ]))
        if AUTO_TRADE and API_KEY: close_position(pair, ps, price, '50% TP', partial=True)
        return True
    if price <= trail:
        icon = 'GANANCIA' if pnl_u >= 0 else 'PÉRDIDA'
        ps['last_sl_time'] = now_dt.isoformat()
        send_msg(build_msg([
            f'🛑 TRAILING SL LONG — {fsym}/USDT',
            f' Precio: ${round(price,2)} | Trail: ${round(trail,2)}',
            f' {icon}: {sign}{round(pnl_pct*LEVERAGE,2)}% → {sign}{round(pnl_u,2)} USDT',
            f' Cooldown: {SL_COOLDOWN_HOURS}h | {now_str}',
        ]))
        if AUTO_TRADE and API_KEY: close_position(pair, ps, price, 'Trailing SL')
        else:
            ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                       'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                       'partial_closed': False, 'trade_amount_used': None})
        return True
    return False

# ── Operaciones SHORT ─────────────────────────────────────────────────────────
def open_short(pair, ps, price, sl, tp, ta):
    sym, fsym, dec = pair['symbol'], pair['fsym'], pair['dec']
    qty     = round(ta * LEVERAGE / price, dec)
    halfway = price - (price - tp) * 0.5
    set_leverage_binance(sym)
    res = market_order(sym, 'SELL', qty)
    oid = res.get('orderId')
    sizing = f"{round(TRADE_PCT*100,1)}% balance" if TRADE_PCT > 0 else 'fijo'
    if oid:
        ps.update({'position': 'SHORT', 'entry_price': price, 'entry_qty': qty,
                   'initial_sl': sl, 'tp_target': tp, 'trailing_sl': sl,
                   'partial_closed': False, 'trades_today': ps.get('trades_today', 0) + 1,
                   'trade_amount_used': ta})
        send_msg(build_msg([
            f'🔴 SHORT abierto — {fsym}/USDT',
            f' Qty: {qty} @ ${round(price,2)}',
            f' Monto: ${ta} ({sizing}) | Lev: {LEVERAGE}x',
            f' SL: ${round(sl,2)} | TP: ${round(tp,2)}',
            f' 50% TP: ${round(halfway,2)} | ID: {oid}',
        ]))
    else:
        send_msg(f"❌ ERROR SHORT {fsym}: {res.get('msg', str(res))}")

def close_short(pair, ps, price, reason, partial=False):
    sym, fsym, dec = pair['symbol'], pair['fsym'], pair['dec']
    entry   = ps['entry_price'] or price
    qty     = ps.get('entry_qty') or 0
    ta      = ps.get('trade_amount_used', TRADE_AMOUNT)
    factor  = 0.5 if partial else (0.5 if ps['partial_closed'] else 1.0)
    buy_qty = round(qty * factor, dec)
    if buy_qty <= 0: return
    set_leverage_binance(sym)
    res = market_order(sym, 'BUY', buy_qty)
    oid = res.get('orderId')
    pnl_pct = (entry - price) / entry * 100
    pnl_u   = ta * factor * LEVERAGE * pnl_pct / 100
    ps['session_pnl'] = ps.get('session_pnl', 0) + pnl_u
    if oid:
        sign  = '+' if pnl_u >= 0 else ''
        label = 'Cierre parcial SHORT' if partial else 'SHORT cerrado'
        send_msg(build_msg([
            f'{label} — {fsym}/USDT', f' Razón: {reason}',
            f' BUY {buy_qty} @ ${round(price,2)}',
            f' PnL: {sign}{round(pnl_u,2)} USDT | Sesión: {sign}{round(ps["session_pnl"],2)} USDT',
            f' ID: {oid}',
        ]))
        if not partial:
            ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                       'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                       'partial_closed': False, 'trade_amount_used': None})
    else:
        send_msg(f"❌ ERROR CLOSE SHORT {fsym}: {res.get('msg', str(res))}")

def manage_short(pair, ps, price, atr, now_str, now_dt):
    entry, tp, trail = ps['entry_price'], ps['tp_target'], ps['trailing_sl']
    partial = ps['partial_closed']
    if None in (entry, tp, trail): return False
    fsym = pair['fsym']
    ta   = ps.get('trade_amount_used', TRADE_AMOUNT)
    pnl_pct   = (entry - price) / entry * 100
    pnl_u     = ta * LEVERAGE * pnl_pct / 100
    sign      = '+' if pnl_u >= 0 else ''
    new_trail = price + atr * TRAIL_MULT
    if new_trail < trail:
        ps['trailing_sl'] = new_trail; trail = new_trail
    halfway = entry - (entry - tp) * 0.5
    if not partial and price <= halfway:
        ps['partial_closed'] = True
        ps['trailing_sl']    = min(trail, entry)
        send_msg(build_msg([
            f'📍 50% TP SHORT — {fsym}/USDT',
            f' Precio: ${round(price,2)} | SL → BE: ${round(entry,2)}',
            f' PnL: {sign}{round(pnl_u,2)} USDT | {now_str}',
        ]))
        if AUTO_TRADE and API_KEY: close_short(pair, ps, price, '50% TP', partial=True)
        return True
    if price >= trail:
        icon = 'GANANCIA' if pnl_u >= 0 else 'PÉRDIDA'
        ps['last_sl_time'] = now_dt.isoformat()
        send_msg(build_msg([
            f'🛑 TRAILING SL SHORT — {fsym}/USDT',
            f' Precio: ${round(price,2)} | Trail: ${round(trail,2)}',
            f' {icon}: {sign}{round(pnl_pct*LEVERAGE,2)}% → {sign}{round(pnl_u,2)} USDT',
            f' Cooldown: {SL_COOLDOWN_HOURS}h | {now_str}',
        ]))
        if AUTO_TRADE and API_KEY: close_short(pair, ps, price, 'Trailing SL')
        else:
            ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                       'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                       'partial_closed': False, 'trade_amount_used': None})
        return True
    return False

# ── Lógica por par ────────────────────────────────────────────────────────────
def process_pair(pair, ps, today, now_str, now_dt, btc_bull, balance, state):
    sym, fsym = pair['symbol'], pair['fsym']
    if ps.get('trades_date') != today:
        ps['trades_today'] = 0; ps['trades_date'] = today

    c = fetch_candles(fsym)
    closes, highs, lows, vols = c['closes'], c['highs'], c['lows'], c['vols']
    price  = closes[-1]
    e21    = calc_ema(closes, 21)
    e89    = calc_ema(closes, 89)
    rsi14  = calc_rsi(closes, 14)
    atr14  = calc_atr(highs, lows, closes, 14)
    adx14  = calc_adx(highs, lows, closes, 14)
    le21, le89 = e21[-1] or 0.0, e89[-1] or 0.0
    lr, la     = rsi14[-1] or 0.0, atr14[-1] or 0.0
    la_adx     = adx14[-1] or 0.0

    sl_long  = price - la * 1.5;  tp_long  = price + la * 3.0
    sl_short = price + la * 1.5;  tp_short = price - la * 3.0

    sig = get_signal(e21, e89, rsi14, RSI_MIN, RSI_MAX)
    vcr = velas_desde_cruce(e21, e89)

    ps.update({'signal': sig, 'price': round(price,2), 'rsi': round(lr,1),
               'ema21': round(le21,2), 'ema89': round(le89,2),
               'atr': round(la,2), 'adx': round(la_adx,1), 'velas_cruce': vcr})
    pos = ps['position']
    print(f' [{sym}] {sig} | ${round(price,2)} | RSI:{round(lr,1)} | ADX:{round(la_adx,1)} | pos:{pos}')

    if pos == 'LONG':  manage_open (pair, ps, price, la, now_str, now_dt)
    if pos == 'SHORT': manage_short(pair, ps, price, la, now_str, now_dt)

    # Cierre por señal contraria
    if sig in ('SELL', 'WAIT_RSI_SHORT') and ps['position'] == 'LONG':
        ep   = ps['entry_price'] or price
        ta_u = ps.get('trade_amount_used', TRADE_AMOUNT)
        pnl_u = ta_u * LEVERAGE * (price - ep) / ep * 100 / 100
        s = '+' if pnl_u >= 0 else ''
        send_msg(build_msg([
            f'🔴 CRUCE BAJISTA — {fsym}/USDT 4H', '❌ CERRAR POSICIÓN LONG',
            f'📊 Precio: ${round(price,2)} | PnL est.: {s}{round(pnl_u,2)} USDT',
            f'📉 EMA21 cruzó por debajo de EMA89 | {now_str}',
        ]))
        if AUTO_TRADE and API_KEY: close_position(pair, ps, price, 'Cruce bajista')
        else: ps.update({'position':'FLAT','entry_price':None,'entry_qty':None,
                         'initial_sl':None,'tp_target':None,'trailing_sl':None,
                         'partial_closed':False,'trade_amount_used':None})

    if sig in ('BUY', 'WAIT_RSI') and ps['position'] == 'SHORT':
        ep   = ps['entry_price'] or price
        ta_u = ps.get('trade_amount_used', TRADE_AMOUNT)
        pnl_u = ta_u * LEVERAGE * (ep - price) / ep * 100 / 100
        s = '+' if pnl_u >= 0 else ''
        send_msg(build_msg([
            f'🟢 CRUCE ALCISTA — {fsym}/USDT 4H', '❌ CERRAR POSICIÓN SHORT',
            f'📊 Precio: ${round(price,2)} | PnL est.: {s}{round(pnl_u,2)} USDT',
            f'📈 EMA21 cruzó por encima de EMA89 | {now_str}',
        ]))
        if AUTO_TRADE and API_KEY: close_short(pair, ps, price, 'Cruce alcista')
        else: ps.update({'position':'FLAT','entry_price':None,'entry_qty':None,
                         'initial_sl':None,'tp_target':None,'trailing_sl':None,
                         'partial_closed':False,'trade_amount_used':None})

    if sig == ps.get('last_signal'): return ps

    # Notificaciones de señal
    ta_now = resolve_trade_amount(balance)
    btc_ok_long  = btc_bull or fsym == 'BTC'
    btc_ok_short = (not btc_bull) or fsym == 'BTC'

    halfway_l  = price + (tp_long  - price) * 0.5
    halfway_s  = price - (price - tp_short) * 0.5
    sl_pct_l   = round((sl_long  - price) / price * 100, 1)
    tp_pct_l   = round((tp_long  - price) / price * 100, 1)
    sl_pct_s   = round((sl_short - price) / price * 100, 1)
    tp_pct_s   = round((tp_short - price) / price * 100, 1)
    pnl_sl_l   = round(ta_now * LEVERAGE * abs(sl_pct_l) / 100, 2)
    pnl_tp_l   = round(ta_now * LEVERAGE * tp_pct_l / 100, 2)
    pnl_sl_s   = round(ta_now * LEVERAGE * abs(sl_pct_s) / 100, 2)
    pnl_tp_s   = round(ta_now * LEVERAGE * abs(tp_pct_s) / 100, 2)

    parts = None
    if sig == 'BUY':
        dec_txt = '✅ ABRIR LONG' if btc_ok_long else '⛔ NO OPERAR — BTC bajista'
        parts = [
            f'🟢 SEÑAL LONG — {fsym}/USDT 4H', '', dec_txt, '',
            f'📊 ${round(price,2)} | EMA21: ${round(le21,2)} | EMA89: ${round(le89,2)}',
            f'📐 RSI: {round(lr,1)} (mín {RSI_MIN}) | ADX: {round(la_adx,1)}',
            '', '━━━ SETUP ━━━',
            f'🛑 SL: ${round(sl_long,2)} ({sl_pct_l}%) → -${pnl_sl_l}',
            f'🎯 TP: ${round(tp_long,2)} (+{tp_pct_l}%) → +${pnl_tp_l}',
            f'📍 50%: ${round(halfway_l,2)} | 🔄 Trailing activo',
            f'⚡ Lev: {LEVERAGE}x | ${ta_now} | R:R 1:2 | {now_str}',
        ]
    elif sig == 'SELL':
        dec_txt = '✅ ABRIR SHORT' if btc_ok_short else '⛔ NO OPERAR — BTC alcista'
        parts = [
            f'🔴 SEÑAL SHORT — {fsym}/USDT 4H', '', dec_txt, '',
            f'📊 ${round(price,2)} | EMA21: ${round(le21,2)} | EMA89: ${round(le89,2)}',
            f'📐 RSI: {round(lr,1)} (máx {RSI_MAX}) | ADX: {round(la_adx,1)}',
            '', '━━━ SETUP ━━━',
            f'🛑 SL: ${round(sl_short,2)} (+{sl_pct_s}%) → -${pnl_sl_s}',
            f'🎯 TP: ${round(tp_short,2)} ({tp_pct_s}%) → +${pnl_tp_s}',
            f'📍 50%: ${round(halfway_s,2)} | 🔄 Trailing activo',
            f'⚡ Lev: {LEVERAGE}x | ${ta_now} | R:R 1:2 | {now_str}',
        ]
    elif sig == 'WAIT_RSI':
        falta = round(RSI_MIN - lr, 1)
        parts = [
            f'🟡 CRUCE ALCISTA — {fsym}/USDT 4H', '⏳ Esperar RSI',
            f'📐 RSI: {round(lr,1)} (faltan +{falta} para >{RSI_MIN})',
            f'🕯 Velas desde cruce: {vcr} | ADX: {round(la_adx,1)}',
            f'🛑 SL ref: ${round(sl_long,2)} | 🎯 TP ref: ${round(tp_long,2)}',
            f'⏰ {now_str}',
        ]
    elif sig == 'WAIT_RSI_SHORT':
        falta = round(lr - RSI_MAX, 1)
        parts = [
            f'🟠 CRUCE BAJISTA — {fsym}/USDT 4H', '⏳ Esperar RSI',
            f'📐 RSI: {round(lr,1)} (sobran {falta} para <{RSI_MAX})',
            f'🕯 Velas desde cruce: {vcr} | ADX: {round(la_adx,1)}',
            f'🛑 SL ref: ${round(sl_short,2)} | 🎯 TP ref: ${round(tp_short,2)}',
            f'⏰ {now_str}',
        ]
    elif sig == 'LONG_ACTIVE':
        dec_txt = f'⚡ Entrada tardía válida ({vcr} velas)' if vcr <= 3 else f'⏸ Cruce antiguo ({vcr} velas)'
        parts = [
            f'🔵 LARGO ACTIVO — {fsym}/USDT 4H', dec_txt,
            f'📊 ${round(price,2)} | RSI: {round(lr,1)} | ADX: {round(la_adx,1)}',
            f'🛑 SL: ${round(sl_long,2)} | 🎯 TP: ${round(tp_long,2)}',
            f'⏰ {now_str}',
        ]
    elif sig == 'SHORT_ACTIVE':
        dec_txt = f'⚡ Entrada tardía válida ({vcr} velas)' if vcr <= 3 else f'⏸ Cruce antiguo ({vcr} velas)'
        parts = [
            f'🟠 CORTO ACTIVO — {fsym}/USDT 4H', dec_txt,
            f'📊 ${round(price,2)} | RSI: {round(lr,1)} | ADX: {round(la_adx,1)}',
            f'🛑 SL: ${round(sl_short,2)} | 🎯 TP: ${round(tp_short,2)}',
            f'⏰ {now_str}',
        ]

    if parts is None:
        ps['last_signal'] = sig; return ps

    send_msg(build_msg(parts))

    # Ejecución automática con todos los filtros
    late_long  = sig == 'LONG_ACTIVE'  and vcr <= 2
    late_short = sig == 'SHORT_ACTIVE' and vcr <= 2
    want_long  = (sig == 'BUY'  or late_long)  and pos == 'FLAT'
    want_short = (sig == 'SELL' or late_short) and pos == 'FLAT'

    if AUTO_TRADE and API_KEY and (want_long or want_short):
        direction = 'LONG' if want_long else 'SHORT'

        # 1. Cooldown
        in_cd, cd_rem = sl_cooldown_active(ps, now_dt)
        if in_cd:
            send_msg(f'⏳ {fsym} en cooldown por SL — {cd_rem}h restantes'); ps['last_signal'] = sig; return ps

        # 2. ADX
        if la_adx < ADX_MIN:
            send_msg(f'⏸ {fsym}: ADX={round(la_adx,1)} < {ADX_MIN} — mercado lateral'); ps['last_signal'] = sig; return ps

        # 3. MTF diario
        if USE_MTF:
            daily_bull = pair_daily_is_bullish(fsym)
            if want_long and not daily_bull:
                send_msg(f'⛔ BLOQUEADO LONG {fsym}: 1D bajista (MTF)'); ps['last_signal'] = sig; return ps
            if want_short and daily_bull:
                send_msg(f'⛔ BLOQUEADO SHORT {fsym}: 1D alcista (MTF)'); ps['last_signal'] = sig; return ps

        # 4. Volumen
        if USE_VOLUME_FILTER:
            vol_ok, vol_ratio = volume_confirmed(vols)
            if not vol_ok:
                send_msg(f'⚠️ {fsym}: volumen bajo ({vol_ratio}x) — entrada omitida'); ps['last_signal'] = sig; return ps

        # 5. Filtro BTC
        if want_long and not btc_ok_long:
            send_msg(f'⛔ BLOQUEADO LONG {fsym}: BTC bajista'); ps['last_signal'] = sig; return ps
        if want_short and not btc_ok_short:
            send_msg(f'⛔ BLOQUEADO SHORT {fsym}: BTC alcista'); ps['last_signal'] = sig; return ps

        # 6. Correlación alts
        if fsym != 'BTC' and count_alt_positions(state) >= MAX_ALT_POS:
            send_msg(f'⛔ {fsym}: límite de {MAX_ALT_POS} alts simultáneas'); ps['last_signal'] = sig; return ps

        # 7. Límite diario de trades
        if ps['trades_today'] >= MAX_TRADES_DAY:
            send_msg(f'⛔ {fsym}: límite {MAX_TRADES_DAY} trades/día'); ps['last_signal'] = sig; return ps

        # ✅ Todos los filtros pasados — abrir posición
        if want_long:  open_long (pair, ps, price, sl_long,  tp_long,  ta_now)
        if want_short: open_short(pair, ps, price, sl_short, tp_short, ta_now)

    ps['last_signal'] = sig
    return ps

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now_dt  = datetime.now(timezone.utc)
    today   = now_dt.strftime('%Y-%m-%d')
    now_str = now_dt.strftime('%Y-%m-%d %H:%M UTC')
    sizing  = f'TRADE_PCT={TRADE_PCT*100}%' if TRADE_PCT > 0 else f'TRADE_AMOUNT=${TRADE_AMOUNT}'
    print('=' * 55)
    print(f' EMA Bot v10.1 | {now_str}')
    print(f' Sizing: {sizing} | Lev: {LEVERAGE}x')
    print(f' RSI LONG>{RSI_MIN} SHORT<{RSI_MAX} | ADX>{ADX_MIN}')
    print(f' MaxPos: {MAX_OPEN_POS} | MaxAlts: {MAX_ALT_POS} | MaxTrades: {MAX_TRADES_DAY}')
    print(f' DailyLoss: ${DAILY_LOSS_LIMIT} | SL Cooldown: {SL_COOLDOWN_HOURS}h')
    print(f' MTF: {USE_MTF} | VolFilter: {USE_VOLUME_FILTER}')
    print('=' * 55)

    state   = load_state()
    balance = get_futures_balance() if API_KEY else None

    loss_exceeded, daily_pnl = daily_loss_exceeded(state, today)
    if loss_exceeded:
        send_msg(build_msg([
            '🚨 STOP DIARIO ACTIVADO',
            f'PnL del día: ${round(daily_pnl,2)}',
            f'Límite: ${DAILY_LOSS_LIMIT}',
            f'Bot bloqueado hasta mañana. | {now_str}',
        ]))
        save_state(state, now_str, balance)
        return

    print('[Filtro BTC 4H]')
    btc_bull = btc_is_bullish()
    open_now = count_open_positions(state)
    print(f'[Posiciones abiertas: {open_now}/{MAX_OPEN_POS}]')

    for pair in PAIRS:
        sym = pair['symbol']
        print(chr(10) + '-> ' + sym)
        ps = get_pair_state(state, sym)
        if ps.get('position') == 'FLAT' and open_now >= MAX_OPEN_POS:
            print(' SALTADO: MAX_OPEN_POSITIONS'); state[sym] = ps; continue
        try:
            state[sym] = process_pair(pair, ps, today, now_str, now_dt, btc_bull, balance, state)
        except Exception as exc:
            print(f' ERROR {sym}: {exc}')
            send_msg(f'❌ Error {sym}: {exc}')
        time.sleep(2)

    save_state(state, now_str, balance)
    send_session_report(state, now_str, balance, today)
    print(chr(10) + 'Completado — ' + now_str)

if __name__ == '__main__':
    main()
