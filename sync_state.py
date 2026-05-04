#!/usr/bin/env python3
import json
import os
import hmac
import hashlib
import time
import requests

STATE_FILE = "state.json"
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
RECV_WINDOW = int(os.environ.get("RECV_WINDOW", "10000"))

PAIRS = [
    {"symbol": "BTCUSDT", "fsym": "BTC"},
    {"symbol": "ETHUSDT", "fsym": "ETH"},
    {"symbol": "SOLUSDT", "fsym": "SOL"},
    {"symbol": "BNBUSDT", "fsym": "BNB"},
]

EMPTY_STATE = {
    "last_signal": None,
    "position": "FLAT",
    "entry_price": None,
    "entry_qty": None,
    "initial_sl": None,
    "tp_target": None,
    "trailing_sl": None,
    "partial_closed": False,
    "trades_today": 0,
    "trades_date": None,
    "session_pnl": 0.0,
    "last_sl_time": None,
    "trade_amount_used": None,
    "signal": None,
    "price": None,
    "rsi": None,
    "ema21": None,
    "ema89": None,
    "atr": None,
    "adx": None,
    "velas_cruce": None,
    "entry_time": None,
}


def _sign(params: str) -> str:
    return hmac.new(API_SECRET.encode(), params.encode(), hashlib.sha256).hexdigest()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_pair_state(state, symbol):
    if symbol not in state or not isinstance(state[symbol], dict):
        state[symbol] = dict(EMPTY_STATE)
    else:
        for k, v in EMPTY_STATE.items():
            state[symbol].setdefault(k, v)
    return state[symbol]


def clear_local_position(ps):
    ps.update({
        "position": "FLAT",
        "entry_price": None,
        "entry_qty": None,
        "initial_sl": None,
        "tp_target": None,
        "trailing_sl": None,
        "partial_closed": False,
        "trade_amount_used": None,
        "entry_time": None,
    })


def fetch_open_positions_binance():
    if not API_KEY or not API_SECRET:
        return {}
    ts = int(time.time() * 1000)
    params = f"timestamp={ts}&recvWindow={RECV_WINDOW}"
    sig = _sign(params)
    url = f"https://fapi.binance.com/fapi/v2/positionRisk?{params}&signature={sig}"
    r = requests.get(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=15)
    r.raise_for_status()
    data = r.json()
    open_pos = {}
    for p in data:
        amt = float(p.get("positionAmt", 0) or 0)
        if abs(amt) > 0:
            sym = p.get("symbol")
            open_pos[sym] = {
                "amt": amt,
                "entryPrice": float(p.get("entryPrice", 0) or 0),
                "markPrice": float(p.get("markPrice", 0) or 0),
                "unRealizedProfit": float(p.get("unRealizedProfit", 0) or 0),
                "side": "LONG" if amt > 0 else "SHORT",
            }
    return open_pos


def sync_state_with_exchange(state):
    exchange_pos = fetch_open_positions_binance()
    for pair in PAIRS:
        sym = pair["symbol"]
        ps = get_pair_state(state, sym)
        real = exchange_pos.get(sym)

        if not real:
            if ps.get("position") in ("LONG", "SHORT"):
                print(f"[SYNC] {sym}: state={ps.get('position')} pero Binance está FLAT -> limpiando")
                clear_local_position(ps)
            continue

        if ps.get("position") != real["side"]:
            print(f"[SYNC] {sym}: state={ps.get('position')} vs Binance={real['side']} -> alineando")
            ps["position"] = real["side"]

        if not ps.get("entry_price") and real.get("entryPrice", 0) > 0:
            ps["entry_price"] = real["entryPrice"]

        if not ps.get("entry_qty") and abs(real.get("amt", 0)) > 0:
            ps["entry_qty"] = abs(real["amt"])

    return state


def count_open_positions(state):
    return sum(1 for p in PAIRS if state.get(p["symbol"], {}).get("position") in ("LONG", "SHORT"))


def main():
    state = load_state()
    state = sync_state_with_exchange(state)
    save_state(state)
    print(f"[OK] state sincronizado. Posiciones abiertas en state: {count_open_positions(state)}")


if __name__ == "__main__":
    main()
