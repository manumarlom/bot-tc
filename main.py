import os, re, httpx
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from bs4 import BeautifulSoup

# ─────────── Config ───────────
VERIFY_TOKEN  = "miwhatsapitcambio"
PHONE_ID      = os.getenv("PHONE_NUMBER_ID")   # numérico
WHATS_TOKEN   = os.getenv("WHATS_TOKEN")
TTL           = timedelta(minutes=15)

app, _cache = FastAPI(), {}

# ─────────── Utils ───────────
def cache_get(k):   # leer
    v, t = _cache.get(k, (None, datetime.min))
    return v if t > datetime.utcnow() else None

def cache_set(k, v):  # guardar
    _cache[k] = (v, datetime.utcnow() + TTL)

async def fetch(m, u, **kw):
    kw.setdefault("timeout", 15)
    async with httpx.AsyncClient(verify=False, timeout=kw["timeout"]) as c:
        return await c.request(m, u, **kw)

fmt = lambda n: f"{n:.2f}".replace(".", ",")   # 106,86

# ─────────── Verify ───────────
@app.get("/webhook")
async def verify(r: Request):
    q = r.query_params
    if q.get("hub.mode")=="subscribe" and q.get("hub.verify_token")==VERIFY_TOKEN:
        return int(q["hub.challenge"])
    return {"status":"error"}

# ─────────── Webhook POST ───────────
@app.post("/webhook")
async def webhook(r: Request):
    j = await r.json()
    try:
        m = j["entry"][0]["changes"][0]["value"]["messages"][0]
        if m.get("type")!="text": return {"status":"ignored"}
        txt = m["text"]["body"].strip().lower(); waid = m["from"]
    except (KeyError,IndexError):
        return {"status":"ignored"}

    if "oficial" in txt:
        v = await get_oficial()
        rep = f"📊 Oficial BCV: {fmt(v)} Bs/USD" if v else "BCV fuera de línea"
    elif "p2p" in txt or "paralelo" in txt:
        v = await get_paralelo()
        rep = f"🤝 Paralelo Binance: {fmt(v)} Bs/USDT" if v else "Binance fuera de línea"
    elif "bancos" in txt or "mesas" in txt:
        t = await get_bancos()
        rep = ("\n".join(f"{b}: {fmt(x)}" for b,x in t.items())
               if t else "BCV aún no publica las mesas de hoy.")
    else:
        rep=("Comandos:\n• oficial – BCV\n• p2p – Binance\n• bancos – mesas")

    await send(waid, rep); return {"status":"sent"}

async def send(to, body):
    u=f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
    p={"messaging_product":"whatsapp","to":to,"type":"text",
       "text":{"preview_url":False,"body":body}}
    h={"Authorization":f"Bearer {WHATS_TOKEN}"}
    r=await fetch("POST",u,json=p,headers=h)
    if r.status_code>=300: print("WA err",r.status_code,r.text[:120])

# ─────────── Fetchers ───────────
async def get_oficial():
    if (v:=cache_get("oficial")): return v
    URL="https://www.bcv.org.ve/estadisticas/tipo-cambio-de-referencia-smc"
    try:
        html=(await fetch("GET",URL)).text.lower()

        # Expresión: 'usd' o 'dólar' seguido de la primera cifra  nnn,nn  o  nnn.nn
        m=re.search(r"(usd|d[óo]lar)[^0-9]*?(\d{1,3}(?:[.,]\d{2,})+)", html, re.S)
        if not m:
            print("BCV parse: no match"); return None

        num=m.group(2).replace(".", "").replace(",", ".")
        v=float(num); cache_set("oficial",v); return v
    except Exception as e:
        print("BCV fetch:",e); return None

async def get_paralelo():
    if (v:=cache_get("paralelo")): return v
    pl={"asset":"USDT","fiat":"VES","tradeType":"SELL","page":1,"rows":1}
    try:
        r=await fetch("POST","https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
                      json=pl)
        v=float(r.json()["data"][0]["adv"]["price"]); cache_set("paralelo",v); return v
    except Exception as e:
        print("Binance:",e); return None

async def get_bancos():
    if (t:=cache_get("bancos")): return t
    URL="https://www.bcv.org.ve/tasas-informativas-sistema-bancario"
    try:
        soup=BeautifulSoup((await fetch("GET",URL)).text,"html.parser"); tabla={}
        for tr in soup.select("table tbody tr"):
            c=[td.get_text(strip=True).replace(",",".") for td in tr.find_all("td")]
            if len(c)>=3: tabla[c[0]]=float(c[2])
        if tabla: cache_set("bancos",tabla)
        return tabla or None
    except Exception as e:
        print("Mesas:",e); return None
