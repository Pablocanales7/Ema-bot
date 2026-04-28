import os, time, subprocess, sys
from datetime import datetime

def load_env():
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_file):
        print("ERROR: No se encontro el archivo .env")
        sys.exit(1)
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                val = val.split("#")[0].strip()
                os.environ[key.strip()] = val
    print("Variables cargadas desde .env")

def run_bot():
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now}] Ejecutando bot...")
    result = subprocess.run([sys.executable, "bot.py"], capture_output=False)
    if result.returncode == 0:
        print(f"[{now}] Bot completado OK")
    else:
        print(f"[{now}] Bot termino con error (codigo {result.returncode})")

def main():
    load_env()
    INTERVAL = 30 * 60
    print("=" * 55)
    print(" EMA Bot v10.0 — Modo Local")
    print(" LONG + SHORT | ADX + MTF + Vol + Cooldown")
    print(" Intervalo: cada 30 minutos")
    print(" Presiona Ctrl+C para detener")
    print("=" * 55)
    run_bot()
    while True:
        next_run = time.time() + INTERVAL
        print(f"\nProxima ejecucion en 30 min...")
        while time.time() < next_run:
            remaining = int(next_run - time.time())
            mins, secs = divmod(remaining, 60)
            print(f" {mins:02d}:{secs:02d} restantes", end="\r")
            time.sleep(10)
        run_bot()

if __name__ == "__main__":
    main()
