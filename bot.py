import requests, json, os, hmac, hashlib, time
from datetime import datetime, timezone, timedelta

# ── Configuración ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT       = os.environ['TELEGRAM_CHAT_ID']
API_KEY             = os.environ.get('BINANCE_API_KEY', '')
API_SECRET          = os.environ.get('BINANCE_API_SECRET', '')
AUTO_TRADE          = os.environ.get('AUTO_TRADE', 'false').lower() == 'true'
TRADE_AMOUNT        = float(os.environ.get('TRADE_AMOUNT', '15'))
TRADE_PCT           = float(os.environ.get('TRADE_PCT', '0'))
LEVERAGE            = int(os.environ.get('LEVERAGE', '10'))
MAX_TRADES_DAY      = int(os.environ.get('MAX_DAILY_TRADES', '3'))
MAX_OPEN_POS        = int(os.environ.get('MAX_OPEN_POSITIONS', '3'))
MAX_ALT_POS         = int(os.environ.get('MAX_ALT_POSITIONS', '2'))
RSI_MIN             = float(os.environ.get('RSI_ENTRY_MIN', '53'))
RSI_MAX             = float(os.environ.get('RSI_ENTRY_MAX', '47'))
TRAIL_MULT          = float(os.environ.get('TRAIL_ATR_MULT', '1.5'))
ADX_MIN             = float(os.environ.get('ADX_MIN', '25'))
DAILY_LOSS_LIMIT    = float(os.environ.get('DAILY_LOSS_LIMIT', '-30'))
SL_COOLDOWN_HOURS   = int(os.environ.get('SL_COOLDOWN_HOURS', '12'))
VOL_MULT            = float(os.environ.get('VOL_MULT', '1.2'))
USE_MTF             = os.environ.get('USE_MTF', 'true').lower() == 'true'
USE_VOLUME_FILTER   = os.environ.get('USE_VOLUME_FILTER', 'true').lower() == 'true'

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
    state['last_run']   = now_str
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
def get_futures_balance():
    if not API_KEY: return None
    try:
        ts     = int(time.time() * 1000)
        params = 'timestamp=' + str(ts)
        sig    = hmac.new(API_SECRET.encode(), params.encode(), hashlib.sha256).hexdigest()
        r      = requests.get('https://fapi.binance.com/fapi/v2/account?' + params + '&signature=' + sig,
                              headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
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
        print(' [Balance] Total: $' + str(result['total']) +
              ' | Libre: $' + str(result['available']) +
              ' | Margen: ' + str(result['margin_pct']) + '%')
        return result
    except Exception as exc:
        print(' [Balance] Error: ' + str(exc)); return None

def resolve_trade_amount(balance):
    bal_num = balance['available'] if isinstance(balance, dict) else balance
    if TRADE_PCT > 0 and bal_num is not None:
        ta = round(bal_num * TRADE_PCT, 2)
        print(' [Sizing] Dinámico: $' + str(ta)); return ta
    return TRADE_AMOUNT

def _sign(params):
    return hmac.new(API_SECRET.encode(), params.encode(), hashlib.sha256).hexdigest()

def set_leverage_binance(symbol):
    ts = int(time.time() * 1000)
    p  = 'symbol=' + symbol + '&leverage=' + str(LEVERAGE) + '&timestamp=' + str(ts)
    requests.post('https://fapi.binance.com/fapi/v1/leverage?' + p + '&signature=' + _sign(p),
                  headers={'X-MBX-APIKEY': API_KEY}, timeout=10)

def market_order(symbol, side, qty):
    ts = int(time.time() * 1000)
    p  = 'symbol=' + symbol + '&side=' + side + '&type=MARKET&quantity=' + str(qty) + '&timestamp=' + str(ts)
    r  = requests.post('https://fapi.binance.com/fapi/v1/order?' + p + '&signature=' + _sign(p),
                       headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
    return r.json()

# ── Datos de mercado ──────────────────────────────────────────────────────────
def fetch_candles(symbol, aggregate=4, limit=200):
    url = (f'https://min-api.cryptocompare.com/data/v2/histohour'
           f'?fsym={symbol}&tsym=USDT&limit={limit}&aggregate={aggregate}&e=Binance')
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    raw = r.json()['Data']['Data']
    return {
        'closes': [float(c['close'])    for c in raw],
        'highs':  [float(c['high'])     for c in raw],
        'lows':   [float(c['low'])      for c in raw],
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
        else:        avg_loss += abs(diff)
    avg_gain /= period; avg_loss /= period
    result.append(100 - 100 / (1 + avg_gain / (avg_loss or 1e-9)))
    for i in range(period + 1, len(data)):
        diff     = data[i] - data[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff,  0)) / period
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
        pdm.append(up if up > dn  and up > 0  else 0.0)
        ndm.append(dn if dn >= up and dn > 0  else 0.0)
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
    if not was and bull:  return 'BUY'          if rsi > rsi_min else 'WAIT_RSI'
    if was  and not bull: return 'SELL'         if rsi < rsi_max else 'WAIT_RSI_SHORT'
    if bull:              return 'LONG_ACTIVE'  if rsi > rsi_min else 'WAIT_RSI'
    return                       'SHORT_ACTIVE' if rsi < rsi_max else 'WAIT_RSI_SHORT'

def velas_desde_cruce(ema21, ema89, max_look=10):
    for i in range(1, min(max_look+1, len(ema21)-1)):
        a21, a89, b21, b89 = ema21[-i], ema89[-i], ema21[-i-1], ema89[-i-1]
        if None in (a21, a89, b21, b89): continue
        if (b21 <= b89 and a21 > a89) or (b21 >= b89 and a21 < a89): return i - 1
    return max_look + 1

# ── Filtros externos ──────────────────────────────────────────────────────────
def btc_is_bullish():
    try:
        c    = fetch_candles('BTC')
        e21  = calc_ema(c['closes'], 21)
        e89  = calc_ema(c['closes'], 89)
        bull = (e21[-1] or 0) > (e89[-1] or 0)
        print(' [BTC 4H] ' + ('ALCISTA' if bull else 'BAJISTA'))
        return bull
    except Exception as exc:
        print(' [BTC] Error: ' + str(exc)); return True

def pair_daily_is_bullish(fsym):
    try:
        c    = fetch_daily_candles(fsym)
        e21  = calc_ema(c['closes'], 21)
        e89  = calc_ema(c['closes'], 89)
        bull = (e21[-1] or 0) > (e89[-1] or 0)
        print(' [1D ' + fsym + '] ' + ('ALCISTA' if bull else 'BAJISTA'))
        return bull
    except Exception as exc:
        print(' [1D ' + fsym + '] Error: ' + str(exc)); return True

def volume_confirmed(vols):
    avg  = sum(vols[:-1]) / max(len(vols)-1, 1)
    last = vols[-1]
    ok   = last >= avg * VOL_MULT
    ratio = round(last / (avg or 1e-9), 2)
    print(' [Vol] ' + str(ratio) + 'x promedio — ' + ('✅' if ok else '⚠️ bajo'))
    return ok, ratio

def daily_loss_exceeded(state, today):
    total_pnl = sum(
        state.get(p['symbol'], {}).get('session_pnl', 0.0)
        for p in PAIRS
        if state.get(p['symbol'], {}).get('trades_date') == today
    )
    if total_pnl <= DAILY_LOSS_LIMIT:
        print(' [DAILY LOSS] PnL: $' + str(round(total_pnl, 2)) + ' ≤ límite $' + str(DAILY_LOSS_LIMIT))
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
        # Validar que el mensaje no esté vacío
        if not text or len(text.strip()) == 0:
            print(' ⚠️ Mensaje vacío — no enviado a Telegram')
            return

        # Intentar enviar
        response = requests.post(
            'https://api.telegram.org/bot' + TELEGRAM_TOKEN + '/sendMessage',
            json={'chat_id': TELEGRAM_CHAT, 'text': text, 'parse_mode': 'HTML'},
            timeout=10
        )
        response.raise_for_status()

    except Exception as exc:
        print(' Telegram error: ' + str(exc))
        print(' Mensaje que falló (' + str(len(text)) + ' chars): ' + repr(text[:300]))


def escape_html(text):
    """Escapa caracteres especiales para HTML en Telegram"""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def build_msg(parts): return chr(10).join(parts)

def send_session_report(state, now_str, balance, today):
    lines = ['📊 <b>REPORTE — ' + now_str + '</b>', '']
    total_pnl = 0.0
    for p in PAIRS:
        ps    = state.get(p['symbol'], {})
        pos   = ps.get('position', 'FLAT')
        sig   = ps.get('signal', 'WAIT')
        price = ps.get('price', 0)
        rsi   = ps.get('rsi', 0)
        adx   = ps.get('adx', 0)
        pnl   = ps.get('session_pnl', 0.0)
        total_pnl += pnl if ps.get('trades_date') == today else 0
        icon  = '🟢' if pos == 'LONG' else ('🔴' if pos == 'SHORT' else '⚪')
        s     = '+' if pnl >= 0 else ''
        lines.append(icon + ' <b>' + p['fsym'] + '</b> $' + str(price) +
                     ' RSI:' + str(rsi) + ' ADX:' + str(adx) +
                     ' | ' + sig +
                     ' | PnL:' + s + str(round(pnl, 2)))
    lines.append('')
    s = '+' if total_pnl >= 0 else ''
    lines.append('💰 PnL hoy: <b>' + s + str(round(total_pnl, 2)) + ' USDT</b>')
    lines.append('🚨 Límite diario: $' + str(DAILY_LOSS_LIMIT))
    if isinstance(balance, dict):
        lines.append('💼 Balance: $' + str(balance['total']) +
                     ' | Libre: $' + str(balance['available']) +
                     ' | Margen: ' + str(balance['margin_pct']) + '%')
    send_msg(build_msg(lines))

# ── Operaciones LONG ──────────────────────────────────────────────────────────
def open_long(pair, ps, price, sl, tp, ta):
    sym, fsym, dec = pair['symbol'], pair['fsym'], pair['dec']
    qty     = round(ta * LEVERAGE / price, dec)
    halfway = price + (tp - price) * 0.5
    set_leverage_binance(sym)
    res    = market_order(sym, 'BUY', qty)
    oid    = res.get('orderId')
    sizing = str(round(TRADE_PCT*100,1)) + '% balance' if TRADE_PCT > 0 else 'fijo'
    if oid:
        ps.update({'position': 'LONG', 'entry_price': price, 'entry_qty': qty,
                   'initial_sl': sl, 'tp_target': tp, 'trailing_sl': sl,
                   'partial_closed': False, 'trades_today': ps.get('trades_today', 0) + 1,
                   'trade_amount_used': ta})
        send_msg(build_msg([
            '🟢 LONG abierto — ' + fsym + '/USDT',
            ' Qty: ' + str(qty) + ' @ $' + str(round(price, 2)),
            ' Monto: $' + str(ta) + ' (' + sizing + ') | Lev: ' + str(LEVERAGE) + 'x',
            ' SL: $' + str(round(sl, 2)) + ' | TP: $' + str(round(tp, 2)),
            ' 50% TP: $' + str(round(halfway, 2)) + ' | ID: ' + str(oid),
        ]))
    else:
        send_msg('❌ ERROR BUY ' + fsym + ': ' + res.get('msg', str(res)))

def close_position(pair, ps, price, reason, partial=False):
    sym, fsym, dec = pair['symbol'], pair['fsym'], pair['dec']
    entry    = ps['entry_price'] or price
    qty      = ps.get('entry_qty') or 0
    ta       = ps.get('trade_amount_used', TRADE_AMOUNT)
    factor   = 0.5 if partial else (0.5 if ps['partial_closed'] else 1.0)
    sell_qty = round(qty * factor, dec)
    if sell_qty <= 0: return
    set_leverage_binance(sym)
    res     = market_order(sym, 'SELL', sell_qty)
    oid     = res.get('orderId')
    pnl_pct = (price - entry) / entry * 100
    pnl_u   = ta * factor * LEVERAGE * pnl_pct / 100
    ps['session_pnl'] = ps.get('session_pnl', 0) + pnl_u
    if oid:
        sign  = '+' if pnl_u >= 0 else ''
        label = 'Cierre parcial LONG' if partial else 'LONG cerrado'
        send_msg(build_msg([
            label + ' — ' + fsym + '/USDT',
            ' Razón: ' + reason,
            ' SELL ' + str(sell_qty) + ' @ $' + str(round(price, 2)),
            ' PnL: ' + sign + str(round(pnl_u, 2)) + ' USDT | Sesión: ' + sign + str(round(ps['session_pnl'], 2)) + ' USDT',
            ' ID: ' + str(oid),
        ]))
        if not partial:
            ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                       'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                       'partial_closed': False, 'trade_amount_used': None})
    else:
        send_msg('❌ ERROR SELL ' + fsym + ': ' + res.get('msg', str(res)))

def manage_open(pair, ps, price, atr, now_str, now_dt):
    entry, tp, trail = ps['entry_price'], ps['tp_target'], ps['trailing_sl']
    partial = ps['partial_closed']
    if None in (entry, tp, trail): return False
    fsym    = pair['fsym']
    ta      = ps.get('trade_amount_used', TRADE_AMOUNT)
    pnl_pct = (price - entry) / entry * 100
    pnl_u   = ta * LEVERAGE * pnl_pct / 100
    sign    = '+' if pnl_u >= 0 else ''
    new_trail = price - atr * TRAIL_MULT
    if new_trail > trail:
        ps['trailing_sl'] = new_trail; trail = new_trail
    halfway = entry + (tp - entry) * 0.5
    if not partial and price >= halfway:
        ps['partial_closed'] = True
        ps['trailing_sl']    = max(trail, entry)
        send_msg(build_msg([
            '📍 50% TP LONG — ' + fsym + '/USDT',
            ' Precio: $' + str(round(price, 2)) + ' | SL → BE: $' + str(round(entry, 2)),
            ' PnL: ' + sign + str(round(pnl_u, 2)) + ' USDT | ' + now_str,
        ]))
        if AUTO_TRADE and API_KEY: close_position(pair, ps, price, '50% TP', partial=True)
        return True
    if price <= trail:
        icon = 'GANANCIA' if pnl_u >= 0 else 'PÉRDIDA'
        ps['last_sl_time'] = now_dt.isoformat()
        send_msg(build_msg([
            '🛑 TRAILING SL LONG — ' + fsym + '/USDT',
            ' Precio: $' + str(round(price, 2)) + ' | Trail: $' + str(round(trail, 2)),
            ' ' + icon + ': ' + sign + str(round(pnl_pct*LEVERAGE, 2)) + '% → ' + sign + str(round(pnl_u, 2)) + ' USDT',
            ' Cooldown: ' + str(SL_COOLDOWN_HOURS) + 'h | ' + now_str,
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
    res    = market_order(sym, 'SELL', qty)
    oid    = res.get('orderId')
    sizing = str(round(TRADE_PCT*100,1)) + '% balance' if TRADE_PCT > 0 else 'fijo'
    if oid:
        ps.update({'position': 'SHORT', 'entry_price': price, 'entry_qty': qty,
                   'initial_sl': sl, 'tp_target': tp, 'trailing_sl': sl,
                   'partial_closed': False, 'trades_today': ps.get('trades_today', 0) + 1,
                   'trade_amount_used': ta})
        send_msg(build_msg([
            '🔴 SHORT abierto — ' + fsym + '/USDT',
            ' Qty: ' + str(qty) + ' @ $' + str(round(price, 2)),
            ' Monto: $' + str(ta) + ' (' + sizing + ') | Lev: ' + str(LEVERAGE) + 'x',
            ' SL: $' + str(round(sl, 2)) + ' | TP: $' + str(round(tp, 2)),
            ' 50% TP: $' + str(round(halfway, 2)) + ' | ID: ' + str(oid),
        ]))
    else:
        send_msg('❌ ERROR SHORT ' + fsym + ': ' + res.get('msg', str(res)))

def close_short(pair, ps, price, reason, partial=False):
    sym, fsym, dec = pair['symbol'], pair['fsym'], pair['dec']
    entry   = ps['entry_price'] or price
    qty     = ps.get('entry_qty') or 0
    ta      = ps.get('trade_amount_used', TRADE_AMOUNT)
    factor  = 0.5 if partial else (0.5 if ps['partial_closed'] else 1.0)
    buy_qty = round(qty * factor, dec)
    if buy_qty <= 0: return
    set_leverage_binance(sym)
    res     = market_order(sym, 'BUY', buy_qty)
    oid     = res.get('orderId')
    pnl_pct = (entry - price) / entry * 100
    pnl_u   = ta * factor * LEVERAGE * pnl_pct / 100
    ps['session_pnl'] = ps.get('session_pnl', 0) + pnl_u
    if oid:
        sign  = '+' if pnl_u >= 0 else ''
        label = 'Cierre parcial SHORT' if partial else 'SHORT cerrado'
        send_msg(build_msg([
            label + ' — ' + fsym + '/USDT',
            ' Razón: ' + reason,
            ' BUY ' + str(buy_qty) + ' @ $' + str(round(price, 2)),
            ' PnL: ' + sign + str(round(pnl_u, 2)) + ' USDT | Sesión: ' + sign + str(round(ps['session_pnl'], 2)) + ' USDT',
            ' ID: ' + str(oid),
        ]))
        if not partial:
            ps.update({'position': 'FLAT', 'entry_price': None, 'entry_qty': None,
                       'initial_sl': None, 'tp_target': None, 'trailing_sl': None,
                       'partial_closed': False, 'trade_amount_used': None})
    else:
        send_msg('❌ ERROR CLOSE SHORT ' + fsym + ': ' + res.get('msg', str(res)))

def manage_short(pair, ps, price, atr, now_str, now_dt):
    entry, tp, trail = ps['entry_price'], ps['tp_target'], ps['trailing_sl']
    partial = ps['partial_closed']
    if None in (entry, tp, trail): return False
    fsym    = pair['fsym']
    ta      = ps.get('trade_amount_used', TRADE_AMOUNT)
    pnl_pct = (entry - price) / entry * 100
    pnl_u   = ta * LEVERAGE * pnl_pct / 100
    sign    = '+' if pnl_u >= 0 else ''
    new_trail = price + atr * TRAIL_MULT
    if new_trail < trail:
        ps['trailing_sl'] = new_trail; trail = new_trail
    halfway = entry - (entry - tp) * 0.5
    if not partial and price <= halfway:
        ps['partial_closed'] = True
        ps['trailing_sl']    = min(trail, entry)
        send_msg(build_msg([
            '📍 50% TP SHORT — ' + fsym + '/USDT',
            ' Precio: $' + str(round(price, 2)) + ' | SL → BE: $' + str(round(entry, 2)),
            ' PnL: ' + sign + str(round(pnl_u, 2)) + ' USDT | ' + now_str,
        ]))
        if AUTO_TRADE and API_KEY: close_short(pair, ps, price, '50% TP', partial=True)
        return True
    if price >= trail:
        icon = 'GANANCIA' if pnl_u >= 0 else 'PÉRDIDA'
        ps['last_sl_time'] = now_dt.isoformat()
        send_msg(build_msg([
            '🛑 TRAILING SL SHORT — ' + fsym + '/USDT',
            ' Precio: $' + str(round(price, 2)) + ' | Trail: $' + str(round(trail, 2)),
            ' ' + icon + ': ' + sign + str(round(pnl_pct*LEVERAGE, 2)) + '% → ' + sign + str(round(pnl_u, 2)) + ' USDT',
            ' Cooldown: ' + str(SL_COOLDOWN_HOURS) + 'h | ' + now_str,
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
    price = closes[-1]
    e21   = calc_ema(closes, 21)
    e89   = calc_ema(closes, 89)
    rsi14 = calc_rsi(closes, 14)
    atr14 = calc_atr(highs, lows, closes, 14)
    adx14 = calc_adx(highs, lows, closes, 14)
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
    print(' [' + sym + '] ' + sig + ' | $' + str(round(price,2)) +
          ' | RSI:' + str(round(lr,1)) + ' | ADX:' + str(round(la_adx,1)) + ' | pos:' + pos)

    # ── Gestionar posición abierta ─────────────────────────────────────────
    if pos == 'LONG':  manage_open(pair,  ps, price, la, now_str, now_dt)
    if pos == 'SHORT': manage_short(pair, ps, price, la, now_str, now_dt)

    # ── Cierre por señal contraria ─────────────────────────────────────────
    if sig in ('SELL', 'WAIT_RSI_SHORT') and ps['position'] == 'LONG':
        ep    = ps['entry_price'] or price
        ta_u  = ps.get('trade_amount_used', TRADE_AMOUNT)
        pnl_u = ta_u * LEVERAGE * (price - ep) / ep * 100 / 100
        s     = '+' if pnl_u >= 0 else ''
        send_msg(build_msg([
            '🔴 CRUCE BAJISTA — ' + fsym + '/USDT 4H',
            '❌ CERRAR POSICIÓN LONG',
            '📊 Precio: $' + str(round(price,2)) + ' | PnL est.: ' + s + str(round(pnl_u,2)) + ' USDT',
            '📉 EMA21 cruzó por debajo de EMA89 | ' + now_str,
        ]))
        if AUTO_TRADE and API_KEY: close_position(pair, ps, price, 'Cruce bajista')
        else: ps.update({'position':'FLAT','entry_price':None,'entry_qty':None,
                         'initial_sl':None,'tp_target':None,'trailing_sl':None,
                         'partial_closed':False,'trade_amount_used':None})

    if sig in ('BUY', 'WAIT_RSI') and ps['position'] == 'SHORT':
        ep    = ps['entry_price'] or price
        ta_u  = ps.get('trade_amount_used', TRADE_AMOUNT)
        pnl_u = ta_u * LEVERAGE * (ep - price) / ep * 100 / 100
        s     = '+' if pnl_u >= 0 else ''
        send_msg(build_msg([
            '🟢 CRUCE ALCISTA — ' + fsym + '/USDT 4H',
            '❌ CERRAR POSICIÓN SHORT',
            '📊 Precio: $' + str(round(price,2)) + ' | PnL est.: ' + s + str(round(pnl_u,2)) + ' USDT',
            '📈 EMA21 cruzó por encima de EMA89 | ' + now_str,
        ]))
        if AUTO_TRADE and API_KEY: close_short(pair, ps, price, 'Cruce alcista')
        else: ps.update({'position':'FLAT','entry_price':None,'entry_qty':None,
                         'initial_sl':None,'tp_target':None,'trailing_sl':None,
                         'partial_closed':False,'trade_amount_used':None})

    if sig == ps.get('last_signal'): return ps

    # ── Notificaciones de señal ────────────────────────────────────────────
    ta_now = resolve_trade_amount(balance)
    btc_ok_long  = btc_bull or fsym == 'BTC'
    btc_ok_short = (not btc_bull) or fsym == 'BTC'

    halfway_l = price + (tp_long - price) * 0.5
    halfway_s = price - (price - tp_short) * 0.5
    sl_pct_l  = round((sl_long  - price) / price * 100, 1)
    tp_pct_l  = round((tp_long  - price) / price * 100, 1)
    sl_pct_s  = round((sl_short - price) / price * 100, 1)
    tp_pct_s  = round((tp_short - price) / price * 100, 1)
    pnl_sl_l  = round(ta_now * LEVERAGE * abs(sl_pct_l) / 100, 2)
    pnl_tp_l  = round(ta_now * LEVERAGE * tp_pct_l / 100, 2)
    pnl_sl_s  = round(ta_now * LEVERAGE * abs(sl_pct_s) / 100, 2)
    pnl_tp_s  = round(ta_now * LEVERAGE * abs(tp_pct_s) / 100, 2)

    if sig == 'BUY':
        dec_txt = '✅ ABRIR LONG' if btc_ok_long else '⛔ NO OPERAR — BTC bajista'
        parts = [
            '🟢 SEÑAL LONG — ' + fsym + '/USDT 4H', '', dec_txt, '',
            '📊 $' + str(round(price,2)) + ' | EMA21: $' + str(round(le21,2)) + ' | EMA89: $' + str(round(le89,2)),
            '📐 RSI: ' + str(round(lr,1)) + ' (mín ' + str(RSI_MIN) + ') | ADX: ' + str(round(la_adx,1)),
            '', '━━━ SETUP ━━━',
            '🛑 SL: $' + str(round(sl_long,2)) + ' (' + str(sl_pct_l) + '%) → -$' + str(pnl_sl_l),
            '🎯 TP: $' + str(round(tp_long,2)) + ' (+' + str(tp_pct_l) + '%) → +$' + str(pnl_tp_l),
            '📍 50%: $' + str(round(halfway_l,2)) + ' | 🔄 Trailing activo',
            '⚡ Lev: ' + str(LEVERAGE) + 'x | $' + str(ta_now) + ' | R:R 1:2 | ' + now_str,
        ]
    elif sig == 'SELL':
        dec_txt = '✅ ABRIR SHORT' if btc_ok_short else '⛔ NO OPERAR — BTC alcista'
        parts = [
            '🔴 SEÑAL SHORT — ' + fsym + '/USDT 4H', '', dec_txt, '',
            '📊 $' + str(round(price,2)) + ' | EMA21: $' + str(round(le21,2)) + ' | EMA89: $' + str(round(le89,2)),
            '📐 RSI: ' + str(round(lr,1)) + ' (máx ' + str(RSI_MAX) + ') | ADX: ' + str(round(la_adx,1)),
            '', '━━━ SETUP ━━━',
            '🛑 SL: $' + str(round(sl_short,2)) + ' (+' + str(sl_pct_s) + '%) → -$' + str(pnl_sl_s),
            '🎯 TP: $' + str(round(tp_short,2)) + ' (' + str(tp_pct_s) + '%) → +$' + str(pnl_tp_s),
            '📍 50%: $' + str(round(halfway_s,2)) + ' | 🔄 Trailing activo',
            '⚡ Lev: ' + str(LEVERAGE) + 'x | $' + str(ta_now) + ' | R:R 1:2 | ' + now_str,
        ]
    elif sig == 'WAIT_RSI':
        falta = round(RSI_MIN - lr, 1)
        parts = [
            '🟡 CRUCE ALCISTA — ' + fsym + '/USDT 4H', '⏳ Esperar RSI',
            '📐 RSI: ' + str(round(lr,1)) + ' (faltan +' + str(falta) + ' para &gt;' + str(RSI_MIN) + ')',
            '🕯 Velas desde cruce: ' + str(vcr) + ' | ADX: ' + str(round(la_adx,1)),
            '🛑 SL ref: $' + str(round(sl_long,2)) + ' | 🎯 TP ref: $' + str(round(tp_long,2)),
            '⏰ ' + now_str,
        ]
    elif sig == 'WAIT_RSI_SHORT':
        falta = round(lr - RSI_MAX, 1)
        parts = [
            '🟠 CRUCE BAJISTA — ' + fsym + '/USDT 4H', '⏳ Esperar RSI',
            '📐 RSI: ' + str(round(lr,1)) + ' (sobran ' + str(falta) + ' para &lt;' + str(RSI_MAX) + ')',
            '🕯 Velas desde cruce: ' + str(vcr) + ' | ADX: ' + str(round(la_adx,1)),
            '🛑 SL ref: $' + str(round(sl_short,2)) + ' | 🎯 TP ref: $' + str(round(tp_short,2)),
            '⏰ ' + now_str,
        ]
    elif sig == 'LONG_ACTIVE':
        dec_txt = '⚡ Entrada tardía válida (' + str(vcr) + ' velas)' if vcr <= 3 else '⏸ Cruce antiguo (' + str(vcr) + ' velas)'
        parts = [
            '🔵 LARGO ACTIVO — ' + fsym + '/USDT 4H', dec_txt,
            '📊 $' + str(round(price,2)) + ' | RSI: ' + str(round(lr,1)) + ' | ADX: ' + str(round(la_adx,1)),
            '🛑 SL: $' + str(round(sl_long,2)) + ' | 🎯 TP: $' + str(round(tp_long,2)),
            '⏰ ' + now_str,
        ]
    elif sig == 'SHORT_ACTIVE':
        dec_txt = '⚡ Entrada tardía válida (' + str(vcr) + ' velas)' if vcr <= 3 else '⏸ Cruce antiguo (' + str(vcr) + ' velas)'
        parts = [
            '🟠 CORTO ACTIVO — ' + fsym + '/USDT 4H', dec_txt,
            '📊 $' + str(round(price,2)) + ' | RSI: ' + str(round(lr,1)) + ' | ADX: ' + str(round(la_adx,1)),
            '🛑 SL: $' + str(round(sl_short,2)) + ' | 🎯 TP: $' + str(round(tp_short,2)),
            '⏰ ' + now_str,
        ]
    else:
        ps['last_signal'] = sig; return ps

    send_msg(build_msg(parts))

    # ── Ejecución automática con todos los filtros ─────────────────────────
    late_long  = sig == 'LONG_ACTIVE'  and vcr <= 2
    late_short = sig == 'SHORT_ACTIVE' and vcr <= 2
    want_long  = (sig == 'BUY'  or late_long)  and pos == 'FLAT'
    want_short = (sig == 'SELL' or late_short) and pos == 'FLAT'

    if AUTO_TRADE and API_KEY and (want_long or want_short):
        direction = 'LONG' if want_long else 'SHORT'

        # 1. Cooldown
        in_cd, cd_rem = sl_cooldown_active(ps, now_dt)
        if in_cd:
            send_msg('⏳ ' + fsym + ' en cooldown por SL — ' + str(cd_rem) + 'h restantes'); ps['last_signal'] = sig; return ps

        # 2. ADX
        if la_adx < ADX_MIN:
            send_msg('⏸ ' + fsym + ': ADX=' + str(round(la_adx,1)) + ' &lt; ' + str(ADX_MIN) + ' — mercado lateral'); ps['last_signal'] = sig; return ps

        # 3. MTF diario
        if USE_MTF:
            daily_bull = pair_daily_is_bullish(fsym)
            if want_long  and not daily_bull:
                send_msg('⛔ BLOQUEADO LONG '  + fsym + ': 1D bajista (MTF)'); ps['last_signal'] = sig; return ps
            if want_short and daily_bull:
                send_msg('⛔ BLOQUEADO SHORT ' + fsym + ': 1D alcista (MTF)'); ps['last_signal'] = sig; return ps

        # 4. Volumen
        if USE_VOLUME_FILTER:
            vol_ok, vol_ratio = volume_confirmed(vols)
            if not vol_ok:
                send_msg('⚠️ ' + fsym + ': volumen bajo (' + str(vol_ratio) + 'x) — entrada omitida'); ps['last_signal'] = sig; return ps

        # 5. Filtro BTC
        if want_long  and not btc_ok_long:
            send_msg('⛔ BLOQUEADO LONG '  + fsym + ': BTC bajista'); ps['last_signal'] = sig; return ps
        if want_short and not btc_ok_short:
            send_msg('⛔ BLOQUEADO SHORT ' + fsym + ': BTC alcista'); ps['last_signal'] = sig; return ps

        # 6. Correlación alts
        if fsym != 'BTC' and count_alt_positions(state) >= MAX_ALT_POS:
            send_msg('⛔ ' + fsym + ': límite de ' + str(MAX_ALT_POS) + ' alts simultáneas'); ps['last_signal'] = sig; return ps

        # 7. Límite diario de trades
        if ps['trades_today'] >= MAX_TRADES_DAY:
            send_msg('⛔ ' + fsym + ': límite ' + str(MAX_TRADES_DAY) + ' trades/día'); ps['last_signal'] = sig; return ps

        # ✅ Todos los filtros pasados — abrir posición
        if want_long:  open_long( pair, ps, price, sl_long,  tp_long,  ta_now)
        if want_short: open_short(pair, ps, price, sl_short, tp_short, ta_now)

    ps['last_signal'] = sig
    return ps

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now_dt  = datetime.now(timezone.utc)
    today   = now_dt.strftime('%Y-%m-%d')
    now_str = now_dt.strftime('%Y-%m-%d %H:%M UTC')
    sizing  = 'TRADE_PCT=' + str(TRADE_PCT*100) + '%' if TRADE_PCT > 0 else 'TRADE_AMOUNT=$' + str(TRADE_AMOUNT)
    print('=' * 55)
    print(' EMA Bot v10.0 | ' + now_str)
    print(' Sizing: ' + sizing + ' | Lev: ' + str(LEVERAGE) + 'x')
    print(' RSI LONG>' + str(RSI_MIN) + ' SHORT<' + str(RSI_MAX) + ' | ADX>' + str(ADX_MIN))
    print(' MaxPos: ' + str(MAX_OPEN_POS) + ' | MaxAlts: ' + str(MAX_ALT_POS) + ' | MaxTrades: ' + str(MAX_TRADES_DAY))
    print(' DailyLoss: $' + str(DAILY_LOSS_LIMIT) + ' | SL Cooldown: ' + str(SL_COOLDOWN_HOURS) + 'h')
    print(' MTF: ' + str(USE_MTF) + ' | VolFilter: ' + str(USE_VOLUME_FILTER))
    print('=' * 55)

    state   = load_state()
    balance = get_futures_balance() if API_KEY else None

    # ── Stop diario ────────────────────────────────────────────────────────
    loss_exceeded, daily_pnl = daily_loss_exceeded(state, today)
    if loss_exceeded:
        send_msg(build_msg([
            '🚨 <b>STOP DIARIO ACTIVADO</b>',
            'PnL del día: $' + str(round(daily_pnl, 2)),
            'Límite: $' + str(DAILY_LOSS_LIMIT),
            'Bot bloqueado hasta mañana. | ' + now_str,
        ]))
        save_state(state, now_str, balance)
        return

    print('[Filtro BTC 4H]')
    btc_bull = btc_is_bullish()
    open_now = count_open_positions(state)
    print('[Posiciones abiertas: ' + str(open_now) + '/' + str(MAX_OPEN_POS) + ']')

    for pair in PAIRS:
        sym = pair['symbol']
        print(chr(10) + '-> ' + sym)
        ps  = get_pair_state(state, sym)
        if ps.get('position') == 'FLAT' and open_now >= MAX_OPEN_POS:
            print(' SALTADO: MAX_OPEN_POSITIONS'); state[sym] = ps; continue
        try:
            state[sym] = process_pair(pair, ps, today, now_str, now_dt, btc_bull, balance, state)
        except Exception as exc:
            print(' ERROR ' + sym + ': ' + str(exc))
            send_msg('❌ Error ' + sym + ': ' + str(exc))
        time.sleep(2)

    save_state(state, now_str, balance)
    send_session_report(state, now_str, balance, today)
    print(chr(10) + 'Completado — ' + now_str)

if __name__ == '__main__':
    main()
