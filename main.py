import os, json, re, httpx, asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from bs4 import BeautifulSoup

# ---------------- CONFIG ----------------
VERIFY_TOKEN  = "miwhatsapitcambio"                 # el mismo que pegaste en Meta
PHONE_ID      = os.getenv("PHONE_NUMBER_ID")        # ID numérico, no el +58…
WHATS_TOKEN   = os.getenv("WHATS_TOKEN")            # token largo EAAG…
TTL           = timedelta(minutes=15)               # caché de 15 min

app    = FastAPI()
_cache = {}                                         # key: (valor, expiración)

# ---------------- UTILIDAD DE CACHÉ ----------------
def in_cache(key):      # devuelve True si la clave está y no expiró
    return key in _cache and _cache[key][1] > datetime.utcnow()

def set_cache(key, val):
    _cache[key] = (val, datetime.utcnow() + TTL)

# ---------------- WEBHOOK VERIFY ----------------
@app.get("/webhook")
async def verify(req: Request):
    q = req.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == VERIFY_TOKEN:
        return int(q["hub.challenge"])
    return {"status": "error"}

# ---------------- WEBHOOK MENSAJES ----------------
@app.post("/webhook")
async def incoming(req: Request):
    data = await req.json()
    try:
        msg  = data["entry"][0]["changes"][0]["value"]["messages"][0]
        text = msg["text"]["body"].strip().lower()
        waid = msg["from"]
    except Exception as e:
        print("Parse error:", e, json.dumps(data)[:300])
        return {"status": "ignored"}

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

# ---------------- ENVÍO WHATSAPP ----------------
async def send_whatsapp(to, body):
    url = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body}
    }
    headers = {"Authorization": f"Bearer {WHATS_TOKEN}"}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json=payload, headers=headers)
        if r.status_code >= 300:
            print("WA send error", r.status_code, r.text[:150])

# ---------------- HELPERS HTTP ----------------
async def fetch_url(url, *, method="GET", **kwargs):
    kwargs.setdefault("timeout", 15)
    kwargs.setdefault("verify", False)          # <- desactiva verificación TLS
    async with httpx.AsyncClient() as client:
        r = (await client.request(method, url, **kwargs))
    r.raise_for_status()
    return r

# ---------------- FETCHERS ----------------
async def get_oficial():
    if in_cache("oficial"):
        return _cache["oficial"][0]
    url = "https://www.bcv.org.ve/"
    try:
        html = (await fetch_url(url)).text
        tag  = BeautifulSoup(html, "html.parser").find("p", string=re.compile("Dólar estadounidense"))
        rate = float(
            tag.find_next("strong").text.strip()
                .replace(".", "")   # miles → nada
                .replace(",", ".")  # decimal → .
        )
        set_cache("oficial", rate)
        return rate
    except Exception as e:
        print("BCV fetch error:", e)
        return None

async def get_paralelo():
    if in_cache("paralelo"):
        return _cache["paralelo"][0]
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": "USDT",
        "fiat": "VES",
        "tradeType": "SELL",      # vendedores (tú compras USDT pagando VES)
        "page": 1,
        "rows": 1,
        "publisherType": None
    }
    try:
        r = await fetch_url(url, method="POST", json=payload)
        price = float(r.json()["data"][0]["adv"]["price"])
        set_cache("paralelo", price)
        return price
    except Exception as e:
        print("Binance fetch error:", e)
        return None

async def get_bancos():
    if in_cache("bancos"):
        return _cache["bancos"][0]
    url = "https://www.bcv.org.ve/tasas-informativas-sistema-bancario"
    try:
        html = (await fetch_url(url)).text
        soup = BeautifulSoup(html, "html.parser")
        tabla = {}
        for tr in soup.select("table tbody tr"):
            cols = [c.get_text(strip=True).replace(",", ".") for c in tr.find_all("td")]
            if len(cols) >= 3:
                banco, valor = cols[0], float(cols[2])
                tabla[banco] = valor
        if tabla:
            set_cache("bancos", tabla)
        return tabla or None
    except Exception as e:
        print("Mesas bancarias error:", e)
        return None
