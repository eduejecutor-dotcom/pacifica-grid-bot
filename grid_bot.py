"""
=============================================================
  FUTURES GRID BOT — PACIFICA.FI
  Inspirado en: Pionex "Bullish Trend - Moderate"
  Par: BTC-USDC | Leverage: 5x | Capital: $100 USDC
=============================================================

  LÓGICA DEL GRID BOT:
  ────────────────────
  1. Divide el rango de precio en N niveles (grids)
  2. Coloca órdenes BUY LIMIT en cada nivel bajo el precio actual
  3. Cuando una compra se llena → coloca SELL un grid arriba (TP)
  4. Cuando una venta se llena → coloca BUY un grid abajo (re-entrada)
  5. Cada viaje ida-vuelta = ganancia de 1 grid
  6. Funciona 24/7 — cuanto más mueve el precio, más gana

  PARÁMETROS (basados en Pionex Bullish Trend Moderate):
  ───────────────────────────────────────────────────────
  Rango:    $57,114 ~ $85,672
  Grids:    20 (ajustado para $100 USDC)
  Leverage: 5x
  Capital:  $100 USDC → $500 poder de compra

  PARA EL AIRDROP DE PACIFICA:
  ─────────────────────────────
  Cada grid que se llena = 1 trade = volumen generado
  20 grids activos = hasta 20 trades por movimiento del precio
  Volumen por trade = ($100/20) * 5x = $25 USDC
=============================================================
"""

import time
import json
import hashlib
import hmac
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

# ──────────────────────────────────────────────────────────
#   CONFIGURACIÓN — EDITA ESTOS VALORES
# ──────────────────────────────────────────────────────────

PACIFICA_API_KEY    = "TU_API_KEY_AQUI"
PACIFICA_API_SECRET = "TU_API_SECRET_AQUI"
PACIFICA_WALLET     = "TU_WALLET_SOLANA_AQUI"

TELEGRAM_TOKEN      = "8412005103:AAEOuZ5UK7eUn5HRpAePGBP3G65Q92lKZiw"
TELEGRAM_CHAT_ID    = "6983367737"

# ── Parámetros del Grid ───────────────────────────────────
SYMBOL              = "BTC-USDC"
LEVERAGE            = 5              # 5x como Pionex
TOTAL_CAPITAL_USDC  = 100            # Capital total en USDC
GRID_COUNT          = 20             # Número de grids
GRID_LOWER          = 65000.0        # Precio mínimo del rango
GRID_UPPER          = 78000.0        # Precio máximo del rango

CHECK_INTERVAL      = 15             # Segundos entre revisiones de órdenes

# ── Calculados automáticamente ────────────────────────────
USDC_PER_GRID       = TOTAL_CAPITAL_USDC / GRID_COUNT   # $5 por grid
GRID_SPACING        = (GRID_UPPER - GRID_LOWER) / GRID_COUNT
PACIFICA_BASE_URL   = "https://api.pacifica.fi/api/v1"
CHILE_TZ            = ZoneInfo("America/Santiago")

# ──────────────────────────────────────────────────────────
#   AUTENTICACIÓN PACIFICA
# ──────────────────────────────────────────────────────────

def get_timestamp():
    return str(int(time.time() * 1000))

def sign_request(secret, timestamp, method, path, body=""):
    msg = timestamp + method.upper() + path + body
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

def pacifica_headers(method, path, body=""):
    ts  = get_timestamp()
    sig = sign_request(PACIFICA_API_SECRET, ts, method, path, body)
    return {
        "Content-Type":     "application/json",
        "X-API-Key":        PACIFICA_API_KEY,
        "X-API-Timestamp":  ts,
        "X-API-Signature":  sig,
        "X-Wallet-Address": PACIFICA_WALLET,
    }

# ──────────────────────────────────────────────────────────
#   PACIFICA — API
# ──────────────────────────────────────────────────────────

def get_btc_price():
    """Obtiene precio actual de BTC desde Binance (más rápido)."""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=5
        )
        return float(resp.json()["price"])
    except:
        return None

def place_limit_order(side: str, price: float, size_usdc: float) -> dict | None:
    """Coloca orden límite en Pacifica."""
    path     = "/orders/create_limit"
    body_dict = {
        "symbol":            SYMBOL,
        "side":              side.upper(),
        "price":             round(price, 1),
        "size":              round(size_usdc, 2),
        "size_denomination": "USDC",
        "leverage":          LEVERAGE,
        "reduce_only":       False,
        "agent_wallet":      PACIFICA_API_KEY,
    }
    body_str = json.dumps(body_dict, separators=(",", ":"))
    try:
        headers = pacifica_headers("POST", path, body_str)
        resp    = requests.post(f"{PACIFICA_BASE_URL}{path}", headers=headers, data=body_str, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[Pacifica] Error orden {side} @ ${price}: {e}")
        return None

def cancel_order(order_id: str) -> bool:
    """Cancela una orden activa."""
    path     = f"/orders/{order_id}/cancel"
    body_str = json.dumps({"agent_wallet": PACIFICA_API_KEY}, separators=(",", ":"))
    try:
        headers = pacifica_headers("POST", path, body_str)
        resp    = requests.post(f"{PACIFICA_BASE_URL}{path}", headers=headers, data=body_str, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[Pacifica] Error cancelando {order_id}: {e}")
        return False

def get_open_orders() -> list:
    """Obtiene órdenes abiertas en Pacifica."""
    path = f"/orders?symbol={SYMBOL}&status=open"
    try:
        headers = pacifica_headers("GET", path)
        resp    = requests.get(f"{PACIFICA_BASE_URL}{path}", headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        print(f"[Pacifica] Error obteniendo órdenes: {e}")
        return []

def get_order_history() -> list:
    """Obtiene historial de órdenes (para detectar fills)."""
    path = f"/orders/history?symbol={SYMBOL}&limit=50"
    try:
        headers = pacifica_headers("GET", path)
        resp    = requests.get(f"{PACIFICA_BASE_URL}{path}", headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        print(f"[Pacifica] Error historial: {e}")
        return []

# ──────────────────────────────────────────────────────────
#   TELEGRAM
# ──────────────────────────────────────────────────────────

def send_telegram(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        ).raise_for_status()
    except Exception as e:
        print(f"[Telegram ERROR] {e}")

# ──────────────────────────────────────────────────────────
#   GRID BOT — LÓGICA PRINCIPAL
# ──────────────────────────────────────────────────────────

def calculate_grid_levels() -> list[float]:
    """Calcula los niveles de precio del grid."""
    levels = []
    for i in range(GRID_COUNT + 1):
        level = round(GRID_LOWER + i * GRID_SPACING, 1)
        levels.append(level)
    return levels

def initialize_grid(current_price: float, grid_levels: list[float]) -> dict:
    """
    Coloca las órdenes iniciales del grid:
    - BUY LIMIT en todos los niveles por debajo del precio actual
    - SELL LIMIT en todos los niveles por encima del precio actual
    """
    orders = {}   # {nivel: order_id}
    buy_count  = 0
    sell_count = 0

    print(f"\n[GRID] Inicializando {GRID_COUNT} grids...")
    print(f"[GRID] Precio actual: ${current_price:,.1f}")
    print(f"[GRID] Rango: ${GRID_LOWER:,.1f} — ${GRID_UPPER:,.1f}")
    print(f"[GRID] Spacing: ${GRID_SPACING:,.1f} por grid")

    for level in grid_levels:
        if level < current_price and level >= GRID_LOWER:
            # Compra límite bajo el precio actual
            result = place_limit_order("LONG", level, USDC_PER_GRID)
            if result and result.get("data"):
                order_id = result["data"].get("order_id", "")
                orders[level] = {"id": order_id, "side": "buy", "status": "open"}
                buy_count += 1
                print(f"  BUY  @ ${level:,.1f} — OK")
            time.sleep(0.2)

        elif level > current_price and level <= GRID_UPPER:
            # Venta límite sobre el precio actual
            result = place_limit_order("SHORT", level, USDC_PER_GRID)
            if result and result.get("data"):
                order_id = result["data"].get("order_id", "")
                orders[level] = {"id": order_id, "side": "sell", "status": "open"}
                sell_count += 1
                print(f"  SELL @ ${level:,.1f} — OK")
            time.sleep(0.2)

    return orders

class GridBot:
    def __init__(self):
        self.grid_levels    = calculate_grid_levels()
        self.orders         = {}         # nivel → {id, side, status}
        self.filled_orders  = []         # historial de fills
        self.total_trades   = 0
        self.total_volume   = 0.0
        self.profit_usdc    = 0.0
        self.running        = False
        self.last_known_ids = set()      # IDs ya procesados

    def start(self, current_price: float):
        """Inicia el grid colocando todas las órdenes iniciales."""
        hora = datetime.now(CHILE_TZ).strftime("%d/%m/%Y %H:%M")
        self.orders  = initialize_grid(current_price, self.grid_levels)
        self.running = True

        buys  = sum(1 for o in self.orders.values() if o["side"] == "buy")
        sells = sum(1 for o in self.orders.values() if o["side"] == "sell")

        msg = (
            f"🤖 GRID BOT INICIADO\n"
            f"{'='*28}\n"
            f"Hora: {hora} CLT\n"
            f"Par: {SYMBOL} | {LEVERAGE}x\n"
            f"{'='*28}\n"
            f"Rango: ${GRID_LOWER:,.0f} — ${GRID_UPPER:,.0f}\n"
            f"Grids: {GRID_COUNT} | Spacing: ${GRID_SPACING:,.0f}\n"
            f"Capital: ${TOTAL_CAPITAL_USDC} USDC\n"
            f"Por grid: ${USDC_PER_GRID:.1f} USDC\n"
            f"{'='*28}\n"
            f"Ordenes BUY activas:  {buys}\n"
            f"Ordenes SELL activas: {sells}\n"
            f"{'='*28}\n"
            f"Precio actual: ${current_price:,.1f}\n"
            f"Esperando fills..."
        )
        send_telegram(msg)
        print(f"\n[GRID] Bot iniciado — {buys} BUY + {sells} SELL activos")

    def check_fills(self):
        """Revisa si alguna orden se llenó y coloca la orden contraria."""
        history = get_order_history()
        for order in history:
            oid    = order.get("order_id", "")
            status = order.get("status", "")
            if status != "filled" or oid in self.last_known_ids:
                continue

            self.last_known_ids.add(oid)
            side        = order.get("side", "").lower()
            fill_price  = float(order.get("price", 0))
            fill_size   = float(order.get("size", USDC_PER_GRID))
            vol_trade   = fill_size * LEVERAGE
            hora        = datetime.now(CHILE_TZ).strftime("%d/%m/%Y %H:%M")

            self.total_trades += 1
            self.total_volume += vol_trade

            print(f"\n[FILL] {side.upper()} @ ${fill_price:,.1f} | Vol: ${vol_trade:.0f}")

            # Colocar orden contraria en el grid siguiente
            if side in ("long", "buy"):
                # Compra llenada → colocar venta 1 grid arriba
                target_price = round(fill_price + GRID_SPACING, 1)
                profit_per_grid = round((GRID_SPACING / fill_price) * fill_size * LEVERAGE, 4)
                self.profit_usdc += profit_per_grid

                if target_price <= GRID_UPPER:
                    result = place_limit_order("SHORT", target_price, fill_size)
                    if result and result.get("data"):
                        new_id = result["data"].get("order_id", "")
                        self.orders[target_price] = {"id": new_id, "side": "sell", "status": "open"}

                send_telegram(
                    f"✅ BUY LLENADO\n"
                    f"{'='*28}\n"
                    f"Hora: {hora} CLT\n"
                    f"Precio: ${fill_price:,.1f}\n"
                    f"Sell TP colocado @ ${target_price:,.1f}\n"
                    f"{'='*28}\n"
                    f"Trades hoy: {self.total_trades}\n"
                    f"Volumen: ${self.total_volume:,.0f} USDC\n"
                    f"Profit est.: ${self.profit_usdc:.4f} USDC"
                )

            elif side in ("short", "sell"):
                # Venta llenada → colocar compra 1 grid abajo (re-entrada)
                target_price = round(fill_price - GRID_SPACING, 1)

                if target_price >= GRID_LOWER:
                    result = place_limit_order("LONG", target_price, fill_size)
                    if result and result.get("data"):
                        new_id = result["data"].get("order_id", "")
                        self.orders[target_price] = {"id": new_id, "side": "buy", "status": "open"}

                send_telegram(
                    f"✅ SELL LLENADO\n"
                    f"{'='*28}\n"
                    f"Hora: {hora} CLT\n"
                    f"Precio: ${fill_price:,.1f}\n"
                    f"Buy re-entrada @ ${target_price:,.1f}\n"
                    f"{'='*28}\n"
                    f"Trades hoy: {self.total_trades}\n"
                    f"Volumen: ${self.total_volume:,.0f} USDC"
                )

    def check_price_out_of_range(self, current_price: float):
        """Avisa si el precio sale del rango del grid."""
        if current_price < GRID_LOWER:
            send_telegram(
                f"⚠️ PRECIO FUERA DE RANGO (ABAJO)\n"
                f"Precio: ${current_price:,.1f}\n"
                f"Minimo grid: ${GRID_LOWER:,.1f}\n"
                f"Considera ajustar el rango o detener el bot."
            )
        elif current_price > GRID_UPPER:
            send_telegram(
                f"⚠️ PRECIO FUERA DE RANGO (ARRIBA)\n"
                f"Precio: ${current_price:,.1f}\n"
                f"Maximo grid: ${GRID_UPPER:,.1f}\n"
                f"Considera ajustar el rango o detener el bot."
            )

# ──────────────────────────────────────────────────────────
#   LOOP PRINCIPAL
# ──────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  FUTURES GRID BOT — PACIFICA.FI")
    print(f"  Inspirado en Pionex Bullish Trend Moderate")
    print(f"  Par: {SYMBOL} | {LEVERAGE}x | ${TOTAL_CAPITAL_USDC} USDC")
    print(f"  Rango: ${GRID_LOWER:,.0f} — ${GRID_UPPER:,.0f}")
    print(f"  Grids: {GRID_COUNT} | Spacing: ${GRID_SPACING:,.1f}")
    print(f"  Por grid: ${USDC_PER_GRID:.1f} USDC")
    print("=" * 55)

    # Obtener precio actual
    current_price = None
    while current_price is None:
        current_price = get_btc_price()
        if current_price is None:
            print("[ERROR] No se pudo obtener precio BTC. Reintentando...")
            time.sleep(5)

    print(f"\n[GRID] Precio BTC actual: ${current_price:,.1f}")

    # Verificar que el precio esté dentro del rango
    if current_price < GRID_LOWER or current_price > GRID_UPPER:
        msg = (
            f"⚠️ El precio actual (${current_price:,.1f}) está FUERA del rango del grid\n"
            f"(${GRID_LOWER:,.0f} — ${GRID_UPPER:,.0f})\n\n"
            f"Ajusta GRID_LOWER y GRID_UPPER en el código."
        )
        print(f"\n[AVISO] {msg}")
        send_telegram(msg)
        return

    # Iniciar grid bot
    bot = GridBot()
    bot.start(current_price)

    last_report_hour = -1
    last_price_check = current_price

    # Loop principal
    while True:
        try:
            # Revisar fills de órdenes
            bot.check_fills()

            # Precio actual para monitoreo
            price = get_btc_price()
            if price:
                # Aviso si precio sale del rango
                if abs(price - last_price_check) > GRID_SPACING * 2:
                    bot.check_price_out_of_range(price)
                    last_price_check = price

                # Reporte horario
                hora_actual = datetime.now(CHILE_TZ).hour
                if hora_actual != last_report_hour:
                    hora     = datetime.now(CHILE_TZ).strftime("%d/%m/%Y %H:%M")
                    open_orders = get_open_orders()
                    send_telegram(
                        f"📊 REPORTE HORARIO — GRID BOT\n"
                        f"{'='*28}\n"
                        f"Hora: {hora} CLT\n"
                        f"Precio BTC: ${price:,.1f}\n"
                        f"{'='*28}\n"
                        f"Ordenes activas: {len(open_orders)}\n"
                        f"Trades completados: {bot.total_trades}\n"
                        f"Volumen generado: ${bot.total_volume:,.0f} USDC\n"
                        f"Profit estimado: ${bot.profit_usdc:.4f} USDC\n"
                        f"{'='*28}\n"
                        f"Rango: ${GRID_LOWER:,.0f} — ${GRID_UPPER:,.0f}\n"
                        f"Precio en rango: {'SI' if GRID_LOWER <= price <= GRID_UPPER else 'NO ⚠️'}"
                    )
                    last_report_hour = hora_actual

        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
