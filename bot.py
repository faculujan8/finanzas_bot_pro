import os
import json
import logging
import tempfile
import httpx
import base64
from datetime import datetime, time
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler, CallbackContext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_KEY"]
SHEETS_ID      = os.environ["SHEETS_ID"]
GOOGLE_SA_JSON = os.environ["GOOGLE_SA_JSON"]
CHAT_ID        = os.environ.get("CHAT_ID", "")
MP_TOKEN       = os.environ.get("MP_ACCESS_TOKEN", "")
TZ             = ZoneInfo("America/Argentina/Buenos_Aires")
MODEL          = "claude-haiku-4-5-20251001"

# Track last processed MP payment IDs to avoid duplicates
mp_last_ids: set = set()

# UALA reminder state: None | "waiting_tonight" | "waiting_morning"
uala_reminder_state: dict = {"state": None, "date": None}

# In-memory conversation context per chat
user_context: dict = {}

# ─────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """Sos BOTSTERO, un asistente financiero personal con onda. Procesá el mensaje y devolvé SOLO JSON válido, sin texto ni markdown.

CUENTAS: efectivo, uala, galicia, balanz, binance, mercadopago
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
# MERCADO PAGO API
# ─────────────────────────────────────────────
async def mp_get_movements(limit: int = 50) -> list:
    """Fetch recent movements from Mercado Pago API."""
    if not MP_TOKEN:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.mercadopago.com/v1/account/movements/search",
            headers={"Authorization": f"Bearer {MP_TOKEN}"},
            params={"limit": limit, "offset": 0}
        )
        if resp.status_code != 200:
            logger.error(f"MP API error {resp.status_code}: {resp.text}")
            return []
        data = resp.json()
        return data.get("results", [])


async def mp_get_balance() -> dict:
    """Get current Mercado Pago balance."""
    if not MP_TOKEN:
        return {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.mercadopago.com/v1/account/balance",
            headers={"Authorization": f"Bearer {MP_TOKEN}"}
        )
        if resp.status_code != 200:
            return {}
        return resp.json()


async def mp_sync_job(context: CallbackContext):
    """Job that runs every hour to import new MP movements automatically."""
    if not MP_TOKEN or not CHAT_ID:
        return

    movements = await mp_get_movements(limit=30)
    new_rows = []
    new_count = 0

    for mov in movements:
        mov_id = str(mov.get("id", ""))
        if mov_id in mp_last_ids:
            continue

        mp_last_ids.add(mov_id)

        # Parse movement
        amount    = float(mov.get("amount", 0))
        memo      = mov.get("memo", "") or mov.get("description", "") or "Movimiento MP"
        date_str  = mov.get("date_created", "")[:10]  # YYYY-MM-DD
        mov_type  = mov.get("type", "")

        # Skip internal/technical movements
        if mov_type in ["investment", "reserve"]:
            continue

        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            fecha = dt.strftime("%d/%m/%Y")
        except:
            fecha = datetime.now(TZ).strftime("%d/%m/%Y")

        # Classify rubro using Claude
        rubro = await mp_classify_rubro(memo, amount)

        new_rows.append([fecha, amount, rubro, memo, "mercadopago"])
        new_count += 1

    if new_rows:
        append_rows("MOVIMIENTOS", new_rows)
        msg = f"🟡 *Mercado Pago* — {new_count} movimientos importados automáticamente\n"
        for r in new_rows[:5]:
            signo = "+" if float(r[1]) > 0 else ""
            msg += f"  {'✅' if float(r[1]) > 0 else '💸'} {r[0]} | {signo}${float(r[1]):,.0f} | {r[2]} | {r[3][:30]}\n"
        if new_count > 5:
            msg += f"  ...y {new_count - 5} más"
        await context.bot.send_message(chat_id=int(CHAT_ID), text=msg, parse_mode="Markdown")


async def mp_classify_rubro(descripcion: str, monto: float) -> str:
    """Use Claude to classify a MP movement into a rubro."""
    try:
        rubros = "almacen, auto, cafe, cancha, cochera, combustible, comida, farmacia, gimnasio, impuestos, indumentaria, inversion, nafta, prepaga, regalos, rendimientos, salud, sueldo, suscripciones, transporte, varios, cena, almuerzo"
        raw = await call_claude(
            f"Clasificá este movimiento de Mercado Pago en exactamente uno de estos rubros: {rubros}\n"
            f"Si es un rendimiento/interés → rendimientos\n"
            f"Si es transferencia recibida → varios\n"
            f"Respondé SOLO con el nombre del rubro, nada más.",
            f"Descripción: {descripcion} | Monto: {monto}",
            max_tokens=20
        )
        return raw.strip().lower().split()[0]
    except:
        return "varios"


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




def get_uala_memory() -> dict:
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


def save_uala_memory(comercio: str, rubro: str):
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


def save_bulk_uala_memory(mappings: list):
    """Save multiple comercio->rubro mappings at once."""
    for m in mappings:
        if m.get("comercio") and m.get("rubro"):
            save_uala_memory(m["comercio"], m["rubro"])


async def process_uala_screenshot(image_bytes: bytes, mime: str, today: str) -> dict:
    """Send UALA screenshot to Claude for processing."""
    memory = get_uala_memory()
    memory_str = json.dumps(memory, ensure_ascii=False) if memory else "{}"
    b64 = base64.standard_b64encode(image_bytes).decode()

    system = """Sos BOTSTERO analizando una captura de pantalla de movimientos de UALA.
Devolvé SOLO JSON válido, sin texto ni markdown.

Formato de respuesta:
{
  "movimientos_ok": [
    {"fecha":"DD/MM/YYYY","monto":-1500,"rubro":"comida","detalle":"Nombre comercio/descripcion","cuenta":"uala"}
  ],
  "movimientos_dudosos": [
    {"fecha":"DD/MM/YYYY","monto":-800,"detalle":"Nombre exacto del comercio","cuenta":"uala"}
  ],
  "transferencias_propias": [
    {"fecha":"DD/MM/YYYY","monto":50000,"detalle":"Transferencia de Facundo Dardo Lujan","cuenta":"uala"}
  ],
  "rendimientos": [
    {"fecha":"DD/MM/YYYY","monto":250,"detalle":"Rendimiento diario","cuenta":"uala"}
  ]
}

REGLAS:
1. movimientos_ok: los que podés categorizar con certeza usando la memoria o el nombre del comercio
2. movimientos_dudosos: comercios desconocidos que no están en memoria y no son obvios
3. transferencias_propias: cualquier transferencia de/a "Facundo Dardo Lujan" o "Facundo Lujan"
4. rendimientos: cualquier acreditación de rendimientos/intereses
5. Montos negativos = gastos, positivos = ingresos
6. Fechas en DD/MM/YYYY. Si ves solo día/mes, usá el año actual
7. NUNCA inventes rubros si no estás seguro — mandalo a dudosos"""

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": MODEL,
                "max_tokens": 2048,
                "system": system,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": f"Hoy es {today}. Memoria de comercios conocidos: {memory_str}\n\nAnalizá esta captura de UALA y clasificá los movimientos."},
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
                ]}]
            }
        )
    raw = resp.json()["content"][0]["text"]
    raw = raw.strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)


async def handle_uala_screenshot(image_bytes: bytes, mime: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full flow for processing a UALA screenshot."""
    chat_id = update.effective_chat.id
    today = datetime.now(TZ).strftime("%d/%m/%Y")

    await update.message.reply_text("📱 Analizando movimientos de UALA...")

    try:
        result = await process_uala_screenshot(image_bytes, mime, today)
    except Exception as e:
        await update.message.reply_text(f"❌ Error procesando la imagen: {str(e)}")
        return

    rows_to_save = []
    msg_parts = []
    total_saved = 0

    # 1. Save confirmed movements
    ok = result.get("movimientos_ok", [])
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

    if rows_to_save:
        append_rows("MOVIMIENTOS", rows_to_save)

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
    # Reset UALA reminder state
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
        rows = [[f.get("fecha",""), f.get("monto",""), f.get("rubro",""), f.get("detalle",""), f.get("cuenta","")] for f in data.get("filas", [])]
        if rows:
            append_rows("MOVIMIENTOS", rows)
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
    context.bot_data["last_message_date"] = datetime.now(TZ).date()

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
    context.bot_data["last_message_date"] = datetime.now(TZ).date()
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
    context.bot_data["last_message_date"] = datetime.now(TZ).date()

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
                    "📸 ¿Qué es esta imagen?\n\n1️⃣ Captura de UALA\n2️⃣ Resumen de tarjeta de crédito",
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



async def uala_reminder_noche(context: CallbackContext):
    """Send UALA screenshot reminder at 22:00."""
    if not CHAT_ID:
        return
    today = datetime.now(TZ).date()
    if uala_reminder_state.get("date") == today:
        return  # already processed today
    uala_reminder_state["state"] = "waiting_tonight"
    uala_reminder_state["date"] = today
    await context.bot.send_message(
        chat_id=int(CHAT_ID),
        text="📱 ¡Hora de registrar UALA!\n\nMandame una captura de pantalla de tus movimientos de hoy."
    )


async def uala_reminder_manana(context: CallbackContext):
    """Morning reminder if UALA screenshot wasn't sent last night."""
    if not CHAT_ID:
        return
    today = datetime.now(TZ).date()
    if uala_reminder_state.get("state") == "waiting_tonight":
        uala_reminder_state["state"] = "waiting_morning"
        await context.bot.send_message(
            chat_id=int(CHAT_ID),
            text="☀️ Buenos días! Todavía no registraste los movimientos de UALA de ayer.\n\nMandame la captura cuando puedas."
        )


# ─────────────────────────────────────────────
# RECORDATORIOS
# ─────────────────────────────────────────────
async def reminder_job(context: CallbackContext):
    if not CHAT_ID:
        return
    today = datetime.now(TZ).date()
    last = context.bot_data.get("last_message_date")
    if last != today:
        hour = datetime.now(TZ).hour
        msg = "👋 ¿Gastaste algo hoy que no hayas anotado?" if hour < 20 else "🌙 Antes de dormir — ¿algo que registrar del día?"
        await context.bot.send_message(chat_id=int(CHAT_ID), text=msg)


# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────

async def cmd_mp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show MP balance and last movements."""
    if not MP_TOKEN:
        await update.message.reply_text("❌ No configuraste MP_ACCESS_TOKEN en Railway.")
        return
    await update.message.reply_text("🟡 Consultando Mercado Pago...")
    try:
        balance = await mp_get_balance()
        movements = await mp_get_movements(limit=10)

        saldo = balance.get("available_balance", 0)
        msg = f"🟡 *Mercado Pago*\n💰 Saldo disponible: ${float(saldo):,.2f}\n\n📋 Últimos movimientos:\n"

        for mov in movements[:8]:
            amount = float(mov.get("amount", 0))
            memo   = (mov.get("memo") or mov.get("description") or "Movimiento")[:35]
            date   = mov.get("date_created", "")[:10]
            signo  = "+" if amount > 0 else ""
            emoji  = "✅" if amount > 0 else "💸"
            msg += f"{emoji} {date} | {signo}${amount:,.0f} | {memo}\n"

        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def cmd_mp_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger MP sync."""
    if not MP_TOKEN:
        await update.message.reply_text("❌ No configuraste MP_ACCESS_TOKEN en Railway.")
        return
    await update.message.reply_text("🔄 Sincronizando Mercado Pago...")
    await mp_sync_job(context)
    await update.message.reply_text("✅ Sincronización completada")



async def cmd_uala(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger UALA screenshot request."""
    uala_reminder_state["state"] = "waiting_tonight"
    await update.message.reply_text(
        "📱 Mandame la captura de pantalla de tus movimientos de UALA."
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 *Hola, soy BOTSTERO!*\n\n"
        f"Tu chat ID: `{update.effective_chat.id}`\n"
        f"Cargalo en Railway como `CHAT_ID` para recordatorios.\n\n"
        f"📌 Comandos:\n"
        f"/cartera — ver cómo van tus inversiones\n"
        f"/resumen — resumen del mes\n"
        f"/mp — saldo y movimientos de Mercado Pago\n"
        f"/mpsync — importar movimientos MP ahora\n"
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
    app.add_handler(CommandHandler("mp", cmd_mp))
    app.add_handler(CommandHandler("mpsync", cmd_mp_sync))
    app.add_handler(CommandHandler("uala", cmd_uala))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_document))

    app.job_queue.run_daily(reminder_job, time=time(18, 0, tzinfo=TZ))
    app.job_queue.run_daily(uala_reminder_noche, time=time(22, 0, tzinfo=TZ))
    app.job_queue.run_daily(uala_reminder_manana, time=time(9, 0, tzinfo=TZ))
    if MP_TOKEN:
        app.job_queue.run_repeating(mp_sync_job, interval=3600, first=10)
    app.job_queue.run_daily(reminder_job, time=time(23, 0, tzinfo=TZ))

    logger.info("🤖 BOTSTERO iniciado ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
