# main.py
import os, re, asyncio, httpx
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, BackgroundTasks

# ---------- Configuración ----------
VERIFY_TOKEN = "miwhatsapitcambio"                      # el que pusiste en Meta
WHATS_TOKEN  = os.getenv("WHATS_TOKEN")                 # token largo
PHONE_ID     = os.getenv("PHONE_NUMBER_ID")             # phone-number-id
TTL          = timedelta(minutes=15)                    # cache 15 min

app = FastAPI()
_cache: dict[str, tuple[any, datetime]] = {}            # {key: (valor, expira)}

# ---------- Utilidades ----------
async def fetch_url(url: str, method: str = "GET", json=None):
    async with httpx.AsyncClient(timeout=15) as client:
        if method == "POST":
            r = await client.post(url, json=json, headers={"Content-Type": "application/json"})
        else:
            r = await client.get(url)
        r.raise_for_status()
        return r

def in_cache(key: str):
    return key in _cache and _cache[key][1] > datetime.utcnow()

def set_cache(key: str, value: any):
    _cache[key] = (value, datetime.utcnow() + TTL)

# ---------- Fetchers ----------
async def get_oficial():
    if in_cache("oficial"):
        return _cache["oficial"][0]
    r = await fetch_url("https://www.bcv.org.ve/")
    soup = BeautifulSoup(r.text, "html.parser")
    # Localiza la celda que contiene “Dólar estadounidense”
    tag = soup.find(string=re.compile("Dólar estadounidense", re.I))
    rate = float(
        tag.find_next("strong").text.strip()
          .replace(".", "")       # miles → nada
          .replace(",", ".")      # decimal → .
    )
    set_cache("oficial", rate)
    return rate

async def get_paralelo():
    if in_cache("paralelo"):
        return _cache["paralelo"][0]
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": "USDT",
        "fiat": "VES",
        "tradeType": "SELL",         # vendedores (tú compras VES)
        "page": 1, "rows": 5,
        "payTypes": [],
        "publisherType": None        # None = sin ‘Merchant only’
    }
    r = await fetch_url(url, method="POST", json=payload)
    data = r.json()["data"]

    # Filtra anuncios “proMerchantAds”: true (=promocionados) y toma el precio más bajo
    precios = [
        float(ad["adv"]["price"])
        for ad in data
        if not ad["adv"].get("proMerchantAds", False)
    ]
    best = min(precios) if precios else float(data[0]["adv"]["price"])
    set_cache("paralelo", best)
    return best

async def get_bancos():
    if in_cache("bancos"):
        return _cache["bancos"][0]
    url = "https://www.bcv.org.ve/tasas-informativas-sistema-bancario"
    r = await fetch_url(url)
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    bancos = {}
    if table:
        rows = table.find_all("tr")[1:]   # omite encabezado
        for tr in rows:
            cols = [c.get_text(strip=True) for c in tr.find_all("td")]
            if len(cols) >= 2:
                banco = cols[0]
                tasa = float(cols[1].replace(".", "").replace(",", "."))
                bancos[banco] = tasa
    set_cache("bancos", bancos)
    return bancos

# ---------- WhatsApp ---------
async def send_whatsapp(to: str, body: str):
    url = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body}
    }
    headers = {"Authorization": f"Bearer {WHATS_TOKEN}"}
    await fetch_url(url, method="POST", json=payload)
    
# ---------- Webhook ----------
@app.get("/webhook")
async def verify(req: Request):
    p = req.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == VERIFY_TOKEN:
        return int(p.get("hub.challenge"))
    return {"status": "error"}

@app.post("/webhook")
async def incoming(req: Request, bg: BackgroundTasks):
    data = await req.json()
    try:
        msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
        text = msg["text"]["body"].strip().lower()
        wa_id = msg["from"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    if "oficial" in text:
        rate = await get_oficial()
        reply = f"🔹 Oficial BCV: {rate:,.2f} Bs/USD"
    elif "paralelo" in text:
        rate = await get_paralelo()
        reply = f"🔸 Paralelo Binance: {rate:,.2f} Bs/USDT (mejor vendedor)"
    elif "bancos" in text or "mesas" in text:
        tabla = await get_bancos()
        reply = "🏦 Mesas Bancos (Bs/USD):\n" + "\n".join(f"{b}: {v:,.2f}" for b,v in tabla.items())
    else:
        reply = ("📋 Comandos:\n"
                 "• oficial – tasa BCV\n"
                 "• paralelo – mejor vendedor Binance\n"
                 "• bancos   – tasas informativas")

    # Enviar sin bloquear la respuesta HTTP
    bg.add_task(send_whatsapp, wa_id, reply)
    return {"status": "queued"}
