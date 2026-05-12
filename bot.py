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

def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF using pdfplumber for accurate number reading."""
    try:
        import pdfplumber
        import io
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
        return "\n".join(text_parts)
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return ""



logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_KEY"]
SHEETS_ID      = os.environ["SHEETS_ID"]
GOOGLE_SA_JSON = os.environ["GOOGLE_SA_JSON"]
CHAT_ID        = os.environ.get("CHAT_ID", "")
TZ             = ZoneInfo("America/Argentina/Buenos_Aires")
MODEL          = "claude-haiku-4-5-20251001"

# UALA reminder state — persisted in Sheets (MEMORIA_UALA col E:F)
# In-memory cache only, real state is in Sheets
uala_reminder_state: dict = {"state": None, "date": None}

def get_uala_reminder_state() -> dict:
    """Read UALA reminder state from Sheets (persists across restarts)."""
    try:
        rows = read_sheet("MEMORIA_UALA", "E1:F3")
        state = None
        date_str = None
        for row in rows:
            if len(row) >= 2:
                if str(row[0]).strip() == "REMINDER_STATE":
                    state = str(row[1]).strip() or None
                elif str(row[0]).strip() == "REMINDER_DATE":
                    date_str = str(row[1]).strip() or None
        result = {"state": state, "date": None}
        if date_str:
            try:
                from datetime import date
                result["date"] = datetime.strptime(date_str, "%Y-%m-%d").date()
            except:
                pass
        return result
    except:
        return {"state": None, "date": None}

def save_uala_reminder_state(state: str, date_val=None):
    """Persist UALA reminder state to Sheets."""
    try:
        service = get_sheets_service()
        date_str = date_val.strftime("%Y-%m-%d") if date_val else ""
        service.spreadsheets().values().update(
            spreadsheetId=SHEETS_ID,
            range="MEMORIA_UALA!E1:F3",
            valueInputOption="RAW",
            body={"values": [
                ["REMINDER_STATE", state or ""],
                ["REMINDER_DATE",  date_str],
                ["LAST_UALA_PROCESSED", ""]
            ]}
        ).execute()
        # Update in-memory cache
        uala_reminder_state["state"] = state
        uala_reminder_state["date"] = date_val
    except Exception as e:
        logger.error(f"Error saving reminder state: {e}")
        uala_reminder_state["state"] = state
        uala_reminder_state["date"] = date_val

# In-memory conversation context per chat
user_context: dict = {}

# ─────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """Sos BOTSTERO, un asistente financiero personal con onda. Procesá el mensaje y devolvé SOLO JSON válido, sin texto ni markdown.

CUENTAS (siempre en mayúsculas en el JSON): EFECTIVO, UALA, GALICIA, BALANZ, BINANCE
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

11) TARJETA/EXTRACTO — extracto o resumen de tarjeta de crédito:
{"tipo":"tarjeta","banco":"GALICIA","marca":"VISA","banco":"GALICIA","marca":"VISA","cuenta_pago":"GALICIA","total_extracto":184257.50,"filas":[{"fecha":"DD/MM/YYYY","monto":-5000,"tipo":"consulta_rubro","detalle":"Nombre del comercio exacto"},{"tipo":"consulta_rubro","detalle":"RADIO*SOMETHING*AR","monto":-3000,"fecha":"DD/MM/YYYY"}],"mensaje":"💳 Procesando..."}
- total_extracto: usar "TOTAL CONSUMOS DEL MES" si existe, sino "TARJETA XXXX Total Consumos". NO usar TOTAL A PAGAR (ese incluye saldo anterior e impuestos del financiamiento). Número positivo.

CÓMO IDENTIFICAR EL BANCO Y MARCA:
- Si dice "Galicia" o logo naranja → banco=GALICIA
- Si dice "Banco de la Nación Argentina" o "Banco Nación" o "BNA" → banco=NACION  
- Si dice "VISA" → marca=VISA
- Si dice "MASTERCARD" o "Mastercard Gold" → marca=MASTERCARD
- cuenta_pago = banco solamente (ej: GALICIA, NACION)

FORMATOS QUE PODÉS VER:
Galicia VISA: tabla FECHA / REFERENCIA / CUOTA / COMPROBANTE / PESOS — cada fila es un consumo
Galicia Mastercard: secciones "DEBITOS AUTOMATICOS" y "CUOTA DEL MES" — extraé todas las filas de ambas secciones
BNA VISA/Mastercard: tabla FECHA / COMPROBANTE / DETALLE DE TRANSACCION / PESOS — cada fila es un consumo

REGLAS CRÍTICAS:
1. Extraé CADA línea de consumo individual — nunca uses totales ni subtotales
2. Monto de cada fila = SIEMPRE NEGATIVO para gastos/consumos. Si el número aparece sin signo en la tabla de consumos, ponerlo negativo. Solo positivo si es una devolución/acreditación real al cliente.
3. Para cuotas (ej: PAYU*AR*ADIDAS 02/06): extraé solo esa cuota, no el total
4. Ignorá SIEMPRE estas líneas (NO son gastos del mes): SALDO ANTERIOR, SU PAGO EN PESOS, SU PAGO, SU PAGO U$S, SALDO PENDIENTE, SUBTOTAL, TOTAL TITULAR, TOTAL A PAGAR, DEV PER RG, SALDO ACTUAL, PAGO MINIMO
5. IMPUESTOS Y PERCEPCIONES → agrupalos en UNA sola fila con rubro="impuestos" y detalle="Impuestos y percepciones (AFIP/IIBB/Sellos)". Sumalos todos juntos.
   Incluye: IMPUESTO DE SELLOS, PERCEPCION IVA, PERCEP.AFIP, PERC IIBB, PERC IIBB SERV DIG CABA
   Ejemplo: si hay IMPUESTO DE SELLOS $581, PERCEP.AFIP $830, PERC IIBB $55 → una fila con monto=-1466
6. Fechas: USA SIEMPRE la fecha que se te indica como FECHA_PAGO para TODAS las filas. NO uses las fechas del extracto.
7. TODOS los consumos van como tipo consulta_rubro — el sistema consulta la memoria automáticamente
8. Excepción: solo ponés rubro directamente si es COMBUSTIBLE→combustible, PREVENS→salud, MAPFRE→seguro
9. Si hay gastos en DÓLARES (columna DOLARES tiene valor): incluirlos como filas separadas con monto en USD y detalle que diga "(USD)"
10. NUNCA incluyas el TOTAL TITULAR ni TOTAL A PAGAR como fila

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


def normalize_date(val):
    """Convert any date format to DD/MM/YYYY for Google Sheets."""
    if not val:
        return val
    s = str(val).strip()
    # Already DD/MM/YYYY
    if len(s) == 10 and s[2] == "/" and s[5] == "/":
        return s
    # YYYY-MM-DD format
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        parts = s.split("-")
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    return val

def append_rows(sheet_name: str, rows: list):
    # Normalize dates in column A (index 0) to DD/MM/YYYY
    normalized = []
    for row in rows:
        if row and len(row) > 0:
            new_row = list(row)
            new_row[0] = normalize_date(new_row[0])
            normalized.append(new_row)
        else:
            normalized.append(row)
    service = get_sheets_service()
    service.spreadsheets().values().append(
        spreadsheetId=SHEETS_ID,
        range=f"{sheet_name}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": normalized}
    ).execute()


def read_sheet(sheet_name: str, range_: str = "A:F") -> list:
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEETS_ID,
        range=f"{sheet_name}!{range_}"
    ).execute()
    return result.get("values", [])


def get_tickers_from_sheet() -> list:
    # Columns: A=fecha, B=tipo, C=ticker, D=detalle, E=cantidad, F=precio_compra_usd
    rows = read_sheet("INVERSIONES", "A:F")
    tickers = {}
    for row in rows[2:]:  # skip 2 header rows
        if len(row) < 3:
            continue
        ticker = str(row[2]).strip().upper() if len(row) > 2 else ""
        if not ticker or ticker == "TICKER /":
            continue
        try:
            qty   = float(str(row[4]).replace(",", ".").replace("$", "").replace(" ", "")) if len(row) > 4 else 0
            price = float(str(row[5]).replace(",", ".").replace("$", "").replace(" ", "")) if len(row) > 5 else 0
        except:
            qty, price = 0, 0
        if qty == 0:
            continue
        if ticker in tickers:
            tickers[ticker]["cantidad"] += qty
            # weighted average price
            old_qty = tickers[ticker]["cantidad"] - qty
            tickers[ticker]["precio_compra"] = (tickers[ticker]["precio_compra"] * old_qty + price * qty) / tickers[ticker]["cantidad"]
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

        # Detect if this is a TRANSFER to a person (vs a payment to a business)
        # Transfers: subtitle was "Transferencia enviada", name is plain words
        # Payments: subtitle was "Transporte", "Restaurantes", etc. — use memory
        import re
        subtitulo = str(m.get("subtitulo", "")).lower()
        is_transfer = "transferencia" in subtitulo or bool(
            re.match(r'^[A-Za-záéíóúñÁÉÍÓÚÑ,. ]+$', detalle) and
            len(detalle.split()) >= 2 and
            not any(c.isdigit() for c in detalle) and
            "*" not in detalle
        )
        if is_transfer:
            self_names = ["facundo dardo lujan", "facundo lujan", "lujan facundo"]
            if detalle_key not in self_names:
                # Always ask for transfers — don't use memory
                extra_dudosos.append(m)
                continue

        ok.append(m)

    # For extra_dudosos: check memory one more time before asking
    final_dudosos = []
    for m in extra_dudosos:
        detalle_key = str(m.get("detalle","")).strip().lower()
        if detalle_key in memory:
            m["rubro"] = memory[detalle_key]
            ok.append(m)
            rows_to_save_later = [m.get("fecha", today), m.get("monto",""), memory[detalle_key], m.get("detalle",""), "UALA"]
            # Will be saved via ok path above - just add to rows_to_save
        else:
            final_dudosos.append(m)
    result["movimientos_dudosos"] = result.get("movimientos_dudosos", []) + final_dudosos

    # Check for duplicates before saving
    if ok:
        existing_rows = read_sheet("MOVIMIENTOS", "A:D")
        existing_keys = set()
        for row in existing_rows:
            if len(row) >= 4:
                # Key: fecha + detalle (normalized)
                fecha_norm = str(row[0]).strip()
                detalle_norm = str(row[3]).strip().lower()
                existing_keys.add(f"{fecha_norm}|{detalle_norm}")

        ok_new = []
        ok_dupes = []
        for m in ok:
            fecha_m = m.get("fecha", today)
            detalle_m = str(m.get("detalle", "")).strip().lower()
            key = f"{fecha_m}|{detalle_m}"
            if key in existing_keys:
                ok_dupes.append(m)
            else:
                ok_new.append(m)
                rows_to_save.append([fecha_m, m.get("monto",""), m.get("rubro",""), m.get("detalle",""), "UALA"])

        if ok_new:
            total_saved += len(ok_new)
            msg_parts.append(f"✅ {len(ok_new)} movimientos guardados")
        if ok_dupes:
            msg_parts.append(f"⏭️ {len(ok_dupes)} ya estaban anotados (omitidos)")
    

    # 2. Save rendimientos (check duplicates)
    rendimientos = result.get("rendimientos", [])
    if rendimientos:
        try:
            _ = existing_keys
        except NameError:
            existing_rows = read_sheet("MOVIMIENTOS", "A:D")
            existing_keys = set()
            for row in existing_rows:
                if len(row) >= 4:
                    existing_keys.add(f"{str(row[0]).strip()}|{str(row[3]).strip().lower()}")
        rend_new = []
        for r in rendimientos:
            fecha_r = r.get("fecha", today)
            detalle_r = r.get("detalle","Rendimiento UALA").lower()
            key = f"{fecha_r}|{detalle_r}"
            if key not in existing_keys:
                rend_new.append(r)
                rows_to_save.append([fecha_r, r.get("monto",""), "rendimientos", r.get("detalle","Rendimiento UALA"), "UALA"])
        if rend_new:
            total_saved += len(rend_new)
            msg_parts.append(f"💰 {len(rend_new)} rendimientos guardados")
        elif rendimientos:
            msg_parts.append(f"⏭️ Rendimientos ya anotados (omitidos)")

    # 3. Save devoluciones (check duplicates)
    devoluciones = result.get("devoluciones", [])
    if devoluciones:
        dev_new = []
        for d in devoluciones:
            if not is_duplicate(d.get("fecha", today), abs(float(d.get("monto",0))), d.get("detalle","Devolución")):
                dev_new.append(d)
                rows_to_save.append([d.get("fecha", today), abs(float(d.get("monto",0))), d.get("rubro","reintegro gastos"), d.get("detalle","Devolución"), "UALA"])
        if dev_new:
            total_saved += len(dev_new)
            msg_parts.append(f"↩️ {len(dev_new)} devoluciones guardadas")
        elif devoluciones:
            msg_parts.append(f"⏭️ Devoluciones ya anotadas (omitidas)")

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
        await update.message.reply_text("\n".join(msg_parts), parse_mode="HTML")
        return

    # All done
    if not msg_parts:
        msg_parts.append("✅ Sin movimientos nuevos en la captura")
    await update.message.reply_text("\n".join(msg_parts))
    # Save today as last processed date and reset reminder
    save_last_uala_date(today)
    save_uala_reminder_state("done", datetime.now(TZ).date())

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

VALID_RUBROS = {
    "almacen", "auto", "cafe", "cancha", "celular", "cochera", "combustible",
    "comida", "concierto", "credito", "cumpleaños", "dolares", "farmacia",
    "futbol", "gimnasio", "impuestos", "indumentaria", "inversion", "juegos",
    "kiosko", "nafta", "panaderia", "peluqueria", "prepaga", "regalos",
    "rendimientos", "salida", "salud", "seguro", "sueldo", "suscripciones",
    "taone", "transporte", "traspaso", "varios", "verduleria", "cena",
    "almuerzo", "tarjeta", "compras", "delivery", "supermercado", "gimnasio",
    "streaming", "servicios", "hobby", "mascota", "educacion", "viajes"
}

def extract_rubro(text: str) -> str:
    """Extract rubro from user response. Returns 'varios' if unclear."""
    words = text.strip().lower().split()
    if not words:
        return "varios"
    # Take first word
    candidate = words[0].rstrip(".,!?")
    # If it's in valid rubros, use it
    if candidate in VALID_RUBROS:
        return candidate
    # If it's a short word (likely a rubro), use it anyway
    if len(candidate) <= 15 and len(words) <= 2:
        return candidate
    # Long sentence = probably not a rubro answer
    return None  # signals it's not a valid rubro answer


def get_vencimientos() -> list:
    """Read upcoming vencimientos from MEMORIA_UALA sheet (col H:J)."""
    try:
        rows = read_sheet("MEMORIA_UALA", "H1:J50")
        vencimientos = []
        for row in rows:
            if len(row) >= 3 and str(row[0]).strip() == "VENCIMIENTO":
                try:
                    fecha_str = str(row[1]).strip()
                    desc = str(row[2]).strip()
                    if fecha_str:
                        fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
                        vencimientos.append({"fecha": fecha, "descripcion": desc})
                except:
                    pass
        return vencimientos
    except:
        return []


def save_vencimiento(fecha_date, descripcion: str):
    """Save a vencimiento reminder to Sheets."""
    try:
        rows = read_sheet("MEMORIA_UALA", "H1:J50")
        # Find next empty row in H column
        next_row = 1
        for i, row in enumerate(rows, 1):
            if row and str(row[0]).strip():
                next_row = i + 1
        service = get_sheets_service()
        service.spreadsheets().values().update(
            spreadsheetId=SHEETS_ID,
            range=f"MEMORIA_UALA!H{next_row}:J{next_row}",
            valueInputOption="RAW",
            body={"values": [["VENCIMIENTO", fecha_date.strftime("%Y-%m-%d"), descripcion]]}
        ).execute()
    except Exception as e:
        logger.error(f"Error saving vencimiento: {e}")


async def check_vencimientos_standalone(bot) -> None:
    """Check and send vencimiento reminders daily at 9:00."""
    if not CHAT_ID:
        return
    import datetime as dt_mod
    today = datetime.now(TZ).date()
    vencimientos = get_vencimientos()
    for v in vencimientos:
        days_until = (v["fecha"] - today).days
        if days_until in [0, 1, 3, 7]:  # remind 7, 3, 1 days before and on the day
            if days_until == 0:
                msg = f"🔴 HOY vence: {v['descripcion']}"
            elif days_until == 1:
                msg = f"🟡 Mañana vence: {v['descripcion']}"
            elif days_until == 3:
                msg = f"🟠 En 3 días vence: {v['descripcion']}"
            else:
                msg = f"📅 En {days_until} días vence: {v['descripcion']} ({v['fecha'].strftime('%d/%m')})"
            await bot.send_message(chat_id=int(CHAT_ID), text=msg)


def es_mensaje_nuevo(text: str) -> bool:
    """Detect if text is clearly a new financial message, not a rubro answer."""
    t = text.strip().lower()
    # Commands
    if t.startswith("/"):
        return True
    # Clearly new financial operations
    new_keywords = ["gasté", "gaste", "cobré", "cobre", "pagué", "pague",
                    "traspasé", "traspase", "compré", "compre", "anota",
                    "anotar", "invertí", "inverti", "rendimientos",
                    "cuanto", "cuánto", "como", "cómo", "mostrá", "mostra"]
    for kw in new_keywords:
        if kw in t:
            return True
    # Contains numbers with currency context
    import re
    if re.search(r'\d+.*(?:pesos?|peso|usd|dolar|k)', t):
        return True
    return False

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
            # Also save to TAONE sheet if rubro is taone
            taone_rows = []
            for r in rows_ok:
                if str(r[2]).lower() == "taone":
                    taone_rows.append([r[0], r[3], r[1], "", r[3]])  # fecha, concepto, monto, saldo, detalle
            if taone_rows:
                append_rows("TAONE", taone_rows)

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
            await update.message.reply_text(msg0, parse_mode="HTML")
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
        rows = [[f.get("fecha",""), f.get("monto",""), f.get("rubro","").lower(), f.get("detalle",""), str(f.get("cuenta","")).upper()] for f in data.get("filas", [])]
        if rows:
            append_rows("DOLARES", rows)
        mp = data.get("movimiento_pesos")
        if mp:
            append_rows("MOVIMIENTOS", [[mp.get("fecha",""), mp.get("monto",""), mp.get("rubro","").lower(), mp.get("detalle",""), str(mp.get("cuenta","")).upper()]])
        user_context.pop(chat_id, None)
        await update.message.reply_text(data.get("mensaje", "✅ Dólares guardado"))

    elif tipo == "inversion":
        rows = [[f.get("fecha",""), f.get("tipo_instr",""), f.get("ticker",""), f.get("detalle",""), f.get("cantidad",""), f.get("precio_compra","")] for f in data.get("filas", [])]
        if rows:
            append_rows("INVERSIONES", rows)
        mp = data.get("movimiento_pesos")
        if mp:
            append_rows("MOVIMIENTOS", [[mp.get("fecha",""), mp.get("monto",""), mp.get("rubro","").lower(), mp.get("detalle",""), str(mp.get("cuenta","")).upper()]])
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
                rows_ok.append([f.get("fecha",""), f.get("monto",""), f.get("rubro",""), f.get("detalle",""), cuenta_pago])
        if rows_ok:
            append_rows("MOVIMIENTOS", rows_ok)
        if dudosos:
            msg = f"💳 Guardé {len(rows_ok)} gastos.\n\n❓ No supe categorizar:\n\n"
            for i, d in enumerate(dudosos, 1):
                msg += f"{i}. {d.get('detalle','?')} — ${abs(float(d.get('monto',0))):,.0f}\n"
            msg += "\n¿Qué rubro les ponés? Ej: *1 comida, 2 suscripciones*"
            user_context[chat_id] = {"pendiente": "tarjeta_rubros", "dudosos": dudosos, "cuenta_pago": cuenta_pago}
            await update.message.reply_text(msg, parse_mode="HTML")
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

    elif tipo == "vencimiento":
        import datetime as dt_mod
        dia = int(data.get("dia", 1))
        mes_offset = int(data.get("mes_offset", 1))
        descripcion = data.get("descripcion", "Tarjeta")
        # Calculate the target date
        now = datetime.now(TZ)
        target_month = now.month + mes_offset
        target_year = now.year + (target_month - 1) // 12
        target_month = ((target_month - 1) % 12) + 1
        try:
            import calendar
            max_day = calendar.monthrange(target_year, target_month)[1]
            target_day = min(dia, max_day)
            target_date = dt_mod.date(target_year, target_month, target_day)
            save_vencimiento(target_date, descripcion)
            user_context.pop(chat_id, None)
            await update.message.reply_text(
                f"✅ Guardado! Te voy a recordar el {target_date.strftime('%d/%m/%Y')}: <b>{descripcion}</b>",
                parse_mode="HTML"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error guardando vencimiento: {str(e)}")

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
    info = "CARTERA (precios en USD):\n"
    total_invertido = 0
    total_actual = 0
    total_resultado = 0
    total_diario = 0

    for t in tickers_data:
        tk = t["ticker"]
        qty = t["cantidad"]
        compra = t["precio_compra"]
        p = prices.get(tk, {})
        hoy = p.get("hoy")
        ayer = p.get("ayer")

        invertido = qty * compra
        actual = qty * hoy if hoy else 0
        resultado = actual - invertido if hoy else 0
        diario = qty * (hoy - ayer) if hoy and ayer else 0

        total_invertido += invertido
        total_actual += actual
        total_resultado += resultado
        total_diario += diario

        info += (f"- {tk}: {qty} nom | compra U$D{compra:.2f} | "
                 f"hoy U$D{hoy if hoy else 'N/D'} | ayer U$D{ayer if ayer else 'N/D'} | "
                 f"resultado U$D{resultado:+.2f} | hoy U$D{diario:+.2f}\n")

    info += f"\nTOTALES: invertido U$D{total_invertido:,.2f} | actual U$D{total_actual:,.2f} | resultado U$D{total_resultado:+,.2f} | hoy U$D{total_diario:+,.2f}"
    answer = await call_claude(INVERSIONES_SYSTEM, f"Hoy es {today}.\n{info}\nMostrá un resumen claro con emojis de la cartera.")
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
            falta = original.get("falta", "")
            monto = original.get("monto", "")
            rubro_orig = original.get("rubro", "")
            cuenta_orig = original.get("cuenta", "")
            detalle_orig = original.get("detalle", "")
            prompt = (
                f"Hoy es {today}. El usuario registra un movimiento. "
                f"Ya sabemos: monto={monto}"
                + (f", rubro={rubro_orig}" if rubro_orig else "")
                + (f", cuenta={cuenta_orig}" if cuenta_orig else "")
                + (f", detalle={detalle_orig}" if detalle_orig else "")
                + f". Faltaba: {falta}. "
                f"El usuario aclaró: '{text}'. "
                f"Generá el JSON de movimiento completo. Si todavía falta algún dato clave, devolvé pedir_detalle con lo que falta."
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
            if es_mensaje_nuevo(text):
                user_context.pop(chat_id, None)
            elif text.strip().lower() in ["ya anotado", "ya esta", "ya está", "skip", "omitir", "existe", "ya existe", "no", "n"]:
                # User says it's already recorded — skip this item
                dudosos   = ctx.get("dudosos", [])
                idx       = ctx.get("idx", 0)
                next_idx  = idx + 1
                await update.message.reply_text("⏭️ Omitido")
                if next_idx < len(dudosos):
                    ctx["idx"] = next_idx
                    d2 = dudosos[next_idx]
                    monto2 = abs(float(d2.get("monto", 0)))
                    detalle2 = str(d2.get("detalle", "?")).strip()
                    await update.message.reply_text(
                        f"❓ {next_idx+1}/{len(dudosos)}: <b>{detalle2}</b> — ${monto2:,.0f}\n¿Qué rubro es? (o 'ya anotado' para omitir)",
                        parse_mode="HTML"
                    )
                else:
                    user_context.pop(chat_id, None)
                    save_uala_reminder_state("done", datetime.now(TZ).date())
                    await update.message.reply_text("✅ ¡UALA procesado!")
                return
            else:
              dudosos       = ctx.get("dudosos", [])
            idx           = ctx.get("idx", 0)
            nuevas_memorias = ctx.get("nuevas_memorias", [])
            d             = dudosos[idx]
            rubro         = text.strip().lower().split()[0]
            fecha         = d.get("fecha", today)
            monto         = d.get("monto", 0)
            detalle       = str(d.get("detalle", "")).strip()

            # Save movement with assigned rubro
            append_rows("MOVIMIENTOS", [[fecha, monto, rubro, detalle, "UALA"]])
            # Also save to TAONE if rubro is taone
            if rubro == "taone":
                append_rows("TAONE", [[fecha, detalle, monto, "", detalle]])

            # Save to memory — but NOT for person names (transfers vary by purpose)
            import re as _re
            is_transfer_name = bool(_re.match(r'^[A-Za-záéíóúñÁÉÍÓÚÑ,. ]+$', detalle)) and len(detalle.split()) >= 2 and "*" not in detalle
            if detalle and rubro not in ["traspaso", "sueldo", "rendimientos", "inversion", "dolares"] and not is_transfer_name:
                nuevas_memorias.append({"comercio": detalle, "rubro": rubro})
                save_memory(detalle, rubro)
                await update.message.reply_text(
                    f"✅ Guardado como <b>{rubro}</b> 🧠 La próxima vez que aparezca '{detalle}' lo categorizo solo.",
                    parse_mode="HTML"
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
                    f"❓ {next_idx+1}/{len(dudosos)}: <b>{detalle2}</b> — ${monto2:,.0f}\n¿Qué rubro es? (o escribí <i>ya anotado</i> para omitir)",
                    parse_mode="HTML"
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
                [fecha, -abs(monto), "traspaso", f"Traspaso a {cuenta_destino.upper()}", "UALA"],
                [fecha,  abs(monto), "traspaso", f"Traspaso desde UALA", cuenta_destino.upper()],
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
                    f"❓ 1/{len(dudosos)}: <b>{detalle_d}</b> — ${monto_d:,.0f}\n¿Qué rubro es?",
                    parse_mode="HTML"
                )
                return

            user_context.pop(chat_id, None)
            save_last_uala_date(today)
            save_uala_reminder_state("done", datetime.now(TZ).date())
            await update.message.reply_text("✅ ¡UALA procesado!")
            return

        elif pendiente == "rubro_desconocido":
            if es_mensaje_nuevo(text):
                user_context.pop(chat_id, None)
                # Fall through to normal processing below
            else:
              filas_pendientes = ctx.get("filas_pendientes", [])
            idx = ctx.get("idx", 0)
            f = filas_pendientes[idx]
            rubro = text.strip().lower().split()[0]
            detalle = str(f.get("detalle", "")).strip()
            fecha = f.get("fecha", today)
            monto = f.get("monto", 0)
            cuenta = f.get("cuenta", "")

            # Save movement with new rubro
            append_rows("MOVIMIENTOS", [[fecha, monto, rubro, detalle, str(cuenta).upper()]])

            # Save to memory
            if detalle and rubro not in ["traspaso", "sueldo", "rendimientos", "inversion", "dolares"]:
                save_memory(detalle, rubro)
                await update.message.reply_text(
                    f"✅ Guardado como <b>{rubro}</b> 🧠 Memoricé que '{detalle}' = {rubro}",
                    parse_mode="HTML"
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
            await update.message.reply_text(msg0, parse_mode="HTML")
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
        await update.message.reply_text(f"📝 <i>{transcription}</i>", parse_mode="HTML")
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

        # If PDF — extract text and process as tarjeta directly
        if mime == "application/pdf":
            await update.message.reply_text("📄 Leyendo PDF...")
            pdf_text = extract_pdf_text(content_bytes)
            if not pdf_text.strip():
                await update.message.reply_text("❌ No pude leer el PDF. Intentá mandarlo como imagen.")
                return
            logger.info(f"PDF text extracted: {len(pdf_text)} chars")
            # Send text to Claude for tarjeta processing
            raw = await call_claude(
                SYSTEM_PROMPT,
                f"Hoy es {today}. FECHA_PAGO={today} (usá esta fecha para TODAS las filas, no las fechas del extracto). "
                f"El siguiente es el texto extraído de un extracto/resumen de tarjeta de crédito. "
                f"Procesalo y devolvé el JSON tipo tarjeta.\n\nTEXTO DEL PDF:\n{pdf_text[:6000]}"
            )
            raw = raw.strip().replace("```json","").replace("```","").strip()
            data = json.loads(raw)
            await handle_result(data, update, context)
            return

        # If it's a photo and we're in UALA context, process as UALA
        if update.message.photo or is_uala_context:
            caption = update.message.caption or ""
            if "uala" in caption.lower() or is_uala_context:
                await handle_uala_screenshot(content_bytes, mime, update, context)
                return
            elif "tarjeta" in caption.lower() or "tc" in caption.lower() or "extracto" in caption.lower() or "resumen" in caption.lower():
                pass  # fall through to tarjeta processing
            else:
                user_context[chat_id] = {"pendiente": "tipo_imagen", "bytes": content_bytes, "mime": mime}
                await update.message.reply_text(
                    "📸 ¿Qué es esta imagen?\n\nRespondé <b>uala</b> o <b>tarjeta</b>",
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
                        {"type": "text", "text": f"Hoy es {today}. FECHA_PAGO={today} (usá esta fecha para TODAS las filas sin excepción). Este es un resumen de tarjeta de crédito. Extraé todos los gastos y devolvé JSON tipo tarjeta. Para los gastos cuyo rubro no queda claro, marcalos como tipo consulta_rubro."},
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


# Track which heartbeat jobs already fired today (in-memory, resets on restart)
heartbeat_fired: dict = {}

async def heartbeat_check(bot) -> None:
    """Every 5 min: check if any scheduled job should have fired but didn't.
    Each job fires AT MOST ONCE per day via this heartbeat."""
    now = datetime.now(TZ)
    hour, minute = now.hour, now.minute
    today = now.date()

    # UALA noche: fire once between 21:00-21:10 if not already done today
    if hour == 21 and minute <= 10:
        key = f"uala_noche_{today}"
        if key not in heartbeat_fired:
            heartbeat_fired[key] = True
            await uala_reminder_noche_standalone(bot)

    # UALA mañana: fire once between 9:00-9:10
    if hour == 9 and minute <= 10:
        key = f"uala_manana_{today}"
        if key not in heartbeat_fired:
            heartbeat_fired[key] = True
            await uala_reminder_manana_standalone(bot)

    # Vencimientos: fire once between 9:15-9:25
    if hour == 9 and 15 <= minute <= 25:
        key = f"vencimientos_{today}"
        if key not in heartbeat_fired:
            heartbeat_fired[key] = True
            await check_vencimientos_standalone(bot)


async def uala_reminder_noche_standalone(bot) -> None:
    """Send UALA screenshot reminder at 22:30."""
    if not CHAT_ID:
        return
    today = datetime.now(TZ).date()
    # Read persisted state
    state = get_uala_reminder_state()
    if state.get("date") == today and state.get("state") in ["done", "waiting_morning"]:
        return  # already processed or already reminded this morning
    if state.get("date") == today and state.get("state") == "waiting_tonight":
        return  # already sent tonight reminder
    # Send reminder
    save_uala_reminder_state("waiting_tonight", today)
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
    import datetime as dt_module
    state = get_uala_reminder_state()
    today = datetime.now(TZ).date()
    yesterday = today - dt_module.timedelta(days=1)
    current_state = state.get("state", "")
    state_date = state.get("date")
    logger.info(f"Morning reminder check: state={current_state}, date={state_date}, yesterday={yesterday}")
    # Send if: waiting_tonight from yesterday OR waiting_morning from yesterday (retry)
    if current_state in ["waiting_tonight", "waiting_morning"] and state_date == yesterday:
        save_uala_reminder_state("waiting_morning", yesterday)
        await bot.send_message(
            chat_id=int(CHAT_ID),
            text=f"☀️ Buenos días! Todavía no registraste los movimientos de UALA del {yesterday.strftime('%d/%m')}.\n\nMandame la captura cuando puedas."
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
    save_uala_reminder_state("waiting_tonight", datetime.now(TZ).date())
    last = await get_last_uala_date()
    msg = "📱 Mandame la captura de pantalla de tus movimientos de UALA."
    if last:
        msg += f"\n\n_(Solo procesaré movimientos posteriores al {last})_"
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_resetuala(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset UALA last processed date (to reprocess everything)."""
    save_last_uala_date("")
    await update.message.reply_text("🔄 Fecha UALA reseteada. La próxima captura procesará todos los movimientos visibles.")

async def cmd_movimientos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show last movements from MOVIMIENTOS sheet."""
    await update.message.reply_text("📋 Buscando últimos movimientos...")
    try:
        rows = read_sheet("MOVIMIENTOS", "A:E")
        if not rows:
            await update.message.reply_text("No hay movimientos registrados.")
            return

        # Get last 15 rows with data
        data_rows = [r for r in rows if len(r) >= 2 and any(str(c).strip() for c in r)]
        last_rows = data_rows[-15:]

        summary = "\n".join([" | ".join(str(c) for c in r) for r in last_rows])
        today = datetime.now(TZ).strftime("%d/%m/%Y")

        answer = await call_claude(
            "Sos BOTSTERO. Recibís los últimos movimientos de una planilla de finanzas en formato: fecha|monto|rubro|detalle|cuenta. "
            "Mostrá cada movimiento en una línea clara con emoji según el tipo (💸 gasto, ✅ ingreso, 🔄 traspaso). "
            "Al final mostrá el total del período. Texto plano sin markdown.",
            f"Hoy es {today}. Últimos movimientos:\nfecha|monto|rubro|detalle|cuenta\n{summary}"
        )
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error(f"Movimientos error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def cmd_vencimientos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show upcoming vencimientos."""
    import datetime as dt_mod
    today = datetime.now(TZ).date()
    vencimientos = get_vencimientos()
    if not vencimientos:
        await update.message.reply_text("📅 No tenés vencimientos guardados.\n\nPodés agregar uno diciéndome: 'Me vence la Visa Galicia el 8 del mes que viene'")
        return
    venc_futuros = sorted([v for v in vencimientos if v["fecha"] >= today], key=lambda x: x["fecha"])
    if not venc_futuros:
        await update.message.reply_text("📅 No tenés vencimientos próximos.")
        return
    msg = "📅 <b>Próximos vencimientos:</b>\n\n"
    for v in venc_futuros[:10]:
        days = (v["fecha"] - today).days
        if days == 0:
            msg += f"🔴 HOY — {v['descripcion']}\n"
        elif days == 1:
            msg += f"🟡 Mañana — {v['descripcion']}\n"
        else:
            msg += f"📅 {v['fecha'].strftime('%d/%m/%Y')} ({days} días) — {v['descripcion']}\n"
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_tenencias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current balances from TENENCIA sheet."""
    await update.message.reply_text("💰 Consultando tus saldos...")
    try:
        # Read the full TENENCIA sheet relevant section
        # Read only the saldos section (rows 14-22 based on sheet layout)
        rows_pesos  = read_sheet("TENENCIA", "A14:B22")   # Efectivo, UALA, Galicia, Nacion, etc + TOTAL
        rows_usd    = read_sheet("TENENCIA", "E14:F22")   # USD disponible, invertido, TC, equiv pesos
        rows_inv    = read_sheet("TENENCIA", "H14:I22")   # Inversiones

        # Build a text summary to send to Claude for clean formatting
        def rows_to_text(rows):
            return "\n".join([" | ".join(str(c) for c in r) for r in rows if any(str(c).strip() for c in r)])

        pesos_txt = "\n".join([" | ".join(str(c) for c in r) for r in rows_pesos if any(str(c).strip() for c in r)])
        usd_txt = "\n".join([" | ".join(str(c) for c in r) for r in rows_usd if any(str(c).strip() for c in r)])
        inv_txt = "\n".join([" | ".join(str(c) for c in r) for r in rows_inv if any(str(c).strip() for c in r)])
        summary = "SALDOS EN PESOS:\n" + pesos_txt + "\n\nDOLARES:\n" + usd_txt + "\n\nINVERSIONES:\n" + inv_txt
        answer = await call_claude(
            "Sos BOTSTERO. Recibís datos de la sección SALDOS de una planilla. "
            "Mostrá SOLO los saldos actuales: cada cuenta en pesos con su monto, total pesos, USD disponible con TC y equivalente en pesos, inversiones si hay, y patrimonio total. "
            "NO menciones movimientos ni resumen mensual. Usá emojis. Texto plano sin markdown. Montos con formato $X.XXX",
            "Datos de la planilla TENENCIA:\n" + summary
        )
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error(f"Tenencias error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")



async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 *Hola, soy BOTSTERO!*\n\n"
        f"Tu chat ID: `{update.effective_chat.id}`\n"
        f"Cargalo en Railway como `CHAT_ID` para recordatorios.\n\n"
        f"📌 Comandos:\n"
        f"/cartera — ver cómo van tus inversiones\n"
        f"/resumen — resumen del mes\n"
        f"/uala — procesar captura de UALA\n"
        f"/tenencias — ver saldos por cuenta\n"
        f"/movimientos — ver últimos movimientos\n"
        f"/vencimientos — ver próximos vencimientos\n\n"
        f"¡Mandame texto o audio!",
        parse_mode="HTML"
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
async def post_init(app) -> None:
    """Initialize scheduled jobs after app starts."""
    from datetime import time as dtime
    jq = app.job_queue

    # Daily reminders
    jq.run_daily(lambda ctx: reminder_job_standalone(ctx.bot), time=dtime(18, 0, tzinfo=TZ), name="reminder_18")
    jq.run_daily(lambda ctx: reminder_job_standalone(ctx.bot), time=dtime(23, 0, tzinfo=TZ), name="reminder_23")
    jq.run_daily(lambda ctx: uala_reminder_noche_standalone(ctx.bot), time=dtime(21, 0, tzinfo=TZ), name="uala_noche")
    jq.run_daily(lambda ctx: uala_reminder_manana_standalone(ctx.bot), time=dtime(9, 0, tzinfo=TZ), name="uala_manana")
    jq.run_daily(lambda ctx: check_vencimientos_standalone(ctx.bot), time=dtime(9, 15, tzinfo=TZ), name="vencimientos")

    # Heartbeat every 5 min — catches cases where Railway restarts mid-day
    jq.run_repeating(lambda ctx: heartbeat_check(ctx.bot), interval=300, first=60, name="heartbeat")

    logger.info("✅ Jobs scheduled via PTB job_queue")


def main():
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .post_init(post_init)
           .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cartera", cmd_cartera))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("uala", cmd_uala))
    app.add_handler(CommandHandler("tenencias", cmd_tenencias))
    app.add_handler(CommandHandler("movimientos", cmd_movimientos))
    app.add_handler(CommandHandler("vencimientos", cmd_vencimientos))
    app.add_handler(CommandHandler("resetuala", cmd_resetuala))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_document))

    logger.info("🤖 BOTSTERO iniciado ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
