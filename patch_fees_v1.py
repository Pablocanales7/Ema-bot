#!/usr/bin/env python3
"""
patch_fees.py - Aplica comisiones Binance Futures 0.05% taker en bot.py
Ejecutar en: ~/emabot/
Uso: python3 patch_fees.py
"""

import re, shutil, sys, subprocess
from datetime import datetime

TARGET = "bot.py"
BACKUP = "bot_backup_prefees_{}.py".format(datetime.now().strftime("%Y%m%d_%H%M"))

CONSTANTS_BLOCK = (
    "\n"
    "# ──────────────────────────────────────────────────────────\n"
    "# FEES BINANCE FUTURES TAKER (market orders) Binance VIP0\n"
    "# ──────────────────────────────────────────────────────────\n"
    "FEE_RATE       = 0.0005   # 0.05% por transaccion (taker)\n"
    "FEE_ROUNDTRIP  = 0.0010   # 0.10% total (entrada + salida)\n"
    "FEE_BUFFER_PCT = 0.10     # en pct descontado de pnlpct bruto\n"
)

CALC_PNL_FUNC = (
    "\n"
    "def calc_pnl_net(pos, entry, price, trade_amount, leverage):\n"
    "    # Calcula PnL NETO descontando fees Binance taker 0.05%\n"
    "    if not entry or entry <= 0:\n"
    "        return 0.0, 0.0\n"
    "    if pos == 'LONG':\n"
    "        pnlpct_gross = (price - entry) / entry * 100\n"
    "    else:\n"
    "        pnlpct_gross = (entry - price) / entry * 100\n"
    "    pnlpct_net = pnlpct_gross - FEE_BUFFER_PCT\n"
    "    pnl_usd = trade_amount * leverage * pnlpct_net / 100\n"
    "    return pnlpct_net, pnl_usd\n"
    "\n"
)


def _repl_pnl_generic(m):
    indent = m.group(1) if m.lastindex and m.group(1) else ""
    return (indent + "ta = ps.get('tradeamountused', TRADE_AMOUNT)\n"
            + indent + "pnlpct, pnlu = calc_pnl_net(pos, entry, price, ta, LEVERAGE)")

def _repl_pnl_report(m):
    indent = m.group(1) if m.lastindex and m.group(1) else ""
    return (indent + "pnlpct, _ = calc_pnl_net(pos, entry, price, TRADE_AMOUNT, LEVERAGE)\n"
            + indent + "icon = '\U0001f7e2' if pnlpct >= 0 else '\U0001f534'")

def _repl_trail_long(m):
    indent = m.group(1) if m.lastindex and m.group(1) else ""
    expr   = m.group(2)
    return (indent + expr + "\n"
            + indent + "be_long = entry * (1 + FEE_ROUNDTRIP)  # break-even con fees\n"
            + indent + "if new_tsl > trailingsl:\n"
            + indent + "    trailingsl = max(new_tsl, be_long)")

def _repl_trail_short(m):
    indent = m.group(1) if m.lastindex and m.group(1) else ""
    expr   = m.group(2)
    return (indent + expr + "\n"
            + indent + "be_short = entry * (1 - FEE_ROUNDTRIP)  # break-even con fees\n"
            + indent + "if new_tsl < trailingsl:\n"
            + indent + "    trailingsl = min(new_tsl, be_short)")


REPLACEMENTS = [
    (
        "PnL LONG/SHORT + ta + pnlu -> calc_pnl_net",
        (r"if pos == ['\"]{1,2}LONG['\"]{1,2}:\n"
         r"([ \t]+)pnlpct = \(price - entry\) / entry \* 100\n"
         r"else:[^\n]*\n"
         r"\1pnlpct = \(entry - price\) / entry \* 100\n"
         r"([ \t]*)ta = ps\.get\(['\"]{1,2}tradeamountused['\"]{1,2},.+?\)\n"
         r"\2pnlu = ta \* LEVERAGE \* pnlpct / 100"),
        _repl_pnl_generic,
    ),
    (
        "PnL sendsessionreport -> calc_pnl_net",
        (r"([ \t]*)if pos == ['\"]{1,2}LONG['\"]{1,2}:\n"
         r"[ \t]+pnlpct = \(price - entry\) / entry \* 100 if entry else 0\n"
         r"[ \t]+icon = [^\n]+\n"
         r"[ \t]*else:\n"
         r"[ \t]+pnlpct = \(entry - price\) / entry \* 100 if entry else 0\n"
         r"[ \t]+icon = [^\n]+"),
        _repl_pnl_report,
    ),
    ("TP LONG + fees",
     r"tptarget\s*=\s*entry\s*\*\s*\(1\s*\+\s*TP_PCT\)",
     "tptarget  = entry * (1 + TP_PCT + FEE_ROUNDTRIP)  # +fees buffer"),
    ("TP SHORT + fees",
     r"tptarget\s*=\s*entry\s*\*\s*\(1\s*-\s*TP_PCT\)",
     "tptarget  = entry * (1 - TP_PCT - FEE_ROUNDTRIP)  # -fees buffer"),
    ("SL LONG + fees",
     r"initialsl\s*=\s*entry\s*\*\s*\(1\s*-\s*SL_PCT\)",
     "initialsl = entry * (1 - SL_PCT - FEE_ROUNDTRIP)  # -fees buffer"),
    ("SL SHORT + fees",
     r"initialsl\s*=\s*entry\s*\*\s*\(1\s*\+\s*SL_PCT\)",
     "initialsl = entry * (1 + SL_PCT + FEE_ROUNDTRIP)  # +fees buffer"),
    (
        "Trailing SL LONG break-even fees",
        (r"([ \t]*)(new_tsl\s*=\s*price\s*\*\s*\(1\s*-\s*[A-Z_]+\s*\*\s*atr\s*/\s*price\))\n"
         r"if new_tsl > trailingsl:\n[ \t]+trailingsl = new_tsl"),
        _repl_trail_long,
    ),
    (
        "Trailing SL SHORT break-even fees",
        (r"([ \t]*)(new_tsl\s*=\s*price\s*\*\s*\(1\s*\+\s*[A-Z_]+\s*\*\s*atr\s*/\s*price\))\n"
         r"if new_tsl < trailingsl:\n[ \t]+trailingsl = new_tsl"),
        _repl_trail_short,
    ),
]


def inject_constants(code):
    if "FEE_RATE" in code:
        return code, False
    matches = list(re.finditer(r"^(import |from )\S+.*$", code, re.MULTILINE))
    pos = matches[-1].end() if matches else 0
    return code[:pos] + "\n" + CONSTANTS_BLOCK + code[pos:], True


def inject_calc_func(code):
    if "def calc_pnl_net" in code:
        return code, False
    for t in ["openlong","openshort","manageopen","manageshort","sendsession"]:
        m = re.search(r"^def " + t, code, re.MULTILINE)
        if m:
            return code[:m.start()] + CALC_PNL_FUNC + code[m.start():], True
    return code + CALC_PNL_FUNC, True


def apply_replacements(code):
    log = []
    for desc, pat, repl in REPLACEMENTS:
        if callable(repl):
            new_code, n = re.subn(pat, repl, code, flags=re.MULTILINE | re.DOTALL)
        else:
            new_code, n = re.subn(pat, repl, code, flags=re.MULTILINE)
        icon = "OK" if n else "--"
        log.append("  [{}] {}x  {}".format(icon, n, desc))
        code = new_code
    return code, log


def main():
    sep = "=" * 62
    print(sep)
    print("  EMA Fee Patcher v1.0  |  Binance Futures 0.05% taker")
    print(sep)
    try:
        with open(TARGET, "r", encoding="utf-8") as f:
            original = f.read()
    except FileNotFoundError:
        print("ERROR: No se encontro {}".format(TARGET))
        print("       Ejecutar desde ~/emabot/")
        sys.exit(1)
    print("Archivo : {}  ({:,} chars)".format(TARGET, len(original)))
    shutil.copy(TARGET, BACKUP)
    print("Backup  : {}".format(BACKUP))
    code = original
    print("\n[1/3] Inyectando constantes de fees...")
    code, ok = inject_constants(code)
    print("  OK FEE_RATE / FEE_ROUNDTRIP / FEE_BUFFER_PCT" if ok else "  -- Ya existian")
    print("\n[2/3] Inyectando calc_pnl_net()...")
    code, ok = inject_calc_func(code)
    print("  OK Funcion inyectada" if ok else "  -- Ya existia")
    print("\n[3/3] Reemplazando PnL / TP / SL / Trailing...")
    code, log = apply_replacements(code)
    for line in log:
        print(line)
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(code)
    print("\nVerificando sintaxis...")
    result = subprocess.run(["python3", "-m", "py_compile", TARGET], capture_output=True, text=True)
    if result.returncode == 0:
        print("  OK Sintaxis correcta")
    else:
        print("  ERROR Sintaxis incorrecta:")
        print(result.stderr)
        print("  Restaurando backup...")
        shutil.copy(BACKUP, TARGET)
        print("  Restaurado desde {}".format(BACKUP))
        sys.exit(1)
    print("")
    print(sep)
    print("PATCH APLICADO CORRECTAMENTE")
    print("")
    print("Proximos pasos:")
    print("  git add bot.py")
    print('  git commit -m "feat: Add Binance taker fees 0.05pct PnL/TP/SL/Trailing"')
    print("  git push origin main")
    print("  sudo systemctl restart emabot")
    print("  journalctl -u emabot -f")
    print(sep)


if __name__ == "__main__":
    main()
