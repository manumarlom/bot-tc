# ──────────────────────────────────────────────────────────────
# main.py  – WhatsApp FX-Bot (BCV, Binance P2P, Mesas Bancarias)
# Requisitos:
#   pip install fastapi "uvicorn[standard]" httpx bs4 python-dotenv
#   Establecer variables de entorno:
#     WHATS_TOKEN       →  token permanente o temporal
#     PHONE_NUMBER_ID   →  id numérico del WABA
# ──────────────────────────────────────────────────────────────
import os, re, json, asyncio, httpx
from typing import List, Tuple
from fastapi import FastAPI, Request, HTTPException
from bs4 import BeautifulSoup
from datetime import datetime

TOKEN          = os.getenv("WHATS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

app = FastAPI()

# ──────────────────────────────────────────────────────────────
# Helpers HTTP
# ──────────────────────────────────────────────────────────────
async def fetch(method: str, url: str, **kwargs) -> httpx.Response:
    """Pequeño wrapper con timeout y follow_redirects."""
    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (FX-Bot)"}
    ) as client:
        r = await client.request(method, url, **kwargs)
        r.raise_for_status()
        return r

async def send_text(text: str, to: str) -> None:
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    headers = {"Authorization": f"Bearer {TOKEN}"}
    r = await fetch("POST", url, json=payload, headers=headers)
    return r.json()

# ──────────────────────────────────────────────────────────────
# 1. TC oficial BCV (widget del home)
# ──────────────────────────────────────────────────────────────
async def get_oficial() -> float:
    url = "https://www.bcv.org.ve/"
    html = (await fetch("GET", url)).text
    soup = BeautifulSoup(html, "lxml")

    # Busca la fila ‘USD’ en la caja de referencia
    fila = soup.find("div", string=re.compile(r"^\s*USD\s*$"))
    if not fila:
        raise ValueError("USD no encontrado")

    valor_txt = fila.find_next("div").get_text(strip=True)
    valor = float(valor_txt.replace(",", "."))
    return valor

# ──────────────────────────────────────────────────────────────
# 2. Paralelo Binance (mejor VENDEDOR no promocionado)
# ──────────────────────────────────────────────────────────────
async def get_paralelo() -> float:
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": "USDT",
        "fiat": "VES",
        "tradeType": "SELL",        # vendedores → tú compras VES
        "page": 1,
        "rows": 10,
        "payTypes": [],
        "publisherType": None
    }
    data = (await fetch("POST", url, json=payload)).json()["data"]

    # filtra ads patrocinados (proMerchantAds = true)
    precios = [float(ad["adv"]["price"])
               for ad in data
               if not ad["adv"].get("proMerchantAds")]

    if not precios:
        raise ValueError("sin precios libres")
    return min(precios)             # mejor (más barato)

# ──────────────────────────────────────────────────────────────
# 3. Mesas Bancarias (compra / venta)
# ──────────────────────────────────────────────────────────────
async def get_bancos() -> Tuple[str, List[Tuple[str,float,float]]]:
    url = "https://www.bcv.org.ve/tasas-informativas-sistema-bancario"
    html = (await fetch("GET", url)).text
    soup = BeautifulSoup(html, "lxml")

    rows = soup.select("table tbody tr")
    if not rows:
        return None, []

    datos, last_date = [], None
    for tr in rows:
        cols = [td.get_text(strip=True) for td in tr.select("td")]
        if len(cols) < 4:
            continue
        fecha, banco, compra, venta = cols[:4]

        if last_date is None:
            last_date = fecha
        if fecha != last_date:
            break

        datos.append((
            banco,
            float(compra.replace(",", ".")),
            float(venta.replace(",", "."))
        ))
    return last_date, datos

# ──────────────────────────────────────────────────────────────
# 4. Webhook
# ──────────────────────────────────────────────────────────────
@app.get("/webhook")
async def verify(mode: str = "", challenge: str = "", verify_token: str = ""):
    if mode == "subscribe" and verify_token == "miwhatsappcambio":
        return int(challenge)
    raise HTTPException(status_code=403)

@app.post("/webhook")
async def incoming(request: Request):
    body = await request.json()
    try:
        entry = body["entry"][0]["changes"][0]["value"]["messages"][0]
        text = entry["text"]["body"].lower().strip()
        from_wa = entry["from"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    # --- comandos ---
    if text in {"oficial", "bcv"}:
        try:
            tasa = await get_oficial()
            msg = f"📊 Oficial BCV: {tasa:,.2f} Bs/USD"
        except Exception as e:
            msg = "BCV fuera de línea"
        await send_text(msg, from_wa)

    elif text.startswith(("paralelo", "binance", "p2p")):
        try:
            tasa = await get_paralelo()
            msg = f"🤝 Paralelo Binance: {tasa:,.2f} Bs/USDT"
        except Exception:
            msg = "Binance fuera de línea"
        await send_text(msg, from_wa)

    elif text in {"bancos", "mesas"}:
        fecha, filas = await get_bancos()
        if not filas:
            await send_text("BCV aún no publica las mesas de hoy.", from_wa)
        else:
            header = f"🏦 Mesas ({fecha})\nCOMPRA | VENTA\n"
            body = "\n".join(f"{b:18} {c:6.2f} | {v:6.2f}"
                             for b,c,v in filas)
            await send_text(header+body, from_wa)

    else:
        await send_text("Comandos: oficial | paralelo | bancos", from_wa)

    return {"status": "ok"}

# ──────────────────────────────────────────────────────────────
# ejecuta con:  uvicorn main:app --host 0.0.0.0 --port 10000
# ──────────────────────────────────────────────────────────────
