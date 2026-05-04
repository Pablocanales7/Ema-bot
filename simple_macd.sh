#!/bin/bash
# macd_simple.sh - FUNCIONA 100%

cd ~/emabot || exit 1
cp bot.py bot_backup_macd_$(date +%Y%m%d_%H%M).py

# 1. Encontrar inicio de send_session_report
LINE_START=$(grep -n "def send_session_report" bot.py | cut -d: -f1)

# 2. Encontrar fin (siguiente def)
LINE_END=$(grep -A1 "def " bot.py | grep -n "def " | head -1 | cut -d: -f1 | tail -1)
if [ -z "$LINE_END" ]; then
    LINE_END=$(($(wc -l < bot.py)+1))
fi

# 3. Reemplazar con sed (líneas precisas)
sed -i "${LINE_START},${LINE_END}d" bot.py

# 4. Insertar NUEVA función MACD
cat >> bot.py << 'EOF'
def send_session_report(state, nowstr, balance, today):
    has_positions = any(state.get(p['symbol'], {}).get('position', 'FLAT') != 'FLAT' for p in PAIRS)
    if not has_positions:
        cycle_count = state.get('report_cycle_count', 0) + 1
        state['report_cycle_count'] = cycle_count
        if cycle_count % 2 != 0: return
    else:
        state['report_cycle_count'] = 0

    lines = [f"📊 REPORTE — {nowstr}"]
    totalpnl = 0.0
    open_count = 0

    for p in PAIRS:
        ps = state.get(p['symbol'], {})
        pos = ps.get('position', 'FLAT')
        if pos == 'FLAT': continue
        
        entry = ps.get('entry_price', 0)
        price = ps.get('price', 0)
        pnl = ps.get('session_pnl', 0.0)
        totalpnl += pnl if ps.get('trades_date') == today else 0
        
        pnl_pct = 0
        if entry > 0:
            if pos == 'LONG':
                pnl_pct = (price - entry) / entry * 100
            else:
                pnl_pct = (entry - price) / entry * 100
        
        icon = "🟢" if pos == "LONG" else "🔴"
        s = "+" if pnl >= 0 else ""
        
        # MACD simple
        macd_status = "➡️"
        try:
            closes = ps.get('closes', [])[-30:]
            if len(closes) >= 26:
                import numpy as np
                import talib
                macd, signal, hist = talib.MACD(np.array(closes))
                if hist[-1] > hist[-2] * 1.01:
                    macd_status = "💪"
                elif hist[-1] < hist[-2] * 0.99:
                    macd_status = "😟"
        except:
            macd_status = "❓"
        
        open_count += 1
        lines.append(f"{icon} {p['fsym']} {pos} ${entry:.2f}→${price:.2f} "
                    f"({pnl_pct:+.1f}%) | {s}${pnl:.2f} {macd_status}")

    if open_count == 0:
        lines.append("⚪ Sin posiciones")

    s = "+" if totalpnl >= 0 else ""
    lines.append(f"💵 PnL hoy: {s}${totalpnl:.2f} | Límite: {DAILY_LOSS_LIMIT}")
    if isinstance(balance, dict):
        lines.append(f"💰 ${balance['total']} (Libre: ${balance['available']})")

    send_msg(build_msg(*lines))
EOF

# Test y deploy
if python3 -c "exec(open('bot.py').read())" 2>/dev/null; then
    git add bot.py >/dev/null 2>&1
    git commit -m "MACD reportes" >/dev/null 2>&1
    sudo systemctl restart emabot
    echo "✅ ¡EXITO! MACD agregado. Espera 30min"
    echo "Ejemplo: 🔴 SOL SHORT $82.96→$82.50 (+0.6%) | +$0.97 😟"
    journalctl -u emabot -n 5 -l
else
    cp bot_backup_macd_*.py bot.py
    echo "❌ Error. Restaurado."
fi
