import re
with open('bot.py', 'r') as f:
    content = f.read()

# Patrón para send_session_report completa
pattern = r'def\s+send_session_report\([^)]*\).*?(?=def\s|$)', re.DOTALL | re.MULTILINE | re.IGNORECASE
match = re.search(pattern, content)
if not match:
    print("❌ No encontrada send_session_report")
    sys.exit(1)

# NUEVA función (corta y limpia)
new_func = """
def send_session_report(state, nowstr, balance, today):
    has_positions = any(state.get(p['symbol'], {}).get('position', 'FLAT') != 'FLAT' for p in PAIRS)
    if not has_positions:
        cycle_count = state.get('report_cycle_count', 0) + 1
        state['report_cycle_count'] = cycle_count
        if cycle_count % 2 != 0: return
    else:
        state['report_cycle_count'] = 0

    lines = [f'📊 REPORTE — {nowstr}']
    totalpnl = sum(ps.get('session_pnl', 0.0) for p in PAIRS 
                   if state.get(p['symbol'], {}).get('trades_date') == today)

    for p in PAIRS:
        ps = state.get(p['symbol'], {})
        pos = ps.get('position', 'FLAT')
        if pos == 'FLAT': continue
        
        entry = ps.get('entry_price', 0)
        price = ps.get('price', 0)
        pnl = ps.get('session_pnl', 0.0)
        
        pnl_pct = ((price - entry) / entry * 100) if entry and pos == 'LONG' else \
                  ((entry - price) / entry * 100) if entry else 0
        icon = '🟢' if pos == 'LONG' else '🔴'
        s = '+' if pnl >= 0 else ''

        # MACD
        macd_status = '❓'
        try:
            closes = ps.get('closes', [])[-50:]
            if len(closes) >= 26:
                import numpy as np
                import talib
                _, _, hist = talib.MACD(np.array(closes, dtype=float))
                curr, prev = hist[-1], hist[-2]
                adx = ps.get('adx', 0)
                if curr > prev * 1.02 and adx > 20:
                    macd_status = '💪 FUERTE'
                elif curr < prev * 0.98 and adx > 20:
                    macd_status = '😟 DEBIL'
                else:
                    macd_status = '➡️'
        except: pass

        lines.append(f'{icon} {p["fsym"]} {pos} ${round(entry,2)}→${round(price,2)} '
                    f'({pnl_pct:+.2f}%) PnL:{s}{round(pnl,2)} {macd_status}')

    if not any(state.get(p['symbol'], {}).get('position', 'FLAT') != 'FLAT' for p in PAIRS):
        lines.append('⚪ Sin posiciones')
    
    s = '+' if totalpnl >= 0 else ''
    lines.append(f'💵 PnL hoy: {s}{round(totalpnl,2)}USDT | Límite: {DAILY_LOSS_LIMIT}')
    if isinstance(balance, dict):
        lines.append(f'💰 ${balance["total"]} (Libre:${balance["available"]})')

    send_msg(build_msg(*lines))
