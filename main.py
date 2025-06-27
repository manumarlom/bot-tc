# main.py  –  Bot Tipo de Cambio VE  (WhatsApp Cloud)

import os, re, httpx
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from bs4 import BeautifulSoup
from collections import deque

# ───────────── Config ─────────────
VERIFY_TOKEN  = "miwhatsapitcambio"
PHONE_ID      = os.getenv("PHONE_NUMBER_ID")
WHATS_TOKEN   = os.getenv("WHATS_TOKEN")
TTL_DEFAULT   = timedelta(minutes=15)

app = FastAPI()
_cache       = {}                 # key → (value, expiry)
PROCESADOS   = deque(maxlen=100)  # ids de mensajes ya tratados (eco)

fmt = lambda n: f"{n:.2f}".replace(".", ",")   # 106,86 → '106,86'


# ── Caché ─────────────────────────
def cache_get(k):
    v, exp = _cache.get(k, (None, datetime.min))
    return v if exp > datetime.utcnow() else None

def cache_set(k, v, ttl: timedelta = TTL_DEFAULT):
    _cache[k] = (v, datetime.utcnow() + ttl)


# ── HTTP helper ───────────────────
async def fetch(m, u, **kw):
    kw.setdefault("timeout", 15)
    async with httpx.AsyncClient(verify=False, timeout=kw["timeout"]) as c:
        return await c.request(m, u, **kw)


# ── Webhook VERIFY ────────────────
@app.get("/webhook")
async def verify(r: Request):
    q = r.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == VERIFY_TOKEN:
        return int(q["hub.challenge"])
    return {"status": "error"}


# ── Webhook POST ──────────────────
@app.post("/webhook")
async def incoming(r: Request):
    data = await r.json()

    try:
        msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
        if msg.get("type") != "text":
            return {"status": "ignored"}
        msg_id = msg["id"]
        if msg_id in PROCESADOS:
            return {"status": "duplicate"}
        PROCESADOS.append(msg_id)

        text = msg["text"]["body"].strip().lower()
        waid = msg["from"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    # ── Comandos ──
    if "oficial" in text:
        v = await get_oficial()
        reply = f"📊 Oficial BCV: {fmt(v)} Bs/USD" if v else "BCV fuera de línea"

    elif "paralelo" in text or "p2p" in text:
        v = await get_paralelo()
        reply = f"🤝 Paralelo Binance: {fmt(v)} Bs/USDT" if v else "Binance fuera de línea"

    elif "bancos" in text or "mesas" in text:
        pares = await get_bancos()
        if pares:
            compra, venta = pares
            reply = ("🟢 COMPRA\n" +
                     "\n".join(f"{b}: {fmt(v)}" for b, v in compra.items()) +
                     "\n\n🔴 VENTA\n" +
                     "\n".join(f"{b}: {fmt(v)}" for b, v in venta.items()))
        else:
            reply = "BCV aún no publica las mesas de hoy."

    else:
        reply = ("Comandos disponibles:\n"
                 "• oficial  – tasa BCV\n"
                 "• paralelo – mejor precio Binance\n"
                 "• bancos   – tasas bancarias")

    await send_whatsapp(waid, reply)
    return {"status": "sent"}


# ── Envío WhatsApp ────────────────
async def send_whatsapp(to, body):
    url = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
    payload = {"messaging_product": "whatsapp",
               "to": to,
               "type": "text",
               "text": {"preview_url": False, "body": body}}
    headers = {"Authorization": f"Bearer {WHATS_TOKEN}"}
    r = await fetch("POST", url, json=payload, headers=headers)
    if r.status_code >= 300:
        print("WA send error", r.status_code, r.text[:150])


# ── FETCHERS ──────────────────────
async def get_oficial():
    """
    BCV Bs/USD.  Fuentes en cascada:
      1) Banco Exterior – columna VENTA
      2) Monitor Dólar   – valor BCV
      3) Finanzas Digital – post 'Tasa BCV'
    Cache 15 min.
    """
    if (v := cache_get("oficial")):
        return v

    sources = [
        ("https://www.bancoexterior.com/tasas-bcv/",
         r"venta[^0-9]{0,30}(\d{1,3}(?:[.,]\d{2,})+)"),
        ("https://monitordolarvenezuela.com/",
         r"(bcv|d[óo]lar)[^0-9]{0,30}(\d{1,3}(?:[.,]\d{2,})+)"),
        ("https://finanzasdigital.com/category/tasa-bcv/",
         r"(bcv|d[óo]lar)[^0-9]{0,30}(\d{1,3}(?:[.,]\d{2,})+)")
    ]

    for url, pattern in sources:
        try:
            html = (await fetch("GET", url)).text.lower()
            m = re.search(pattern, html, re.S)
            if not m:
                continue
            num = m.group(1 if "bancoexterior" in url else 2)
            val = float(num.replace(".", "").replace(",", "."))
            cache_set("oficial", val)
            return val
        except Exception as e:
            print("Fuente BCV error:", url, e)

    print("Todas las fuentes BCV fallaron")
    return None


async def get_paralelo():
    """
    Mejor precio NO promocionado, saldo >0.  Cache 3 min.
    """
    if (v := cache_get("paralelo")):
        return v

    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {"asset": "USDT", "fiat": "VES", "tradeType": "SELL",
               "page": 1, "rows": 20, "publisherType": None}

    try:
        r = await fetch("POST", url, json=payload)
        ads = r.json()["data"]

        precios = [
            float(ad["adv"]["price"])
            for ad in ads
            if not ad["adv"].get("proMerchantAds")
               and float(ad["adv"]["surplusAmount"]) > 0
        ]

        if precios:
            best = min(precios)
            cache_set("paralelo", best, ttl=timedelta(minutes=3))
            return best

        print("Binance: sin anuncios válidos")
        return None

    except Exception as e:
        print("Binance fetch error:", e)
        return None


async def get_bancos():
    """
    Devuelve (compras, ventas)  dicts. Cache 15 min.
    """
    if (pairs := cache_get("bancos")):
        return pairs

    url = "https://www.bcv.org.ve/tasas-informativas-sistema-bancario"
    try:
        soup = BeautifulSoup((await fetch("GET", url)).text, "html.parser")
        compra, venta = {}, {}
        for tr in soup.select("table tbody tr"):
            c = [td.get_text(strip=True).replace(",", ".") for td in tr.find_all("td")]
            if len(c) >= 3:
                banco = c[0]
                compra[banco] = float(c[1])
                venta[banco]  = float(c[2])
        if compra:
            cache_set("bancos", (compra, venta))
        return (compra, venta) if compra else None
    except Exception as e:
        print("Mesas:", e)
        return None
