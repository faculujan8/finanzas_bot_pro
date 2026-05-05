import os
import json
import logging
import tempfile
import httpx
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_KEY"]
SHEETS_ID        = os.environ["SHEETS_ID"]
GOOGLE_SA_JSON   = os.environ["GOOGLE_SA_JSON"]   # service account JSON string

SYSTEM_PROMPT = """Sos un asistente financiero personal. Procesá el mensaje del usuario y devolvé SOLO un JSON válido, sin texto antes ni después, sin markdown.

CUENTAS DISPONIBLES: efectivo, uala, galicia, balanz, binance
RUBROS: almacen, auto, cafe, cancha, celular, cochera, combustible, comida, concierto, credito, cumpleaños, dolares, farmacia, futbol, gimnasio, impuestos, indumentaria, inversion, juegos, kiosko, nafta, panaderia, peluqueria, prepaga, regalos, rendimientos, reintegro gastos, salida, salud, seguro, sueldo, suscripciones, taone, transporte, traspaso, varios, verduleria, cena, almuerzo

TIPOS DE OPERACIÓN:

1) GASTO o INGRESO simple:
{"tipo":"movimiento","filas":[{"fecha":"DD/MM/YYYY","monto":-5000,"rubro":"cochera","detalle":"Gasto en cochera","cuenta":"uala"}],"mensaje":"✅ Anotado: -$5.000 en Cochera (UALA)"}

2) TRASPASO entre cuentas — SIEMPRE dos filas:
{"tipo":"movimiento","filas":[{"fecha":"DD/MM/YYYY","monto":-20000,"rubro":"traspaso","detalle":"Traspaso a UALA","cuenta":"efectivo"},{"fecha":"DD/MM/YYYY","monto":20000,"rubro":"traspaso","detalle":"Traspaso desde Efectivo","cuenta":"uala"}],"mensaje":"✅ Traspaso: -$20.000 Efectivo → +$20.000 UALA"}

3) COMPRA DE DÓLARES:
{"tipo":"dolares","filas":[{"fecha":"DD/MM/YYYY","monto":100,"rubro":"compra","detalle":"Compra USD a $1.400","cuenta":"dolares"}],"movimiento_pesos":{"fecha":"DD/MM/YYYY","monto":-140000,"rubro":"dolares","detalle":"Compra 100 USD a $1.400","cuenta":"uala"},"mensaje":"✅ Compra: +100 USD | -$140.000 pesos"}

4) GASTO EN DÓLARES:
{"tipo":"dolares","filas":[{"fecha":"DD/MM/YYYY","monto":-15,"rubro":"gasto","detalle":"Netflix","cuenta":"dolares"}],"mensaje":"✅ Gasto USD: -U$D 15 (Netflix)"}

5) INVERSIÓN CEDEAR/ACCIÓN con todos los datos:
{"tipo":"inversion","instrumento":"CEDEAR","filas":[{"fecha":"DD/MM/YYYY","tipo_instr":"CEDEAR","ticker":"SPY","detalle":"CEDEAR S&P 500","cantidad":10,"precio_compra":85000,"cuenta":"balanz"}],"movimiento_pesos":{"fecha":"DD/MM/YYYY","monto":-850000,"rubro":"inversion","detalle":"Compra 10 nominales SPY","cuenta":"balanz"},"mensaje":"✅ 10 SPY a $85.000 = $850.000 (Balanz)"}

6) INVERSIÓN FCI con todos los datos:
{"tipo":"inversion","instrumento":"FCI","filas":[{"fecha":"DD/MM/YYYY","tipo_instr":"FCI","ticker":"NOMBRE FONDO","detalle":"Fondo Común","cantidad":15234.56,"precio_compra":6.55,"cuenta":"balanz"}],"movimiento_pesos":{"fecha":"DD/MM/YYYY","monto":-100000,"rubro":"inversion","detalle":"Suscripción FCI","cuenta":"balanz"},"mensaje":"✅ FCI: $100.000 → 15.234 cuotapartes"}

7) INVERSIÓN CRYPTO con todos los datos:
{"tipo":"inversion","instrumento":"CRYPTO","filas":[{"fecha":"DD/MM/YYYY","tipo_instr":"CRYPTO","ticker":"BTC","detalle":"Bitcoin","cantidad":0.005,"precio_compra":67000,"cuenta":"binance"}],"movimiento_pesos":{"fecha":"DD/MM/YYYY","monto":-100000,"rubro":"inversion","detalle":"Compra BTC","cuenta":"binance"},"mensaje":"✅ Crypto: 0.005 BTC a U$D 67.000"}

8) FALTA INFO PARA INVERSIÓN:
{"tipo":"pedir_datos","instrumento":"CEDEAR","mensaje":"Para anotar la inversión necesito:\n📌 ¿Qué ticker? (ej: SPY, AAPL, GGAL)\n📌 ¿Cuántos nominales compraste?\n📌 ¿A qué precio por nominal?\n📌 ¿En qué cuenta? (balanz / uala)"}

9) CONSULTA sobre finanzas:
{"tipo":"consulta","mensaje":"Buscando info 🔍"}

10) TAONE (negocio gorras/pilusos/matriz/Sr Juan):
{"tipo":"taone","filas":[{"fecha":"DD/MM/YYYY","concepto":"GORRAS x30","monto":-180000,"detalle":"30 gorras a $6.000"}],"mensaje":"✅ TAONE: -$180.000"}

REGLAS:
1. Respondé SOLO JSON válido, sin texto ni markdown.
2. Fecha DD/MM/YYYY. Sin fecha → usá la de hoy.
3. Montos sin formato: 50000 no '50.000'.
4. k/K = miles: 50k = 50000.
5. Gastos = negativo. Ingresos = positivo.
6. Traspaso → SIEMPRE dos filas.
7. Inversión sin datos completos → pedir_datos.
8. TAONE: gorras, pilusos, matriz, Sr Juan."""

CONSULTA_SYSTEM = """Sos un asistente financiero personal. Tenés acceso a los movimientos del usuario en formato CSV.
Respondé preguntas sobre sus finanzas de forma clara y concisa, en español rioplatense.
Usá emojis para hacer la respuesta más visual.
Filtrá por fecha o rubro según lo que pidan.
Siempre mostrá totales.
Solo texto plano con emojis, sin markdown complejo."""


async def call_claude(system: str, user_message: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user_message}],
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe audio using Claude's vision/audio capability via base64."""
    import base64
    audio_b64 = base64.standard_b64encode(audio_bytes).decode()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 512,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Transcribí exactamente lo que dice este audio. Devolvé solo el texto transcripto, sin explicaciones."
                        },
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": audio_b64
                            }
                        }
                    ]
                }]
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


def get_sheets_service():
    """Get Google Sheets service using service account."""
    import google.auth
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_info = json.loads(GOOGLE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def append_rows(sheet_name: str, rows: list[list]):
    """Append rows to a Google Sheet."""
    service = get_sheets_service()
    service.spreadsheets().values().append(
        spreadsheetId=SHEETS_ID,
        range=f"{sheet_name}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()


def read_sheet(sheet_name: str, range_: str = "A:E") -> list[list]:
    """Read rows from a Google Sheet."""
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEETS_ID,
        range=f"{sheet_name}!{range_}"
    ).execute()
    return result.get("values", [])


async def handle_result(data: dict, update: Update):
    """Process the Claude JSON response and write to Sheets."""
    tipo = data.get("tipo")

    if tipo == "movimiento":
        rows = []
        for fila in data.get("filas", []):
            rows.append([
                fila.get("fecha", ""),
                fila.get("monto", ""),
                fila.get("rubro", ""),
                fila.get("detalle", ""),
                fila.get("cuenta", ""),
            ])
        if rows:
            append_rows("MOVIMIENTOS", rows)
        await update.message.reply_text(data.get("mensaje", "✅ Guardado"))

    elif tipo == "dolares":
        rows = []
        for fila in data.get("filas", []):
            rows.append([
                fila.get("fecha", ""),
                fila.get("monto", ""),
                fila.get("rubro", ""),
                fila.get("detalle", ""),
                fila.get("cuenta", ""),
            ])
        if rows:
            append_rows("DOLARES", rows)
        # Also save pesos movement if present
        mp = data.get("movimiento_pesos")
        if mp:
            append_rows("MOVIMIENTOS", [[
                mp.get("fecha", ""),
                mp.get("monto", ""),
                mp.get("rubro", ""),
                mp.get("detalle", ""),
                mp.get("cuenta", ""),
            ]])
        await update.message.reply_text(data.get("mensaje", "✅ Guardado en DÓLARES"))

    elif tipo == "inversion":
        rows = []
        for fila in data.get("filas", []):
            rows.append([
                fila.get("fecha", ""),
                fila.get("tipo_instr", ""),
                fila.get("ticker", ""),
                fila.get("detalle", ""),
                fila.get("cantidad", ""),
                fila.get("precio_compra", ""),
                "", "", "", "", "", "", "",  # cols G-M empty (formulas)
                fila.get("cuenta", ""),
            ])
        if rows:
            append_rows("INVERSIONES", rows)
        # Also save pesos movement
        mp = data.get("movimiento_pesos")
        if mp:
            append_rows("MOVIMIENTOS", [[
                mp.get("fecha", ""),
                mp.get("monto", ""),
                mp.get("rubro", ""),
                mp.get("detalle", ""),
                mp.get("cuenta", ""),
            ]])
        await update.message.reply_text(data.get("mensaje", "✅ Inversión guardada"))

    elif tipo == "taone":
        rows = []
        for fila in data.get("filas", []):
            rows.append([
                fila.get("fecha", ""),
                fila.get("concepto", ""),
                fila.get("monto", ""),
                "",  # saldo acum — formula en sheets
                fila.get("detalle", ""),
            ])
        if rows:
            append_rows("TAONE", rows)
        await update.message.reply_text(data.get("mensaje", "✅ TAONE guardado"))

    elif tipo == "pedir_datos":
        await update.message.reply_text(data.get("mensaje", "Necesito más datos"))

    elif tipo == "consulta":
        # Read movements and ask Claude to answer
        rows = read_sheet("MOVIMIENTOS", "A:E")
        csv_text = "\n".join([",".join(str(c) for c in row) for row in rows[:500]])
        from datetime import datetime
        today = datetime.now().strftime("%d/%m/%Y")
        answer = await call_claude(
            CONSULTA_SYSTEM,
            f"Hoy es {today}.\n\nDATOS MOVIMIENTOS (CSV):\nfecha,monto,rubro,detalle,cuenta\n{csv_text}\n\nPREGUNTA: {update.message.text}"
        )
        await update.message.reply_text(answer)

    else:
        await update.message.reply_text("❓ No entendí el mensaje. Intentá de nuevo.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages."""
    from datetime import datetime
    today = datetime.now().strftime("%d/%m/%Y")
    text = update.message.text

    await update.message.reply_text("⏳ Procesando...")

    try:
        raw = await call_claude(
            SYSTEM_PROMPT,
            f"Hoy es {today}. Mensaje: {text}"
        )
        # Clean possible markdown fences
        raw = raw.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        await handle_result(data, update)
    except json.JSONDecodeError:
        logger.error(f"Claude returned non-JSON: {raw}")
        await update.message.reply_text("❌ Error procesando el mensaje. Intentá de nuevo.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages."""
    from datetime import datetime
    today = datetime.now().strftime("%d/%m/%Y")

    await update.message.reply_text("🎤 Transcribiendo audio...")

    try:
        # Download audio
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await voice_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                audio_bytes = f.read()

        # Transcribe
        transcription = await transcribe_audio(audio_bytes, "audio/ogg")
        logger.info(f"Transcription: {transcription}")

        await update.message.reply_text(f"📝 Entendí: _{transcription}_", parse_mode="Markdown")

        # Process as text
        raw = await call_claude(
            SYSTEM_PROMPT,
            f"Hoy es {today}. Mensaje (transcripción de audio): {transcription}"
        )
        raw = raw.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        await handle_result(data, update)

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text(f"❌ Error procesando audio: {str(e)}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    logger.info("Bot started ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
