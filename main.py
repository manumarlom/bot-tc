"""
Bot Tipo de Cambio VE  –  Webhook WhatsApp Cloud
Comandos:
  oficial  → tasa BCV
  p2p      → mejor vendedor USDT/VES en Binance P2P
  bancos   → tasas informativas (mesas bancarias)
"""

import os, re, json, httpx
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from bs4 import BeautifulSoup

# ---------- Configuración ----------
VERIFY_TOKEN  = "miwhatsapitcambio"                   # igual al pegado en Meta
PHONE_ID      = os.getenv("PHONE_NUMBER_ID")          # ID numérico (no el +58…)
WHATS_TOKEN   = os.getenv("WHATS_TOKEN")              # token EAAG…
TTL           = timedelta(minutes=15)                 # caché en memoria

app    = FastAPI()
_cache = {}                                           # key → (value, expiry)

# ---------- Utilidades ----------
def get_cached(key):
    val, exp = _cache.get(key, (None, datetime.min))
    return val if exp > datetime.utcnow() else None

def set_cached(key, val):
    _cache[key] = (val, datetime.utcnow() + TTL)

async def http_request(method: str, url: str, **kwargs):
    """Wrapper HTTP con verify=False (Render Free no trae CA completos)."""
    timeout = kwargs.pop("timeout", 15)
    async with httpx.AsyncClient(timeout=timeout, verify=False) as c:
        return await c.request(method, url, **kwargs)

def formatea_bs(num: float) -> str:
    """‘106,86’ – coma decimal y dos decimales, sin miles."""
    return f"{num:.2f}".replace(".", ",")

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

    # Filtra solo mensajes de texto
    try:
        msg  = data["entry"][0]["changes"][0]["value"]["messages"][0]
        if msg.get("type") != "text":
            return {"status": "ignored"}
        text = msg["text"]["body"].strip().lower()
        waid = msg["from"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    # Comandos
    if "oficial" in text:
        rate = await get_oficial()
        reply = (f"📊 Oficial BCV: {formatea_bs(rate)} Bs/USD"
                 if rate else "BCV fuera de línea")
    elif "p2p" in text or "paralelo" in text:
        rate = await get_paralelo()
        reply = (f"🤝 Paralelo Binance: {formatea_bs(rate)} Bs/USDT"
                 if rate else "Binance fuera de línea")
    elif "bancos" in text or "mesas" in text:
        tabla = await get_bancos()
        reply = ("\n".join(f"{b}: {formatea_bs(v)}" for b, v in tabla.items())
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
        print("WA send error", r.status_code, r.text[:150])

# ---------- Fetchers ----------
async def get_oficial():
    """
    Devuelve el USD del cuadro derecho del home del BCV.
    • Reconoce la fila donde el segundo <td> == 'USD'.
    • Convierte a float y cachea.
    """
    if (rate := get_cached("oficial")) is not None:
        return rate

    try:
        html = (await http_request("GET", "https://www.bcv.org.ve/")).text
        soup = BeautifulSoup(html, "html.parser")

        valor_txt = None
        for tr in soup.select("table tbody tr"):
            tds = tr.find_all("td")
            if len(tds) >= 3 and tds[1].get_text(strip=True).upper() == "USD":
                valor_txt = tds[2].get_text(strip=True)
                break

        if valor_txt:
            # ejemplo '106,86200000'  ó  '106.862.00000'
            valor_norm = valor_txt.replace(".", "").replace(",", ".")
            rate       = float(valor_norm)
            set_cached("oficial", rate)
            return rate

        print("BCV parse: fila USD no encontrada")
        return None

    except Exception as e:
        print("BCV fetch error:", e)
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
