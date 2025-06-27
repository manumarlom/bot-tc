import os, re, httpx
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from bs4 import BeautifulSoup
from collections import deque
PROCESADOS = deque(maxlen=100)

# ─── Config ───
VERIFY_TOKEN  = "miwhatsapitcambio"          # el mismo que pegaste en Meta
PHONE_ID      = os.getenv("PHONE_NUMBER_ID") # dígitos
WHATS_TOKEN   = os.getenv("WHATS_TOKEN")     # token largo EAAG…
TTL           = timedelta(minutes=15)        # caché memoria

app, _cache = FastAPI(), {}

# ─── Utilidades ───
def cache_get(k):
    v, exp = _cache.get(k, (None, datetime.min))
    return v if exp > datetime.utcnow() else None

def cache_set(k, v, ttl: timedelta = TTL):
    _cache[k] = (v, datetime.utcnow() + ttl)

async def fetch(m, u, **kw):
    kw.setdefault("timeout", 15)
    async with httpx.AsyncClient(verify=False, timeout=kw["timeout"]) as c:
        return await c.request(m, u, **kw)

fmt = lambda n: f"{n:.2f}".replace(".", ",")   # 106,86

# ─── Webhook verify ───
@app.get("/webhook")
async def verify(r: Request):
    q = r.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == VERIFY_TOKEN:
        return int(q["hub.challenge"])
    return {"status": "error"}

# ─── Webhook POST ───
@app.post("/webhook")
async def incoming(r: Request):
    j = await r.json()
    try:
        m = j["entry"][0]["changes"][0]["value"]["messages"][0]
        if m.get("type") != "text":
            return {"status": "ignored"}
        txt  = m["text"]["body"].strip().lower()
        waid = m["from"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    if "oficial" in txt:
        v = await get_oficial()
        rep = f"📊 Oficial BCV: {fmt(v)} Bs/USD" if v else "BCV fuera de línea"
    elif "p2p" in txt or "paralelo" in txt:
        v = await get_paralelo()
        rep = f"🤝 Paralelo Binance: {fmt(v)} Bs/USDT" if v else "Binance fuera de línea"
   elif "bancos" in txt or "mesas" in txt:
    pares = await get_bancos()
    if pares:
        compra, venta = pares
        rep  = "🟢 COMPRA\n" + "\n".join(f"{b}: {fmt(v)}" for b, v in compra.items())
        rep += "\n\n🔴 VENTA\n" + "\n".join(f"{b}: {fmt(v)}" for b, v in venta.items())
    else:
        rep = "BCV aún no publica las mesas de hoy."
    else:
        rep = ("Comandos:\n"
               "• oficial – tasa BCV\n"
               "• p2p     – mejor vendedor Binance\n"
               "• bancos  – mesas bancarias")

    await send_whatsapp(waid, rep)
    return {"status": "sent"}

async def send_whatsapp(to, body):
    url = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
    payload = {"messaging_product": "whatsapp",
               "to": to,
               "type": "text",
               "text": {"preview_url": False, "body": body}}
    headers = {"Authorization": f"Bearer {WHATS_TOKEN}"}
    r = await fetch("POST", url, json=payload, headers=headers)
    if r.status_code >= 300:
        print("WA send error", r.status_code, r.text[:120])

# ─── FETCHERS ───
async def get_oficial():
    """
    Devuelve la tasa BCV Bs/USD, intentando tres fuentes en cascada.
      1. Banco Exterior – columna VENTA
      2. Monitor Dólar   – valor BCV en portada
      3. FinanzasDigital – último post tasa BCV
    Cachea 15 min.
    """
    if (v := cache_get("oficial")):
        return v

    SOURCES = [
        # (url, regex que fuerza «VENTA» antes del número)
        ("https://www.bancoexterior.com/tasas-bcv/",
         r"venta[^0-9]{0,30}(\d{1,3}(?:[.,]\d{2,})+)"),
        ("https://monitordolarvenezuela.com/",
         r"(bcv|d[óo]lar)[^0-9]{0,30}(\d{1,3}(?:[.,]\d{2,})+)"),
        ("https://finanzasdigital.com/category/tasa-bcv/",
         r"(bcv|d[óo]lar)[^0-9]{0,30}(\d{1,3}(?:[.,]\d{2,})+)")
    ]

    for url, pattern in SOURCES:
        try:
            html = (await fetch("GET", url)).text.lower()
            m = re.search(pattern, html, re.S)
            if not m:
                continue
            num = m.group(1 if url.startswith("https://www.bancoexterior") else 2)
            val = float(num.replace(".", "").replace(",", "."))
            cache_set("oficial", val)
            return val
        except Exception as e:
            print("Fuente BCV error:", url, e)

    print("Todas las fuentes BCV fallaron")
    return None

    for url, ctx_pat in SOURCES:
        try:
            html = (await fetch("GET", url)).text.lower()
            # Busca contexto y número después
            regex = re.compile(ctx_pat + r"[^0-9]{0,30}" + num_pat, re.S)
            m = regex.search(html)
            if not m:
                continue
            raw = m.group(2).replace(".", "").replace(",", ".")
            val = float(raw)
            cache_set("oficial", val)
            return val
        except Exception as e:
            print("Fuente BCV error:", url, e)

    print("Todas las fuentes BCV fallaron")
    return None

async def get_paralelo():
    async def get_paralelo():
    """
    Devuelve el mejor precio de venta USDT/VES en Binance P2P (no promocionados).
    Cachea solo 10 minutos.
    """
    if (v := cache_get("paralelo")):
        return v

    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": "USDT",
        "fiat": "VES",
        "tradeType": "SELL",   # tú compras VES, el anunciante vende USDT
        "page": 1,
        "rows": 20,
        "publisherType": None  # incluye merchants y usuarios
    }

    try:
        r = await fetch("POST", url, json=payload)
        ads = r.json()["data"]

        precios = [
            float(ad["adv"]["price"])
            for ad in ads
            if not ad["adv"].get("proMerchantAds")      # no promocionados
               and float(ad["adv"]["surplusAmount"]) > 0 # con saldo
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
   async def get_bancos():
    """
    Devuelve (compras, ventas)
      compras = {banco: float}
      ventas  = {banco: float}
    Cachea 15 min.
    """
    if (val := cache_get("bancos")):
        return val          # (compra, venta)

    URL = "https://www.bcv.org.ve/tasas-informativas-sistema-bancario"
    try:
        html  = (await fetch("GET", URL)).text
        soup  = BeautifulSoup(html, "html.parser")
        compra, venta = {}, {}

        for tr in soup.select("table tbody tr"):
            celdas = [td.get_text(strip=True).replace(",", ".") for td in tr.find_all("td")]
            if len(celdas) >= 3:
                banco  = celdas[0]
                compra[banco] = float(celdas[1])
                venta[banco]  = float(celdas[2])

        if compra:
            cache_set("bancos", (compra, venta))
        return (compra, venta) if compra else None

    except Exception as e:
        print("Mesas:", e)
        return None
