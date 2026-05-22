import os
import json
import logging
import tempfile
import httpx
import base64
import re
from datetime import datetime, time as dtime
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
TZ             = ZoneInfo("America/Argentina/Buenos_Aires")
MODEL          = "claude-haiku-4-5-20251001"

# ── State ────────────────────────────────────────────────────────────────────
uala_reminder_state: dict = {"state": None, "date": None}
user_context: dict = {}
last_message_date: dict = {"date": None}
heartbeat_fired: dict = {}

# ── Phrases ──────────────────────────────────────────────────────────────────
YA_ANOTADO_PHRASES = {
    "ya anotado", "ya esta", "ya está", "ya lo anote", "ya lo anoté",
    "ya registrado", "ya existe", "skip", "omitir", "saltear",
    "ya lo tengo", "ya lo tenía", "ya lo tenia"
}
RENDIMIENTO_KEYWORDS = ["rendimiento", "rendimientos", "interes", "interés", "intereses", "renta"]
RUBRO_KEYWORDS = ["comida", "cafe", "cancha", "gimnasio", "sueldo", "ingreso", "varios",
                  "taone", "transporte", "salud", "farmacia", "delivery", "supermercado",
                  "indumentaria", "basquet", "futbol", "credito"]

# ── Prompts ──────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Sos BOTSTERO, un asistente financiero personal con onda. Procesá el mensaje y devolvé SOLO JSON válido, sin texto ni markdown.

CUENTAS (siempre en mayúsculas en el JSON): EFECTIVO, UALA, GALICIA, BALANZ, BINANCE, NACION
RUBROS: almacen, auto, cafe, cancha, celular, cochera, combustible, comida, concierto, credito, cumpleaños, dolares, farmacia, futbol, gimnasio, impuestos, indumentaria, inversion, juegos, kiosko, nafta, panaderia, peluqueria, prepaga, regalos, rendimientos, salida, salud, seguro, sueldo, suscripciones, taone, transporte, traspaso, varios, verduleria, cena, almuerzo, tarjeta, compras, delivery, supermercado, basquet

TIPOS:
1) GASTO/INGRESO completo (rubro Y cuenta presentes):
{"tipo":"movimiento","filas":[{"fecha":"DD/MM/YYYY","monto":-5000,"rubro":"cochera","detalle":"Cochera","cuenta":"UALA"}],"mensaje":"✅ -$5.000 Cochera (UALA)"}

2) GASTO INCOMPLETO — falta rubro o cuenta. NUNCA asumas 'varios' o 'efectivo' si no se dijo:
{"tipo":"pedir_detalle","monto":-1000,"detalle":"gasto","falta":"rubro_y_cuenta","mensaje":"💬 ¿En qué gastaste los $1.000 y con qué pagaste?"}
Si solo falta la cuenta: {"tipo":"pedir_detalle","monto":-1000,"rubro":"cafe","detalle":"Cafe","falta":"cuenta","mensaje":"💬 ¿Con qué pagaste el café?"}
Si solo falta el rubro: {"tipo":"pedir_detalle","monto":-1000,"cuenta":"UALA","detalle":"gasto","falta":"rubro","mensaje":"💬 ¿En qué gastaste los $1.000?"}

3) TRASPASO (SIEMPRE dos filas):
{"tipo":"movimiento","filas":[{"fecha":"DD/MM/YYYY","monto":-20000,"rubro":"traspaso","detalle":"Traspaso a UALA","cuenta":"EFECTIVO"},{"fecha":"DD/MM/YYYY","monto":20000,"rubro":"traspaso","detalle":"Traspaso desde Efectivo","cuenta":"UALA"}],"mensaje":"✅ Traspaso $20.000 Efectivo → UALA"}

4) COMPRA USD:
{"tipo":"dolares","filas":[{"fecha":"DD/MM/YYYY","monto":100,"rubro":"compra","detalle":"Compra USD a $1.400","cuenta":"dolares"}],"movimiento_pesos":{"fecha":"DD/MM/YYYY","monto":-140000,"rubro":"dolares","detalle":"Compra 100 USD","cuenta":"UALA"},"mensaje":"✅ +100 USD | -$140.000"}

5) GASTO USD:
{"tipo":"dolares","filas":[{"fecha":"DD/MM/YYYY","monto":-15,"rubro":"gasto","detalle":"Netflix","cuenta":"dolares"}],"mensaje":"✅ -U$D 15 Netflix"}

6) INVERSIÓN con todos los datos:
{"tipo":"inversion","instrumento":"CEDEAR","filas":[{"fecha":"DD/MM/YYYY","tipo_instr":"CEDEAR","ticker":"SPY","detalle":"CEDEAR S&P 500","cantidad":10,"precio_compra":85000}],"movimiento_pesos":{"fecha":"DD/MM/YYYY","monto":-850000,"rubro":"inversion","detalle":"Compra 10 SPY","cuenta":"BALANZ"},"mensaje":"✅ 10 SPY a $85.000 = $850.000"}

7) INVERSIÓN sin datos:
{"tipo":"pedir_inversion","instrumento":"CEDEAR","datos_presentes":{"ticker":"SPY"},"falta":["cantidad","precio_compra"],"mensaje":"📌 SPY — ¿Cuántos nominales y a qué precio?"}

8) CONSULTA:
{"tipo":"consulta","mensaje":"🔍 Buscando..."}

9) CONSULTA DE INVERSIONES:
{"tipo":"consulta_inversiones","mensaje":"📈 Revisando tu cartera..."}

10) TAONE (gorras, pilusos, matriz, Sr Juan):
{"tipo":"taone","filas":[{"fecha":"DD/MM/YYYY","concepto":"GORRAS x30","monto":-180000,"detalle":"30 gorras"}],"mensaje":"✅ TAONE -$180.000"}

11) TARJETA/EXTRACTO:
{"tipo":"tarjeta","banco":"GALICIA","marca":"VISA","cuenta_pago":"GALICIA","total_extracto":184257.50,"filas":[{"fecha":"DD/MM/YYYY","monto":-5000,"tipo":"consulta_rubro","detalle":"Nombre del comercio"}],"mensaje":"💳 Procesando..."}
- total_extracto: "TOTAL CONSUMOS DEL MES"
- TODOS los consumos van como tipo consulta_rubro (el sistema consulta la memoria)
- FECHA_PAGO se te indica — usá esa fecha para TODAS las filas
- Ignorá: SALDO ANTERIOR, SU PAGO, SU PAGO EN PESOS, SALDO PENDIENTE, SUBTOTAL, TOTAL A PAGAR
- IMPUESTOS Y PERCEPCIONES → una sola fila con rubro="impuestos", sumá todos juntos

12) VENCIMIENTO DE TARJETA:
{"tipo":"vencimiento","dia":8,"mes_offset":1,"descripcion":"Tarjeta Visa Galicia","mensaje":"✅ Te recuerdo el 8 del mes que viene"}

REGLAS CRÍTICAS:
1. SOLO JSON válido, sin texto ni markdown.
2. Fecha DD/MM/YYYY. Sin fecha → hoy.
3. Montos sin formato. k=miles. Gastos=-. Ingresos=+.
4. Cuenta siempre en MAYÚSCULAS.
5. Si dicen solo monto sin rubro ni cuenta → pedir_detalle.
6. Traspaso → siempre dos filas.
7. Inversión sin datos completos → pedir_inversion.
8. 'como me fue hoy', 'cartera', 'inversiones' → consulta_inversiones."""

UALA_SYSTEM = """Sos BOTSTERO analizando una captura de pantalla de movimientos de UALA.
Devolvé SOLO JSON válido, sin texto ni markdown.

FORMATO DE PANTALLA DE UALA:
- Cada fila tiene: NOMBRE / SUBTÍTULO / MONTO / FECHA
- MONTO en verde con "+" = ingreso
- MONTO en negro sin "+" = gasto o transferencia enviada
- FECHA formato DD/MM (sin año)

TIPOS DE MOVIMIENTOS:
- "Rendimientos / Operación exitosa" → rendimientos[], monto POSITIVO
- "Payu*ar*uber..." / "Transporte" → movimientos_ok, rubro=transporte, monto NEGATIVO
- "Dlo*pedidosya..." / "Restaurantes y bares" → movimientos_ok, rubro=comida, monto NEGATIVO
- "[Nombre persona]" / "Transferencia enviada" → SIEMPRE movimientos_dudosos, monto NEGATIVO
- "Facundo Dardo Lujan" o "Lujan Facundo Dardo" o "Lujan,facundo Dardo" / "Transferencia enviada" → transferencias_propias, monto NEGATIVO
- "Facundo Dardo Lujan" o "Lujan Facundo Dardo" / "Transferencia recibida" → SI monto < 2000 → rendimientos[]. SI monto grande → transferencias_propias, monto POSITIVO
- "[Nombre persona]" / "Transferencia recibida" → movimientos_ok, monto POSITIVO, rubro="ingreso"
- "[Comercio]" / "Devolucion en cuenta" → devoluciones[], monto POSITIVO
- "[Comercio]" / "Supermercado" → movimientos_ok, rubro=supermercado, monto NEGATIVO
- "[Comercio]" / "Servicios" → movimientos_dudosos

Formato de respuesta:
{
  "movimientos_ok": [{"fecha":"DD/MM/YYYY","monto":-1500,"rubro":"comida","detalle":"Nombre comercio","cuenta":"UALA","subtitulo":"Restaurantes y bares"}],
  "movimientos_dudosos": [{"fecha":"DD/MM/YYYY","monto":-800,"detalle":"Nombre exacto","cuenta":"UALA","subtitulo":"Transferencia enviada"}],
  "transferencias_propias": [{"fecha":"DD/MM/YYYY","monto":-30000,"detalle":"Facundo Dardo Lujan"}],
  "rendimientos": [{"fecha":"DD/MM/YYYY","monto":630,"detalle":"Rendimiento diario UALA"}],
  "devoluciones": [{"fecha":"DD/MM/YYYY","monto":13906,"detalle":"Devolucion Payu*ar*uber","rubro":"transporte"}]
}

REGLAS CRÍTICAS:
1. Solo incluí movimientos con fecha >= FECHA_DESDE (se te va a indicar)
2. movimientos_ok: solo si tenés certeza del rubro por el subtítulo
3. movimientos_dudosos: transferencias a personas (SIEMPRE), comercios desconocidos
4. Transferencias a personas → NUNCA a movimientos_ok, SIEMPRE a movimientos_dudosos
5. NUNCA adivines el rubro si no estás seguro → dudosos
6. rendimientos: SOLO "Rendimientos / Operación exitosa"
7. devoluciones: subtítulo "Devolucion en cuenta"
8. ANTI-DUPLICADOS: NO incluyas movimientos con fecha < FECHA_DESDE"""

CONSULTA_SYSTEM = """Sos BOTSTERO, asistente financiero con onda. Tenés los movimientos del usuario en CSV.
Respondé en español rioplatense, con emojis, claro y conciso. Mostrá siempre totales.
Sin markdown, solo texto plano con emojis."""

INVERSIONES_SYSTEM = """Sos BOTSTERO, asistente financiero. Tenés la cartera del usuario y precios del mercado.
Calculá y mostrá para cada ticker: precio hoy vs ayer, resultado diario en $ y %, resultado total.
Al final mostrá totales. Respondé en español rioplatense con emojis. Texto plano."""

# ── Date utils ───────────────────────────────────────────────────────────────
def normalize_date(val):
    if not val:
        return val
    s = str(val).strip()
    if len(s) == 10 and s[2] == "/" and s[5] == "/":
        return s
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        parts = s.split("-")
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    if len(s) == 5 and s[2] == "/":
        year = datetime.now(TZ).year
        return f"{s}/{year}"
    return val

def norm_monto(m):
    try:
        return str(abs(round(float(str(m).replace("$","").replace(",",".").replace(" ","").strip()), 2)))
    except:
        return str(m)

# ── Claude API ───────────────────────────────────────────────────────────────
async def call_claude(system: str, user_message: str, max_tokens: int = 1024) -> str:
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": max_tokens, "system": system,
                  "messages": [{"role": "user", "content": user_message}]},
        )
        if resp.status_code != 200:
            logger.error(f"Anthropic {resp.status_code}: {resp.text}")
            raise Exception(f"Error Anthropic {resp.status_code}: {resp.text[:200]}")
        return resp.json()["content"][0]["text"]

async def transcribe_audio(audio_bytes: bytes) -> str:
    openai_key = os.environ.get("OPENAI_KEY", "")
    if not openai_key:
        raise Exception("Para audios necesitás agregar OPENAI_KEY en Railway (platform.openai.com — gratis hasta cierto uso)")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {openai_key}"},
            files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
            data={"model": "whisper-1", "language": "es"},
        )
        resp.raise_for_status()
        return resp.json()["text"]

def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        import pdfplumber, io
        parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    parts.append(t)
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return ""

# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_sheets_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa_info = json.loads(GOOGLE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds)

def append_rows(sheet_name: str, rows: list):
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
        spreadsheetId=SHEETS_ID, range=f"{sheet_name}!A1",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": normalized}
    ).execute()

def read_sheet(sheet_name: str, range_: str = "A:F") -> list:
    service = get_sheets_service()
    result = service.spreadsheets().values().get(spreadsheetId=SHEETS_ID, range=f"{sheet_name}!{range_}").execute()
    return result.get("values", [])

# ── Memory ────────────────────────────────────────────────────────────────────
def get_memory() -> dict:
    try:
        rows = read_sheet("MEMORIA_UALA", "A:B")
        memory = {}
        for row in rows[1:]:
            if len(row) >= 2:
                k = str(row[0]).strip().lower()
                v = str(row[1]).strip().lower()
                if k:
                    memory[k] = v
        return memory
    except:
        return {}

def save_memory(comercio: str, rubro: str):
    try:
        rows = read_sheet("MEMORIA_UALA", "A:B")
        if not rows:
            append_rows("MEMORIA_UALA", [["COMERCIO", "RUBRO"]])
        append_rows("MEMORIA_UALA", [[comercio.strip(), rubro.strip().lower()]])
    except Exception as e:
        logger.error(f"Error saving memory: {e}")

def get_last_uala_date() -> str:
    try:
        rows = read_sheet("MEMORIA_UALA", "D1:D2")
        if len(rows) >= 2:
            val = str(rows[1][0]).strip() if rows[1] else ""
            if val and len(val) == 10 and val[2] == "/" and val[5] == "/":
                return val
    except:
        pass
    return ""

def save_last_uala_date(fecha: str):
    try:
        service = get_sheets_service()
        service.spreadsheets().values().update(
            spreadsheetId=SHEETS_ID, range="MEMORIA_UALA!D1:D2",
            valueInputOption="RAW", body={"values": [["ULTIMA_FECHA_UALA"], [fecha]]}
        ).execute()
    except Exception as e:
        logger.error(f"Error saving last UALA date: {e}")

def get_uala_reminder_state() -> dict:
    try:
        rows = read_sheet("MEMORIA_UALA", "E1:F3")
        state = None; date_str = None
        for row in rows:
            if len(row) >= 2:
                if str(row[0]).strip() == "REMINDER_STATE":
                    state = str(row[1]).strip() or None
                elif str(row[0]).strip() == "REMINDER_DATE":
                    date_str = str(row[1]).strip() or None
        result = {"state": state, "date": None}
        if date_str:
            try:
                import datetime as dt_mod
                result["date"] = datetime.strptime(date_str, "%Y-%m-%d").date()
            except:
                pass
        return result
    except:
        return {"state": None, "date": None}

def save_uala_reminder_state(state: str, date_val=None):
    try:
        service = get_sheets_service()
        date_str = date_val.strftime("%Y-%m-%d") if date_val else ""
        service.spreadsheets().values().update(
            spreadsheetId=SHEETS_ID, range="MEMORIA_UALA!E1:F2",
            valueInputOption="RAW",
            body={"values": [["REMINDER_STATE", state or ""], ["REMINDER_DATE", date_str]]}
        ).execute()
        uala_reminder_state["state"] = state
        uala_reminder_state["date"] = date_val
    except Exception as e:
        logger.error(f"Error saving reminder state: {e}")
        uala_reminder_state["state"] = state
        uala_reminder_state["date"] = date_val

def get_vencimientos() -> list:
    try:
        rows = read_sheet("MEMORIA_UALA", "H1:J50")
        result = []
        for row in rows:
            if len(row) >= 3 and str(row[0]).strip() == "VENCIMIENTO":
                try:
                    fecha = datetime.strptime(str(row[1]).strip(), "%Y-%m-%d").date()
                    result.append({"fecha": fecha, "descripcion": str(row[2]).strip()})
                except:
                    pass
        return result
    except:
        return []

def save_vencimiento(fecha_date, descripcion: str):
    try:
        rows = read_sheet("MEMORIA_UALA", "H1:J50")
        next_row = max(1, len(rows) + 1)
        service = get_sheets_service()
        service.spreadsheets().values().update(
            spreadsheetId=SHEETS_ID, range=f"MEMORIA_UALA!H{next_row}:J{next_row}",
            valueInputOption="RAW",
            body={"values": [["VENCIMIENTO", fecha_date.strftime("%Y-%m-%d"), descripcion]]}
        ).execute()
    except Exception as e:
        logger.error(f"Error saving vencimiento: {e}")

def get_tickers_from_sheet() -> list:
    rows = read_sheet("INVERSIONES", "A:F")
    tickers = {}
    for row in rows[2:]:
        if len(row) < 3:
            continue
        ticker = str(row[2]).strip().upper()
        if not ticker or ticker in ["TICKER /", "TICKER", ""]:
            continue
        try:
            qty   = float(str(row[4]).replace(",",".").replace("$","").replace(" ","")) if len(row) > 4 else 0
            price = float(str(row[5]).replace(",",".").replace("$","").replace(" ","")) if len(row) > 5 else 0
        except:
            qty, price = 0, 0
        if qty == 0:
            continue
        if ticker in tickers:
            old_qty = tickers[ticker]["cantidad"]
            tickers[ticker]["precio_compra"] = (tickers[ticker]["precio_compra"] * old_qty + price * qty) / (old_qty + qty)
            tickers[ticker]["cantidad"] += qty
        else:
            tickers[ticker] = {"cantidad": qty, "precio_compra": price}
    return [{"ticker": k, **v} for k, v in tickers.items()]

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

# ── UALA Processing ───────────────────────────────────────────────────────────
async def process_uala_screenshot(image_bytes: bytes, mime: str, today: str, desde_fecha: str = "") -> dict:
    memory = get_memory()
    memory_str = json.dumps(memory, ensure_ascii=False) if memory else "{}"
    b64 = base64.standard_b64encode(image_bytes).decode()
    user_msg = (
        f"Hoy es {today}. "
        + (f"IMPORTANTE: Solo procesá movimientos con fecha >= {desde_fecha}. Ignorá todo lo anterior." if desde_fecha else "Procesá todos los movimientos visibles.")
        + f" Memoria de comercios conocidos: {memory_str}\n\nAnalizá esta captura de UALA."
    )
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 2048, "system": UALA_SYSTEM,
                  "messages": [{"role": "user", "content": [
                      {"type": "text", "text": user_msg},
                      {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
                  ]}]}
        )
    raw = resp.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)

async def handle_uala_screenshot(image_bytes: bytes, mime: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    desde_fecha = get_last_uala_date()
    logger.info(f"Processing UALA from: {desde_fecha or 'ALL'}")

    try:
        result = await process_uala_screenshot(image_bytes, mime, today, desde_fecha)
    except Exception as e:
        await update.message.reply_text(f"❌ Error procesando la imagen: {str(e)}")
        return

    rows_to_save = []
    msg_parts = []
    total_saved = 0

    # ── Build dedup index ──────────────────────────────────────────────────
    existing_rows = read_sheet("MOVIMIENTOS", "A:E")
    ex_keys = set()
    for row in existing_rows[1:]:
        if len(row) < 2:
            continue
        f_e = normalize_date(str(row[0]).strip())
        if not f_e:
            continue
        m_norm = norm_monto(row[1])
        d_e = str(row[3]).strip().lower() if len(row) > 3 else ""
        ex_keys.add(f"{f_e}|{m_norm}|{d_e}")
        ex_keys.add(f"{f_e}|{m_norm}")
        if d_e:
            ex_keys.add(f"{f_e}|{d_e}")

    def is_dup(fecha, monto, detalle=""):
        fn = normalize_date(str(fecha).strip())
        mn = norm_monto(monto)
        dn = str(detalle).strip().lower()
        if f"{fn}|{mn}|{dn}" in ex_keys: return True
        if f"{fn}|{mn}" in ex_keys: return True
        if dn and f"{fn}|{dn}" in ex_keys: return True
        return False

    def mark_saved(fecha, monto, detalle=""):
        fn = normalize_date(str(fecha).strip())
        mn = norm_monto(monto)
        dn = str(detalle).strip().lower()
        ex_keys.add(f"{fn}|{mn}|{dn}"); ex_keys.add(f"{fn}|{mn}")
        if dn: ex_keys.add(f"{fn}|{dn}")

    memory = get_memory()

    # ── Process ok_raw ────────────────────────────────────────────────────
    ok_raw = result.get("movimientos_ok", [])
    ok = []
    extra_dudosos = []
    for m in ok_raw:
        rubro   = str(m.get("rubro", "")).strip().lower()
        detalle = str(m.get("detalle", "")).strip()
        dk = detalle.lower()
        subtitulo = str(m.get("subtitulo", "")).lower()
        # Check memory
        if not rubro and dk in memory:
            m["rubro"] = memory[dk]
            rubro = m["rubro"]
        # Force empty rubro to dudosos
        if not rubro or rubro in ["", "none", "null"]:
            extra_dudosos.append(m)
            continue
        # Transfers to persons → always ask
        is_transfer = "transferencia" in subtitulo or (
            bool(re.match(r'^[A-Za-záéíóúñÁÉÍÓÚÑ,. ]+$', detalle)) and
            len(detalle.split()) >= 2 and not any(c.isdigit() for c in detalle) and "*" not in detalle
        )
        if is_transfer:
            self_names = ["facundo dardo lujan", "facundo lujan", "lujan facundo"]
            if dk not in self_names:
                extra_dudosos.append(m)
                continue
        ok.append(m)

    # Merge memory-resolved extra_dudosos back to ok
    final_dudosos_extra = []
    for m in extra_dudosos:
        dk = str(m.get("detalle","")).strip().lower()
        if dk in memory:
            m["rubro"] = memory[dk]
            ok.append(m)
        else:
            final_dudosos_extra.append(m)
    result["movimientos_dudosos"] = result.get("movimientos_dudosos", []) + final_dudosos_extra

    # Save ok
    ok_saved = ok_dupes = 0
    for m in ok:
        if is_dup(m.get("fecha", today), m.get("monto",""), m.get("detalle","")):
            ok_dupes += 1
        else:
            rows_to_save.append([m.get("fecha", today), m.get("monto",""), m.get("rubro",""), m.get("detalle",""), "UALA"])
            mark_saved(m.get("fecha", today), m.get("monto",""), m.get("detalle",""))
            ok_saved += 1
    if ok_saved: msg_parts.append(f"✅ {ok_saved} movimientos guardados")
    if ok_dupes: msg_parts.append(f"⏭️ {ok_dupes} ya estaban anotados (omitidos)")
    total_saved += ok_saved

    # Rendimientos
    rendimientos = result.get("rendimientos", [])
    rend_saved = rend_dupes = 0
    for r in rendimientos:
        if is_dup(r.get("fecha", today), r.get("monto",""), r.get("detalle","Rendimiento UALA")):
            rend_dupes += 1
        else:
            rows_to_save.append([r.get("fecha", today), r.get("monto",""), "rendimientos", r.get("detalle","Rendimiento UALA"), "UALA"])
            mark_saved(r.get("fecha", today), r.get("monto",""), r.get("detalle","Rendimiento UALA"))
            rend_saved += 1
    if rend_saved: msg_parts.append(f"💰 {rend_saved} rendimientos guardados")
    if rend_dupes: msg_parts.append(f"⏭️ {rend_dupes} rendimientos ya anotados (omitidos)")
    total_saved += rend_saved

    # Devoluciones
    devoluciones = result.get("devoluciones", [])
    dev_saved = dev_dupes = 0
    for d in devoluciones:
        monto_d = abs(float(d.get("monto", 0)))
        if is_dup(d.get("fecha", today), monto_d, d.get("detalle","Devolución")):
            dev_dupes += 1
        else:
            rows_to_save.append([d.get("fecha", today), monto_d, d.get("rubro","reintegro gastos"), d.get("detalle","Devolución"), "UALA"])
            mark_saved(d.get("fecha", today), monto_d, d.get("detalle","Devolución"))
            dev_saved += 1
    if dev_saved: msg_parts.append(f"↩️ {dev_saved} devoluciones guardadas")
    if dev_dupes: msg_parts.append(f"⏭️ {dev_dupes} devoluciones ya anotadas (omitidas)")
    total_saved += dev_saved

    if rows_to_save:
        append_rows("MOVIMIENTOS", rows_to_save)

    # Save latest date
    all_dates = []
    for lst in [ok, rendimientos, devoluciones]:
        for m in lst:
            if m.get("fecha"):
                all_dates.append(m["fecha"])
    if all_dates:
        try:
            latest = sorted(all_dates, key=lambda x: datetime.strptime(x, "%d/%m/%Y"))[-1]
            save_last_uala_date(latest)
        except:
            save_last_uala_date(today)

    # Handle transferencias_propias
    transferencias = result.get("transferencias_propias", [])
    dudosos = result.get("movimientos_dudosos", [])

    if transferencias:
        user_context[chat_id] = {"pendiente": "uala_transferencias", "transferencias": transferencias, "dudosos": dudosos, "idx": 0}
        t = transferencias[0]
        monto_t = abs(float(t.get("monto", 0)))
        signo = "+" if float(t.get("monto", 0)) > 0 else "-"
        msg_parts.append(f"\n💸 Transferencia de/a vos mismo: {signo}${monto_t:,.0f}")
        msg_parts.append("¿A qué cuenta? (ej: galicia, efectivo) — o decí 'rendimiento' si es eso")
        await update.message.reply_text("\n".join(msg_parts))
        return

    if dudosos:
        user_context[chat_id] = {"pendiente": "uala_dudosos", "dudosos": dudosos, "idx": 0, "nuevas_memorias": []}
        d = dudosos[0]
        monto_d = abs(float(d.get("monto", 0)))
        detalle_d = str(d.get("detalle", "?")).strip()
        msg_parts.append(f"\n❓ 1/{len(dudosos)}: <b>{detalle_d}</b> — ${monto_d:,.0f}\n¿Qué rubro es?\n<i>Escribí 'ya anotado' para omitir</i>")
        await update.message.reply_text("\n".join(msg_parts), parse_mode="HTML")
        return

    if not msg_parts:
        msg_parts.append("✅ Sin movimientos nuevos en la captura")
    save_uala_reminder_state("done", datetime.now(TZ).date())
    await update.message.reply_text("\n".join(msg_parts))

# ── Handle Result ─────────────────────────────────────────────────────────────
async def handle_result(data: dict, update: Update, context: ContextTypes.DEFAULT_TYPE):
    tipo = data.get("tipo")
    chat_id = update.effective_chat.id
    today = datetime.now(TZ).strftime("%d/%m/%Y")

    if tipo == "movimiento":
        filas = data.get("filas", [])
        memory = get_memory()
        existing_rows = read_sheet("MOVIMIENTOS", "A:E")
        ex_keys = set()
        for row in existing_rows[1:]:
            if len(row) < 2: continue
            f_e = normalize_date(str(row[0]).strip())
            m_norm = norm_monto(row[1])
            d_e = str(row[3]).strip().lower() if len(row) > 3 else ""
            ex_keys.add(f"{f_e}|{m_norm}|{d_e}"); ex_keys.add(f"{f_e}|{m_norm}")
            if d_e: ex_keys.add(f"{f_e}|{d_e}")

        def is_dup_m(fecha, monto, detalle=""):
            fn=normalize_date(str(fecha)); mn=norm_monto(monto); dn=str(detalle).lower().strip()
            return f"{fn}|{mn}|{dn}" in ex_keys or f"{fn}|{mn}" in ex_keys or (dn and f"{fn}|{dn}" in ex_keys)

        rows_ok, rows_ask = [], []
        obvious = ["rendimientos","sueldo","traspaso","inversion","impuestos","seguro","combustible","salud"]
        for f in filas:
            rubro   = str(f.get("rubro","")).strip().lower()
            detalle = str(f.get("detalle","")).strip()
            dk      = detalle.lower()
            monto   = f.get("monto", 0)
            fecha   = f.get("fecha", today)
            cuenta  = str(f.get("cuenta","")).upper()
            if not rubro and dk in memory: rubro = memory[dk]; f["rubro"] = rubro
            if not rubro or rubro in ["","none","null"]:
                rows_ask.append(f); continue
            if detalle and dk not in memory and rubro not in obvious:
                save_memory(detalle, rubro)
            rows_ok.append([fecha, monto, rubro, detalle, cuenta])
            # TAONE dual save
            if rubro == "taone":
                append_rows("TAONE", [[fecha, detalle, monto, "", detalle]])

        if rows_ok:
            append_rows("MOVIMIENTOS", rows_ok)

        if rows_ask:
            user_context[chat_id] = {"pendiente": "rubro_desconocido", "filas_pendientes": rows_ask, "idx": 0}
            f0 = rows_ask[0]
            msg0 = f"❓ ¿Qué rubro es <b>{f0.get('detalle','?')}</b> (${abs(float(f0.get('monto',0))):,.0f})?\nEj: comida, transporte, salud...\n<i>Te voy a recordar para la próxima 🧠</i>"
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
        rows = [[f.get("fecha",""), f.get("monto",""), f.get("rubro",""), f.get("detalle",""), f.get("cuenta","").upper()] for f in data.get("filas", [])]
        if rows: append_rows("DOLARES", rows)
        mp = data.get("movimiento_pesos")
        if mp: append_rows("MOVIMIENTOS", [[mp.get("fecha",""), mp.get("monto",""), mp.get("rubro","").lower(), mp.get("detalle",""), str(mp.get("cuenta","")).upper()]])
        user_context.pop(chat_id, None)
        await update.message.reply_text(data.get("mensaje", "✅ Dólares guardado"))

    elif tipo == "inversion":
        rows = [[f.get("fecha",""), f.get("tipo_instr",""), f.get("ticker",""), f.get("detalle",""), f.get("cantidad",""), f.get("precio_compra","")] for f in data.get("filas", [])]
        if rows: append_rows("INVERSIONES", rows)
        mp = data.get("movimiento_pesos")
        if mp: append_rows("MOVIMIENTOS", [[mp.get("fecha",""), mp.get("monto",""), mp.get("rubro","").lower(), mp.get("detalle",""), str(mp.get("cuenta","")).upper()]])
        user_context.pop(chat_id, None)
        await update.message.reply_text(data.get("mensaje", "✅ Inversión guardada"))

    elif tipo == "taone":
        rows = [[f.get("fecha",""), f.get("concepto",""), f.get("monto",""), "", f.get("detalle","")] for f in data.get("filas", [])]
        if rows: append_rows("TAONE", rows)
        # Also in MOVIMIENTOS
        mov_rows = [[f.get("fecha",""), f.get("monto",""), "taone", f.get("concepto",""), "UALA"] for f in data.get("filas", [])]
        if mov_rows: append_rows("MOVIMIENTOS", mov_rows)
        user_context.pop(chat_id, None)
        await update.message.reply_text(data.get("mensaje", "✅ TAONE guardado"))

    elif tipo == "tarjeta":
        await handle_tarjeta(data, update, context)

    elif tipo == "consulta":
        rows = read_sheet("MOVIMIENTOS", "A:E")
        csv = "\n".join([",".join(str(c) for c in r) for r in rows[:500]])
        answer = await call_claude(CONSULTA_SYSTEM, f"Hoy es {today}.\nMOVIMIENTOS:\nfecha,monto,rubro,detalle,cuenta\n{csv}\n\nPREGUNTA: {update.message.text}")
        await update.message.reply_text(answer)

    elif tipo == "consulta_inversiones":
        await handle_consulta_inversiones(update)

    elif tipo == "vencimiento":
        import datetime as dt_mod, calendar
        dia = int(data.get("dia", 1))
        mes_offset = int(data.get("mes_offset", 1))
        descripcion = data.get("descripcion", "Tarjeta")
        now = datetime.now(TZ)
        target_month = now.month + mes_offset
        target_year = now.year + (target_month - 1) // 12
        target_month = ((target_month - 1) % 12) + 1
        try:
            max_day = calendar.monthrange(target_year, target_month)[1]
            target_date = dt_mod.date(target_year, target_month, min(dia, max_day))
            save_vencimiento(target_date, descripcion)
            user_context.pop(chat_id, None)
            await update.message.reply_text(f"✅ Te recuerdo el {target_date.strftime('%d/%m/%Y')}: <b>{descripcion}</b>", parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
    else:
        await update.message.reply_text("❓ No entendí. Intentá de nuevo.")

async def handle_tarjeta(data: dict, update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    today_str = datetime.now(TZ).strftime("%d/%m/%Y")
    filas = data.get("filas", [])
    banco = data.get("banco", "")
    marca = data.get("marca", "")
    cuenta_pago = banco.upper() if banco else data.get("cuenta_pago", "GALICIA").upper()
    for prefix in ["TARJETA_VISA_","TARJETA_MASTERCARD_","TARJETA_"]:
        if cuenta_pago.startswith(prefix):
            cuenta_pago = cuenta_pago[len(prefix):]
            break
    total_extracto = abs(float(data.get("total_extracto", 0)))
    memory = get_memory()
    rows_ok, dudosos = [], []
    obvious = ["rendimientos","sueldo","traspaso","impuestos","seguro","combustible","salud"]
    for f in filas:
        detalle = str(f.get("detalle","")).strip()
        dk = detalle.lower()
        rubro = str(f.get("rubro","")).strip().lower()
        monto = f.get("monto", 0)
        fecha = today_str  # Always use payment date
        if f.get("tipo") == "consulta_rubro":
            if dk in memory:
                rows_ok.append([fecha, monto, memory[dk], detalle, cuenta_pago])
            else:
                dudosos.append(f)
        else:
            if dk in memory:
                rows_ok.append([fecha, monto, memory[dk], detalle, cuenta_pago])
            elif rubro in obvious:
                rows_ok.append([fecha, monto, rubro, detalle, cuenta_pago])
                if detalle and dk not in memory: save_memory(detalle, rubro)
            else:
                f["tipo"] = "consulta_rubro"
                dudosos.append(f)
    if rows_ok: append_rows("MOVIMIENTOS", rows_ok)
    total_guardado = sum(abs(float(r[1])) for r in rows_ok)
    if dudosos:
        user_context[chat_id] = {"pendiente": "tarjeta_rubros", "dudosos": dudosos, "cuenta_pago": cuenta_pago, "idx": 0, "guardados": len(rows_ok), "total_guardado": total_guardado, "total_extracto": total_extracto, "marca": marca}
        d = dudosos[0]
        monto_d = abs(float(d.get("monto", 0)))
        detalle_d = str(d.get("detalle","?")).strip()
        msg = f"💳 <b>{marca} {cuenta_pago}</b> — {len(rows_ok)} guardados\n\n❓ 1/{len(dudosos)}: <b>{detalle_d}</b> — ${monto_d:,.0f}\n¿Qué rubro es?\n<i>Escribí 'ya anotado' para omitir 🧠</i>"
        await update.message.reply_text(msg, parse_mode="HTML")
    else:
        user_context.pop(chat_id, None)
        msg = f"💳 ✅ <b>{marca} {cuenta_pago}</b> — {len(rows_ok)} gastos guardados\n💰 Total: ${total_guardado:,.0f}"
        if total_extracto > 0:
            diff = abs(total_guardado - total_extracto)
            msg += f"\n{'✅ Cuadra' if diff<=500 else '⚠️ Diferencia: $'+f'{diff:,.0f}'} (extracto ${total_extracto:,.0f})"
        await update.message.reply_text(msg, parse_mode="HTML")

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
    total_inv = total_act = total_res = total_dia = 0
    for t in tickers_data:
        tk = t["ticker"]; qty = t["cantidad"]; compra = t["precio_compra"]
        p = prices.get(tk, {}); hoy = p.get("hoy"); ayer = p.get("ayer")
        inv = qty*compra; act = qty*hoy if hoy else 0
        res = act-inv if hoy else 0; dia = qty*(hoy-ayer) if hoy and ayer else 0
        total_inv+=inv; total_act+=act; total_res+=res; total_dia+=dia
        info += f"- {tk}: {qty} nom | compra U$D{compra:.2f} | hoy U$D{hoy if hoy else 'N/D'} | ayer U$D{ayer if ayer else 'N/D'} | resultado U$D{res:+.2f} | hoy U$D{dia:+.2f}\n"
    info += f"\nTOTALES: inv U$D{total_inv:,.2f} | act U$D{total_act:,.2f} | res U$D{total_res:+,.2f} | hoy U$D{total_dia:+,.2f}"
    answer = await call_claude(INVERSIONES_SYSTEM, f"Hoy es {today}.\n{info}\nMostrá resumen claro con emojis.")
    await update.message.reply_text(answer)

# ── Message Handlers ──────────────────────────────────────────────────────────
def es_mensaje_nuevo(text: str) -> bool:
    t = text.strip().lower()
    if t.startswith("/"): return True
    new_kw = ["gasté","gaste","cobré","cobre","pagué","pague","traspasé","traspase","compré","compre","anota","anotar","invertí","inverti","cuanto","cuánto","como","cómo"]
    for kw in new_kw:
        if kw in t: return True
    if re.search(r'[0-9]+.*(?:pesos?|peso|usd|dolar|k\b)', t): return True
    return False

VALID_RUBROS = {"almacen","auto","cafe","cancha","celular","cochera","combustible","comida","concierto","credito","cumpleaños","dolares","farmacia","futbol","gimnasio","impuestos","indumentaria","inversion","juegos","kiosko","nafta","panaderia","peluqueria","prepaga","regalos","rendimientos","salida","salud","seguro","sueldo","suscripciones","taone","transporte","traspaso","varios","verduleria","cena","almuerzo","tarjeta","compras","delivery","supermercado","basquet","cancha","credito","indumentaria"}

def extract_rubro(text: str):
    words = text.strip().lower().split()
    if not words: return None
    candidate = words[0].rstrip(".,!?")
    if candidate in VALID_RUBROS: return candidate
    if len(candidate) <= 15 and len(words) <= 2: return candidate
    return None

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    last_message_date["date"] = datetime.now(TZ).date()

    ctx = user_context.get(chat_id)
    if ctx:
        pendiente = ctx.get("pendiente")

        # Universal skip handler
        if text.strip().lower() in YA_ANOTADO_PHRASES:
            dudosos = ctx.get("dudosos") or ctx.get("filas_pendientes", [])
            idx = ctx.get("idx", 0)
            next_idx = idx + 1
            await update.message.reply_text("⏭️ Omitido — no se anota")
            if dudosos and next_idx < len(dudosos):
                ctx["idx"] = next_idx
                d2 = dudosos[next_idx]
                monto2 = abs(float(d2.get("monto", 0)))
                detalle2 = str(d2.get("detalle", "?")).strip()
                await update.message.reply_text(f"❓ {next_idx+1}/{len(dudosos)}: <b>{detalle2}</b> — ${monto2:,.0f}\n¿Qué rubro es?\n<i>O 'ya anotado' para omitir</i>", parse_mode="HTML")
            else:
                user_context.pop(chat_id, None)
                await update.message.reply_text("✅ Listo")
            return

        if pendiente == "detalle":
            if es_mensaje_nuevo(text):
                user_context.pop(chat_id, None)
            else:
                original = ctx["data"]
                falta = original.get("falta", "")
                prompt = (f"Hoy es {today}. El usuario registra un movimiento. Ya sabemos: monto={original.get('monto')}"
                          + (f", rubro={original.get('rubro')}" if original.get('rubro') else "")
                          + (f", cuenta={original.get('cuenta')}" if original.get('cuenta') else "")
                          + f". Faltaba: {falta}. El usuario aclaró: '{text}'. Generá el JSON de movimiento completo.")
                raw = await call_claude(SYSTEM_PROMPT, prompt)
                raw = raw.strip().replace("```json","").replace("```","").strip()
                data = json.loads(raw)
                user_context.pop(chat_id, None)
                await handle_result(data, update, context)
                return

        elif pendiente == "inversion":
            if not es_mensaje_nuevo(text):
                original = ctx["data"]
                datos = original.get("datos_presentes", {})
                prompt = (f"Hoy es {today}. Inversión tipo {original.get('instrumento','CEDEAR')}. Ya sabemos: {json.dumps(datos)}. El usuario completó: '{text}'. Generá el JSON de inversión completo.")
                raw = await call_claude(SYSTEM_PROMPT, prompt)
                raw = raw.strip().replace("```json","").replace("```","").strip()
                data = json.loads(raw)
                user_context.pop(chat_id, None)
                await handle_result(data, update, context)
                return

        elif pendiente == "uala_dudosos":
            if es_mensaje_nuevo(text):
                user_context.pop(chat_id, None)
            else:
                dudosos = ctx.get("dudosos", [])
                idx = ctx.get("idx", 0)
                nuevas_memorias = ctx.get("nuevas_memorias", [])
                d = dudosos[idx]
                rubro = extract_rubro(text)
                if rubro is None:
                    user_context.pop(chat_id, None)
                    await update.message.reply_text("❓ No entendí el rubro. Procesando como mensaje nuevo...")
                else:
                    fecha = d.get("fecha", today)
                    monto = d.get("monto", 0)
                    detalle = str(d.get("detalle", "")).strip()
                    append_rows("MOVIMIENTOS", [[fecha, monto, rubro, detalle, "UALA"]])
                    if rubro == "taone": append_rows("TAONE", [[fecha, detalle, monto, "", detalle]])
                    is_transfer_name = bool(re.match(r'^[A-Za-záéíóúñÁÉÍÓÚÑ,. ]+$', detalle)) and len(detalle.split()) >= 2 and "*" not in detalle
                    if detalle and rubro not in ["traspaso","sueldo","rendimientos","inversion","dolares"] and not is_transfer_name:
                        nuevas_memorias.append({"comercio": detalle, "rubro": rubro})
                        save_memory(detalle, rubro)
                        await update.message.reply_text(f"✅ <b>{rubro}</b> 🧠 Memoricé '{detalle}' = {rubro}", parse_mode="HTML")
                    else:
                        await update.message.reply_text(f"✅ Guardado como {rubro}")
                    next_idx = idx + 1
                    if next_idx < len(dudosos):
                        ctx["idx"] = next_idx; ctx["nuevas_memorias"] = nuevas_memorias
                        d2 = dudosos[next_idx]
                        monto2 = abs(float(d2.get("monto", 0)))
                        detalle2 = str(d2.get("detalle","?")).strip()
                        await update.message.reply_text(f"❓ {next_idx+1}/{len(dudosos)}: <b>{detalle2}</b> — ${monto2:,.0f}\n¿Qué rubro es?", parse_mode="HTML")
                        return
                    user_context.pop(chat_id, None)
                    save_last_uala_date(today)
                    save_uala_reminder_state("done", datetime.now(TZ).date())
                    msg = "✅ ¡UALA procesado!"
                    if nuevas_memorias: msg += f" Memoricé {len(nuevas_memorias)} comercio(s) 🧠"
                    await update.message.reply_text(msg)
                    return

        elif pendiente == "uala_transferencias":
            transferencias = ctx.get("transferencias", [])
            dudosos = ctx.get("dudosos", [])
            idx = ctx.get("idx", 0)
            t = transferencias[idx]
            monto = float(t.get("monto", 0))
            fecha = t.get("fecha", today)
            txt_lower = text.strip().lower()

            if txt_lower in YA_ANOTADO_PHRASES or txt_lower in ["no", "n"]:
                await update.message.reply_text("⏭️ Omitido — no se anota")
            elif any(k in txt_lower for k in RENDIMIENTO_KEYWORDS):
                append_rows("MOVIMIENTOS", [[fecha, abs(monto), "rendimientos", "Rendimiento UALA", "UALA"]])
                await update.message.reply_text(f"✅ Guardado como rendimiento: +${abs(monto):,.2f}")
            elif any(k in txt_lower for k in RUBRO_KEYWORDS):
                append_rows("MOVIMIENTOS", [[fecha, monto, txt_lower.split()[0], t.get("detalle",""), "UALA"]])
                await update.message.reply_text(f"✅ Guardado como {txt_lower.split()[0]}")
            else:
                cuenta_destino = txt_lower
                rows_t = [
                    [fecha, -abs(monto), "traspaso", f"Traspaso a {cuenta_destino.upper()}", "UALA"],
                    [fecha,  abs(monto), "traspaso", f"Traspaso desde UALA", cuenta_destino.upper()],
                ]
                append_rows("MOVIMIENTOS", rows_t)
                await update.message.reply_text(f"✅ Traspaso: UALA → {cuenta_destino.upper()}")

            next_idx = idx + 1
            if next_idx < len(transferencias):
                ctx["idx"] = next_idx
                t2 = transferencias[next_idx]
                monto2 = abs(float(t2.get("monto", 0)))
                await update.message.reply_text(f"💸 Otra transferencia: ${monto2:,.0f}\n¿A qué cuenta? (o 'rendimiento' si es eso)")
                return
            if dudosos:
                user_context[chat_id] = {"pendiente": "uala_dudosos", "dudosos": dudosos, "idx": 0, "nuevas_memorias": []}
                d = dudosos[0]
                await update.message.reply_text(f"❓ 1/{len(dudosos)}: <b>{d.get('detalle','?')}</b> — ${abs(float(d.get('monto',0))):,.0f}\n¿Qué rubro es?", parse_mode="HTML")
                return
            user_context.pop(chat_id, None)
            save_last_uala_date(today)
            save_uala_reminder_state("done", datetime.now(TZ).date())
            await update.message.reply_text("✅ ¡UALA procesado!")
            return

        elif pendiente == "rubro_desconocido":
            if es_mensaje_nuevo(text):
                user_context.pop(chat_id, None)
            else:
                filas_pendientes = ctx.get("filas_pendientes", [])
                idx = ctx.get("idx", 0)
                f = filas_pendientes[idx]
                rubro = extract_rubro(text)
                if rubro is None:
                    user_context.pop(chat_id, None)
                else:
                    detalle = str(f.get("detalle","")).strip()
                    fecha = f.get("fecha", today)
                    monto = f.get("monto", 0)
                    cuenta = str(f.get("cuenta","")).upper()
                    append_rows("MOVIMIENTOS", [[fecha, monto, rubro, detalle, cuenta]])
                    if rubro == "taone": append_rows("TAONE", [[fecha, detalle, monto, "", detalle]])
                    if detalle and rubro not in ["traspaso","sueldo","rendimientos","inversion","dolares"]:
                        save_memory(detalle, rubro)
                        await update.message.reply_text(f"✅ <b>{rubro}</b> 🧠 Memoricé '{detalle}' = {rubro}", parse_mode="HTML")
                    else:
                        await update.message.reply_text(f"✅ Guardado como {rubro}")
                    next_idx = idx + 1
                    if next_idx < len(filas_pendientes):
                        ctx["idx"] = next_idx
                        f2 = filas_pendientes[next_idx]
                        await update.message.reply_text(f"❓ <b>{f2.get('detalle','?')}</b> (${abs(float(f2.get('monto',0))):,.0f}) ¿Qué rubro?", parse_mode="HTML")
                        return
                    user_context.pop(chat_id, None)
                    return

        elif pendiente == "tipo_imagen":
            bytes_data = ctx.get("bytes", b"")
            mime_data  = ctx.get("mime", "image/jpeg")
            user_context.pop(chat_id, None)
            txt_lower = text.strip().lower()
            if txt_lower in ["1","uala","1️⃣"] or "uala" in txt_lower:
                await handle_uala_screenshot(bytes_data, mime_data, update, context)
            else:
                await update.message.reply_text("📄 Analizando resumen de tarjeta...")
                b64 = base64.standard_b64encode(bytes_data).decode()
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                        json={"model": MODEL, "max_tokens": 2048, "system": SYSTEM_PROMPT,
                              "messages": [{"role": "user", "content": [
                                  {"type": "text", "text": f"Hoy es {today}. FECHA_PAGO={today}. Resumen de tarjeta. JSON tipo tarjeta."},
                                  {"type": "image", "source": {"type": "base64", "media_type": mime_data, "data": b64}}
                              ]}]}
                    )
                raw = resp.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
                data = json.loads(raw)
                await handle_result(data, update, context)
            return

        elif pendiente == "tarjeta_rubros":
            if es_mensaje_nuevo(text):
                user_context.pop(chat_id, None)
            else:
                dudosos     = ctx.get("dudosos", [])
                cuenta_pago = ctx.get("cuenta_pago", "GALICIA")
                idx         = ctx.get("idx", 0)
                guardados   = ctx.get("guardados", 0)
                d           = dudosos[idx]
                rubro       = extract_rubro(text)
                if rubro is None:
                    user_context.pop(chat_id, None)
                else:
                    detalle = str(d.get("detalle","")).strip()
                    monto   = d.get("monto", 0)
                    fecha   = datetime.now(TZ).strftime("%d/%m/%Y")
                    append_rows("MOVIMIENTOS", [[fecha, monto, rubro, detalle, cuenta_pago]])
                    if rubro == "taone": append_rows("TAONE", [[fecha, detalle, monto, "", detalle]])
                    if detalle and rubro not in ["traspaso","sueldo","rendimientos"]:
                        save_memory(detalle, rubro)
                        await update.message.reply_text(f"✅ <b>{rubro}</b> 🧠 '{detalle}' = {rubro}", parse_mode="HTML")
                    else:
                        await update.message.reply_text(f"✅ {rubro}")
                    next_idx = idx + 1
                    if next_idx < len(dudosos):
                        ctx["idx"] = next_idx
                        d2 = dudosos[next_idx]
                        await update.message.reply_text(f"❓ {next_idx+1}/{len(dudosos)}: <b>{d2.get('detalle','?')}</b> — ${abs(float(d2.get('monto',0))):,.0f}\n¿Qué rubro?", parse_mode="HTML")
                        return
                    user_context.pop(chat_id, None)
                    total = guardados + len(dudosos)
                    total_guardado_f = ctx.get("total_guardado",0) + sum(abs(float(d2.get("monto",0))) for d2 in dudosos)
                    total_extracto_f = ctx.get("total_extracto",0)
                    marca_f = ctx.get("marca","")
                    msg = f"💳 ✅ <b>{marca_f} {cuenta_pago}</b> — {total} gastos\n💰 ${total_guardado_f:,.0f}"
                    if total_extracto_f > 0:
                        diff = abs(total_guardado_f - total_extracto_f)
                        msg += f"\n{'✅ Cuadra' if diff<=500 else f'⚠️ Diferencia ${diff:,.0f}'} (extracto ${total_extracto_f:,.0f})"
                    await update.message.reply_text(msg, parse_mode="HTML")
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
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    last_message_date["date"] = datetime.now(TZ).date()
    chat_id = update.effective_chat.id
    ctx = user_context.get(chat_id, {})
    is_uala_context = (
        uala_reminder_state.get("state") in ["waiting_tonight","waiting_morning"] or
        ctx.get("pendiente","").startswith("uala")
    )
    try:
        if update.message.photo:
            file_obj = update.message.photo[-1]; mime = "image/jpeg"
        else:
            file_obj = update.message.document; mime = file_obj.mime_type or "image/jpeg"
        tg_file = await file_obj.get_file()
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            await tg_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                content_bytes = f.read()
        if mime == "application/pdf":
            await update.message.reply_text("📄 Leyendo PDF...")
            pdf_text = extract_pdf_text(content_bytes)
            if not pdf_text.strip():
                await update.message.reply_text("❌ No pude leer el PDF. Intentá como imagen.")
                return
            raw = await call_claude(SYSTEM_PROMPT, f"Hoy es {today}. FECHA_PAGO={today}. Texto del extracto:\n{pdf_text[:6000]}\nDevolvé JSON tipo tarjeta.")
            raw = raw.strip().replace("```json","").replace("```","").strip()
            data = json.loads(raw)
            await handle_result(data, update, context)
            return
        if update.message.photo or is_uala_context:
            caption = update.message.caption or ""
            if "uala" in caption.lower() or is_uala_context:
                await handle_uala_screenshot(content_bytes, mime, update, context)
                return
            elif any(k in caption.lower() for k in ["tarjeta","tc","extracto","resumen"]):
                pass
            else:
                user_context[chat_id] = {"pendiente": "tipo_imagen", "bytes": content_bytes, "mime": mime}
                await update.message.reply_text("📸 ¿Qué es esta imagen?\n\nRespondé <b>uala</b> o <b>tarjeta</b>", parse_mode="HTML")
                return
    except Exception as e:
        logger.error(f"Document error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
        return
    await update.message.reply_text("📄 Analizando resumen de tarjeta...")
    try:
        b64 = base64.standard_b64encode(content_bytes).decode()
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": 2048, "system": SYSTEM_PROMPT,
                      "messages": [{"role": "user", "content": [
                          {"type": "text", "text": f"Hoy es {today}. FECHA_PAGO={today}. Extracto de tarjeta. JSON tipo tarjeta."},
                          {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
                      ]}]}
            )
        raw = resp.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
        data = json.loads(raw)
        await handle_result(data, update, context)
    except Exception as e:
        logger.error(f"Tarjeta error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

# ── Reminders ─────────────────────────────────────────────────────────────────
async def uala_reminder_noche_standalone(bot) -> None:
    if not CHAT_ID: return
    today = datetime.now(TZ).date()
    state = get_uala_reminder_state()
    if state.get("date") == today and state.get("state") in ["done", "waiting_tonight"]:
        logger.info("Skipping noche reminder — already done or sent")
        return
    save_uala_reminder_state("waiting_tonight", today)
    await bot.send_message(chat_id=int(CHAT_ID), text="📱 ¡Hora de registrar UALA!\n\nMandame una captura de pantalla de tus movimientos de hoy.")

async def uala_reminder_manana_standalone(bot) -> None:
    if not CHAT_ID: return
    import datetime as dt_mod
    state = get_uala_reminder_state()
    today = datetime.now(TZ).date()
    yesterday = today - dt_mod.timedelta(days=1)
    if state.get("date") == today and state.get("state") == "done":
        return
    if state.get("state") in ["waiting_tonight","waiting_morning"] and state.get("date") == yesterday:
        save_uala_reminder_state("waiting_morning", yesterday)
        await bot.send_message(chat_id=int(CHAT_ID), text=f"☀️ Buenos días! No registraste los movimientos de UALA del {yesterday.strftime('%d/%m')}.\n\nMandame la captura cuando puedas.")

async def reminder_job_standalone(bot) -> None:
    if not CHAT_ID: return
    today = datetime.now(TZ).date()
    if last_message_date.get("date") == today: return
    hour = datetime.now(TZ).hour
    msg = "👋 ¿Gastaste algo hoy que no hayas anotado?" if hour < 20 else "🌙 Antes de dormir — ¿algo que registrar del día?"
    await bot.send_message(chat_id=int(CHAT_ID), text=msg)

async def check_vencimientos_standalone(bot) -> None:
    if not CHAT_ID: return
    import datetime as dt_mod
    today = datetime.now(TZ).date()
    for v in get_vencimientos():
        days = (v["fecha"] - today).days
        if days in [0, 1, 3, 7]:
            if days == 0: msg = f"🔴 HOY vence: {v['descripcion']}"
            elif days == 1: msg = f"🟡 Mañana vence: {v['descripcion']}"
            elif days == 3: msg = f"🟠 En 3 días vence: {v['descripcion']}"
            else: msg = f"📅 En {days} días vence: {v['descripcion']} ({v['fecha'].strftime('%d/%m')})"
            await bot.send_message(chat_id=int(CHAT_ID), text=msg)

async def heartbeat_check(bot) -> None:
    now = datetime.now(TZ)
    hour, minute, today = now.hour, now.minute, now.date()
    if hour == 21 and minute <= 10:
        key = f"uala_noche_{today}"
        if key not in heartbeat_fired:
            heartbeat_fired[key] = True
            await uala_reminder_noche_standalone(bot)
    if hour == 9 and minute <= 10:
        key = f"uala_manana_{today}"
        if key not in heartbeat_fired:
            heartbeat_fired[key] = True
            await uala_reminder_manana_standalone(bot)
    if hour == 9 and 15 <= minute <= 25:
        key = f"vencimientos_{today}"
        if key not in heartbeat_fired:
            heartbeat_fired[key] = True
            await check_vencimientos_standalone(bot)

# ── Commands ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 <b>Hola, soy BOTSTERO!</b>\n\nTu chat ID: <code>{update.effective_chat.id}</code>\n\n"
        f"📌 Comandos:\n/cartera — inversiones hoy\n/resumen — resumen del mes\n"
        f"/tenencias — saldos por cuenta\n/movimientos — últimos movimientos\n"
        f"/vencimientos — próximos vencimientos\n/uala — procesar captura UALA\n"
        f"/resetuala — resetear fecha UALA\n\n¡Mandame texto o audio!",
        parse_mode="HTML"
    )

async def cmd_cartera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Consultando tu cartera...")
    await handle_consulta_inversiones(update)

async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = read_sheet("MOVIMIENTOS", "A:E")
    csv = "\n".join([",".join(str(c) for c in r) for r in rows[:500]])
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    answer = await call_claude(CONSULTA_SYSTEM, f"Hoy es {today}.\nMOVIMIENTOS:\n{csv}\n\nResumen del mes: ingresos, gastos, resultado neto, top 3 rubros.")
    await update.message.reply_text(answer)

async def cmd_tenencias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Consultando tus saldos...")
    try:
        rows_pesos = read_sheet("TENENCIA", "A14:B22")
        rows_usd   = read_sheet("TENENCIA", "E14:F22")
        rows_inv   = read_sheet("TENENCIA", "H14:I22")
        def rows_to_text(rows):
            return "\n".join([" | ".join(str(c) for c in r) for r in rows if any(str(c).strip() for c in r)])
        pesos_txt = rows_to_text(rows_pesos)
        usd_txt   = rows_to_text(rows_usd)
        inv_txt   = rows_to_text(rows_inv)
        summary   = "SALDOS EN PESOS:\n" + pesos_txt + "\n\nDOLARES:\n" + usd_txt + "\n\nINVERSIONES:\n" + inv_txt
        answer = await call_claude(
            "Sos BOTSTERO. Mostrá SOLO los saldos actuales: cada cuenta en pesos, total USD con TC y equivalente en pesos, inversiones si hay, patrimonio total. NO menciones movimientos. Emojis, texto plano.",
            "Datos de la planilla TENENCIA:\n" + summary
        )
        await update.message.reply_text(answer)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def cmd_movimientos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Últimos movimientos...")
    try:
        rows = read_sheet("MOVIMIENTOS", "A:E")
        data_rows = [r for r in rows if len(r) >= 2 and any(str(c).strip() for c in r)]
        last_rows = data_rows[-15:]
        summary = "\n".join([" | ".join(str(c) for c in r) for r in last_rows])
        today = datetime.now(TZ).strftime("%d/%m/%Y")
        answer = await call_claude("Sos BOTSTERO. Mostrá los últimos movimientos con emoji por tipo (💸 gasto, ✅ ingreso, 🔄 traspaso). Al final mostrá el total. Texto plano.", f"Hoy es {today}. Últimos:\nfecha|monto|rubro|detalle|cuenta\n{summary}")
        await update.message.reply_text(answer)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def cmd_vencimientos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import datetime as dt_mod
    today = datetime.now(TZ).date()
    vencimientos = get_vencimientos()
    if not vencimientos:
        await update.message.reply_text("📅 No tenés vencimientos guardados.\n\nEj: 'Me vence la Visa Galicia el 8 del mes que viene'")
        return
    futuros = sorted([v for v in vencimientos if v["fecha"] >= today], key=lambda x: x["fecha"])
    if not futuros:
        await update.message.reply_text("📅 No tenés vencimientos próximos.")
        return
    msg = "📅 <b>Próximos vencimientos:</b>\n\n"
    for v in futuros[:10]:
        days = (v["fecha"] - today).days
        emoji = "🔴" if days == 0 else "🟡" if days == 1 else "📅"
        msg += f"{emoji} {v['fecha'].strftime('%d/%m/%Y')} ({days}d) — {v['descripcion']}\n"
    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_uala(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_uala_reminder_state("waiting_tonight", datetime.now(TZ).date())
    last = get_last_uala_date()
    msg = "📱 Mandame la captura de pantalla de tus movimientos de UALA."
    if last: msg += f"\n\n<i>(Solo procesaré movimientos posteriores al {last})</i>"
    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_resetuala(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_last_uala_date("")
    await update.message.reply_text("🔄 Fecha UALA reseteada. La próxima captura procesará todos los movimientos visibles.")

# ── Main ──────────────────────────────────────────────────────────────────────
async def post_init(app) -> None:
    jq = app.job_queue
    jq.run_daily(lambda ctx: reminder_job_standalone(ctx.bot), time=dtime(18, 0, tzinfo=TZ), name="reminder_18")
    jq.run_daily(lambda ctx: reminder_job_standalone(ctx.bot), time=dtime(23, 0, tzinfo=TZ), name="reminder_23")
    jq.run_daily(lambda ctx: uala_reminder_noche_standalone(ctx.bot), time=dtime(21, 0, tzinfo=TZ), name="uala_noche")
    jq.run_daily(lambda ctx: uala_reminder_manana_standalone(ctx.bot), time=dtime(9, 0, tzinfo=TZ), name="uala_manana")
    jq.run_daily(lambda ctx: check_vencimientos_standalone(ctx.bot), time=dtime(9, 15, tzinfo=TZ), name="vencimientos")
    jq.run_repeating(lambda ctx: heartbeat_check(ctx.bot), interval=300, first=60, name="heartbeat")
    logger.info("✅ Jobs scheduled")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cartera", cmd_cartera))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("tenencias", cmd_tenencias))
    app.add_handler(CommandHandler("movimientos", cmd_movimientos))
    app.add_handler(CommandHandler("vencimientos", cmd_vencimientos))
    app.add_handler(CommandHandler("uala", cmd_uala))
    app.add_handler(CommandHandler("resetuala", cmd_resetuala))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_document))
    logger.info("🤖 BOTSTERO iniciado ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
