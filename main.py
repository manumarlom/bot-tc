# main.py  –  Bot Tipo de Cambio VE  (WhatsApp Cloud)

import os
import re
import httpx
from datetime import datetime, timedelta
from collections import deque
from fastapi import FastAPI, Request
from bs4 import BeautifulSoup

# ───────────── Configuración ─────────────
VERIFY_TOKEN = "miwhatsapitcambio"                  # token de verificación webhook
PHONE_ID     = os.getenv("PHONE_NUMBER_ID")         # ID numérico del teléfono
WHATS_TOKEN  = os.getenv("WHATS_TOKEN")             # token largo EAAG…
TTL_DEFAULT  = timedelta(minutes=15)

app        = FastAPI()
_cache     = {}                      # key → (valor, expiración)
PROCESADOS = deque(maxlen=100)       # ids ya atendidos

fmt = lambda n: f"{n:.2f}".replace(".", ",")        # 106,86

# ───────────── Cache helpers ─────────────
def cache_get(k):
    val, exp = _cache.get(k, (None, datetime.min))
    return val if exp > datetime.utcnow() else None

def cache_set(k, v, ttl: timedelta = TTL_DEFAULT):
    _cache[k] = (v, datetime.utcnow() + ttl)

# ───────────── HTTP helper (verify=False) ─────────────
async def fetch(method, url, **kw):
    kw.setdefault("timeout", 15)
    async with httpx.AsyncClient(verify=False, timeout=kw["timeout"]) as c:
        return await c.request(method, url, **kw)

# ───────────── Webhook VERIFY ─────────────
@app.get("/webhook")
async def verify(r: Request):
    q = r.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == VERIFY_TOKEN:
        return int(q["hub.challenge"])
    return {"status": "error"}

# ───────────── Webhook POST ─────────────
@app.post("/webhook")
async def incoming(r: Request):
    data = await r.json()
    try:
        msg  = data["entry"][0]["changes"][0]["value"]["messages"][0]
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

    # ── Comandos ───────────────────────────
    if "oficial" in text:
        v = await get_oficial()
        reply = f"📊 Oficial BCV: {fmt(v)} Bs/USD" if v else "BCV fuera de línea"

    elif "paralelo" in text or "p2p" in text:
        v = await get_paralelo()
        reply = f"🤝 Paralelo Binance: {fmt(v)} Bs/USDT" if v else "Binance fuera de línea"

    elif "bancos" in text or "mesas" in text:
        res = await get_bancos()
        if res:
            fecha, compra, venta = res
            cab = f"📅 {fecha}\n" if fecha else ""
            reply = (cab +
                     "🟢 COMPRA\n" +
                     "\n".join(f"{b}: {fmt(x)}" for b, x in compra.items()) +
                     "\n\n🔴 VENTA\n" +
                     "\n".join(f"{b}: {fmt(x)}" for b, x in venta.items()))
        else:
            reply = "BCV aún no publica las mesas de hoy."

    else:
        reply = ("Comandos disponibles:\n"
                 "• oficial  – tasa BCV\n"
                 "• paralelo – mejor precio Binance\n"
                 "• bancos   – tasas bancarias")

    await send_whatsapp(waid, reply)
    return {"status": "sent"}

# ───────────── Envío WhatsApp ─────────────
async def send_whatsapp(to, body):
    url = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body}
    }
    headers = {"Authorization": f"Bearer {WHATS_TOKEN}"}
    r = await fetch("POST", url, json=payload, headers=headers)
    if r.status_code >= 300:
        print("WA send error", r.status_code, r.text[:150])

# ───────────── FETCHERS ─────────────
async def get_oficial():
    """Tasa BCV Bs/USD:  Banco Exterior → MonitorDolar → FinanzasDigital."""
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

    for url, pat in sources:
        try:
            html = (await fetch("GET", url)).text.lower()
            m = re.search(pat, html, re.S)
            if not m:
                continue
            num = m.group(1 if "bancoexterior" in url else 2)
            val = float(num.replace(".", "").replace(",", "."))
            cache_set("oficial", val)
            return val
        except Exception as e:
            print("Fuente BCV error:", url, e)
    return None

async def get_paralelo():
    """Mejor precio (sin patrocinados) de Binance P2P VES→USDT."""
    if (v := cache_get("paralelo")):
        return v

    # 1) HTML visible ─────────────────────────────
    try:
        html = (await fetch(
            "GET",
            "https://p2p.binance.com/es/trade/all-payments/USDT?fiat=VES",
            headers={"User-Agent": "Mozilla/5.0"}
        )).text

        prices = re.findall(r"Bs\s*([\d.]+,\d{2,})", html)
        if len(prices) >= 2:
            val = float(prices[1].replace(".", "").replace(",", "."))
        elif prices:
            val = float(prices[0].replace(".", "").replace(",", "."))
        else:
            val = None

        if val:
            cache_set("paralelo", val, ttl=timedelta(minutes=3))
            return val
    except Exception as e:
        print("HTML Binance:", e)

    # 2) API respaldo ─────────────────────────────
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        payload = {
            "asset": "USDT",
            "fiat":  "VES",
            "tradeType": "SELL",
            "page": 1,
            "rows": 20,
            "publisherType": None
        }
        ads = (await fetch("POST", url, json=payload)).json()["data"]

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
    except Exception as e:
        print("API Binance:", e)

    return None

async def get_bancos():
    """
    Devuelve (fecha, dict_compra, dict_venta) –– toma la fila MÁS RECIENTE
    de cada banco en https://www.bcv.org.ve/tasas-informativas-sistema-bancario
    Cache 15 min.
    """
    if (v := cache_get("bancos")):
        return v

    url = "https://www.bcv.org.ve/tasas-informativas-sistema-bancario"
    try:
        html = (await fetch("GET", url)).text
        soup = BeautifulSoup(html, "html.parser")

        filas = []
        for tr in soup.select("table tbody tr"):
            tds = [td.get_text(strip=True).replace(",", ".") for td in tr.find_all("td")]
            if len(tds) >= 4:
                try:
                    f = datetime.strptime(tds[0], "%d-%m-%Y").date()
                    filas.append((f, tds[1], float(tds[2]), float(tds[3])))
                except ValueError:
                    continue          # ignora cabeceras repetidas

        if not filas:
            return None

        # pick última fila por banco
        ult = {}
        for f, banco, comp, vent in filas:
            if banco not in ult or f > ult[banco][0]:
                ult[banco] = (f, comp, vent)

        fecha_max = max(v[0] for v in ult.values())
        compra = {b: v[1] for b, v in ult.items()}
        venta  = {b: v[2] for b, v in ult.items()}

        out = (fecha_max.strftime("%d-%m-%Y"), compra, venta)
        cache_set("bancos", out, ttl=timedelta(minutes=15))
        return out

    except Exception as e:
        print("Mesas:", e)
        return None

