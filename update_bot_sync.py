#!/usr/bin/env python3
import re
import sys
import os

def read_bot_py():
    """Lee el bot.py actual"""
    if not os.path.exists('bot.py'):
        print("❌ ERROR: bot.py no encontrado")
        sys.exit(1)
    with open('bot.py', 'r', encoding='utf-8') as f:
        content = f.read()
    print("✅ bot.py leído ({} chars)".format(len(content)))
    return content

def add_sync_function(content):
    """Agrega la función sync_positions_from_binance"""
    sync_func = '''
def sync_positions_from_binance(state):
    """Sincroniza posiciones reales de Binance con state.json"""
    if not APIKEY or not APISECRET:
        print("Sync: Sin API keys, saltando")
        return
    
    try:
        ts = int(time.time() * 1000)
        params = f"timestamp={ts}&recvWindow={RECVWINDOW}"
        signature = signparams(params)  # Reusa tu función signparams
        url = f"https://fapi.binance.com/fapi/v2/positionRisk?{params}&signature={signature}"
        headers = {"X-MBX-APIKEY": APIKEY}
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        positions = r.json()
        
        print("Sync: Posiciones Binance:", len([p for p in positions if float(p['positionAmt']) != 0]))
        
        for pos_binance in positions:
            symbol = pos_binance['symbol']
            amt = float(pos_binance['positionAmt'])
            entry_price = float(pos_binance['entryPrice']) if pos_binance['entryPrice'] != '0' else None
            unrealized_pnl = float(pos_binance['unRealizedProfit'])
            
            if symbol not in [p["symbol"] for p in PAIRS]:
                continue
            
            ps = state.get(symbol, {})
            if abs(amt) > 0.001:  # Posición abierta
                side = 'LONG' if amt > 0 else 'SHORT'
                print(f"Sync: {symbol} {side} real, amt={amt}, entry={entry_price}")
                ps.update({
                    'position': side,
                    'entryprice': entry_price,
                    'entryqty': abs(amt),
                    'sessionpnl': unrealized_pnl,
                    'tradeamountused': abs(amt) * entry_price
                })
            else:  # Cerrada en Binance
                if ps.get('position') in ['LONG', 'SHORT']:
                    print(f"Sync: {symbol} cerrada en Binance, limpiando local")
                    ps.update({
                        'position': 'FLAT', 'entryprice': None, 'entryqty': None,
                        'initialsl': None, 'tptarget': None, 'trailingsl': None,
                        'partialclosed': False, 'tradeamountused': None
                    })
        
        # Limpia posiciones solo locales
        for symbol in [p["symbol"] for p in PAIRS]:
            ps = state.get(symbol, {})
            if ps.get('position') in ['LONG', 'SHORT']:
                binance_pos = next((p for p in positions if p['symbol'] == symbol and float(p['positionAmt']) != 0), None)
                if not binance_pos:
                    print(f"Sync: {symbol} solo local, asumiendo cerrada")
                    ps.update({'position': 'FLAT', 'entryprice': None, 'entryqty': None,
                              'initialsl': None, 'tptarget': None, 'trailingsl': None,
                              'partialclosed': False, 'tradeamountused': None})
        
        print("Sync: Estado actualizado con Binance")
    
    except Exception as e:
        print(f"Sync error: {e}")

'''
    # Busca después de getfuturesbalance()
    pattern = r'def getfuturesbalance\(.*?\n\n\s*def '
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, r'def getfuturesbalance(.*?)\n\n' + sync_func + r'\n\s*def ', content, 1, re.DOTALL)
        print("✅ Función sync_positions_from_binance agregada")
    else:
        print("⚠️  No encontrado getfuturesbalance(), agregando al final")
        content += '\n' + sync_func
    return content

def add_sync_call(content):
    """Agrega llamada en runbotcycle()"""
    sync_call = "    sync_positions_from_binance(state)  # NUEVA: Valida posiciones con Binance\n"
    
    # Busca después de state = loadstate()
    pattern = r'state\s*=\s*loadstate\(\)\s*\n\s*balance\s*='
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, r'state = loadstate()\n' + sync_call + r'    balance = ', content, 1, re.DOTALL)
        print("✅ Llamada sync agregada en runbotcycle()")
    else:
        print("⚠️  No encontrado patrón en runbotcycle(), agregando manual")
        return content
    
    return content

def main():
    print("🚀 Actualizando bot.py con sync Binance...")
    
    content = read_bot_py()
    content = add_sync_function(content)
    content = add_sync_call(content)
    
    # Backup
    backup = 'bot.py.backup.' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.py'
    with open(backup, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"💾 Backup: {backup}")
    
    # Escribe nuevo bot.py
    with open('bot.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("✅ bot.py actualizado con sync Binance")
    
    print("\n📋 Para deploy:")
    print("1. git add bot.py")
    print("2. git commit -m 'add binance positions sync'")
    print("3. git push")
    print("4. pkill -f bot.py")
    print("5. nohup python3 bot.py > bot.log 2>&1 &")
    print("6. tail -f bot.log | grep Sync")

if __name__ == "__main__":
    from datetime import datetime
    main()
