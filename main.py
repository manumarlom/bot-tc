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

# ---------- Credenciales y config ----------
VERIFY_TOKEN  = "miwhatsapitcambio"                   # igual al pegado en Meta
PHONE_ID      = os.getenv("PHONE_NUMBER_ID")          # solo dígitos
WHATS_TOKEN   = os.getenv("WHATS_TOKEN")              # token EAAG…
TTL           = timedelta(minutes=15)                 # caché 15 min

app    = FastAPI()
_cache = {}                                           # key → (value, expiry)

# ---------- Utilidades de caché ----------
def cache_get(key):
    if key in _cache and _cache[key][1] > datetime.utcnow():
        return _cache[key][0]
    return None

def cache_set(key, val):
    _cache[key] = (val, datetime.utcnow() + TTL)

# ---------- Cliente HTTP (verify=False centralizado) ----------
async def http_request(method: str, url: str, **kwargs):
    """Pequeño wrapper que siempre usa verify=False para sortear SSL en Render Free."""
    timeout = kwargs.pop("timeout", 15)
    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
        return await client.request(method, url, **kwargs)

# ---------- Webhook VERIFY (GET) ----------
@app.get("/webhook")
async def verify(req: Request):
    qp = req.query_params
    if qp.get("hub.mode") == "subscribe" and qp.get("hub.verify_token") == VERIFY_TOKEN:
        return int(qp["hub.challenge"])
    return {"status": "error"}

# ---------- Webhook MENSAJES (POST) ----------
@app.post("/webhook")
async def incoming(req: Request):
    data = await req.json()

    # —— Filtra solo mensajes de texto; ignora 'statuses', etc. ——
    try:
        msg  = data["entry"][0]["changes"][0]["value"]["messages"][0]
        if msg.get("type") != "text":
            return {"status": "ignored"}
        text = msg["text"]["body"].strip().lower()
        waid = msg["from"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    # —— Comandos ——
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

# ---------- Enviar mensaje WhatsApp ----------
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
    if (rate := cache_get("oficial")) is not None:
        return rate
    try:
        html = (await http_request("GET", "https://www.bcv.org.ve/")).text
        tag  = BeautifulSoup(html, "html.parser").find("p", string=re.compile("Dólar estadounidense"))
        rate = float(
            tag.find_next("strong").text.strip()
                .replace(".", "")    # miles sep
                .replace(",", ".")   # decimal
        )
        cache_set("oficial", rate)
        return rate
    except Exception as e:
        print("BCV fetch error:", e)
        return None

async def get_paralelo():
    if (rate := cache_get("paralelo")) is not None:
        return rate
    payload = {
        "asset": "USDT",
        "fiat": "VES",
        "tradeType": "SELL",      # vendedores → tú compras USDT
        "page": 1,
        "rows": 1,
        "publisherType": None
    }
    try:
        r = await http_request("POST",
                               "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
                               json=payload)
        rate = float(r.json()["data"][0]["adv"]["price"])
        cache_set("paralelo", rate)
        return rate
    except Exception as e:
        print("Binance fetch error:", e)
        return None

async def get_bancos():
    if (tabla := cache_get("bancos")) is not None:
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
            cache_set("bancos", tabla)
        return tabla or None
    except Exception as e:
        print("Mesas bancarias error:", e)
        return None
