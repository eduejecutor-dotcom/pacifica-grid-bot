"""
=============================================================
  PANEL WEB — GRID BOT PACIFICA.FI
  Estrategia: Long-only Bullish Trend (perpetuos one-way)
  - BUY limit orders en niveles bajo el precio actual
  - SELL reduce_only se colocan reactivamente al llenar BUYs
=============================================================
"""

import json
import math
import time
import uuid
import threading
import requests
import base58
import nacl.signing
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request, render_template_string

app      = Flask(__name__)
CHILE_TZ = ZoneInfo("America/Santiago")

# ──────────────────────────────────────────────────────────
#   CONFIGURACIÓN INICIAL
# ──────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "pacifica_api_key":    "",
    "pacifica_api_secret": "",
    "pacifica_wallet":     "",
    "telegram_token":      "8412005103:AAEOuZ5UK7eUn5HRpAePGBP3G65Q92lKZiw",
    "telegram_chat_id":    "6983367737",
    "symbol":              "BTC",
    "leverage":            5,
    "capital_usdc":        100,
    "grid_count":          20,
    "grid_lower":          50000.0,
    "grid_upper":          80000.0,
    "check_interval":      15,
}

bot_state = {
    "running":      False,
    "config":       DEFAULT_CONFIG.copy(),
    "status": {
        "trades_today":   0,
        "volume_today":   0.0,
        "profit_usdc":    0.0,
        "current_price":  0.0,
        "active_orders":  0,
        "last_fill":      "—",
        "started_at":     "—",
        "grid_spacing":   0.0,
        "price_in_range": False,
        "fills":          [],
    },
    "thread":       None,
    "stop_event":   threading.Event(),
    "known_fills":  set(),
}

# ──────────────────────────────────────────────────────────
#   PACIFICA API
# ──────────────────────────────────────────────────────────

def get_cfg():
    return bot_state["config"]

def sign_ed25519(private_key_b58: str, message: str) -> str:
    key_bytes = base58.b58decode(private_key_b58)
    if len(key_bytes) == 64:
        key_bytes = key_bytes[:32]
    signing_key = nacl.signing.SigningKey(key_bytes)
    signed = signing_key.sign(message.encode("utf-8"))
    return base58.b58encode(signed.signature).decode()

def get_btc_price():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": "BTCUSDT"}, timeout=5)
        return float(r.json()["price"])
    except:
        return 0.0

def place_limit_order(side, price, size_usdc, reduce_only=False):
    """
    side: "BUY" (abrir long) o "SELL" (cerrar long)
    reduce_only=True en SELL → nunca abre corto, solo cierra largo existente
    """
    cfg        = get_cfg()
    ts         = int(time.time() * 1000)
    btc_price  = get_btc_price() or price
    leverage   = cfg.get("leverage", 5)

    btc_amount = (size_usdc * leverage) / btc_price
    # Mínimo: Pacifica exige $10 notional calculado sobre el precio de la ORDEN
    # Usamos 10.5 para tener margen y redondeamos hacia ARRIBA a 5 decimales
    min_btc = 10.5 / price
    btc_amount = max(btc_amount, min_btc)
    btc_amount = math.ceil(btc_amount * 100000) / 100000  # ceil a 5 decimales

    pac_side = "bid" if side.upper() in ("BUY", "LONG") else "ask"

    signature_header = {
        "timestamp":     ts,
        "expiry_window": 5000,
        "type":          "create_order",
    }
    signature_payload = {
        "symbol":          "BTC",
        "side":            pac_side,
        "price":           str(int(round(price))),
        "amount":          f"{btc_amount:.5f}",
        "tif":             "GTC",
        "reduce_only":     reduce_only,
        "client_order_id": str(uuid.uuid4()),
    }
    message_dict = {**signature_header, "data": signature_payload}
    message_str  = json.dumps(message_dict, separators=(",", ":"), sort_keys=True)
    sig          = sign_ed25519(cfg["pacifica_api_secret"], message_str)

    request_body = {
        "account":       cfg["pacifica_wallet"],
        "agent_wallet":  cfg["pacifica_api_key"],
        "signature":     sig,
        "timestamp":     ts,
        "expiry_window": 5000,
        **signature_payload,
    }
    body_str = json.dumps(request_body, separators=(",", ":"))
    ro_tag   = " [reduce_only]" if reduce_only else ""
    try:
        base = "https://api.pacifica.fi/api/v1"
        resp = requests.post(f"{base}/orders/create",
                             headers={"Content-Type": "application/json"},
                             data=body_str, timeout=10)
        print(f"[Pacifica] {pac_side}{ro_tag} @ ${price:.0f} → {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError:
        print(f"[Pacifica] HTTP {resp.status_code} {side}{ro_tag} @ ${price}: {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"[Pacifica] Error {side} @ ${price}: {e}")
        return None

def cancel_all_orders():
    """Cancela todas las órdenes abiertas en BTC."""
    cfg = get_cfg()
    ts  = int(time.time() * 1000)
    signature_header = {
        "timestamp":     ts,
        "expiry_window": 5000,
        "type":          "cancel_all_orders",
    }
    signature_payload = {"symbol": "BTC"}
    message_dict = {**signature_header, "data": signature_payload}
    message_str  = json.dumps(message_dict, separators=(",", ":"), sort_keys=True)
    sig          = sign_ed25519(cfg["pacifica_api_secret"], message_str)
    request_body = {
        "account":       cfg["pacifica_wallet"],
        "agent_wallet":  cfg["pacifica_api_key"],
        "signature":     sig,
        "timestamp":     ts,
        "expiry_window": 5000,
        **signature_payload,
    }
    body_str = json.dumps(request_body, separators=(",", ":"))
    try:
        base = "https://api.pacifica.fi/api/v1"
        resp = requests.delete(f"{base}/orders/cancel-all",
                               headers={"Content-Type": "application/json"},
                               data=body_str, timeout=10)
        print(f"[Pacifica] cancel-all → {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError:
        print(f"[Pacifica] cancel-all HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"[Pacifica] Error cancel-all: {e}")
        return None

def get_open_orders():
    cfg = get_cfg()
    try:
        base = "https://api.pacifica.fi/api/v1"
        resp = requests.get(
            f"{base}/orders?symbol=BTC&status=open&account={cfg['pacifica_wallet']}",
            headers={"Content-Type": "application/json"}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except:
        return []

def get_order_history():
    cfg = get_cfg()
    try:
        base = "https://api.pacifica.fi/api/v1"
        resp = requests.get(
            f"{base}/orders/history?symbol=BTC&limit=50&account={cfg['pacifica_wallet']}",
            headers={"Content-Type": "application/json"}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except:
        return []

def send_telegram(msg):
    cfg = get_cfg()
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage",
            json={"chat_id": cfg["telegram_chat_id"], "text": msg},
            timeout=10)
    except:
        pass

# ──────────────────────────────────────────────────────────
#   GRID BOT — ESTRATEGIA LONG-ONLY BULLISH TREND
# ──────────────────────────────────────────────────────────

def initialize_grid(current_price, grid_levels, usdc_per_grid):
    """
    Solo coloca BUY (open long) en niveles BAJO el precio actual.
    Las SELL reduce_only se colocan reactivamente cuando se llenan los BUYs.
    Así nunca hay órdenes cortas — el bot es 100% long-only.
    """
    orders  = {}
    cfg     = get_cfg()
    placed  = 0
    failed  = 0

    for level in grid_levels:
        if cfg["grid_lower"] <= level < current_price:
            r = place_limit_order("BUY", level, usdc_per_grid, reduce_only=False)
            if r and r.get("data"):
                orders[level] = {"id": r["data"].get("order_id", ""), "side": "buy"}
                placed += 1
            else:
                failed += 1
            time.sleep(0.3)

    print(f"[GRID] Init: {placed} BUY órdenes colocadas, {failed} fallidas")
    return orders

def grid_bot_loop(stop_event):
    cfg            = get_cfg()
    grid_count     = cfg["grid_count"]
    grid_lower     = cfg["grid_lower"]
    grid_upper     = cfg["grid_upper"]
    capital        = cfg["capital_usdc"]
    leverage       = cfg["leverage"]
    check_interval = cfg["check_interval"]
    symbol         = cfg["symbol"]

    grid_spacing  = (grid_upper - grid_lower) / grid_count
    usdc_per_grid = capital / grid_count
    vol_per_trade = usdc_per_grid * leverage

    grid_levels = [round(grid_lower + i * grid_spacing, 1) for i in range(grid_count + 1)]

    current_price = get_btc_price()
    bot_state["status"]["current_price"]  = current_price
    bot_state["status"]["grid_spacing"]   = round(grid_spacing, 1)
    bot_state["status"]["started_at"]     = datetime.now(CHILE_TZ).strftime("%d/%m/%Y %H:%M")
    bot_state["status"]["price_in_range"] = grid_lower <= current_price <= grid_upper

    print(f"[GRID] Iniciando | BTC: ${current_price:,.1f} | Spacing: ${grid_spacing:,.1f} | Long-only")

    if not (grid_lower <= current_price <= grid_upper):
        msg = (f"⚠️ Precio ${current_price:,.1f} FUERA del rango "
               f"(${grid_lower:,.0f} — ${grid_upper:,.0f}). Ajusta los parámetros.")
        send_telegram(msg)
        bot_state["running"] = False
        return

    orders = initialize_grid(current_price, grid_levels, usdc_per_grid)
    buys   = sum(1 for o in orders.values() if o["side"] == "buy")

    send_telegram(
        f"🤖 Grid Bot LONG iniciado\n"
        f"Par: {symbol} | {leverage}x\n"
        f"Rango: ${grid_lower:,.0f} — ${grid_upper:,.0f}\n"
        f"Grids: {grid_count} | Spacing: ${grid_spacing:,.0f}\n"
        f"BUY órdenes: {buys}\n"
        f"Estrategia: Long-only (Bullish Trend)\n"
        f"Vol/trade: ${vol_per_trade:.0f} USDC"
    )

    known_fills   = bot_state["known_fills"]
    last_rpt_hour = -1

    while not stop_event.is_set():
        try:
            price = get_btc_price()
            if price:
                bot_state["status"]["current_price"]  = price
                bot_state["status"]["price_in_range"] = grid_lower <= price <= grid_upper

            history = get_order_history()
            for order in history:
                oid    = order.get("order_id", "")
                status = order.get("status", "")
                if status != "filled" or oid in known_fills:
                    continue

                known_fills.add(oid)
                side       = order.get("side", "").lower()
                fill_price = float(order.get("price", 0))
                fill_size  = float(order.get("size", usdc_per_grid))
                vol        = fill_size * leverage
                hora       = datetime.now(CHILE_TZ).strftime("%H:%M CLT")

                bot_state["status"]["trades_today"] += 1
                bot_state["status"]["volume_today"] += vol
                bot_state["status"]["last_fill"]     = f"{side.upper()} @ ${fill_price:,.1f} — {hora}"

                bot_state["status"]["fills"].insert(0, {
                    "side":  side.upper(),
                    "price": fill_price,
                    "time":  hora,
                    "vol":   round(vol, 2),
                })
                bot_state["status"]["fills"] = bot_state["status"]["fills"][:10]

                if side in ("long", "buy", "bid"):
                    # BUY se llenó → largo abierto → colocar SELL take-profit (reduce_only)
                    target = round(fill_price + grid_spacing, 1)
                    profit = round((grid_spacing / fill_price) * fill_size * leverage, 4)
                    bot_state["status"]["profit_usdc"] += profit
                    if target <= grid_upper:
                        r = place_limit_order("SELL", target, fill_size, reduce_only=True)
                        if r and r.get("data"):
                            orders[target] = {"id": r["data"].get("order_id", ""), "side": "sell"}
                    send_telegram(
                        f"✅ BUY llenado @ ${fill_price:,.1f}\n"
                        f"SELL TP → ${target:,.1f}\n"
                        f"Profit est: ${profit:.4f}\n"
                        f"Trades: {bot_state['status']['trades_today']} | Vol: ${bot_state['status']['volume_today']:,.0f}"
                    )
                else:
                    # SELL se llenó → largo cerrado con ganancia → re-colocar BUY
                    target = round(fill_price - grid_spacing, 1)
                    if target >= grid_lower:
                        r = place_limit_order("BUY", target, fill_size, reduce_only=False)
                        if r and r.get("data"):
                            orders[target] = {"id": r["data"].get("order_id", ""), "side": "buy"}
                    send_telegram(
                        f"💰 SELL TP @ ${fill_price:,.1f}\n"
                        f"BUY re-entrada → ${target:,.1f}\n"
                        f"Trades: {bot_state['status']['trades_today']} | Vol: ${bot_state['status']['volume_today']:,.0f}"
                    )

            open_orders = get_open_orders()
            bot_state["status"]["active_orders"] = len(open_orders)

            hora_actual = datetime.now(CHILE_TZ).hour
            if hora_actual != last_rpt_hour:
                send_telegram(
                    f"📊 Reporte horario Grid Bot\n"
                    f"BTC: ${price:,.1f}\n"
                    f"Trades: {bot_state['status']['trades_today']}\n"
                    f"Volumen: ${bot_state['status']['volume_today']:,.0f} USDC\n"
                    f"Profit: ${bot_state['status']['profit_usdc']:.4f} USDC\n"
                    f"Órdenes activas: {len(open_orders)}"
                )
                last_rpt_hour = hora_actual

        except Exception as e:
            print(f"[ERROR bot] {e}")

        stop_event.wait(check_interval)

    print("[GRID] Bot detenido.")
    bot_state["running"] = False

# ──────────────────────────────────────────────────────────
#   RUTAS WEB
# ──────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Grid Bot — Pacifica.fi</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', sans-serif; padding: 20px; }
  h1 { color: #58a6ff; margin-bottom: 4px; font-size: 1.4rem; }
  h2 { color: #8b949e; font-size: 0.85rem; font-weight: 400; margin-bottom: 20px; }
  h3 { color: #58a6ff; font-size: 1rem; margin-bottom: 12px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; max-width: 1000px; margin: 0 auto; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; }
  .card.full { grid-column: 1 / -1; }
  label { display: block; font-size: 0.82rem; color: #8b949e; margin-bottom: 4px; }
  input { width: 100%; background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
          color: #e6edf3; padding: 8px 10px; font-size: 0.9rem; margin-bottom: 12px; }
  input:focus { outline: none; border-color: #58a6ff; }
  .btn { width: 100%; padding: 10px; border: none; border-radius: 6px; font-size: 0.95rem;
         font-weight: 600; cursor: pointer; transition: opacity 0.2s; }
  .btn:hover { opacity: 0.85; }
  .btn-start  { background: #238636; color: #fff; }
  .btn-stop   { background: #da3633; color: #fff; }
  .btn-save   { background: #1f6feb; color: #fff; margin-bottom: 8px; }
  .btn-cancel { background: #6e40c9; color: #fff; margin-top: 8px; }
  .stat { display: flex; justify-content: space-between; align-items: center;
          padding: 8px 0; border-bottom: 1px solid #21262d; }
  .stat:last-child { border-bottom: none; }
  .stat-label { font-size: 0.83rem; color: #8b949e; }
  .stat-value { font-size: 0.95rem; font-weight: 600; color: #e6edf3; }
  .badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 0.78rem; font-weight: 600; }
  .badge-on  { background: #1a4731; color: #3fb950; }
  .badge-off { background: #3d1f1f; color: #f85149; }
  .badge-strategy { background: #1a3050; color: #58a6ff; font-size: 0.72rem; padding: 2px 8px; border-radius: 10px; }
  .fills-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; margin-top: 8px; }
  .fills-table th { color: #8b949e; text-align: left; padding: 6px 8px; border-bottom: 1px solid #21262d; }
  .fills-table td { padding: 6px 8px; border-bottom: 1px solid #161b22; }
  .buy  { color: #3fb950; }
  .sell { color: #f85149; }
  .warning { background: #3d2b1f; border: 1px solid #d29922; border-radius: 6px;
             padding: 10px 14px; font-size: 0.83rem; color: #d29922; margin-bottom: 16px; }
  .grid-info { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin-top: 12px; }
  .grid-info-item { background: #0d1117; border-radius: 6px; padding: 10px; text-align: center; }
  .grid-info-item .val { font-size: 1.1rem; font-weight: 700; color: #58a6ff; }
  .grid-info-item .lbl { font-size: 0.75rem; color: #8b949e; margin-top: 2px; }
  .strategy-box { background: #0d1117; border-radius: 6px; padding: 10px 14px; margin-top: 12px;
                  font-size: 0.8rem; color: #8b949e; line-height: 1.6; }
  .strategy-box b { color: #3fb950; }
</style>
</head>
<body>
<div style="max-width:1000px;margin:0 auto;">
  <h1>⚡ Grid Bot — Pacifica.fi</h1>
  <h2>Futures Grid Trading · <span class="badge-strategy">Long-only · Bullish Trend</span></h2>

  <div id="warning" class="warning" style="display:none">
    ⚠️ El precio actual está fuera del rango configurado. Ajusta los límites.
  </div>

  <div class="grid">

    <!-- STATUS -->
    <div class="card">
      <h3>📊 Estado del Bot</h3>
      <div class="stat">
        <span class="stat-label">Estado</span>
        <span id="st-status" class="badge badge-off">DETENIDO</span>
      </div>
      <div class="stat">
        <span class="stat-label">Precio BTC</span>
        <span id="st-price" class="stat-value">—</span>
      </div>
      <div class="stat">
        <span class="stat-label">Precio en rango</span>
        <span id="st-inrange" class="stat-value">—</span>
      </div>
      <div class="stat">
        <span class="stat-label">Órdenes activas</span>
        <span id="st-orders" class="stat-value">—</span>
      </div>
      <div class="stat">
        <span class="stat-label">Trades hoy</span>
        <span id="st-trades" class="stat-value">0</span>
      </div>
      <div class="stat">
        <span class="stat-label">Volumen hoy</span>
        <span id="st-volume" class="stat-value">$0</span>
      </div>
      <div class="stat">
        <span class="stat-label">Profit estimado</span>
        <span id="st-profit" class="stat-value">$0</span>
      </div>
      <div class="stat">
        <span class="stat-label">Último fill</span>
        <span id="st-lastfill" class="stat-value" style="font-size:0.82rem">—</span>
      </div>
      <div class="stat">
        <span class="stat-label">Iniciado</span>
        <span id="st-started" class="stat-value" style="font-size:0.82rem">—</span>
      </div>

      <div class="grid-info">
        <div class="grid-info-item">
          <div id="gi-spacing" class="val">—</div>
          <div class="lbl">Spacing $</div>
        </div>
        <div class="grid-info-item">
          <div id="gi-perGrid" class="val">—</div>
          <div class="lbl">$ por grid</div>
        </div>
        <div class="grid-info-item">
          <div id="gi-volTrade" class="val">—</div>
          <div class="lbl">Vol/trade</div>
        </div>
      </div>

      <div class="strategy-box">
        📈 <b>Long-only:</b> BUY bajo precio actual → cuando llena, SELL take-profit arriba.<br>
        Sin posiciones cortas. Gana en cada rebote del grid.
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:14px">
        <button class="btn btn-start" onclick="startBot()">▶ INICIAR</button>
        <button class="btn btn-stop"  onclick="stopBot()">■ DETENER</button>
      </div>
      <button class="btn btn-cancel" onclick="cancelOrders()">🗑 Cancelar todas las órdenes</button>
    </div>

    <!-- CONFIG -->
    <div class="card">
      <h3>⚙️ Configuración del Grid</h3>
      <label>Precio mínimo del rango (USDC)</label>
      <input id="grid_lower" type="number" placeholder="50000">
      <label>Precio máximo del rango (USDC)</label>
      <input id="grid_upper" type="number" placeholder="80000">
      <label>Número de grids</label>
      <input id="grid_count" type="number" placeholder="20" min="5" max="200">
      <label>Capital total (USDC)</label>
      <input id="capital_usdc" type="number" placeholder="100">
      <label>Apalancamiento (x)</label>
      <input id="leverage" type="number" placeholder="5" min="1" max="20">
      <button class="btn btn-save" onclick="saveConfig()">💾 Guardar cambios</button>
    </div>

    <!-- API KEYS -->
    <div class="card">
      <h3>🔑 API Keys — Pacifica</h3>
      <label>API Key (público)</label>
      <input id="pacifica_api_key" type="text" placeholder="Tu API Key de Pacifica">
      <label>API Secret (privado)</label>
      <input id="pacifica_api_secret" type="password" placeholder="Tu API Secret">
      <label>Wallet Solana (pública)</label>
      <input id="pacifica_wallet" type="text" placeholder="Tu dirección Solana">
      <button class="btn btn-save" onclick="saveConfig()">💾 Guardar credenciales</button>
    </div>

    <!-- HISTORIAL FILLS -->
    <div class="card">
      <h3>📋 Últimos Fills</h3>
      <table class="fills-table">
        <thead>
          <tr><th>Lado</th><th>Precio</th><th>Volumen</th><th>Hora</th></tr>
        </thead>
        <tbody id="fills-body">
          <tr><td colspan="4" style="color:#8b949e;text-align:center;padding:16px">Sin fills aún</td></tr>
        </tbody>
      </table>
    </div>

  </div>
</div>

<script>
function loadConfig() {
  fetch('/api/config').then(r => r.json()).then(cfg => {
    document.getElementById('grid_lower').value       = cfg.grid_lower;
    document.getElementById('grid_upper').value       = cfg.grid_upper;
    document.getElementById('grid_count').value       = cfg.grid_count;
    document.getElementById('capital_usdc').value     = cfg.capital_usdc;
    document.getElementById('leverage').value         = cfg.leverage;
    document.getElementById('pacifica_api_key').value = cfg.pacifica_api_key;
    document.getElementById('pacifica_wallet').value  = cfg.pacifica_wallet;
    updateGridInfo(cfg);
  });
}

function updateGridInfo(cfg) {
  const spacing  = ((cfg.grid_upper - cfg.grid_lower) / cfg.grid_count).toFixed(1);
  const perGrid  = (cfg.capital_usdc / cfg.grid_count).toFixed(2);
  const volTrade = (perGrid * cfg.leverage).toFixed(0);
  document.getElementById('gi-spacing').textContent  = '$' + spacing;
  document.getElementById('gi-perGrid').textContent  = '$' + perGrid;
  document.getElementById('gi-volTrade').textContent = '$' + volTrade;
}

function saveConfig() {
  const cfg = {
    grid_lower:          parseFloat(document.getElementById('grid_lower').value),
    grid_upper:          parseFloat(document.getElementById('grid_upper').value),
    grid_count:          parseInt(document.getElementById('grid_count').value),
    capital_usdc:        parseFloat(document.getElementById('capital_usdc').value),
    leverage:            parseInt(document.getElementById('leverage').value),
    pacifica_api_key:    document.getElementById('pacifica_api_key').value,
    pacifica_api_secret: document.getElementById('pacifica_api_secret').value || undefined,
    pacifica_wallet:     document.getElementById('pacifica_wallet').value,
  };
  fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg)})
    .then(r => r.json()).then(res => {
      alert(res.ok ? '✅ Configuración guardada' : '❌ Error: ' + res.error);
      updateGridInfo(cfg);
    });
}

function startBot() {
  fetch('/api/start', {method:'POST'}).then(r => r.json())
    .then(res => { if (!res.ok) alert('❌ ' + res.error); });
}

function stopBot() {
  fetch('/api/stop', {method:'POST'}).then(r => r.json())
    .then(res => { if (!res.ok) alert('❌ ' + res.error); });
}

function cancelOrders() {
  if (!confirm('¿Cancelar TODAS las órdenes abiertas en Pacifica?')) return;
  fetch('/api/cancel', {method:'POST'}).then(r => r.json())
    .then(res => { alert(res.ok ? '✅ Órdenes canceladas' : '❌ Error: ' + (res.error || 'desconocido')); });
}

function updateStatus() {
  fetch('/api/status').then(r => r.json()).then(s => {
    const running = s.running;
    document.getElementById('st-status').textContent  = running ? 'EN EJECUCIÓN' : 'DETENIDO';
    document.getElementById('st-status').className    = 'badge ' + (running ? 'badge-on' : 'badge-off');
    document.getElementById('st-price').textContent   = s.status.current_price ? '$' + s.status.current_price.toLocaleString('es-CL', {minimumFractionDigits:1}) : '—';
    document.getElementById('st-inrange').textContent = s.status.price_in_range ? '✅ Sí' : '⚠️ No';
    document.getElementById('st-orders').textContent  = s.status.active_orders;
    document.getElementById('st-trades').textContent  = s.status.trades_today;
    document.getElementById('st-volume').textContent  = '$' + s.status.volume_today.toLocaleString('es-CL', {minimumFractionDigits:0});
    document.getElementById('st-profit').textContent  = '$' + s.status.profit_usdc.toFixed(4);
    document.getElementById('st-lastfill').textContent = s.status.last_fill;
    document.getElementById('st-started').textContent  = s.status.started_at;
    document.getElementById('warning').style.display = (!running && s.status.current_price && !s.status.price_in_range) ? 'block' : 'none';

    const tbody = document.getElementById('fills-body');
    if (s.status.fills && s.status.fills.length > 0) {
      tbody.innerHTML = s.status.fills.map(f =>
        `<tr>
          <td class="${f.side === 'BUY' || f.side === 'BID' ? 'buy' : 'sell'}">${f.side}</td>
          <td>$${f.price.toLocaleString('es-CL')}</td>
          <td>$${f.vol}</td>
          <td>${f.time}</td>
        </tr>`
      ).join('');
    }
  });
}

loadConfig();
updateStatus();
setInterval(updateStatus, 5000);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = bot_state["config"].copy()
    cfg.pop("pacifica_api_secret", None)
    return jsonify(cfg)

@app.route("/api/config", methods=["POST"])
def api_set_config():
    if bot_state["running"]:
        return jsonify({"ok": False, "error": "Detén el bot antes de cambiar la configuración."})
    data = request.json
    for k, v in data.items():
        if k in bot_state["config"] and v is not None and v != "":
            bot_state["config"][k] = v
    return jsonify({"ok": True})

@app.route("/api/start", methods=["POST"])
def api_start():
    if bot_state["running"]:
        return jsonify({"ok": False, "error": "El bot ya está en ejecución."})
    cfg = bot_state["config"]
    if not cfg["pacifica_api_key"] or not cfg["pacifica_api_secret"]:
        return jsonify({"ok": False, "error": "Configura las API keys de Pacifica primero."})

    bot_state["status"]["trades_today"] = 0
    bot_state["status"]["volume_today"] = 0.0
    bot_state["status"]["profit_usdc"]  = 0.0
    bot_state["status"]["fills"]        = []
    bot_state["status"]["last_fill"]    = "—"
    bot_state["known_fills"]            = set()

    stop_evt = threading.Event()
    bot_state["stop_event"] = stop_evt
    bot_state["running"]    = True

    t = threading.Thread(target=grid_bot_loop, args=(stop_evt,), daemon=True)
    bot_state["thread"] = t
    t.start()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not bot_state["running"]:
        return jsonify({"ok": False, "error": "El bot no está en ejecución."})
    bot_state["stop_event"].set()
    bot_state["running"] = False
    return jsonify({"ok": True})

@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    cfg = bot_state["config"]
    if not cfg["pacifica_api_key"] or not cfg["pacifica_api_secret"]:
        return jsonify({"ok": False, "error": "Configura las API keys primero."})
    result = cancel_all_orders()
    if result is not None:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "No se pudo cancelar. Revisa los logs."})

@app.route("/api/status")
def api_status():
    return jsonify({
        "running": bot_state["running"],
        "status":  bot_state["status"],
    })

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
