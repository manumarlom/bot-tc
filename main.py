"""
Bot Tipo de Cambio VE – Webhook WhatsApp Cloud
Responde a:
  oficial  → tasa BCV
  p2p      → mejor vendedor USDT/VES en Binance P2P
  bancos   → tasas informativas (mesas bancarias)
"""

import os, re, json, httpx, asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from bs4 import BeautifulSoup

# ---------- Configuración ----------
VERIFY_TOKEN  = "miwhatsapitcambio"                   # igual al pegado en Meta
PHONE_ID      = os.getenv("PHONE_NUMBER_ID")          # ID numérico (no el +58…)
WHATS_TOKEN   = os.getenv("WHATS_TOKEN")              # token EAAG…
TTL           = timedelta(minutes=15)                 # caché 15 min

app    = FastAPI()
_cache = {}                                           # key → (valor, expiración)

# ---------- Caché ----------
def get_cached(key):
    return _cache.get(key, (None, datetime.min))[0] \
           if key in _cache and _cache[key][1] > datetime.utcnow() else None

def set_cached(key, val):
    _cache[key] = (val, datetime.utcnow() + TTL)

# ---------- Cliente HTTP (verify=False centralizado) ----------
async def http_request(method: str, url: str, **kwargs):
    timeout = kwargs.pop("timeout", 15)
    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
        return await client.request(method, url, **kwargs)

# ---------- Webhook VERIFY ----------
@app.get("/webhook")
async def verify(req: Request):
    qp = req.query_params
    if qp.get("hub.mode") == "subscribe" and qp.get("hub.verify_token") == VERIFY_TOKEN:
        return int(qp["hub.challenge"])
    return {"status": "error"}

# ---------- Webhook MENSAJES ----------
@app.post("/webhook")
async def incoming(req: Request):
    data = await req.json()

    # –– Filtrar mensajes de texto ––
    try:
        msg  = data["entry"][0]["changes"][0]["value"]["messages"][0]
        if msg.get("type") != "text":
            return {"status": "ignored"}
        text = msg["text"]["body"].strip().lower()
        waid = msg["from"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    # –– Comandos ––
    if "oficial" in text:
        rate = await get_oficial()
        reply = f"📊 Oficial BCV: {rate:,.2f} Bs/USD" if rate else "BCV fuera de línea"
    elif "p2p" in text or "paralelo" in text:
        rate = await get_paralelo()
        reply = f"🤝 Paralelo Binance: {rate:,.2f} Bs/USDT" if rate else "Binance fuera de línea"
    elif "bancos" in text or "mesas" in text:
        tabla = await get_bancos()
        reply = ("\n".join(f"{b}: {v:,.2f}" for b, v in tabla.items())
                 if tabla else "BCV aún no publica las tasas bancarias de hoy.")
    else:
        reply = ("Comandos:\n"
                 "• oficial – tasa BCV\n"
                 "• p2p     – mejor vendedor Binance\n"
                 "• bancos  – mesas bancarias")

    await send_whatsapp(waid, reply)
    return {"status": "sent"}

# ---------- Envío WhatsApp ----------
async def send_whatsapp(to, body):
    url = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
    payload = {"messaging_product": "whatsapp",
               "to": to,
               "type": "text",
               "text": {"preview_url": False, "body": body}}
    headers = {"Authorization": f"Bearer {WHATS_TOKEN}"}
    r = await http_request("POST", url, json=payload, headers=headers)
    if r.status_code >= 300:
        print("WA send error", r.status_code, r.text[:160])

# ---------- Fetchers ----------
async def get_oficial():
    """Extrae el USD del cuadro a la derecha del home del BCV."""
    if (rate := get_cached("oficial")) is not None:
        return rate
    try:
        html = (await http_request("GET", "https://www.bcv.org.ve/")).text
        soup = BeautifulSoup(html, "html.parser")

        # El valor aparece en la tabla a la derecha: busca fila cuyo span contenga 'USD'
        usd_row = next((tr for tr in soup.select("table tbody tr")
                        if tr.find("td") and "usd" in tr.get_text(strip=True).lower()), None)
        if usd_row:
            valor_str = usd_row.find_all("td")[-1].get_text(strip=True)
        else:
            # Fallback: primera cifra con punto decimal (ej. 106,86200000)
            m = re.search(r"(\d{1,3}[.,]\d{2,})", html)
            valor_str = m.group(1) if m else None

        if valor_str:
            rate = float(valor_str.replace(".", "").replace(",", "."))
            set_cached("oficial", rate)
            return rate

        print("BCV parse: no USD found")
        return None
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
