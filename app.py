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

    # Mapa de seguimiento: order_id → {price, side}
    # Cuando una orden desaparece de "abiertas" = se llenó
    order_map = {
        info["id"]: {"price": level, "side": info["side"]}
        for level, info in orders.items()
        if info["id"]
    }

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

            # ── Detección de fills por desaparición de órdenes abiertas ──
            open_orders  = get_open_orders()
            current_ids  = {o.get("order_id", "") for o in open_orders if o.get("order_id")}
            bot_state["status"]["active_orders"] = len(open_orders)

            for oid in list(order_map.keys()):
                if oid in current_ids or oid in known_fills:
                    continue  # sigue abierta o ya procesada

                # Desapareció → se llenó
                known_fills.add(oid)
                details    = order_map.pop(oid)
                side       = details["side"]
                fill_price = details["price"]
                hora       = datetime.now(CHILE_TZ).strftime("%H:%M CLT")
                vol        = usdc_per_grid * leverage

                print(f"[FILL] {side.upper()} @ ${fill_price:,.0f} detectado por desaparición")

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

                if side == "buy":
                    # BUY llenado → colocar SELL TP reduce_only arriba
                    target = round(fill_price + grid_spacing, 1)
                    profit = round((grid_spacing / fill_price) * usdc_per_grid * leverage, 4)
                    bot_state["status"]["profit_usdc"] += profit
                    if target <= grid_upper:
                        r = place_limit_order("SELL", target, usdc_per_grid, reduce_only=True)
                        if r and r.get("data"):
                            new_id = r["data"].get("order_id", "")
                            order_map[new_id] = {"price": target, "side": "sell"}
                    send_telegram(
                        f"✅ BUY llenado @ ${fill_price:,.1f}\n"
                        f"SELL TP → ${target:,.1f}\n"
                        f"Profit est: ${profit:.4f}\n"
                        f"Trades: {bot_state['status']['trades_today']} | Vol: ${bot_state['status']['volume_today']:,.0f}"
                    )
                else:
                    # SELL TP llenado → re-colocar BUY abajo
                    target = round(fill_price - grid_spacing, 1)
                    if target >= grid_lower:
                        r = place_limit_order("BUY", target, usdc_per_grid, reduce_only=False)
                        if r and r.get("data"):
                            new_id = r["data"].get("order_id", "")
                            order_map[new_id] = {"price": target, "side": "buy"}
                    send_telegram(
                        f"💰 SELL TP @ ${fill_price:,.1f}\n"
                        f"BUY re-entrada → ${target:,.1f}\n"
                        f"Trades: {bot_state['status']['trades_today']} | Vol: ${bot_state['status']['volume_today']:,.0f}"
                    )

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
<title>GRID BOT // PACIFICA.fi</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --sol-green:  #14F195;
    --sol-purple: #9945FF;
    --sol-blue:   #03E1FF;
    --bg:         #05050f;
    --bg2:        #0b0b1a;
    --bg3:        #0f0f22;
    --border:     rgba(153,69,255,0.3);
    --border-g:   rgba(20,241,149,0.25);
    --text:       #c8d0e0;
    --dim:        #4a5568;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Rajdhani', sans-serif;
    font-size: 15px;
    padding: 24px 20px;
    min-height: 100vh;
    background-image:
      linear-gradient(rgba(153,69,255,0.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(153,69,255,0.03) 1px, transparent 1px);
    background-size: 40px 40px;
  }

  /* scanline overlay */
  body::before {
    content: '';
    position: fixed; top:0; left:0; right:0; bottom:0;
    background: repeating-linear-gradient(
      0deg, transparent, transparent 2px,
      rgba(0,0,0,0.07) 2px, rgba(0,0,0,0.07) 4px
    );
    pointer-events: none;
    z-index: 9999;
  }

  .wrap { max-width: 1020px; margin: 0 auto; }

  /* HEADER */
  .header { margin-bottom: 28px; position: relative; }
  .header-title {
    font-family: 'Share Tech Mono', monospace;
    font-size: 1.7rem;
    color: var(--sol-green);
    text-shadow: 0 0 20px rgba(20,241,149,0.6), 0 0 40px rgba(20,241,149,0.2);
    letter-spacing: 2px;
  }
  .header-title span { color: var(--sol-purple); text-shadow: 0 0 20px rgba(153,69,255,0.7); }
  .header-sub {
    font-size: 0.78rem;
    color: var(--dim);
    letter-spacing: 3px;
    text-transform: uppercase;
    margin-top: 4px;
    font-family: 'Share Tech Mono', monospace;
  }
  .header-tag {
    display: inline-block;
    font-size: 0.68rem;
    padding: 2px 10px;
    border: 1px solid var(--sol-purple);
    color: var(--sol-purple);
    letter-spacing: 2px;
    margin-left: 12px;
    text-shadow: 0 0 8px rgba(153,69,255,0.5);
    box-shadow: 0 0 8px rgba(153,69,255,0.2);
  }

  /* GRID LAYOUT */
  .panels { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }

  /* CARD */
  .card {
    background: var(--bg2);
    border: 1px solid var(--border);
    padding: 20px;
    position: relative;
    clip-path: polygon(0 0, calc(100% - 12px) 0, 100% 12px, 100% 100%, 0 100%);
    box-shadow: 0 0 20px rgba(153,69,255,0.08), inset 0 0 30px rgba(153,69,255,0.03);
  }
  .card::before {
    content: '';
    position: absolute; top:0; left:0; right:0; height:1px;
    background: linear-gradient(90deg, transparent, var(--sol-purple), var(--sol-green), transparent);
    opacity: 0.6;
  }
  .card-title {
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.72rem;
    color: var(--sol-purple);
    letter-spacing: 3px;
    text-transform: uppercase;
    margin-bottom: 16px;
    text-shadow: 0 0 10px rgba(153,69,255,0.5);
  }
  .card-title::before { content: '// '; opacity: 0.5; }

  /* STATS */
  .stat {
    display: flex; justify-content: space-between; align-items: center;
    padding: 7px 0;
    border-bottom: 1px solid rgba(153,69,255,0.1);
  }
  .stat:last-of-type { border-bottom: none; }
  .stat-label {
    font-size: 0.78rem;
    color: var(--dim);
    letter-spacing: 1px;
    font-family: 'Share Tech Mono', monospace;
  }
  .stat-value {
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.9rem;
    color: var(--sol-blue);
    text-shadow: 0 0 8px rgba(3,225,255,0.4);
  }

  /* BADGES */
  .badge {
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.72rem;
    padding: 3px 12px;
    letter-spacing: 2px;
    border: 1px solid;
    clip-path: polygon(6px 0%, 100% 0%, calc(100% - 6px) 100%, 0% 100%);
  }
  .badge-on {
    border-color: var(--sol-green);
    color: var(--sol-green);
    background: rgba(20,241,149,0.08);
    text-shadow: 0 0 10px rgba(20,241,149,0.7);
    box-shadow: 0 0 12px rgba(20,241,149,0.2);
  }
  .badge-off {
    border-color: #ff4466;
    color: #ff4466;
    background: rgba(255,68,102,0.08);
    text-shadow: 0 0 10px rgba(255,68,102,0.5);
  }

  /* GRID INFO BOXES */
  .grid-info { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin: 14px 0; }
  .gi {
    background: var(--bg3);
    border: 1px solid var(--border-g);
    padding: 10px 8px;
    text-align: center;
    clip-path: polygon(4px 0%, 100% 0%, calc(100% - 4px) 100%, 0% 100%);
  }
  .gi .val {
    font-family: 'Share Tech Mono', monospace;
    font-size: 1rem;
    color: var(--sol-green);
    text-shadow: 0 0 10px rgba(20,241,149,0.5);
  }
  .gi .lbl {
    font-size: 0.65rem;
    color: var(--dim);
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-top: 2px;
  }

  /* STRATEGY BOX */
  .strategy-box {
    background: rgba(20,241,149,0.04);
    border: 1px solid rgba(20,241,149,0.15);
    border-left: 3px solid var(--sol-green);
    padding: 10px 14px;
    font-size: 0.78rem;
    color: var(--dim);
    line-height: 1.7;
    margin: 14px 0;
    font-family: 'Share Tech Mono', monospace;
  }
  .strategy-box b { color: var(--sol-green); }

  /* BUTTONS */
  .btn-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 14px; }
  .btn {
    width: 100%;
    padding: 10px 6px;
    border: none;
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.78rem;
    letter-spacing: 2px;
    cursor: pointer;
    transition: all 0.2s;
    clip-path: polygon(8px 0%, 100% 0%, calc(100% - 8px) 100%, 0% 100%);
    text-transform: uppercase;
  }
  .btn-start {
    background: linear-gradient(135deg, rgba(20,241,149,0.15), rgba(20,241,149,0.05));
    color: var(--sol-green);
    border: 1px solid var(--sol-green);
    text-shadow: 0 0 8px rgba(20,241,149,0.6);
    box-shadow: 0 0 15px rgba(20,241,149,0.15);
  }
  .btn-start:hover {
    background: rgba(20,241,149,0.2);
    box-shadow: 0 0 25px rgba(20,241,149,0.3);
  }
  .btn-stop {
    background: linear-gradient(135deg, rgba(255,68,102,0.15), rgba(255,68,102,0.05));
    color: #ff4466;
    border: 1px solid #ff4466;
    text-shadow: 0 0 8px rgba(255,68,102,0.6);
    box-shadow: 0 0 15px rgba(255,68,102,0.1);
  }
  .btn-stop:hover { background: rgba(255,68,102,0.2); }
  .btn-save {
    background: linear-gradient(135deg, rgba(153,69,255,0.2), rgba(153,69,255,0.05));
    color: var(--sol-purple);
    border: 1px solid var(--sol-purple);
    text-shadow: 0 0 8px rgba(153,69,255,0.6);
    box-shadow: 0 0 15px rgba(153,69,255,0.15);
    margin-bottom: 0;
  }
  .btn-save:hover { background: rgba(153,69,255,0.25); box-shadow: 0 0 25px rgba(153,69,255,0.3); }
  .btn-cancel {
    width: 100%;
    margin-top: 8px;
    background: rgba(255,68,102,0.05);
    color: #ff4466;
    border: 1px solid rgba(255,68,102,0.4);
    opacity: 0.7;
  }
  .btn-cancel:hover { opacity: 1; }

  /* INPUTS */
  label {
    display: block;
    font-size: 0.68rem;
    color: var(--dim);
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 4px;
    font-family: 'Share Tech Mono', monospace;
  }
  input {
    width: 100%;
    background: var(--bg3);
    border: 1px solid var(--border);
    color: var(--sol-blue);
    padding: 8px 12px;
    font-size: 0.88rem;
    margin-bottom: 12px;
    font-family: 'Share Tech Mono', monospace;
    outline: none;
    transition: border-color 0.2s, box-shadow 0.2s;
  }
  input:focus {
    border-color: var(--sol-purple);
    box-shadow: 0 0 12px rgba(153,69,255,0.25);
  }

  /* FILLS TABLE */
  .fills-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; margin-top: 8px; }
  .fills-table th {
    font-family: 'Share Tech Mono', monospace;
    color: var(--dim);
    text-align: left;
    padding: 6px 8px;
    border-bottom: 1px solid var(--border);
    letter-spacing: 1px;
    font-size: 0.68rem;
    text-transform: uppercase;
  }
  .fills-table td {
    padding: 7px 8px;
    border-bottom: 1px solid rgba(153,69,255,0.06);
    font-family: 'Share Tech Mono', monospace;
  }
  .buy  { color: var(--sol-green); text-shadow: 0 0 6px rgba(20,241,149,0.4); }
  .sell { color: #ff4466; text-shadow: 0 0 6px rgba(255,68,102,0.4); }

  /* WARNING */
  .warning {
    background: rgba(255,180,0,0.05);
    border: 1px solid rgba(255,180,0,0.4);
    border-left: 3px solid #ffb400;
    padding: 10px 16px;
    font-size: 0.78rem;
    color: #ffb400;
    margin-bottom: 18px;
    font-family: 'Share Tech Mono', monospace;
    letter-spacing: 1px;
  }

  /* PULSE animation for running state */
  @keyframes pulse-green {
    0%, 100% { box-shadow: 0 0 12px rgba(20,241,149,0.2); }
    50%       { box-shadow: 0 0 24px rgba(20,241,149,0.45); }
  }
  .badge-on { animation: pulse-green 2s ease-in-out infinite; }

  /* ticker animation */
  @keyframes ticker {
    0%   { opacity: 0.6; }
    50%  { opacity: 1; }
    100% { opacity: 0.6; }
  }
  #st-price { animation: ticker 3s ease-in-out infinite; }
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <div class="header-title">⚡ GRID_BOT <span>//</span> PACIFICA.fi</div>
    <div class="header-sub">
      Futures Grid Trading
      <span class="header-tag">LONG-ONLY · BULLISH</span>
      <span class="header-tag">SOL-CHAIN</span>
    </div>
  </div>

  <div id="warning" class="warning" style="display:none">
    !! PRECIO FUERA DE RANGO — AJUSTA LOS LIMITES DEL GRID
  </div>

  <div class="panels">

    <!-- STATUS -->
    <div class="card">
      <div class="card-title">system_status</div>
      <div class="stat">
        <span class="stat-label">estado</span>
        <span id="st-status" class="badge badge-off">OFFLINE</span>
      </div>
      <div class="stat">
        <span class="stat-label">btc_price</span>
        <span id="st-price" class="stat-value">—</span>
      </div>
      <div class="stat">
        <span class="stat-label">en_rango</span>
        <span id="st-inrange" class="stat-value">—</span>
      </div>
      <div class="stat">
        <span class="stat-label">ordenes_activas</span>
        <span id="st-orders" class="stat-value">—</span>
      </div>
      <div class="stat">
        <span class="stat-label">trades_24h</span>
        <span id="st-trades" class="stat-value">0</span>
      </div>
      <div class="stat">
        <span class="stat-label">volumen_24h</span>
        <span id="st-volume" class="stat-value">$0</span>
      </div>
      <div class="stat">
        <span class="stat-label">profit_est</span>
        <span id="st-profit" class="stat-value">$0.0000</span>
      </div>
      <div class="stat">
        <span class="stat-label">ultimo_fill</span>
        <span id="st-lastfill" class="stat-value" style="font-size:0.75rem">—</span>
      </div>
      <div class="stat">
        <span class="stat-label">uptime_desde</span>
        <span id="st-started" class="stat-value" style="font-size:0.75rem">—</span>
      </div>

      <div class="grid-info">
        <div class="gi"><div id="gi-spacing" class="val">—</div><div class="lbl">spacing</div></div>
        <div class="gi"><div id="gi-perGrid" class="val">—</div><div class="lbl">$/grid</div></div>
        <div class="gi"><div id="gi-volTrade" class="val">—</div><div class="lbl">vol/trade</div></div>
      </div>

      <div class="strategy-box">
        &gt; <b>LONG_ONLY:</b> BUY bajo precio → fill → SELL_TP arriba<br>
        &gt; Sin cortos. Cicla en cada rebote del grid.
      </div>

      <div class="btn-row">
        <button class="btn btn-start" onclick="startBot()">▶ INICIAR</button>
        <button class="btn btn-stop"  onclick="stopBot()">■ DETENER</button>
      </div>
      <button class="btn btn-cancel" onclick="cancelOrders()">⊗ CANCELAR TODAS LAS ORDENES</button>
    </div>

    <!-- CONFIG -->
    <div class="card">
      <div class="card-title">grid_config</div>
      <label>precio_minimo [ usdc ]</label>
      <input id="grid_lower" type="number" placeholder="50000">
      <label>precio_maximo [ usdc ]</label>
      <input id="grid_upper" type="number" placeholder="80000">
      <label>num_grids</label>
      <input id="grid_count" type="number" placeholder="20" min="5" max="200">
      <label>capital_total [ usdc ]</label>
      <input id="capital_usdc" type="number" placeholder="100">
      <label>apalancamiento [ x ]</label>
      <input id="leverage" type="number" placeholder="5" min="1" max="20">
      <button class="btn btn-save" onclick="saveConfig()">// GUARDAR CONFIG</button>
    </div>

    <!-- API KEYS -->
    <div class="card">
      <div class="card-title">api_credentials</div>
      <label>agent_wallet [ public ]</label>
      <input id="pacifica_api_key" type="text" placeholder="API Key de Pacifica">
      <label>private_key [ secret ]</label>
      <input id="pacifica_api_secret" type="password" placeholder="API Secret">
      <label>wallet_solana [ pubkey ]</label>
      <input id="pacifica_wallet" type="text" placeholder="Dirección Solana">
      <button class="btn btn-save" onclick="saveConfig()">// GUARDAR CREDENCIALES</button>
    </div>

    <!-- FILLS -->
    <div class="card">
      <div class="card-title">fill_history</div>
      <table class="fills-table">
        <thead>
          <tr><th>lado</th><th>precio</th><th>vol_usd</th><th>hora</th></tr>
        </thead>
        <tbody id="fills-body">
          <tr><td colspan="4" style="color:#4a5568;text-align:center;padding:20px;font-family:'Share Tech Mono',monospace;letter-spacing:2px">-- NO_FILLS --</td></tr>
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
      alert(res.ok ? '// CONFIG_SAVED' : '!! ERROR: ' + res.error);
      updateGridInfo(cfg);
    });
}

function startBot() {
  fetch('/api/start', {method:'POST'}).then(r => r.json())
    .then(res => { if (!res.ok) alert('!! ' + res.error); });
}

function stopBot() {
  fetch('/api/stop', {method:'POST'}).then(r => r.json())
    .then(res => { if (!res.ok) alert('!! ' + res.error); });
}

function cancelOrders() {
  if (!confirm('CANCELAR TODAS LAS ORDENES ABIERTAS EN PACIFICA?')) return;
  fetch('/api/cancel', {method:'POST'}).then(r => r.json())
    .then(res => { alert(res.ok ? '// ORDERS_CANCELLED' : '!! ERROR: ' + (res.error || 'unknown')); });
}

function updateStatus() {
  fetch('/api/status').then(r => r.json()).then(s => {
    const running = s.running;
    document.getElementById('st-status').textContent = running ? 'ONLINE' : 'OFFLINE';
    document.getElementById('st-status').className   = 'badge ' + (running ? 'badge-on' : 'badge-off');
    document.getElementById('st-price').textContent  = s.status.current_price
      ? '$' + s.status.current_price.toLocaleString('es-CL', {minimumFractionDigits:1}) : '—';
    document.getElementById('st-inrange').textContent = s.status.price_in_range ? '[ OK ]' : '[ OUT ]';
    document.getElementById('st-inrange').style.color = s.status.price_in_range ? 'var(--sol-green)' : '#ffb400';
    document.getElementById('st-orders').textContent  = s.status.active_orders;
    document.getElementById('st-trades').textContent  = s.status.trades_today;
    document.getElementById('st-volume').textContent  = '$' + s.status.volume_today.toLocaleString('es-CL', {minimumFractionDigits:0});
    document.getElementById('st-profit').textContent  = '$' + s.status.profit_usdc.toFixed(4);
    document.getElementById('st-lastfill').textContent = s.status.last_fill;
    document.getElementById('st-started').textContent  = s.status.started_at;
    document.getElementById('warning').style.display  =
      (!running && s.status.current_price && !s.status.price_in_range) ? 'block' : 'none';

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

@app.route("/api/debug/history")
def api_debug_history():
    """Muestra los últimos 5 órdenes del historial con todos sus campos — para debug."""
    orders = get_order_history()
    return jsonify({"count": len(orders), "orders": orders[:5]})

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
