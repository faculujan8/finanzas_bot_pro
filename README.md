# 🤖 Bot de Finanzas Personal

Bot de Telegram para registrar gastos, ingresos, inversiones y dólares en Google Sheets usando Claude AI.

## Deploy en Railway (5 minutos)

### Paso 1 — Subir el código a GitHub

1. Crear cuenta en **github.com** si no tenés
2. Click en **"New repository"**
3. Nombre: `finanzas-bot`
4. Click **"Create repository"**
5. Subir los 3 archivos: `bot.py`, `requirements.txt`, `railway.toml`
   - En la página del repo, click **"uploading an existing file"**
   - Arrastrá los 3 archivos
   - Click **"Commit changes"**

### Paso 2 — Deploy en Railway

1. Entrá a **railway.app**
2. Click **"Start a New Project"**
3. Click **"Deploy from GitHub repo"**
4. Conectá tu cuenta de GitHub y seleccioná `finanzas-bot`
5. Railway detecta automáticamente que es Python ✅

### Paso 3 — Configurar las variables de entorno

En Railway, ir a tu proyecto → **Variables** → agregar estas 4:

| Variable | Valor |
|---|---|
| `TELEGRAM_TOKEN` | Token de tu bot (de @BotFather) |
| `ANTHROPIC_KEY` | Tu API key de Anthropic (sk-ant-...) |
| `SHEETS_ID` | `1OPihtX1Cjn94CJf_xr3Nct5DlxdGxT6ZEX-zV7VJSZw` |
| `GOOGLE_SA_JSON` | El JSON de la cuenta de servicio (ver Paso 4) |

### Paso 4 — Configurar Google Sheets (Service Account)

Para que el bot pueda escribir en tu planilla:

1. Entrá a **console.cloud.google.com**
2. Crear proyecto nuevo (o usar uno existente)
3. Buscar **"Google Sheets API"** → Habilitar
4. Ir a **"Credenciales"** → **"Crear credenciales"** → **"Cuenta de servicio"**
5. Nombre: `finanzas-bot` → Crear
6. Click en la cuenta creada → **"Claves"** → **"Agregar clave"** → **"JSON"**
7. Se descarga un archivo `.json` — ese es el `GOOGLE_SA_JSON`
8. Copiá TODO el contenido del archivo y pegalo como valor de la variable
9. **Importante:** en tu Google Sheets, click **"Compartir"** → pegá el email de la cuenta de servicio (está en el JSON, campo `client_email`) → darle permiso de **Editor**

### Paso 5 — Verificar

Una vez deployado, en Railway vas a ver los logs en tiempo real.
Mandá un mensaje al bot: `gasté 500 en café por uala`
Deberías recibir: `✅ Anotado: -$500 en Cafe (UALA)`

## Variables de entorno requeridas

```
TELEGRAM_TOKEN=tu_token_aqui
ANTHROPIC_KEY=sk-ant-api03-...
SHEETS_ID=1OPihtX1Cjn94CJf_xr3Nct5DlxdGxT6ZEX-zV7VJSZw
GOOGLE_SA_JSON={"type":"service_account","project_id":...}
```

## Funcionalidades

- ✅ Texto y audios (transcripción automática)
- ✅ Gastos e ingresos → hoja MOVIMIENTOS
- ✅ Traspasos entre cuentas (2 filas automático)
- ✅ Compra/gasto en dólares → hoja DOLARES
- ✅ Inversiones (CEDEAR, FCI, Crypto) → hoja INVERSIONES
- ✅ TAONE (negocio) → hoja TAONE
- ✅ Consultas en lenguaje natural ("¿cuánto gasté este mes?")
