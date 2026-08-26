import os
import json
import io
import re
import asyncio
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, HTTPException
from PIL import Image
from google import genai
from google.genai import types

app = FastAPI()

# --- 1. CONFIGURACIÓN Y VARIABLES DE ENTORNO (De Render) ---
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ZONA_HORARIA = ZoneInfo("Europe/Madrid")

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODELO = "gemini-3.7-flash"

GENERATION_CONFIG = types.GenerateContentConfig(
    response_mime_type="application/json"
)

# --- Helper con reintentos para llamadas HTTP salientes ---
async def request_con_reintentos(metodo, url, headers, json_payload=None, params=None, intentos=3):
    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client_http:
                respuesta = await client_http.request(
                    metodo, url, headers=headers, json=json_payload, params=params
                )
                return respuesta
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout) as e:
            ultimo_error = e
            print(f"⏳ Intento {intento}/{intentos} falló ({type(e).__name__}), reintentando...")
            await asyncio.sleep(2 * intento)
    print(f"⚠️ Todos los intentos fallaron: {type(ultimo_error).__name__}: {ultimo_error}")
    return None

# --- 2. FUNCIONES DE INTELIGENCIA ARTIFICIAL ---
async def procesar_gasto_con_ia(texto_usuario: str):
    prompt_sistema = """
    Eres un asistente financiero estricto. Analiza el mensaje y extrae los datos del gasto.
    Categorías permitidas: [Comida, Transporte, Ocio, Servicios, Compras, Supermercado].
    Devuelve un JSON con esta estructura exacta: {"monto": 0.0, "categoria": "Categoría", "descripcion": "Descripción breve"}
    """
    contenido = f"{prompt_sistema}\n\nMensaje: {texto_usuario}"
    try:
        respuesta = client.models.generate_content(
            model=MODELO,
            contents=contenido,
            config=GENERATION_CONFIG,
        )
        return json.loads(respuesta.text)
    except Exception as e:
        print(f"Error IA: {type(e).__name__}: {e}")
        return {"monto": 0.0, "categoria": "Error", "descripcion": "Error procesando"}

async def descargar_imagen_whatsapp(media_id: str) -> Image.Image:
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    async with httpx.AsyncClient() as client_http:
        url_metadata = f"https://graph.facebook.com/v20.0/{media_id}"
        respuesta_meta = await client_http.get(url_metadata, headers=headers)
        url_descarga = respuesta_meta.json().get("url")
        if not url_descarga:
            raise Exception("Sin URL de descarga")
        respuesta_imagen = await client_http.get(url_descarga, headers=headers)
        return Image.open(io.BytesIO(respuesta_imagen.content))

async def procesar_recibo_con_ia(imagen: Image.Image):
    prompt_sistema = """
    Eres un asistente financiero. Extrae el total gastado de esta imagen.
    Categorías: [Comida, Transporte, Ocio, Servicios, Compras, Supermercado].
    Devuelve SOLO un JSON: {"monto": 0.0, "categoria": "Categoría", "descripcion": "Nombre comercio"}
    """
    try:
        respuesta = client.models.generate_content(
            model=MODELO,
            contents=[prompt_sistema, imagen],
            config=GENERATION_CONFIG,
        )
        return json.loads(respuesta.text)
    except Exception as e:
        print(f"Error IA imagen: {type(e).__name__}: {e}")
        return {"monto": 0.0, "categoria": "Error", "descripcion": "Error leyendo recibo"}

# --- 3. BASE DE DATOS (SUPABASE) ---
async def guardar_gasto(numero: str, datos: dict):
    url = f"{SUPABASE_URL}/rest/v1/gastos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    payload = {
        "numero": numero,
        "monto": datos.get("monto", 0.0),
        "categoria": datos.get("categoria", "Error"),
        "descripcion": datos.get("descripcion", ""),
    }
    respuesta = await request_con_reintentos("POST", url, headers, json_payload=payload)
    if respuesta is None or respuesta.status_code not in (200, 201):
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        texto = respuesta.text if respuesta else ""
        print(f"⚠️ Error guardando gasto en Supabase: {codigo} - {texto}")

async def obtener_gastos(numero: str, desde: datetime, hasta: datetime):
    url = f"{SUPABASE_URL}/rest/v1/gastos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    params = {
        "numero": f"eq.{numero}",
        "fecha": [f"gte.{desde.isoformat()}", f"lt.{hasta.isoformat()}"],
        "select": "monto,categoria,descripcion,fecha",
        "order": "fecha.asc",
    }
    respuesta = await request_con_reintentos("GET", url, headers, params=params)
    if respuesta is None or respuesta.status_code != 200:
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error consultando gastos en Supabase: {codigo}")
        return []
    return respuesta.json()

# --- 4. DETECCIÓN Y GENERACIÓN DE INFORMES ---
PALABRAS_INFORME = ["resumen", "informe", "reporte"]
PERIODOS = {
    "diario": ["diario", "diarios", "de hoy", "hoy"],
    "semanal": ["semanal", "semanales", "semana"],
    "mensual": ["mensual", "mensuales", "mes"],
    "anual": ["anual", "anuales", "año", "ano"],
}

def detectar_periodo_informe(texto: str):
    texto_lower = texto.lower()
    if not any(p in texto_lower for p in PALABRAS_INFORME):
        return None
    for periodo, palabras_clave in PERIODOS.items():
        if any(palabra in texto_lower for palabra in palabras_clave):
            return periodo
    return None

def calcular_rango_fechas(periodo: str):
    ahora = datetime.now(ZONA_HORARIA)
    hoy_inicio = ahora.replace(hour=0, minute=0, second=0, microsecond=0)

    if periodo == "diario":
        desde = hoy_inicio
    elif periodo == "semanal":
        desde = hoy_inicio - timedelta(days=hoy_inicio.weekday())  # lunes de esta semana
    elif periodo == "mensual":
        desde = hoy_inicio.replace(day=1)
    elif periodo == "anual":
        desde = hoy_inicio.replace(month=1, day=1)
    else:
        desde = hoy_inicio

    hasta = ahora + timedelta(minutes=1)  # incluir el momento actual
    return desde, hasta

def generar_texto_informe(periodo: str, gastos: list):
    etiquetas = {
        "diario": "📅 Resumen diario",
        "semanal": "📅 Resumen semanal",
        "mensual": "📅 Resumen mensual",
        "anual": "📅 Resumen anual",
    }
    titulo = etiquetas.get(periodo, "📅 Resumen")

    if not gastos:
        return f"{titulo}\n\nNo tienes gastos registrados en este período. 🎉"

    total = sum(float(g["monto"]) for g in gastos)

    por_categoria = {}
    for g in gastos:
        cat = g.get("categoria", "Otros")
        por_categoria[cat] = por_categoria.get(cat, 0.0) + float(g["monto"])

    lineas = [titulo, "", f"💰 Total: {total:.2f}", "", "Por categoría:"]
    for cat, monto in sorted(por_categoria.items(), key=lambda x: -x[1]):
        lineas.append(f"  🏷️ {cat}: {monto:.2f}")

    lineas.append("")
    lineas.append(f"Detalle ({len(gastos)} gastos):")
    for g in gastos:
        fecha_str = ""
        try:
            fecha_str = datetime.fromisoformat(g["fecha"]).astimezone(ZONA_HORARIA).strftime("%d/%m %H:%M")
        except Exception:
            pass
        lineas.append(f"  • {fecha_str} — {g['categoria']}: {g['monto']} ({g.get('descripcion', '')})")

    return "\n".join(lineas)

# --- 5. ENVÍO DE RESPUESTA A WHATSAPP ---
async def enviar_mensaje_whatsapp(numero_destino: str, texto: str):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "text",
        "text": {"body": texto},
    }
    respuesta = await request_con_reintentos("POST", url, headers, json_payload=payload)
    if respuesta is not None and respuesta.status_code != 200:
        print(f"⚠️ Error enviando mensaje a WhatsApp: {respuesta.status_code} - {respuesta.text}")
    return respuesta

def formatear_confirmacion(datos: dict) -> str:
    if datos.get("categoria") == "Error":
        return "⚠️ No pude procesar ese gasto. ¿Puedes intentar describirlo de otra forma?"
    return (
        f"✅ Gasto registrado\n"
        f"💰 Monto: {datos.get('monto')}\n"
        f"🏷️ Categoría: {datos.get('categoria')}\n"
        f"📝 {datos.get('descripcion')}"
    )

# --- 6. RUTAS DEL WEBHOOK ---
@app.get("/webhook")
async def verify_webhook(request: Request):
    hub_mode = request.query_params.get("hub.mode")
    hub_challenge = request.query_params.get("hub.challenge")
    hub_verify_token = request.query_params.get("hub.verify_token")

    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        print("¡Webhook verificado por Meta!")
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Token inválido")

@app.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()

    if body.get("object") == "whatsapp_business_account":
        try:
            entry = body["entry"][0]
            changes = entry["changes"][0]
            value = changes["value"]

            if "messages" in value:
                message = value["messages"][0]
                numero_remitente = message["from"]

                try:
                    # Procesar mensajes de texto
                    if message["type"] == "text":
                        texto = message["text"]["body"]
                        periodo = detectar_periodo_informe(texto)

                        if periodo:
                            # El usuario pidió un resumen/informe
                            desde, hasta = calcular_rango_fechas(periodo)
                            gastos = await obtener_gastos(numero_remitente, desde, hasta)
                            informe = generar_texto_informe(periodo, gastos)
                            print(f"📊 Informe {periodo} generado para {numero_remitente} ({len(gastos)} gastos)")
                            await enviar_mensaje_whatsapp(numero_remitente, informe)
                        else:
                            # Es un gasto normal
                            datos = await procesar_gasto_con_ia(texto)
                            print(f"✅ Gasto registrado (Texto): {datos}")
                            if datos.get("categoria") != "Error":
                                await guardar_gasto(numero_remitente, datos)
                            await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion(datos))

                    # Procesar fotos de recibos
                    elif message["type"] == "image":
                        media_id = message["image"]["id"]
                        imagen = await descargar_imagen_whatsapp(media_id)
                        datos = await procesar_recibo_con_ia(imagen)
                        print(f"✅ Gasto registrado (Foto): {datos}")
                        if datos.get("categoria") != "Error":
                            await guardar_gasto(numero_remitente, datos)
                        await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion(datos))

                except Exception as e:
                    print(f"⚠️ Error procesando/respondiendo mensaje: {type(e).__name__}: {e}")

        except (KeyError, IndexError) as e:
            print(f"⚠️ Error parseando webhook: {e}")

    return {"status": "ok"}
