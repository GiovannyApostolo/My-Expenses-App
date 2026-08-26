import os
import json
import io
import httpx
from fastapi import FastAPI, Request, HTTPException
from PIL import Image
from google import genai
from google.genai import types

app = FastAPI()

# --- 1. CONFIGURACIÓN Y VARIABLES DE ENTORNO (De Render) ---
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODELO = "gemini-3.7-flash"

GENERATION_CONFIG = types.GenerateContentConfig(
    response_mime_type="application/json"
)

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

# --- 3. ENVÍO DE RESPUESTA A WHATSAPP ---
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
    async with httpx.AsyncClient() as client_http:
        respuesta = await client_http.post(url, headers=headers, json=payload)
        if respuesta.status_code != 200:
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

# --- 4. RUTAS DEL WEBHOOK ---
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

                # Procesar mensajes de texto
                if message["type"] == "text":
                    texto = message["text"]["body"]
                    datos = await procesar_gasto_con_ia(texto)
                    print(f"✅ Gasto registrado (Texto): {datos}")
                    await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion(datos))

                # Procesar fotos de recibos
                elif message["type"] == "image":
                    media_id = message["image"]["id"]
                    imagen = await descargar_imagen_whatsapp(media_id)
                    datos = await procesar_recibo_con_ia(imagen)
                    print(f"✅ Gasto registrado (Foto): {datos}")
                    await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion(datos))

        except (KeyError, IndexError) as e:
            print(f"⚠️ Error parseando webhook: {e}")

    return {"status": "ok"}
