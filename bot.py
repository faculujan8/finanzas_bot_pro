import os
import json
import logging
import tempfile
import httpx
import base64
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_KEY"]
SHEETS_ID      = os.environ["SHEETS_ID"]
GOOGLE_SA_JSON = os.environ["GOOGLE_SA_JSON"]
CHAT_ID        = os.environ.get("CHAT_ID", "")
TZ             = ZoneInfo("America/Argentina/Buenos_Aires")
MODEL          = "claude-haiku-4-5-20251001"

# UALA reminder state: None | "waiting_tonight" | "waiting_morning"
uala_reminder_state: dict = {"state": None, "date": None}

# In-memory conversation context per chat
user_context: dict = {}

# ─────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """Sos BOTSTERO, un asistente financiero personal con onda. Procesá el mensaje y devolvé SOLO JSON válido, sin texto ni markdown.

CUENTAS: efectivo, uala, galicia, balanz, binance
RUBROS: almacen, auto, cafe, cancha, celular, cochera, combustible, comida, concierto, credito, cumpleaños, dolares, farmacia, futbol, gimnasio, impuestos, indumentaria, inversion, juegos, kiosko, nafta, panaderia, peluqueria, prepaga, regalos, rendimientos, salida, salud, seguro, sueldo, suscripciones, taone, transporte, traspaso, varios, verduleria, cena, almuerzo, tarjeta

TIPOS:

1) GASTO/INGRESO completo (rubro Y cuenta presentes):
{"tipo":"movimiento","filas":[{"fecha":"DD/MM/YYYY","monto":-5000,"rubro":"cochera","detalle":"Cochera","cuenta":"uala"}],"mensaje":"✅ -$5.000 Cochera (UALA)"}

2) GASTO INCOMPLETO — falta rubro o cuenta. NUNCA asumas "varios" o "efectivo" si no se dijo:
{"tipo":"pedir_detalle","monto":-1000,"detalle":"gasto","falta":"rubro_y_cuenta","mensaje":"💬 ¿En qué gastaste los $1.000 y con qué pagaste?"}
Si solo falta la cuenta: {"tipo":"pedir_detalle","monto":-1000,"rubro":"cafe","detalle":"Cafe","falta":"cuenta","mensaje":"💬 ¿Con qué pagaste el café?"}
Si solo falta el rubro: {"tipo":"pedir_detalle","monto":-1000,"cuenta":"uala","detalle":"gasto","falta":"rubro","mensaje":"💬 ¿En qué gastaste los $1.000?"}

3) TRASPASO (SIEMPRE dos filas):
{"tipo":"movimiento","filas":[{"fecha":"DD/MM/YYYY","monto":-20000,"rubro":"traspaso","detalle":"Traspaso a UALA","cuenta":"efectivo"},{"fecha":"DD/MM/YYYY","monto":20000,"rubro":"traspaso","detalle":"Traspaso desde Efectivo","cuenta":"uala"}],"mensaje":"✅ Traspaso $20.000 Efectivo → UALA"}

4) COMPRA USD:
{"tipo":"dolares","filas":[{"fecha":"DD/MM/YYYY","monto":100,"rubro":"compra","detalle":"Compra USD a $1.400","cuenta":"dolares"}],"movimiento_pesos":{"fecha":"DD/MM/YYYY","monto":-140000,"rubro":"dolares","detalle":"Compra 100 USD","cuenta":"uala"},"mensaje":"✅ +100 USD | -$140.000"}

5) GASTO USD:
{"tipo":"dolares","filas":[{"fecha":"DD/MM/YYYY","monto":-15,"rubro":"gasto","detalle":"Netflix","cuenta":"dolares"}],"mensaje":"✅ -U$D 15 Netflix"}

6) INVERSIÓN con todos los datos (ticker + cantidad + precio):
{"tipo":"inversion","instrumento":"CEDEAR","filas":[{"fecha":"DD/MM/YYYY","tipo_instr":"CEDEAR","ticker":"SPY","detalle":"CEDEAR S&P 500","cantidad":10,"precio_compra":85000}],"movimiento_pesos":{"fecha":"DD/MM/YYYY","monto":-850000,"rubro":"inversion","detalle":"Compra 10 SPY","cuenta":"balanz"},"mensaje":"✅ 10 SPY a $85.000 = $850.000"}

7) INVERSIÓN incompleta — indicá exactamente qué datos ya tenés y qué falta:
{"tipo":"pedir_inversion","instrumento":"CEDEAR","datos_presentes":{"ticker":"SPY","cantidad":2},"falta":["precio_compra"],"mensaje":"📌 SPY x2 — ¿A qué precio por nominal?"}

8) CONSULTA general:
{"tipo":"consulta","mensaje":"🔍 Buscando..."}

9) CONSULTA DE INVERSIONES ("como me fue", "cartera hoy", variaciones):
{"tipo":"consulta_inversiones","mensaje":"📈 Revisando tu cartera..."}

10) TAONE (gorras, pilusos, matriz, Sr Juan):
{"tipo":"taone","filas":[{"fecha":"DD/MM/YYYY","concepto":"GORRAS x30","monto":-180000,"detalle":"30 gorras"}],"mensaje":"✅ TAONE -$180.000"}

11) TARJETA — resumen de gastos:
{"tipo":"tarjeta","cuenta_pago":"galicia","filas":[{"fecha":"DD/MM/YYYY","monto":-5000,"rubro":"comida","detalle":"Restaurante X"},{"tipo":"consulta_rubro","detalle":"Pago ABC","monto":-3000}],"mensaje":"💳 Procesando..."}

REGLAS CRÍTICAS:
1. SOLO JSON válido, sin texto ni markdown.
2. Fecha DD/MM/YYYY. Sin fecha → hoy.
3. Montos sin formato. k=miles. Negativo=gasto. Positivo=ingreso.
4. Si dicen solo el monto sin rubro ni cuenta → pedir_detalle con falta="rubro_y_cuenta".
5. Si tienen rubro pero no cuenta → pedir_detalle con falta="cuenta".
6. Traspaso → siempre dos filas.
7. Inversión: si falta ticker O cantidad O precio → pedir_inversion con los datos_presentes que haya.
8. "como me fue hoy", "cartera", "inversiones" → consulta_inversiones."""

CONSULTA_SYSTEM = """Sos BOTSTERO, asistente financiero con onda. Tenés los movimientos del usuario en CSV.
Respondé en español rioplatense, con emojis, claro y conciso. Mostrá siempre totales.
Sin markdown, solo texto plano con emojis."""

INVERSIONES_SYSTEM = """Sos BOTSTERO, asistente financiero. Tenés la cartera del usuario y precios del mercado.
Calculá y mostrá para cada ticker:
- Precio de hoy vs ayer
- Resultado diario en $ y %
- Resultado total desde la compra

Al final mostrá totales de la cartera.
Respondé en español rioplatense con emojis. Texto plano sin markdown."""



# ─────────────────────────────────────────────
# CLAUDE API
# ─────────────────────────────────────────────
async def call_claude(system: str, user_message: str, max_tokens: int = 1024) -> str:
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user_message}],
            },
        )
        if resp.status_code != 200:
            logger.error(f"Anthropic {resp.status_code}: {resp.text}")
            raise Exception(f"Error Anthropic {resp.status_code}: {resp.text[:200]}")
        return resp.json()["content"][0]["text"]


async def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe audio — uses Whisper if OPENAI_KEY is set, otherwise tries Claude."""
    openai_key = os.environ.get("OPENAI_KEY", "")
    if openai_key:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {openai_key}"},
                files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
                data={"model": "whisper-1", "language": "es"},
            )
            resp.raise_for_status()
            return resp.json()["text"]
    else:
        raise Exception(
            "Para transcribir audios necesitás agregar OPENAI_KEY en Railway.\n"
            "Es gratis hasta cierto uso. Creá cuenta en platform.openai.com"
        )


# ─────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────
def get_sheets_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa_info = json.loads(GOOGLE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def append_rows(sheet_name: str, rows: list):
    service = get_sheets_service()
    service.spreadsheets().values().append(
        spreadsheetId=SHEETS_ID,
        range=f"{sheet_name}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()


def read_sheet(sheet_name: str, range_: str = "A:F") -> list:
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEETS_ID,
        range=f"{sheet_name}!{range_}"
    ).execute()
    return result.get("values", [])


def get_tickers_from_sheet() -> list:
    rows = read_sheet("INVERSIONES", "A:F")
    tickers = {}
    for row in rows[1:]:
        if len(row) < 5:
            continue
        ticker = str(row[2]).strip().upper() if len(row) > 2 else ""
        if not ticker:
            continue
        try:
            qty   = float(str(row[4]).replace(",", ".").replace("$", "")) if len(row) > 4 else 0
            price = float(str(row[5]).replace(",", ".").replace("$", "")) if len(row) > 5 else 0
        except:
            qty, price = 0, 0
        if ticker in tickers:
            tickers[ticker]["cantidad"] += qty
        else:
            tickers[ticker] = {"cantidad": qty, "precio_compra": price}
    return [{"ticker": k, **v} for k, v in tickers.items()]




def get_memory() -> dict:
    """Read the UALA memory from Sheets (comercio -> rubro mapping)."""
    try:
        rows = read_sheet("MEMORIA_UALA", "A:B")
        memory = {}
        for row in rows[1:]:  # skip header
            if len(row) >= 2:
                comercio = str(row[0]).strip().lower()
                rubro    = str(row[1]).strip().lower()
                if comercio:
                    memory[comercio] = rubro
        return memory
    except:
        return {}


def save_memory(comercio: str, rubro: str):
    """Save a new comercio -> rubro mapping to UALA memory sheet."""
    try:
        # Check if sheet exists, if not create header
        rows = read_sheet("MEMORIA_UALA", "A:B")
        if not rows:
            append_rows("MEMORIA_UALA", [["COMERCIO", "RUBRO"]])
        # Check if already exists
        for row in rows[1:]:
            if len(row) >= 1 and str(row[0]).strip().lower() == comercio.lower():
                # Update existing — for simplicity just append new entry
                # (latest entry wins when reading)
                break
        append_rows("MEMORIA_UALA", [[comercio.strip(), rubro.strip().lower()]])
        logger.info(f"Memoria guardada: {comercio} -> {rubro}")
    except Exception as e:
        logger.error(f"Error saving memory: {e}")


def save_bulk_memory(mappings: list):
    """Save multiple comercio->rubro mappings at once."""
    for m in mappings:
        if m.get("comercio") and m.get("rubro"):
            save_memory(m["comercio"], m["rubro"])


async def process_uala_screenshot(image_bytes: bytes, mime: str, today: str, desde_fecha: str = "") -> dict:
    """Send UALA screenshot to Claude for processing."""
    memory = get_memory()
    memory_str = json.dumps(memory, ensure_ascii=False) if memory else "{}"
    b64 = base64.standard_b64encode(image_bytes).decode()

    system = """Sos BOTSTERO analizando una captura de pantalla de movimientos de UALA.
Devolvé SOLO JSON válido, sin texto ni markdown.

FORMATO DE PANTALLA DE UALA:
- Cada fila tiene: NOMBRE / SUBTÍTULO / MONTO / FECHA
- MONTO en verde con "+" = ingreso (rendimientos, devoluciones, transferencias recibidas)
- MONTO en negro sin "+" = gasto o transferencia enviada
- SUBTÍTULO puede ser: "Operación exitosa", "Transporte", "Restaurantes y bares", "Transferencia enviada", "Devolucion en cuenta", etc.
- FECHA formato DD/MM (sin año)

TIPOS DE MOVIMIENTOS QUE VAS A VER:
- "Rendimientos / Operación exitosa" → rendimiento diario, monto positivo
- "Payu*ar*uber..." / "Transporte" → gasto en Uber, rubro=transporte
- "Dlo*pedidosya..." / "Restaurantes y bares" → gasto en PedidosYa, rubro=comida
- "Pedidosya*plus..." / "Restaurantes y bares" → gasto en PedidosYa, rubro=comida
- "[Nombre persona]" / "Transferencia enviada" → SIEMPRE va a movimientos_dudosos (nunca a movimientos_ok)
- "Facundo Dardo Lujan" / "Transferencia enviada" → va a transferencias_propias
- "[Comercio]" / "Devolucion en cuenta" → devolución, monto POSITIVO

Formato de respuesta:
{
  "movimientos_ok": [
    {"fecha":"DD/MM/YYYY","monto":-1500,"rubro":"comida","detalle":"Nombre tal como aparece en pantalla","cuenta":"uala"}
  ],
  "movimientos_dudosos": [
    {"fecha":"DD/MM/YYYY","monto":-800,"detalle":"Nombre exacto del comercio","cuenta":"uala"}
  ],
  "transferencias_propias": [
    {"fecha":"DD/MM/YYYY","monto":-30000,"detalle":"Facundo Dardo Lujan"}
  ],
  "rendimientos": [
    {"fecha":"DD/MM/YYYY","monto":403,"detalle":"Rendimiento diario UALA"}
  ],
  "devoluciones": [
    {"fecha":"DD/MM/YYYY","monto":13906,"detalle":"Devolucion Payu*ar*uber","rubro":"transporte"}
  ]
}

REGLAS CRÍTICAS:
1. SOLO incluí movimientos con fecha >= FECHA_DESDE (se te va a indicar)
2. movimientos_ok: categorizá con certeza usando memoria o subtítulo de UALA
3. movimientos_dudosos: nombres de comercios desconocidos no en memoria
4. transferencias_propias: SOLO "Facundo Dardo Lujan" / "Transferencia enviada"
5. CUALQUIER "Transferencia enviada" a nombre de persona (NO Facundo Dardo Lujan) → movimientos_dudosos SIEMPRE, aunque creas saber el rubro
6. rendimientos: SOLO "Rendimientos / Operación exitosa"
7. devoluciones: subtítulo "Devolucion en cuenta" → monto POSITIVO
8. Año = año actual
9. NUNCA asumas rubro si no estás seguro → mandalo a dudosos"""

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": MODEL,
                "max_tokens": 2048,
                "system": system,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": f"Hoy es {today}. " + (f"IMPORTANTE: Solo procesá movimientos con fecha >= {desde_fecha}. Ignorá todo lo anterior a esa fecha." if desde_fecha else "Procesá todos los movimientos visibles.") + f" Memoria de comercios conocidos: {memory_str}\n\nAnalizá esta captura de UALA y clasificá los movimientos."},
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
                ]}]
            }
        )
    raw = resp.json()["content"][0]["text"]
    raw = raw.strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)


async def get_last_uala_date() -> str:
    """Get the last processed UALA date from Sheets (stored in D2)."""
    try:
        rows = read_sheet("MEMORIA_UALA", "D1:D2")
        if len(rows) >= 2:
            val = str(rows[1][0]).strip() if rows[1] else ""
            # Validate it looks like a date DD/MM/YYYY
            if val and len(val) == 10 and val[2] == "/" and val[5] == "/":
                return val
    except:
        pass
    return ""  # empty = first time, process everything shown


def save_last_uala_date(fecha: str):
    """Save the last processed UALA date."""
    try:
        service = get_sheets_service()
        # Write to a specific cell D2 in MEMORIA_UALA as a config value
        service.spreadsheets().values().update(
            spreadsheetId=SHEETS_ID,
            range="MEMORIA_UALA!D1:D2",
            valueInputOption="RAW",
            body={"values": [["ULTIMA_FECHA_UALA"], [fecha]]}
        ).execute()
    except Exception as e:
        logger.error(f"Error saving last UALA date: {e}")


async def handle_uala_screenshot(image_bytes: bytes, mime: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full flow for processing a UALA screenshot."""
    chat_id = update.effective_chat.id
    today = datetime.now(TZ).strftime("%d/%m/%Y")

    await update.message.reply_text("📱 Analizando movimientos de UALA...")

    # Get last processed date to avoid duplicates
    desde_fecha = await get_last_uala_date()
    logger.info(f"Processing UALA from date: {desde_fecha or 'ALL (first time)'}")

    try:
        result = await process_uala_screenshot(image_bytes, mime, today, desde_fecha)
    except Exception as e:
        await update.message.reply_text(f"❌ Error procesando la imagen: {str(e)}")
        return

    rows_to_save = []
    msg_parts = []
    total_saved = 0

    # 1. Save confirmed movements
    ok_raw = result.get("movimientos_ok", [])
    ok = []
    extra_dudosos = []
    memory = get_memory()

    for m in ok_raw:
        rubro   = str(m.get("rubro", "")).strip().lower()
        detalle = str(m.get("detalle", "")).strip()
        detalle_key = detalle.lower()

        # Check memory first
        if not rubro and detalle_key in memory:
            m["rubro"] = memory[detalle_key]
            rubro = m["rubro"]

        # If rubro still empty → ask
        if not rubro or rubro in ["", "none", "null"]:
            extra_dudosos.append(m)
            continue

        # If it looks like a person name (no * or digits, title case words) → ask
        import re
        is_person = bool(re.match(r'^[A-Za-z ,]+$', detalle))
        if is_person and rubro not in memory.get(detalle_key, rubro):
            extra_dudosos.append(m)
            continue

        ok.append(m)

    # Merge extra_dudosos into the main dudosos list
    result["movimientos_dudosos"] = result.get("movimientos_dudosos", []) + extra_dudosos

    if ok:
        for m in ok:
            rows_to_save.append([m.get("fecha", today), m.get("monto",""), m.get("rubro",""), m.get("detalle",""), "uala"])
        total_saved += len(ok)
        msg_parts.append(f"✅ {len(ok)} movimientos guardados")

    # 2. Save rendimientos
    rendimientos = result.get("rendimientos", [])
    if rendimientos:
        for r in rendimientos:
            rows_to_save.append([r.get("fecha", today), r.get("monto",""), "rendimientos", r.get("detalle","Rendimiento UALA"), "uala"])
        total_saved += len(rendimientos)
        msg_parts.append(f"💰 {len(rendimientos)} rendimientos guardados")

    # 3. Save devoluciones
    devoluciones = result.get("devoluciones", [])
    if devoluciones:
        for d in devoluciones:
            rows_to_save.append([d.get("fecha", today), abs(float(d.get("monto",0))), d.get("rubro","reintegro gastos"), d.get("detalle","Devolución"), "uala"])
        total_saved += len(devoluciones)
        msg_parts.append(f"↩️ {len(devoluciones)} devoluciones guardadas")

    if rows_to_save:
        append_rows("MOVIMIENTOS", rows_to_save)
    
    # Save the most recent date processed to avoid duplicates next time
    all_dates = []
    for items in [ok, rendimientos, devoluciones]:
        for m in items:
            fecha_m = m.get("fecha", "")
            if fecha_m and len(fecha_m) == 10:
                all_dates.append(fecha_m)
    if all_dates:
        try:
            latest = sorted(all_dates, key=lambda x: datetime.strptime(x, "%d/%m/%Y"))[-1]
            save_last_uala_date(latest)
            logger.info(f"Saved last UALA date: {latest}")
        except Exception as e:
            logger.error(f"Error saving date: {e}")
            save_last_uala_date(today)

    # 3. Handle transferencias propias
    transferencias = result.get("transferencias_propias", [])
    if transferencias:
        ctx_transfers = []
        for t in transferencias:
            ctx_transfers.append(t)
        user_context[chat_id] = {
            "pendiente": "uala_transferencias",
            "transferencias": ctx_transfers,
            "dudosos": result.get("movimientos_dudosos", []),
            "idx": 0
        }
        t = ctx_transfers[0]
        signo = "+" if float(t.get("monto", 0)) > 0 else "-"
        monto = abs(float(t.get("monto", 0)))
        msg_parts.append(f"\n💸 Transferencia de/a vos mismo: {signo}${monto:,.0f}")
        msg_parts.append("¿A qué cuenta te pasaste la plata? (ej: galicia, efectivo, balanz)")
        await update.message.reply_text("\n".join(msg_parts))
        return

    # 4. Handle dudosos
    dudosos = result.get("movimientos_dudosos", [])
    if dudosos:
        user_context[chat_id] = {"pendiente": "uala_dudosos", "dudosos": dudosos, "idx": 0, "nuevas_memorias": []}
        msg_parts.append(f"\n❓ No reconocí {len(dudosos)} comercio(s):")
        d = dudosos[0]
        msg_parts.append(f"\n1/{len(dudosos)}: *{d.get('detalle','?')}* — ${abs(float(d.get('monto',0))):,.0f}")
        msg_parts.append("¿Qué rubro es? (ej: comida, farmacia, indumentaria...)")
        msg_parts.append("_O escribí 'varios' si no sabés_")
        await update.message.reply_text("\n".join(msg_parts), parse_mode="Markdown")
        return

    # All done
    if not msg_parts:
        msg_parts.append("✅ Sin movimientos nuevos en la captura")
    await update.message.reply_text("\n".join(msg_parts))
    # Save today as last processed date and reset reminder
    save_last_uala_date(today)
    uala_reminder_state["state"] = None
    uala_reminder_state["date"] = datetime.now(TZ).date()

async def get_market_prices(tickers: list) -> dict:
    prices = {}
    for ticker in tickers:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.BA?interval=1d&range=5d"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                closes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                closes = [c for c in closes if c is not None]
                if len(closes) >= 2:
                    prices[ticker] = {"hoy": round(closes[-1], 2), "ayer": round(closes[-2], 2)}
                elif len(closes) == 1:
                    prices[ticker] = {"hoy": round(closes[-1], 2), "ayer": round(closes[-1], 2)}
                else:
                    prices[ticker] = {"hoy": None, "ayer": None}
            else:
                prices[ticker] = {"hoy": None, "ayer": None}
        except Exception as e:
            logger.warning(f"Price error {ticker}: {e}")
            prices[ticker] = {"hoy": None, "ayer": None}
    return prices


# ─────────────────────────────────────────────
# HANDLE RESULT
# ─────────────────────────────────────────────
async def handle_result(data: dict, update: Update, context: ContextTypes.DEFAULT_TYPE):
    tipo = data.get("tipo")
    chat_id = update.effective_chat.id

    if tipo == "movimiento":
        filas = data.get("filas", [])
        memory = get_memory()
        rows_ok = []
        rows_ask = []

        for f in filas:
            rubro   = str(f.get("rubro", "")).strip().lower()
            detalle = str(f.get("detalle", "")).strip()
            monto   = f.get("monto", 0)
            fecha   = f.get("fecha", "")
            cuenta  = f.get("cuenta", "")

            # Check memory for known comercio
            detalle_key = detalle.lower()
            if not rubro or rubro in ["", "varios", "none", "traspaso"] and rubro != "traspaso":
                if detalle_key in memory:
                    rubro = memory[detalle_key]
                    f["rubro"] = rubro
                    rows_ok.append([fecha, monto, rubro, detalle, cuenta])
                elif rubro == "traspaso":
                    rows_ok.append([fecha, monto, rubro, detalle, cuenta])
                else:
                    rows_ask.append(f)
            else:
                # Has rubro — save to memory if it's a real comercio name
                if detalle and detalle_key not in memory and rubro not in ["traspaso", "sueldo", "rendimientos", "inversion", "dolares"]:
                    save_memory(detalle, rubro)
                rows_ok.append([fecha, monto, rubro, detalle, cuenta])

        if rows_ok:
            append_rows("MOVIMIENTOS", rows_ok)

        if rows_ask:
            # Ask about first unknown rubro
            user_context[chat_id] = {
                "pendiente": "rubro_desconocido",
                "filas_pendientes": rows_ask,
                "filas_guardadas": rows_ok,
                "idx": 0
            }
            f0 = rows_ask[0]
            monto0 = f0.get("monto", 0)
            detalle0 = f0.get("detalle", "movimiento")
            msg0 = "❓ ¿Qué rubro es *" + detalle0 + f"* (${abs(float(monto0)):,.0f})? \nEj: comida, transporte, salud, indumentaria...\n_Te voy a recordar para la próxima_ 🧠"
            await update.message.reply_text(msg0, parse_mode="Markdown")
            return

        user_context.pop(chat_id, None)
        await update.message.reply_text(data.get("mensaje", "✅ Guardado"))

    elif tipo == "pedir_detalle":
        user_context[chat_id] = {"pendiente": "detalle", "data": data}
        await update.message.reply_text(data.get("mensaje", "💬 ¿En qué gastaste y con qué pagaste?"))

    elif tipo == "pedir_inversion":
        user_context[chat_id] = {"pendiente": "inversion", "data": data}
        await update.message.reply_text(data.get("mensaje", "📌 Necesito más datos de la inversión"))

    elif tipo == "dolares":
        rows = [[f.get("fecha",""), f.get("monto",""), f.get("rubro",""), f.get("detalle",""), f.get("cuenta","")] for f in data.get("filas", [])]
        if rows:
            append_rows("DOLARES", rows)
        mp = data.get("movimiento_pesos")
        if mp:
            append_rows("MOVIMIENTOS", [[mp.get("fecha",""), mp.get("monto",""), mp.get("rubro",""), mp.get("detalle",""), mp.get("cuenta","")]])
        user_context.pop(chat_id, None)
        await update.message.reply_text(data.get("mensaje", "✅ Dólares guardado"))

    elif tipo == "inversion":
        rows = [[f.get("fecha",""), f.get("tipo_instr",""), f.get("ticker",""), f.get("detalle",""), f.get("cantidad",""), f.get("precio_compra","")] for f in data.get("filas", [])]
        if rows:
            append_rows("INVERSIONES", rows)
        mp = data.get("movimiento_pesos")
        if mp:
            append_rows("MOVIMIENTOS", [[mp.get("fecha",""), mp.get("monto",""), mp.get("rubro",""), mp.get("detalle",""), mp.get("cuenta","")]])
        user_context.pop(chat_id, None)
        await update.message.reply_text(data.get("mensaje", "✅ Inversión guardada"))

    elif tipo == "taone":
        rows = [[f.get("fecha",""), f.get("concepto",""), f.get("monto",""), "", f.get("detalle","")] for f in data.get("filas", [])]
        if rows:
            append_rows("TAONE", rows)
        user_context.pop(chat_id, None)
        await update.message.reply_text(data.get("mensaje", "✅ TAONE guardado"))

    elif tipo == "tarjeta":
        filas = data.get("filas", [])
        cuenta_pago = data.get("cuenta_pago", "tarjeta")
        rows_ok, dudosos = [], []
        for f in filas:
            if f.get("tipo") == "consulta_rubro":
                dudosos.append(f)
            else:
                rows_ok.append([f.get("fecha",""), f.get("monto",""), f.get("rubro",""), f.get("detalle",""), f"tarjeta_{cuenta_pago}"])
        if rows_ok:
            append_rows("MOVIMIENTOS", rows_ok)
        if dudosos:
            msg = f"💳 Guardé {len(rows_ok)} gastos.\n\n❓ No supe categorizar:\n\n"
            for i, d in enumerate(dudosos, 1):
                msg += f"{i}. {d.get('detalle','?')} — ${abs(float(d.get('monto',0))):,.0f}\n"
            msg += "\n¿Qué rubro les ponés? Ej: *1 comida, 2 suscripciones*"
            user_context[chat_id] = {"pendiente": "tarjeta_rubros", "dudosos": dudosos, "cuenta_pago": cuenta_pago}
            await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            user_context.pop(chat_id, None)
            await update.message.reply_text(f"💳 ✅ {len(rows_ok)} gastos de tarjeta guardados")

    elif tipo == "consulta":
        rows = read_sheet("MOVIMIENTOS", "A:E")
        csv = "\n".join([",".join(str(c) for c in r) for r in rows[:500]])
        today = datetime.now(TZ).strftime("%d/%m/%Y")
        answer = await call_claude(CONSULTA_SYSTEM, f"Hoy es {today}.\nMOVIMIENTOS:\nfecha,monto,rubro,detalle,cuenta\n{csv}\n\nPREGUNTA: {update.message.text}")
        await update.message.reply_text(answer)

    elif tipo == "consulta_inversiones":
        await handle_consulta_inversiones(update)

    else:
        await update.message.reply_text("❓ No entendí. Intentá de nuevo.")


async def handle_consulta_inversiones(update: Update):
    tickers_data = get_tickers_from_sheet()
    if not tickers_data:
        await update.message.reply_text("📊 No tenés inversiones registradas todavía.")
        return
    ticker_names = [t["ticker"] for t in tickers_data]
    await update.message.reply_text(f"📈 Consultando {', '.join(ticker_names)}...")
    prices = await get_market_prices(ticker_names)
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    info = "CARTERA:\n"
    for t in tickers_data:
        tk = t["ticker"]
        p = prices.get(tk, {})
        info += f"- {tk}: {t['cantidad']} nominales, compra ${t['precio_compra']:,.0f}, hoy ${p.get('hoy','N/D')}, ayer ${p.get('ayer','N/D')}\n"
    answer = await call_claude(INVERSIONES_SYSTEM, f"Hoy es {today}.\n{info}\nCalculá resultado diario y total.")
    await update.message.reply_text(answer)


# ─────────────────────────────────────────────
# MESSAGE HANDLERS
# ─────────────────────────────────────────────
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    last_message_date["date"] = datetime.now(TZ).date()

    ctx = user_context.get(chat_id)
    if ctx:
        pendiente = ctx.get("pendiente")

        if pendiente == "detalle":
            original = ctx["data"]
            prompt = (
                f"Hoy es {today}. El usuario quería registrar un movimiento. "
                f"Datos originales: monto={original.get('monto')}, detalle='{original.get('detalle')}'. "
                f"Ahora agregó: '{text}'. "
                f"Generá el JSON de movimiento completo con todos los datos."
            )
            raw = await call_claude(SYSTEM_PROMPT, prompt)
            raw = raw.strip().replace("```json","").replace("```","").strip()
            data = json.loads(raw)
            user_context.pop(chat_id, None)
            await handle_result(data, update, context)
            return

        elif pendiente == "inversion":
            original = ctx["data"]
            datos = original.get("datos_presentes", {})
            prompt = (
                f"Hoy es {today}. El usuario cargaba una inversión tipo {original.get('instrumento','CEDEAR')}. "
                f"Datos que ya teníamos: {json.dumps(datos)}. "
                f"Ahora completó: '{text}'. "
                f"Generá el JSON de inversión completo."
            )
            raw = await call_claude(SYSTEM_PROMPT, prompt)
            raw = raw.strip().replace("```json","").replace("```","").strip()
            data = json.loads(raw)
            user_context.pop(chat_id, None)
            await handle_result(data, update, context)
            return

        elif pendiente == "uala_dudosos":
            dudosos       = ctx.get("dudosos", [])
            idx           = ctx.get("idx", 0)
            nuevas_memorias = ctx.get("nuevas_memorias", [])
            d             = dudosos[idx]
            rubro         = text.strip().lower().split()[0]
            fecha         = d.get("fecha", today)
            monto         = d.get("monto", 0)
            detalle       = str(d.get("detalle", "")).strip()

            # Save movement with assigned rubro
            append_rows("MOVIMIENTOS", [[fecha, monto, rubro, detalle, "uala"]])

            # Save to memory for next time
            if detalle and rubro not in ["traspaso", "sueldo", "rendimientos", "inversion", "dolares"]:
                nuevas_memorias.append({"comercio": detalle, "rubro": rubro})
                save_memory(detalle, rubro)
                await update.message.reply_text(
                    f"✅ Guardado como *{rubro}* 🧠 La próxima vez que aparezca '{detalle}' lo categorizo solo.",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(f"✅ Guardado como {rubro}")

            # Move to next dudoso
            next_idx = idx + 1
            if next_idx < len(dudosos):
                ctx["idx"] = next_idx
                ctx["nuevas_memorias"] = nuevas_memorias
                d2 = dudosos[next_idx]
                monto2 = abs(float(d2.get("monto", 0)))
                detalle2 = str(d2.get("detalle", "?")).strip()
                await update.message.reply_text(
                    f"❓ {next_idx+1}/{len(dudosos)}: *{detalle2}* — ${monto2:,.0f}\n¿Qué rubro es?",
                    parse_mode="Markdown"
                )
                return

            # All dudosos resolved
            user_context.pop(chat_id, None)
            save_last_uala_date(today)
            uala_reminder_state["state"] = None
            msg = f"✅ ¡UALA procesado!"
            if nuevas_memorias:
                msg += f" Memoricé {len(nuevas_memorias)} comercio(s) nuevo(s) 🧠"
            await update.message.reply_text(msg)
            return

        elif pendiente == "uala_transferencias":
            transferencias = ctx.get("transferencias", [])
            dudosos        = ctx.get("dudosos", [])
            idx            = ctx.get("idx", 0)
            t              = transferencias[idx]
            cuenta_destino = text.strip().lower()
            monto          = float(t.get("monto", 0))
            fecha          = t.get("fecha", today)

            # Save as traspaso
            rows = [
                [fecha, -abs(monto), "traspaso", f"Traspaso a {cuenta_destino}", "uala"],
                [fecha,  abs(monto), "traspaso", f"Traspaso desde UALA", cuenta_destino],
            ]
            append_rows("MOVIMIENTOS", rows)
            await update.message.reply_text(f"✅ Traspaso guardado: UALA → {cuenta_destino}")

            # Next transfer or move to dudosos
            next_idx = idx + 1
            if next_idx < len(transferencias):
                ctx["idx"] = next_idx
                t2 = transferencias[next_idx]
                monto2 = abs(float(t2.get("monto", 0)))
                signo2 = "+" if float(t2.get("monto", 0)) > 0 else "-"
                await update.message.reply_text(
                    f"💸 Otra transferencia tuya: {signo2}${monto2:,.0f}\n¿A qué cuenta te la pasaste?",
                )
                return

            # Move to dudosos if any
            if dudosos:
                user_context[chat_id] = {"pendiente": "uala_dudosos", "dudosos": dudosos, "idx": 0, "nuevas_memorias": []}
                d = dudosos[0]
                monto_d = abs(float(d.get("monto", 0)))
                detalle_d = str(d.get("detalle", "?")).strip()
                await update.message.reply_text(
                    f"❓ 1/{len(dudosos)}: *{detalle_d}* — ${monto_d:,.0f}\n¿Qué rubro es?",
                    parse_mode="Markdown"
                )
                return

            user_context.pop(chat_id, None)
            save_last_uala_date(today)
            uala_reminder_state["state"] = None
            await update.message.reply_text("✅ ¡UALA procesado!")
            return

        elif pendiente == "rubro_desconocido":
            filas_pendientes = ctx.get("filas_pendientes", [])
            idx = ctx.get("idx", 0)
            f = filas_pendientes[idx]
            rubro = text.strip().lower().split()[0]
            detalle = str(f.get("detalle", "")).strip()
            fecha = f.get("fecha", today)
            monto = f.get("monto", 0)
            cuenta = f.get("cuenta", "")

            # Save movement with new rubro
            append_rows("MOVIMIENTOS", [[fecha, monto, rubro, detalle, cuenta]])

            # Save to memory
            if detalle and rubro not in ["traspaso", "sueldo", "rendimientos", "inversion", "dolares"]:
                save_memory(detalle, rubro)
                await update.message.reply_text(
                    f"✅ Guardado como *{rubro}* 🧠 Memoricé que '{detalle}' = {rubro}",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(f"✅ Guardado como {rubro}")

            # Next unknown fila
            next_idx = idx + 1
            if next_idx < len(filas_pendientes):
                ctx["idx"] = next_idx
                f2 = filas_pendientes[next_idx]
                monto2 = f2.get("monto", 0)
                detalle2 = str(f2.get("detalle", "movimiento")).strip()
            msg0 = "❓ ¿Qué rubro es *" + detalle0 + f"* (${abs(float(monto0)):,.0f})? \nEj: comida, transporte, salud, indumentaria...\n_Te voy a recordar para la próxima_ 🧠"
            await update.message.reply_text(msg0, parse_mode="Markdown")
            return

            user_context.pop(chat_id, None)
            return

        elif pendiente == "tipo_imagen":
            bytes_data = ctx.get("bytes", b"")
            mime_data  = ctx.get("mime", "image/jpeg")
            user_context.pop(chat_id, None)
            txt_lower = text.strip().lower()
            if txt_lower in ["1", "uala", "1️⃣"] or "uala" in txt_lower:
                await handle_uala_screenshot(bytes_data, mime_data, update, context)
            else:
                await update.message.reply_text("📄 Analizando resumen de tarjeta...")
                b64 = base64.standard_b64encode(bytes_data).decode()
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                        json={
                            "model": MODEL, "max_tokens": 2048, "system": SYSTEM_PROMPT,
                            "messages": [{"role": "user", "content": [
                                {"type": "text", "text": f"Hoy es {today}. Resumen de tarjeta. Extraé los gastos en JSON tipo tarjeta."},
                                {"type": "image", "source": {"type": "base64", "media_type": mime_data, "data": b64}}
                            ]}]
                        }
                    )
                raw = resp.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
                data = json.loads(raw)
                await handle_result(data, update, context)
            return

        elif pendiente == "tarjeta_rubros":
            dudosos = ctx["dudosos"]
            cuenta_pago = ctx["cuenta_pago"]
            prompt = (
                f"El usuario asignó rubros a gastos dudosos de tarjeta. "
                f"Gastos sin rubro: {json.dumps(dudosos)}. "
                f"Respuesta del usuario: '{text}'. "
                f"Generá JSON tipo movimiento con los rubros asignados y cuenta='tarjeta_{cuenta_pago}'."
            )
            raw = await call_claude(SYSTEM_PROMPT, prompt)
            raw = raw.strip().replace("```json","").replace("```","").strip()
            data = json.loads(raw)
            user_context.pop(chat_id, None)
            await handle_result(data, update, context)
            return

    # New message
    await update.message.reply_text("⏳ Procesando...")
    try:
        raw = await call_claude(SYSTEM_PROMPT, f"Hoy es {today}. Mensaje: {text}")
        raw = raw.strip().replace("```json","").replace("```","").strip()
        data = json.loads(raw)
        await handle_result(data, update, context)
    except json.JSONDecodeError:
        await update.message.reply_text("❌ No pude procesar el mensaje. Intentá de nuevo.")
    except Exception as e:
        logger.error(f"on_text error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    last_message_date["date"] = datetime.now(TZ).date()
    await update.message.reply_text("🎤 Escuchando...")
    try:
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await voice_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                audio_bytes = f.read()
        transcription = await transcribe_audio(audio_bytes)
        await update.message.reply_text(f"📝 _{transcription}_", parse_mode="Markdown")
        raw = await call_claude(SYSTEM_PROMPT, f"Hoy es {today}. Mensaje de audio: {transcription}")
        raw = raw.strip().replace("```json","").replace("```","").strip()
        data = json.loads(raw)
        await handle_result(data, update, context)
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text(f"❌ {str(e)}")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle image/PDF — UALA screenshot or credit card summary."""
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    last_message_date["date"] = datetime.now(TZ).date()

    # Check if we're waiting for a UALA screenshot
    chat_id = update.effective_chat.id
    ctx = user_context.get(chat_id, {})
    is_uala_context = (
        uala_reminder_state.get("state") in ["waiting_tonight", "waiting_morning"] or
        ctx.get("pendiente", "").startswith("uala")
    )

    try:
        if update.message.photo:
            file_obj = update.message.photo[-1]
            mime = "image/jpeg"
        else:
            file_obj = update.message.document
            mime = file_obj.mime_type or "image/jpeg"
        tg_file = await file_obj.get_file()
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            await tg_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                content_bytes = f.read()

        # If it's a photo and we're in UALA context, process as UALA
        if update.message.photo or is_uala_context:
            # Ask user if it's UALA or tarjeta (only if not already in context)
            caption = update.message.caption or ""
            if "uala" in caption.lower() or is_uala_context:
                await handle_uala_screenshot(content_bytes, mime, update, context)
                return
            elif "tarjeta" in caption.lower() or "tc" in caption.lower():
                pass  # fall through to tarjeta processing
            else:
                # Ask what it is
                user_context[chat_id] = {"pendiente": "tipo_imagen", "bytes": content_bytes, "mime": mime}
                await update.message.reply_text(
                    "📸 ¿Qué es esta imagen?\n\nRespondé *uala* o *tarjeta*",
                )
                return
    except Exception as e:
        logger.error(f"Document download error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
        return

    await update.message.reply_text("📄 Analizando resumen de tarjeta...")
    try:
        if update.message.photo:
            file_obj = update.message.photo[-1]
            mime = "image/jpeg"
        else:
            file_obj = update.message.document
            mime = file_obj.mime_type or "image/jpeg"
        tg_file = await file_obj.get_file()
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            await tg_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                content = f.read()
        b64 = base64.standard_b64encode(content).decode()
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={
                    "model": MODEL,
                    "max_tokens": 2048,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": f"Hoy es {today}. Este es un resumen de tarjeta de crédito. Extraé todos los gastos y devolvé JSON tipo 'tarjeta'. Para los gastos cuyo rubro no queda claro, marcalos como tipo consulta_rubro."},
                        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
                    ]}]
                }
            )
        raw = resp.json()["content"][0]["text"]
        raw = raw.strip().replace("```json","").replace("```","").strip()
        data = json.loads(raw)
        await handle_result(data, update, context)
    except Exception as e:
        logger.error(f"Document error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")



async def uala_reminder_noche(context) -> None:
    pass  # kept for compatibility

async def uala_reminder_noche_standalone(bot) -> None:
    """Send UALA screenshot reminder at 22:00."""
    if not CHAT_ID:
        return
    today = datetime.now(TZ).date()
    if uala_reminder_state.get("date") == today:
        return
    uala_reminder_state["state"] = "waiting_tonight"
    uala_reminder_state["date"] = today
    await bot.send_message(
        chat_id=int(CHAT_ID),
        text="📱 ¡Hora de registrar UALA!\n\nMandame una captura de pantalla de tus movimientos de hoy."
    )


async def uala_reminder_manana(context) -> None:
    pass  # kept for compatibility

async def uala_reminder_manana_standalone(bot) -> None:
    """Morning reminder if UALA screenshot wasn't sent last night."""
    if not CHAT_ID:
        return
    if uala_reminder_state.get("state") == "waiting_tonight":
        uala_reminder_state["state"] = "waiting_morning"
        await bot.send_message(
            chat_id=int(CHAT_ID),
            text="☀️ Buenos días! Todavía no registraste los movimientos de UALA de ayer.\n\nMandame la captura cuando puedas."
        )


# ─────────────────────────────────────────────
# RECORDATORIOS
# ─────────────────────────────────────────────
async def reminder_job(context) -> None:
    pass  # kept for compatibility

last_message_date = {"date": None}  # global to track messages

async def reminder_job_standalone(bot) -> None:
    if not CHAT_ID:
        return
    today = datetime.now(TZ).date()
    if last_message_date.get("date") == today:
        return
    hour = datetime.now(TZ).hour
    msg = "👋 ¿Gastaste algo hoy que no hayas anotado?" if hour < 20 else "🌙 Antes de dormir — ¿algo que registrar del día?"
    await bot.send_message(chat_id=int(CHAT_ID), text=msg)


# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────


async def cmd_uala(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger UALA screenshot request."""
    uala_reminder_state["state"] = "waiting_tonight"
    last = await get_last_uala_date()
    msg = "📱 Mandame la captura de pantalla de tus movimientos de UALA."
    if last:
        msg += f"\n\n_(Solo procesaré movimientos posteriores al {last})_"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_resetuala(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset UALA last processed date (to reprocess everything)."""
    save_last_uala_date("")
    await update.message.reply_text("🔄 Fecha UALA reseteada. La próxima captura procesará todos los movimientos visibles.")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 *Hola, soy BOTSTERO!*\n\n"
        f"Tu chat ID: `{update.effective_chat.id}`\n"
        f"Cargalo en Railway como `CHAT_ID` para recordatorios.\n\n"
        f"📌 Comandos:\n"
        f"/cartera — ver cómo van tus inversiones\n"
        f"/resumen — resumen del mes\n"
        f"/uala — procesar captura de UALA\n\n"
        f"¡Mandame texto o audio!",
        parse_mode="Markdown"
    )


async def cmd_cartera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Consultando tu cartera...")
    await handle_consulta_inversiones(update)


async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = read_sheet("MOVIMIENTOS", "A:E")
    csv = "\n".join([",".join(str(c) for c in r) for r in rows[:500]])
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    answer = await call_claude(
        CONSULTA_SYSTEM,
        f"Hoy es {today}.\nMOVIMIENTOS:\nfecha,monto,rubro,detalle,cuenta\n{csv}\n\n"
        f"Dame resumen del mes: total ingresos, total gastos, resultado neto, top 3 rubros de gasto."
    )
    await update.message.reply_text(answer)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cartera", cmd_cartera))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("uala", cmd_uala))
    app.add_handler(CommandHandler("resetuala", cmd_resetuala))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_document))

    # Scheduled jobs via APScheduler
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(lambda: app.create_task(reminder_job_standalone(app.bot)), 'cron', hour=18, minute=0)
    scheduler.add_job(lambda: app.create_task(reminder_job_standalone(app.bot)), 'cron', hour=23, minute=0)
    scheduler.add_job(lambda: app.create_task(uala_reminder_noche_standalone(app.bot)), 'cron', hour=22, minute=0)
    scheduler.add_job(lambda: app.create_task(uala_reminder_manana_standalone(app.bot)), 'cron', hour=9, minute=0)
    scheduler.start()

    logger.info("🤖 BOTSTERO iniciado ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
