"""
Bot Tipo de Cambio VE   –   Webhook WhatsApp Cloud
Comandos:
  oficial  → tasa BCV
  p2p      → mejor vendedor USDT/VES en Binance P2P
  bancos   → tasas informativas (mesas bancarias)
"""

import os, re, httpx
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from bs4 import BeautifulSoup

# ──────────────────────────── CONFIG ────────────────────────────
VERIFY_TOKEN  = "miwhatsapitcambio"                    # igual al de Meta
PHONE_ID      = os.getenv("PHONE_NUMBER_ID")           # solo dígitos
WHATS_TOKEN   = os.getenv("WHATS_TOKEN")               # token EAAG…
TTL           = timedelta(minutes=15)                  # caché en memoria

app    = FastAPI()
_cache = {}                                            # key → (value, expiry)

# ──────────────────── UTILIDADES GENERALES ─────────────────────
def get_cached(key):
    val, exp = _cache.get(key, (None, datetime.min))
    return val if exp > datetime.utcnow() else None

def set_cached(key, val):
    _cache[key] = (val, datetime.utcnow() + TTL)

async def http_request(method: str, url: str, **kwargs):
    timeout = kwargs.pop("timeout", 15)
    async with httpx.AsyncClient(timeout=timeout, verify=False) as c:
        return await c.request(method, url, **kwargs)

def bs(num: float) -> str:
    """Formatea 106.862000 → '106,86' (coma decimal, 2 decimales)."""
    return f"{num:.2f}".replace(".", ",")

# ───────────────────────── WEBHOOK VERIFY ──────────────────────
@app.get("/webhook")
async def verify(req: Request):
    qp = req.query_params
    if qp.get("hub.mode") == "subscribe" and qp.get("hub.verify_token") == VERIFY_TOKEN:
        return int(qp["hub.challenge"])
    return {"status": "error"}

# ───────────────────────── WEBHOOK POST ────────────────────────
@app.post("/webhook")
async def incoming(req: Request):
    data = await req.json()

    # Filtra mensajes de texto; ignora 'statuses'
    try:
        msg  = data["entry"][0]["changes"][0]["value"]["messages"][0]
        if msg.get("type") != "text":
            return {"status": "ignored"}
        text = msg["text"]["body"].strip().lower()
        waid = msg["from"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    # ----- Comandos -----
    if "oficial" in text:
        rate = await get_oficial()
        reply = f"📊 Oficial BCV: {bs(rate)} Bs/USD" if rate else "BCV fuera de línea"
    elif "p2p" in text or "paralelo" in text:
        rate = await get_paralelo()
        reply = f"🤝 Paralelo Binance: {bs(rate)} Bs/USDT" if rate else "Binance fuera de línea"
    elif "bancos" in text or "mesas" in text:
        tabla = await get_bancos()
        reply = ("\n".join(f"{b}: {bs(v)}" for b, v in tabla.items())
                 if tabla else "BCV aún no publica las tasas bancarias de hoy.")
    else:
        reply = ("Comandos:\n"
                 "• oficial – tasa BCV\n"
                 "• p2p     – mejor vendedor Binance\n"
                 "• bancos  – mesas bancarias")

    await send_whatsapp(waid, reply)
    return {"status": "sent"}

# ─────────────────────── ENVÍO WHATSAPP ────────────────────────
async def send_whatsapp(to, body):
    url = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
    payload = {"messaging_product": "whatsapp",
               "to": to,
               "type": "text",
               "text": {"preview_url": False, "body": body}}
    headers = {"Authorization": f"Bearer {WHATS_TOKEN}"}
    r = await http_request("POST", url, json=payload, headers=headers)
    if r.status_code >= 300:
        print("WA send error", r.status_code, r.text[:150])

# ────────────────────────── FETCHERS ───────────────────────────
async def get_oficial():
    """
    Obtiene el USD (Bs/USD) desde:
    https://www.bcv.org.ve/estadisticas/tipo-cambio-de-referencia-smc
    • Busca la fila donde la primera celda contenga 'Dólar' o 'USD'
    • Devuelve float con dos decimales.
    """
    if (rate := get_cached("oficial")) is not None:
        return rate

    URL = "https://www.bcv.org.ve/estadisticas/tipo-cambio-de-referencia-smc"

    try:
        html = (await http_request("GET", URL)).text
        soup = BeautifulSoup(html, "html.parser")

        # — Localiza la tabla de referencia —
        fila_usd = None
        for tr in soup.select("table tbody tr"):
            first_cell = tr.find("td")
            if first_cell and re.search(r"(usd|dólar)", first_cell.get_text(strip=True).lower()):
                fila_usd = tr
                break

        if not fila_usd:
            print("BCV parse: fila USD no encontrada")
            return None

        valor_txt = fila_usd.find_all("td")[-1].get_text(strip=True)
        # Convierte 106,8620 → 106.8620 y a float
        valor = float(valor_txt.replace(".", "").replace(",", "."))
        set_cached("oficial", valor)
        return valor

    except Exception as e:
        print("BCV fetch error:", e)
        return None


async def get_paralelo():
    if (rate := get_cached("paralelo")) is not None:
        return rate
    payload = {"asset": "USDT", "fiat": "VES", "tradeType": "SELL",
               "page": 1, "rows": 1, "publisherType": None}
    try:
        r = await http_request("POST",
                               "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
                               json=payload)
        rate = float(r.json()["data"][0]["adv"]["price"])
        set_cached("paralelo", rate)
        return rate
    except Exception as e:
        print("Binance fetch error:", e)
        return None

async def get_bancos():
    if (tabla := get_cached("bancos")) is not None:
        return tabla
    try:
        html = (await http_request("GET",
                                   "https://www.bcv.org.ve/tasas-informativas-sistema-bancario")).text
        soup = BeautifulSoup(html, "html.parser")
        tabla = {}
        for tr in soup.select("table tbody tr"):
            cols = [c.get_text(strip=True).replace(",", ".") for c in tr.find_all("td")]
            if len(cols) >= 3:
                tabla[cols[0]] = float(cols[2])
        if tabla:
            set_cached("bancos", tabla)
        return tabla or None
    except Exception as e:
        print("Mesas bancarias error:", e)
        return None
