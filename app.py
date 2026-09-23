import os
import json
import io
import re
import asyncio
import unicodedata
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from PIL import Image
from google import genai
from google.genai import types

app = FastAPI()

# --- Deduplicación de mensajes (Meta reintenta el mismo mensaje si tardamos en responder) ---
IDS_PROCESADOS = set()
ORDEN_IDS = []
MAX_IDS_GUARDADOS = 500

def ya_procesado(message_id: str) -> bool:
    if message_id in IDS_PROCESADOS:
        return True
    IDS_PROCESADOS.add(message_id)
    ORDEN_IDS.append(message_id)
    if len(ORDEN_IDS) > MAX_IDS_GUARDADOS:
        viejo = ORDEN_IDS.pop(0)
        IDS_PROCESADOS.discard(viejo)
    return False

# --- 1. CONFIGURACIÓN Y VARIABLES DE ENTORNO (De Render) ---
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ZONA_HORARIA = ZoneInfo("Europe/Madrid")

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODELO = "gemini-3.7-flash"
MODELO_RESPALDO = "gemini-3.6-flash"

CATEGORIAS = [
    "Taberna",
    "Provisiones",
    "Movilidad y monturas",
    "Servicios activos",
    "Refugio y suministros",
    "Adquisiciones",
    "Ofrendas",
    "Compañero",
    "Salud y estamina",
    "Sabiduría",
    "Tributos y finanzas",
    "Fugas de Oro",
    "Vicios",
    "Miscelánea",
]
CATEGORIAS_TEXTO = ", ".join(CATEGORIAS)
ACLARACION_CATEGORIAS = (
    "Distingue bien entre estas categorías que se prestan a confusión:\n"
    "- 'Taberna': comer/beber fuera de casa (restaurantes, bares, cafeterías, "
    "comida a domicilio) y entretenimiento puntual (cine, conciertos, videojuegos, salidas).\n"
    "- 'Servicios activos': CUALQUIER pago recurrente/periódico, sea de entretenimiento o no "
    "(Netflix, Spotify, Tidal, iCloud/Apple Cloud, Google Cloud, ChatGPT Plus, Suno, hosting, "
    "dominios, gimnasio con cuota mensual, etc.).\n"
    "- 'Fugas de Oro': gastos pequeños, impulsivos y cotidianos hechos en la calle o al paso "
    "(un refresco, un café rápido, chicles, prensa, una chocolatina), distintos de una comida "
    "completa en restaurante (que va en 'Taberna') o de la compra grande de "
    "supermercado.\n"
    "_ 'Vicios': unicamente gastos en tabaco y cigarros.\n"
    "- 'Ofrendas': regalos para otras personas (cumpleaños, Navidad, aniversarios, etc.), "
    "distintos a otro tipo de compras (que va en 'Adquisiciones')\n"
    "_ 'Compañero': compras para mascotas como pienso, juguetes para perros, juguetes para conejos, etc"
)

CATEGORIA_EMOJIS = {
    "Taberna": "🎪",
    "Provisiones": "🥖",
    "Movilidad y monturas": "🐫",
    "Servicios activos": "🔮",
    "Refugio y suministros": "🏰",
    "Adquisiciones": "🏺",
    "Ofrendas": "💎",
    "Compañero": "🐴",
    "Salud y estamina": "🍵",
    "Sabiduría": "📖",
    "Tributos y finanzas": "🪙",
    "Fugas de Oro": "🥷🏻",
    "Vicios": "🍂",
    "Miscelánea": "🧺",
}

# NOTA: renombré "Regalo" -> "Ofrenda de aliados" y "Miscelánea" -> "Objeto encontrado" para que
# coincidan con las claves reales de INGRESO_EMOJIS (en el reskin original esas dos
# categorías se quedaban sin emoji propio y caían siempre en "🧺"). "Oro recuperado"
# queda como entrada de reserva sin usar, igual que "Reintegros" en el original.
CATEGORIAS_INGRESO = ["Botín principal", "Botín de mercenario", "Recompensas extra", "Ofrenda de aliados", "Objeto encontrado"]
CATEGORIAS_INGRESO_TEXTO = ", ".join(CATEGORIAS_INGRESO)
INGRESO_EMOJIS = {
    "Botín principal": "🪎",
    "Contratos de mercenario": "🗡️",
    "Recompensas extra": "✨",
    "Ofrenda de aliados": "💎",
    "Objeto encontrado": "🧺",
    "Oro recuperado": "🫱🏼‍🫲🏽",
}

INGRESO_EMOJIS_NORM = {normalizar(k): v for k, v in INGRESO_EMOJIS.items()}

def emoji_ingreso(categoria: str) -> str:
    """Busca el emoji de una categoría de ingreso de forma tolerante a tildes/mayúsculas,
    para no perder el emoji por variaciones en cómo quedó guardado el texto."""
    return INGRESO_EMOJIS_NORM.get(normalizar(categoria or ""), "❓")

def formatear_fecha_hora_actual():
    ahora = datetime.now(ZONA_HORARIA)
    fecha_str = f"{ahora.day} de {MESES_ES_ABREV[ahora.month]} de {ahora.year}"
    hora_str = ahora.strftime("%H:%M")
    return fecha_str, hora_str

MONEDA_SIMBOLO = "€"

def formatear_monto(monto) -> str:
    """Formato español: 1.500,00 € (punto para miles, coma para decimales,
    espacio de no separación antes del símbolo para que no se parta la línea)."""
    try:
        valor = float(monto)
    except (TypeError, ValueError):
        return f"{monto}\u00a0{MONEDA_SIMBOLO}"
    texto = f"{valor:,.2f}"  # 1,500.00
    texto = texto.replace(",", "X").replace(".", ",").replace("X", ".")  # 1.500,00
    return f"{texto}\u00a0{MONEDA_SIMBOLO}"

# Mismo formato en todas partes: se mantiene el nombre para no tocar el resto del código
formatear_monto_corto = formatear_monto

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
def _es_error_saturacion(e) -> bool:
    texto = str(e)
    return "503" in texto or "UNAVAILABLE" in texto or "429" in texto

async def _generar_con_modelo(modelo: str, contenido, intentos: int):
    """Intenta generar contenido con un modelo concreto, reintentando en caso de saturación
    (503/UNAVAILABLE/429). Devuelve el JSON parseado si tiene éxito, o lanza la última
    excepción si se agotan los intentos."""
    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            respuesta = client.models.generate_content(
                model=modelo,
                contents=contenido,
                config=GENERATION_CONFIG,
            )
            return json.loads(respuesta.text)
        except Exception as e:
            ultimo_error = e
            if _es_error_saturacion(e) and intento < intentos:
                print(f"⏳ {modelo} saturado, intento {intento}/{intentos}, reintentando...")
                await asyncio.sleep(3 * intento)
                continue
            break
    raise ultimo_error

async def generar_con_reintentos(contenido, intentos=3):
    ultimo_error = None
    for modelo_actual in (MODELO, MODELO_RESPALDO):
        try:
            return await _generar_con_modelo(modelo_actual, contenido, intentos)
        except Exception as e:
            ultimo_error = e
            if modelo_actual == MODELO and _es_error_saturacion(e):
                print(f"🔄 {MODELO} saturado tras {intentos} intentos, probando modelo de respaldo {MODELO_RESPALDO}...")
                continue
            break

    print(f"Error IA: {type(ultimo_error).__name__}: {ultimo_error}")
    return {"monto": 0.0, "categoria": "Error", "descripcion": "Error procesando"}

async def procesar_gasto_con_ia(texto_usuario: str):
    prompt_sistema = f"""
    Eres un asistente financiero estricto. Analiza el mensaje y extrae los datos del gasto.
    Categorías permitidas: [{CATEGORIAS_TEXTO}].
    {ACLARACION_CATEGORIAS}
    Devuelve un JSON con esta estructura exacta: {{"monto": 0.0, "categoria": "Categoría", "descripcion": "Descripción breve"}}
    """
    contenido = f"{prompt_sistema}\n\nMensaje: {texto_usuario}"
    return await generar_con_reintentos(contenido)

async def procesar_ingreso_con_ia(texto_usuario: str):
    prompt_sistema = f"""
    Eres un asistente financiero. Analiza el mensaje y extrae los datos de un INGRESO de dinero
    (no un gasto). Categorías permitidas: [{CATEGORIAS_INGRESO_TEXTO}].
    Devuelve un JSON con esta estructura exacta: {{"monto": 0.0, "categoria": "Categoría", "descripcion": "Descripción breve"}}
    """
    contenido = f"{prompt_sistema}\n\nMensaje: {texto_usuario}"
    return await generar_con_reintentos(contenido)

async def procesar_eliminacion_con_ia(texto_usuario: str) -> dict:
    """Se usa cuando detectamos que el usuario quiere eliminar/borrar/cancelar una transacción
    ya registrada. Extrae la descripción que identifica el movimiento (comercio, persona,
    concepto) y, si es posible, si se trata de un gasto o de un ingreso."""
    prompt_sistema = f"""
    Eres un asistente financiero. El usuario quiere ELIMINAR/BORRAR/CANCELAR/ANULAR un gasto
    o un ingreso que ya registró antes, identificándolo por su descripción, comercio, persona
    o concepto (ej. "elimina el gasto de Mercadona", "borra el ingreso de la nómina",
    "cancela lo de Netflix").

    Devuelve un JSON con esta estructura exacta:
    {{"descripcion_buscada": "texto que identifica la transacción", "tipo": "gasto" o "ingreso" o "desconocido"}}

    Reglas:
    - Si el usuario NO da una descripción concreta y solo dice algo como "el último gasto",
      "el último movimiento" o "lo último que puse", deja "descripcion_buscada" como "".
    - Usa "tipo": "gasto" si queda claro que es un gasto, "ingreso" si queda claro que es un
      ingreso/botín, o "desconocido" si no se puede saber con certeza.
    """
    contenido = f"{prompt_sistema}\n\nMensaje: {texto_usuario}"
    resultado = await generar_con_reintentos(contenido)
    if not isinstance(resultado, dict):
        return {"descripcion_buscada": "", "tipo": "desconocido"}
    return resultado

async def extraer_categoria_con_ia(texto_usuario: str, categorias_validas: list):
    """Se usa cuando el usuario responde a la confirmación de una transacción con un texto
    libre que debería indicar la categoría correcta (ej. "esto es del super", "mejor ponlo en
    ocio"), y ni la coincidencia exacta ni los alias conocidos lo resolvieron."""
    lista_texto = ", ".join(categorias_validas)
    prompt_sistema = f"""
    El usuario está respondiendo a la confirmación de una transacción para indicar a qué
    categoría pertenece. Dado su mensaje, determina a cuál de estas categorías se refiere:
    [{lista_texto}].
    Devuelve un JSON con esta estructura exacta: {{"categoria": "una de la lista, o '' si su
    mensaje no parece referirse a ninguna categoría"}}
    """
    contenido = f"{prompt_sistema}\n\nMensaje: {texto_usuario}"
    resultado = await generar_con_reintentos(contenido)
    if not isinstance(resultado, dict):
        return None
    return resolver_categoria(resultado.get("categoria", ""), categorias_validas)

async def clasificar_mensaje_libre_con_ia(texto_usuario: str) -> dict:
    """Se usa solo cuando ninguna palabra clave (informe/porcentaje/balance/exportar/ingreso/
    eliminar) coincidió con el mensaje. Decide si realmente es un gasto, un ingreso, una
    corrección de categoría, una eliminación, o algo fuera del alcance del bot, en vez de
    asumir por defecto que es un gasto."""
    prompt_sistema = f"""
    Eres el clasificador de intención de un bot de finanzas personales por WhatsApp. El bot
    SOLO puede: registrar gastos, registrar ingresos, corregir la categoría de un gasto ya
    registrado, ELIMINAR un gasto o ingreso ya registrado, generar resúmenes/informes,
    calcular porcentajes y balance, y exportar datos a Excel. No hace nada más (no agenda,
    no da consejos generales, no chatea de temas ajenos a las finanzas personales del usuario).

    Analiza el mensaje del usuario:
    - Si describe un GASTO real (algo que compró, pagó o gastó, con o sin monto explícito),
      responde: {{"intencion": "gasto"}}
    - Si describe un INGRESO real (dinero que recibió: Botín principal, Botín de mercenario, regalo, venta, reintegro, etc.),
      responde: {{"intencion": "ingreso"}}
    - Si pide CAMBIAR/CORREGIR la categoría de un gasto que ya registró antes (identificándolo
      por su descripción o nombre, ej. "pon el gasto de Jennifer González en Refugio y suministros"),
      responde: {{"intencion": "corregir_categoria", "descripcion_buscada": "el texto que
      identifica el gasto original", "categoria_nueva": "una de [{CATEGORIAS_TEXTO}]"}}
    - Si pide ELIMINAR/BORRAR/CANCELAR/ANULAR un gasto o ingreso ya registrado (identificándolo
      por su descripción, o "el último gasto/ingreso" si no da descripción concreta), responde:
      {{"intencion": "eliminar_transaccion", "descripcion_buscada": "el texto que identifica
      la transacción (o '' si dijo 'el último')", "tipo": "gasto" o "ingreso" o "desconocido"}}
    - Si es cualquier otra cosa (saludo, pregunta general, petición fuera del alcance del bot,
      o un mensaje ambiguo sin relación clara a lo anterior), responde:
      {{"intencion": "no_soportado", "respuesta": "..."}} donde "respuesta" es 🧌 seguido de un mensaje breve,
      amable, en español y con un todo de RPG de fantasia oscura medieval, explicando que no puedes ayudar con eso, y recordando brevemente
      qué sí puedes hacer (registrar gastos e ingresos por texto o foto, corregir categorías,
      eliminar registros, generar resúmenes, calcular porcentajes/balance, y exportar a Excel).

    Devuelve SOLO el JSON correspondiente.
    """
    contenido = f"{prompt_sistema}\n\nMensaje: {texto_usuario}"
    resultado = await generar_con_reintentos(contenido)
    if not isinstance(resultado, dict) or "intencion" not in resultado:
        return {"intencion": "gasto"}  # último recurso: comportamiento anterior
    return resultado

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

async def procesar_imagen_transacciones_con_ia(imagen: Image.Image):
    prompt_sistema = f"""
    Eres un asistente financiero. La imagen puede ser UN SOLO recibo/ticket de compra,
    o una captura de pantalla de una app bancaria con VARIOS movimientos/transacciones.

    Identifica TODAS las transacciones visibles (una o varias). Para cada una determina:
    - "tipo": "Gasto" si el monto sale de la cuenta (aparece con signo negativo "-", o es una
      compra/pago). "Ingreso" si el monto entra a la cuenta (aparece con signo positivo "+",
      o en color verde).
    - "monto": el valor absoluto del monto, SIN el signo.
    - "categoria": si tipo es "Gasto", elige una de [{CATEGORIAS_TEXTO}].
      Si tipo es "Ingreso", elige una de [{CATEGORIAS_INGRESO_TEXTO}].
    - "descripcion": el nombre del comercio, persona o concepto, tal como aparece.

    {ACLARACION_CATEGORIAS}
    Ignora movimientos que sean transferencias internas entre cuentas propias del mismo banco
    (ej. "Move", "Exchange", "Conversión") si logras identificarlos como tales.

    Devuelve SOLO un JSON con esta estructura exacta:
    {{"transacciones": [{{"tipo": "Gasto", "monto": 0.0, "categoria": "Categoría", "descripcion": "Descripción"}}]}}
    """
    resultado = await generar_con_reintentos([prompt_sistema, imagen])
    transacciones = resultado.get("transacciones") if isinstance(resultado, dict) else None
    if not transacciones or not isinstance(transacciones, list):
        return []
    return transacciones

# --- 3. BASE DE DATOS (SUPABASE) ---
async def guardar_gasto(numero: str, datos: dict):
    """Devuelve el registro insertado (incluye 'id') o None si falló. Necesitamos el id para
    poder asociarle después el wamid de la confirmación y así permitir editar/eliminar
    respondiendo a ese mensaje de WhatsApp."""
    url = f"{SUPABASE_URL}/rest/v1/gastos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
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
        return None
    filas = respuesta.json()
    return filas[0] if filas else None

async def buscar_gastos_por_descripcion(numero: str, descripcion_buscada: str, limite: int = 5):
    url = f"{SUPABASE_URL}/rest/v1/gastos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    params = {
        "numero": f"eq.{numero}",
        "descripcion": f"ilike.*{descripcion_buscada}*",
        "select": "id,monto,categoria,descripcion,fecha",
        "order": "fecha.desc",
        "limit": str(limite),
    }
    respuesta = await request_con_reintentos("GET", url, headers, params=params)
    if respuesta is None or respuesta.status_code != 200:
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error buscando gasto en Supabase: {codigo}")
        return []
    return respuesta.json()

async def buscar_ingresos_por_descripcion(numero: str, descripcion_buscada: str, limite: int = 5):
    url = f"{SUPABASE_URL}/rest/v1/ingresos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    params = {
        "numero": f"eq.{numero}",
        "descripcion": f"ilike.*{descripcion_buscada}*",
        "select": "id,monto,categoria,descripcion,fecha",
        "order": "fecha.desc",
        "limit": str(limite),
    }
    respuesta = await request_con_reintentos("GET", url, headers, params=params)
    if respuesta is None or respuesta.status_code != 200:
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error buscando ingreso en Supabase: {codigo}")
        return []
    return respuesta.json()

async def buscar_gasto_por_wamid(numero: str, wamid: str):
    """Encuentra el gasto asociado al mensaje de confirmación de WhatsApp con ese wamid,
    para cuando el usuario responde ('desliza') sobre ese mensaje."""
    url = f"{SUPABASE_URL}/rest/v1/gastos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    params = {
        "numero": f"eq.{numero}",
        "wamid": f"eq.{wamid}",
        "select": "id,monto,categoria,descripcion",
        "limit": "1",
    }
    respuesta = await request_con_reintentos("GET", url, headers, params=params)
    if respuesta is None or respuesta.status_code != 200:
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error buscando gasto por wamid en Supabase: {codigo}")
        return None
    filas = respuesta.json()
    return filas[0] if filas else None

async def buscar_ingreso_por_wamid(numero: str, wamid: str):
    url = f"{SUPABASE_URL}/rest/v1/ingresos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    params = {
        "numero": f"eq.{numero}",
        "wamid": f"eq.{wamid}",
        "select": "id,monto,categoria,descripcion",
        "limit": "1",
    }
    respuesta = await request_con_reintentos("GET", url, headers, params=params)
    if respuesta is None or respuesta.status_code != 200:
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error buscando ingreso por wamid en Supabase: {codigo}")
        return None
    filas = respuesta.json()
    return filas[0] if filas else None

async def asociar_wamid_gasto(gasto_id: str, wamid: str) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/gastos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    params = {"id": f"eq.{gasto_id}"}
    respuesta = await request_con_reintentos("PATCH", url, headers, json_payload={"wamid": wamid}, params=params)
    return respuesta is not None and respuesta.status_code in (200, 204)

async def asociar_wamid_ingreso(ingreso_id: str, wamid: str) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/ingresos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    params = {"id": f"eq.{ingreso_id}"}
    respuesta = await request_con_reintentos("PATCH", url, headers, json_payload={"wamid": wamid}, params=params)
    return respuesta is not None and respuesta.status_code in (200, 204)

async def actualizar_categoria_gasto(gasto_id: str, categoria_nueva: str) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/gastos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    params = {"id": f"eq.{gasto_id}"}
    payload = {"categoria": categoria_nueva}
    respuesta = await request_con_reintentos("PATCH", url, headers, json_payload=payload, params=params)
    if respuesta is None or respuesta.status_code not in (200, 204):
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error actualizando categoría en Supabase: {codigo}")
        return False
    return True

async def actualizar_categoria_ingreso(ingreso_id: str, categoria_nueva: str) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/ingresos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    params = {"id": f"eq.{ingreso_id}"}
    payload = {"categoria": categoria_nueva}
    respuesta = await request_con_reintentos("PATCH", url, headers, json_payload=payload, params=params)
    if respuesta is None or respuesta.status_code not in (200, 204):
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error actualizando categoría de ingreso en Supabase: {codigo}")
        return False
    return True

async def eliminar_gasto(gasto_id: str) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/gastos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Prefer": "return=minimal",
    }
    params = {"id": f"eq.{gasto_id}"}
    respuesta = await request_con_reintentos("DELETE", url, headers, params=params)
    if respuesta is None or respuesta.status_code not in (200, 204):
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error eliminando gasto en Supabase: {codigo}")
        return False
    return True

async def eliminar_ingreso(ingreso_id: str) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/ingresos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Prefer": "return=minimal",
    }
    params = {"id": f"eq.{ingreso_id}"}
    respuesta = await request_con_reintentos("DELETE", url, headers, params=params)
    if respuesta is None or respuesta.status_code not in (200, 204):
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error eliminando ingreso en Supabase: {codigo}")
        return False
    return True

def resolver_categoria(categoria_texto: str, categorias_validas: list):
    if not categoria_texto:
        return None
    for c in categorias_validas:
        if c.strip().lower() == categoria_texto.strip().lower():
            return c
    return None

async def obtener_gastos(numero: str, desde: datetime, hasta: datetime, categoria: str = None):
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
    if categoria:
        params["categoria"] = f"eq.{categoria}"
    respuesta = await request_con_reintentos("GET", url, headers, params=params)
    if respuesta is None or respuesta.status_code != 200:
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error consultando gastos en Supabase: {codigo}")
        return []
    return respuesta.json()

async def guardar_ingreso(numero: str, datos: dict):
    """Devuelve el registro insertado (incluye 'id') o None si falló."""
    url = f"{SUPABASE_URL}/rest/v1/ingresos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    payload = {
        "numero": numero,
        "monto": datos.get("monto", 0.0),
        "categoria": datos.get("categoria", "Suerte"),
        "descripcion": datos.get("descripcion", ""),
    }
    respuesta = await request_con_reintentos("POST", url, headers, json_payload=payload)
    if respuesta is None or respuesta.status_code not in (200, 201):
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        texto = respuesta.text if respuesta else ""
        print(f"⚠️ Error guardando ingreso en Supabase: {codigo} - {texto}")
        return None
    filas = respuesta.json()
    return filas[0] if filas else None

async def obtener_ingresos(numero: str, desde: datetime, hasta: datetime, categoria: str = None):
    url = f"{SUPABASE_URL}/rest/v1/ingresos"
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
    if categoria:
        params["categoria"] = f"eq.{categoria}"
    respuesta = await request_con_reintentos("GET", url, headers, params=params)
    if respuesta is None or respuesta.status_code != 200:
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error consultando ingresos en Supabase: {codigo}")
        return []
    return respuesta.json()

async def obtener_ultimo_botin_principal(numero: str):
    """El registro de ingreso de categoría 'Botín principal' más reciente, que marca el inicio
    del ciclo actual. None si el usuario nunca ha registrado uno."""
    url = f"{SUPABASE_URL}/rest/v1/ingresos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    params = {
        "numero": f"eq.{numero}",
        "categoria": "eq.Botín principal",
        "select": "id,fecha",
        "order": "fecha.desc",
        "limit": "1",
    }
    respuesta = await request_con_reintentos("GET", url, headers, params=params)
    if respuesta is None or respuesta.status_code != 200:
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error consultando el último Botín principal en Supabase: {codigo}")
        return None
    filas = respuesta.json()
    return filas[0] if filas else None

async def guardar_ahorro(numero: str, monto: float, ciclo_desde: datetime, ciclo_hasta: datetime):
    """Guarda el balance de un ciclo cerrado (positivo = se sumó a la fortuna, negativo = se
    restó) en la tabla 'ahorros', separada de gastos/ingresos."""
    url = f"{SUPABASE_URL}/rest/v1/ahorros"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    payload = {
        "numero": numero,
        "monto": monto,
        "ciclo_desde": ciclo_desde.isoformat(),
        "ciclo_hasta": ciclo_hasta.isoformat(),
    }
    respuesta = await request_con_reintentos("POST", url, headers, json_payload=payload)
    if respuesta is None or respuesta.status_code not in (200, 201):
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        texto = respuesta.text if respuesta else ""
        print(f"⚠️ Error guardando ahorro en Supabase: {codigo} - {texto}")

# --- 4. DETECCIÓN Y GENERACIÓN DE INFORMES ---
def normalizar(texto: str) -> str:
    texto = texto.lower()
    texto = "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")
    return texto

PALABRAS_INFORME = [
    "resumen", "informe", "reporte", "detalle", "detallame", "detalla", "muestrame",
    "cuanto gaste", "cuanto he gastado",
    # nuevas frases tipo pregunta
    "cuales han sido", "cuales fueron", "cual ha sido", "cuales son",
    "que gaste", "que he gastado", "cuanto llevo gastado",
    "listame", "dame el detalle", "ensename",
]
PERIODOS = {
    "diario": ["diario", "diarios", "de hoy", "hoy"],
    "semanal": ["semanal", "semanales", "semana"],
    "mensual": ["mensual", "mensuales", "mes"],
    "trimestral": ["trimestral", "trimestrales", "trimestre"],
    "anual": ["anual", "anuales", "ano"],
}

# Alias -> nombre exacto de categoría (debe coincidir con CATEGORIAS)
ALIASES_CATEGORIA = {
    "restaurantes": "Taberna", "restaurante": "Taberna",
    "restauracion": "Taberna", "ocio": "Taberna",
    "supermercado": "Provisiones", "super": "Provisiones",
    "transporte": "Movilidad y monturas", "vehiculo": "Movilidad y monturas", "gasolina": "Movilidad y monturas",
    "suscripciones": "Servicios activos", "suscripcion": "Servicios activos", "subscripciones": "Servicios activos", "subscripcion": "Servicios activos",
    "vivienda": "Refugio y suministros", "servicios": "Refugio y suministros", "alquiler": "Refugio y suministros",
    "suministros": "Refugio y suministros", "vivienda y suministros": "Refugio y suministros",
    "compras": "Adquisiciones",
    "regalos": "Ofrendas", "regalo": "Ofrendas", "cumpleanos": "Ofrendas",
    "salud": "Salud y estamina", "cuidado personal": "Salud y estamina",
    "educacion": "Sabiduría",
    "finanzas": "Tributos y finanzas",
    "hormigas": "Fugas de Oro", "hormiga": "Fugas de Oro",
    "tabaco": "Vicios", "cigarros": "Vicios",
    "otros": "Miscelánea",
}

def _coincide_periodo(texto_norm: str, palabras_clave: list) -> bool:
    return any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in palabras_clave)

def detectar_periodo_informe(texto: str):
    texto_norm = normalizar(texto)
    if not any(p in texto_norm for p in PALABRAS_INFORME):
        return None
    for periodo, palabras_clave in PERIODOS.items():
        if _coincide_periodo(texto_norm, palabras_clave):
            return periodo
    return None

PALABRAS_PORCENTAJE = ["porcentaje", "%", "que parte de mis gastos", "que fraccion", "distribucion", "reparto"]

def detectar_solicitud_porcentaje(texto: str) -> bool:
    texto_norm = normalizar(texto)
    return any(p in texto_norm for p in PALABRAS_PORCENTAJE)

def detectar_periodo_generico(texto: str, default: str = "mensual") -> str:
    texto_norm = normalizar(texto)
    for periodo, palabras_clave in PERIODOS.items():
        if _coincide_periodo(texto_norm, palabras_clave):
            return periodo
    return default

PALABRAS_REFERENCIA_INGRESOS = ["ingreso", "ingresos"]

def detectar_referencia_ingresos(texto: str) -> bool:
    texto_norm = normalizar(texto)
    return any(re.search(rf"\b{p}\b", texto_norm) for p in PALABRAS_REFERENCIA_INGRESOS)

PALABRAS_BALANCE = ["balance", "saldo", "cuanto me queda", "cuanto tengo disponible", "cuanto dinero me queda"]

def detectar_solicitud_balance(texto: str) -> bool:
    texto_norm = normalizar(texto)
    return any(p in texto_norm for p in PALABRAS_BALANCE)

PALABRAS_INGRESO = ["ingreso", "ingresos", "ingrese", "cobre", "recibi"]

def detectar_solicitud_ingreso(texto: str) -> bool:
    texto_norm = normalizar(texto)
    return any(re.search(rf"\b{p}\b", texto_norm) for p in PALABRAS_INGRESO)

# --- Eliminación de transacciones ---
PALABRAS_ELIMINAR = [
    "elimina", "eliminar", "elimina el", "borra", "borrar", "cancela", "cancelar",
    "anula", "anular", "quita", "quitar", "deshaz", "deshacer",
]

def detectar_solicitud_eliminar(texto: str) -> bool:
    texto_norm = normalizar(texto)
    return any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in PALABRAS_ELIMINAR)

def detectar_categoria_informe(texto: str):
    """Detecta a qué categoría de GASTO se refiere el mensaje, ya sea porque el usuario
    escribió el nombre exacto de la categoría (ej. "ofrendas", "taberna") o un alias
    conocido (ej. "super", "ocio", "regalo")."""
    texto_norm = normalizar(texto)

    # 1) Coincidencia directa con el nombre real de la categoría.
    #    Se ordenan por longitud descendente para que, p.ej., "movilidad y monturas" no
    #    quede eclipsada por una coincidencia parcial más corta.
    for cat in sorted(CATEGORIAS, key=len, reverse=True):
        cat_norm = normalizar(cat)
        if re.search(rf"\b{re.escape(cat_norm)}\b", texto_norm):
            return cat

    # 2) Alias más largos primero, para que "vivienda" no se coma casos más específicos, etc.
    for alias in sorted(ALIASES_CATEGORIA.keys(), key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", texto_norm):
            return ALIASES_CATEGORIA[alias]

    return None

def detectar_categoria_ingreso_informe(texto: str):
    """Igual que detectar_categoria_informe pero para categorías de INGRESO (ej. "cuánto he
    recibido de Botín de mercenario este mes")."""
    texto_norm = normalizar(texto)
    for cat in sorted(CATEGORIAS_INGRESO, key=len, reverse=True):
        cat_norm = normalizar(cat)
        if re.search(rf"\b{re.escape(cat_norm)}\b", texto_norm):
            return cat
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
    elif periodo == "trimestral":
        mes_inicio_trimestre = ((ahora.month - 1) // 3) * 3 + 1
        desde = hoy_inicio.replace(month=mes_inicio_trimestre, day=1)
    elif periodo == "anual":
        desde = hoy_inicio.replace(month=1, day=1)
    else:
        desde = hoy_inicio

    hasta = ahora + timedelta(minutes=1)  # incluir el momento actual
    return desde, hasta

ETIQUETAS_PERIODO = {
    "diario": "hoy",
    "semanal": "esta semana",
    "mensual": "este mes",
    "trimestral": "este trimestre",
    "anual": "este año",
}

# --- ALERTAS DE OBJETIVO DE AHORRO MENSUAL ---
# Cada umbral salta UNA sola vez, cuando un gasto hace que el % gastado del mes lo cruce.
# Si un gasto salta varios escalones de golpe, solo se muestra el más alto que cruzó.
UMBRALES_AHORRO = [
    (70, "🟡 ALERTA DE RECURSOS",
     "¡Tus reservas comienzan a disminuir! Has consumido el 70% de tus recursos mensuales. "
     "Conviene vigilar tus próximos movimientos."),
    (75, "🟠 TOPE DE RESERVA",
     "¡Tus reservas están al límite! Has alcanzado el 75% de tu inventario. "
     "Cada decisión consumirá recursos esenciales."),
    (80, "🔴 RESERVAS CRÍTICAS",
     "¡Tus recursos están bajo mínimos! Solo cuentas con un 20% de tus reservas. "
     "Se recomienda conservar recursos hasta el final del ciclo."),
    (90, "⚔️ RESERVAS AGOTADAS",
     "¡Riesgo inminente! Has consumido más del 90% de tus recursos. "
     "Sigue avanzando bajo tu propio riesgo."),
    (100, "☠️ ZONA DE PELIGRO",
     "¡Has agotado todos tus recursos! Cualquier gasto adicional requerirá recursos de emergencia."),
]
# A diferencia de los umbrales de arriba (que saltan una sola vez), este se repite en
# CADA gasto nuevo mientras sigas por encima del 100%, ya que representa un estado
# continuo ("sigues en números rojos"), no un cruce puntual.
MENSAJE_MODO_SUPERVIVENCIA = (
    "💀 MODO SUPERVIVENCIA",
    "¡Has entrado en MODO SUPERVIVENCIA! Recupera recursos antes de continuar."
)

def determinar_alerta_ahorro(pct_antes: float, pct_despues: float):
    if pct_antes > 100 and pct_despues > pct_antes:
        return MENSAJE_MODO_SUPERVIVENCIA
    for umbral, titulo, texto in reversed(UMBRALES_AHORRO):
        if pct_antes < umbral <= pct_despues:
            return (titulo, texto)
    return None

async def verificar_alerta_ahorro(numero: str, monto_nuevo: float):
    """Se llama DESPUÉS de guardar uno o más gastos nuevos. 'monto_nuevo' es la suma de
    lo recién agregado, para poder reconstruir el % de antes y de después."""
    if not monto_nuevo:
        return None
    desde, hasta = calcular_rango_fechas("mensual")
    gastos_mes = await obtener_gastos(numero, desde, hasta)  # ya incluye lo recién guardado
    ingresos_mes = await obtener_ingresos(numero, desde, hasta)
    total_ingresos = sum(float(i["monto"]) for i in ingresos_mes)
    if total_ingresos <= 0:
        return None
    total_gastos_despues = sum(float(g["monto"]) for g in gastos_mes)
    total_gastos_antes = total_gastos_despues - float(monto_nuevo)
    pct_antes = (total_gastos_antes / total_ingresos) * 100
    pct_despues = (total_gastos_despues / total_ingresos) * 100
    return determinar_alerta_ahorro(pct_antes, pct_despues)

async def enviar_alerta_ahorro_si_corresponde(numero_remitente: str, monto_nuevo: float):
    alerta = await verificar_alerta_ahorro(numero_remitente, monto_nuevo)
    if alerta:
        titulo, texto = alerta
        await enviar_mensaje_whatsapp(numero_remitente, f"{titulo}\n{texto}")

def resolver_periodo(texto: str, periodo_tipo: str):
    """Dado un tipo de período ya detectado, calcula el rango de fechas exacto y una
    etiqueta legible, respetando mes/trimestre específicos si se mencionan (ej. 'julio', 'Q2')."""
    ahora = datetime.now(ZONA_HORARIA)
    if periodo_tipo == "mensual":
        mes_num = detectar_mes_especifico(texto) or ahora.month
        desde, hasta = calcular_rango_mes(mes_num)
        etiqueta = f"{MESES_ES[mes_num]} {desde.year}"
    elif periodo_tipo == "trimestral":
        q_num = detectar_trimestre_especifico(texto) or ((ahora.month - 1) // 3) + 1
        desde, hasta = calcular_rango_trimestre(q_num)
        etiqueta = f"trimestre Q{q_num} {desde.year}"
    else:
        desde, hasta = calcular_rango_fechas(periodo_tipo)
        etiqueta = ETIQUETAS_PERIODO.get(periodo_tipo, periodo_tipo)
    return desde, hasta, etiqueta

def formatear_linea_transaccion(item: dict) -> str:
    descripcion = item.get("descripcion", "")
    return f"• {descripcion} - {formatear_monto_corto(item.get('monto', 0))}"

def generar_texto_informe(etiqueta: str, gastos: list, ingresos: list, categoria: str = None, porcentaje: float = None) -> str:
    # --- Informe filtrado por una sola categoría de gasto (sin sección de ingresos/balance) ---
    if categoria:
        emoji_cat = CATEGORIA_EMOJIS.get(categoria, "❓")
        encabezado = f"{emoji_cat} {categoria}"
        if porcentaje is not None:
            encabezado += f": {porcentaje:.1f}%"
        titulo = f"📜 Crónica de {etiqueta}\n\n {encabezado}"
        if not gastos:
            return f"{titulo}\n\nTus arcas descansan sin gastos durante este período. 🎊"
        total = sum(float(g["monto"]) for g in gastos)
        lineas = [titulo]
        for g in sorted(gastos, key=lambda x: x.get("fecha", "")):
            lineas.append(formatear_linea_transaccion(g))
        lineas.append("")
        lineas.append(f"🪽 Desembolso total: {formatear_monto(total)}")
        return "\n".join(lineas)

    # --- Informe general: gastos por categoría + ingresos por categoría + balance ---
    titulo = f"📜 Crónica de {etiqueta}"
    total_gastos = sum(float(g["monto"]) for g in gastos)
    total_ingresos = sum(float(i["monto"]) for i in ingresos)

    lineas = [titulo, "", f"🪽 Desembolso total: {formatear_monto(total_gastos)}"]

    if gastos:
        por_categoria = {}
        for g in gastos:
            por_categoria.setdefault(g.get("categoria", "Miscelánea"), []).append(g)
        lineas.append("")
        lineas.append("Desglose del inventario:")
        for cat, lista in sorted(por_categoria.items(), key=lambda kv: -sum(float(x["monto"]) for x in kv[1])):
            total_cat = sum(float(x["monto"]) for x in lista)
            emoji_cat = CATEGORIA_EMOJIS.get(cat, "❓")
            lineas.append(f"{emoji_cat} {cat}: {formatear_monto_corto(total_cat)}")
            for g in sorted(lista, key=lambda x: x.get("fecha", "")):
                lineas.append(formatear_linea_transaccion(g))

    lineas.append("")
    lineas.append(f"🪎 Botín total: {formatear_monto(total_ingresos)}")

    if ingresos:
        por_categoria_ing = {}
        for i in ingresos:
            cat = i.get("categoria", "Miscelánea")
            por_categoria_ing[cat] = por_categoria_ing.get(cat, 0.0) + float(i["monto"])
        lineas.append("")
        lineas.append("Desglose del inventario:")
        for cat, total_cat in sorted(por_categoria_ing.items(), key=lambda x: -x[1]):
            emoji_cat = INGRESO_EMOJIS.get(cat, "❓")
            lineas.append(f"{emoji_cat} {cat}: {formatear_monto_corto(total_cat)}")

    balance = total_ingresos - total_gastos
    signo = "+" if balance >= 0 else "-"
    lineas.append("")
    if total_ingresos > 0:
        pct_gastado = (total_gastos / total_ingresos) * 100
        lineas.append(f"🧮 Tesoro restante: {signo}{formatear_monto_corto(abs(balance))} ({pct_gastado:.1f}% de tus ingresos gastado)")
    else:
        lineas.append(f"🧮 Tesoro restante: {signo}{formatear_monto_corto(abs(balance))}")

    return "\n".join(lineas)

def generar_texto_informe_ingresos(etiqueta: str, ingresos: list, categoria: str) -> str:
    """Análogo a generar_texto_informe pero para un informe de INGRESOS filtrado por una
    categoría de ingreso concreta (ej. "cuánto he recibido de Botín de mercenario")."""
    emoji_cat = INGRESO_EMOJIS.get(categoria, "❓")
    titulo = f"📜 Crónica de {etiqueta}\n\n{emoji_cat} {categoria}"
    if not ingresos:
        return f"{titulo}\n\nNo se han registrado botines de esta categoría durante este período. 🎊"
    total = sum(float(i["monto"]) for i in ingresos)
    lineas = [titulo]
    for i in sorted(ingresos, key=lambda x: x.get("fecha", "")):
        lineas.append(formatear_linea_transaccion(i))
    lineas.append("")
    lineas.append(f"🪎 Botín total: {formatear_monto(total)}")
    return "\n".join(lineas)

def generar_texto_reparto(periodo: str, gastos: list, total_ingresos: float) -> str:
    """Reparto general de gastos con porcentajes sobre los ingresos del período."""
    etiqueta_periodo = ETIQUETAS_PERIODO.get(periodo, "en el período")

    if not gastos:
        return f"📜 Tus arcas descansan sin gastos {etiqueta_periodo}. 🎊"

    # Sin ingresos no hay base para el % sobre botín: se usa el reparto sobre gastos
    if total_ingresos <= 0:
        return generar_texto_porcentaje(periodo, gastos)

    por_categoria = {}
    for g in gastos:
        cat = g.get("categoria", "Miscelánea")
        por_categoria[cat] = por_categoria.get(cat, 0.0) + float(g["monto"])

    total_gastos = sum(por_categoria.values())
    restante = total_ingresos - total_gastos
    pct_gastos = (total_gastos / total_ingresos) * 100
    pct_restante = (restante / total_ingresos) * 100

    lineas = [
        f"📜 Reparto del tesoro {etiqueta_periodo}",
        "",
        f"🪽 Desembolso total: {pct_gastos:.1f}%",
        "",
    ]
    for cat, monto in sorted(por_categoria.items(), key=lambda x: -x[1]):
        porcentaje = (monto / total_ingresos) * 100
        emoji_cat = CATEGORIA_EMOJIS.get(cat, "❓")
        lineas.append(f"• {emoji_cat} {cat}: {porcentaje:.1f}%")

    lineas.append("")
    lineas.append(f"🪎 Botín restante: {pct_restante:.1f}%")

    return "\n".join(lineas)

def generar_texto_porcentaje_ingresos(periodo: str, gastos: list, total_ingresos: float, categoria: str = None):
    etiqueta_periodo = ETIQUETAS_PERIODO.get(periodo, "en el período")

    if total_ingresos <= 0:
        return f"🧌 No hay botín registrado {etiqueta_periodo}, así que no puedo calcular esa fracción."

    if categoria:
        monto_categoria = sum(float(g["monto"]) for g in gastos if g.get("categoria") == categoria)
        porcentaje = (monto_categoria / total_ingresos) * 100
        return (
            f"📜 {categoria} representa el {porcentaje:.1f}% de tus ingresos {etiqueta_periodo}\n"
            f"({formatear_monto(monto_categoria)} de {formatear_monto(total_ingresos)} de ingresos)"
        )

    por_categoria = {}
    for g in gastos:
        cat = g.get("categoria", "Miscelánea")
        por_categoria[cat] = por_categoria.get(cat, 0.0) + float(g["monto"])

    total_gastos = sum(por_categoria.values())
    ahorro = total_ingresos - total_gastos

    lineas = [f"📜 Gastos {etiqueta_periodo} sobre tus ingresos", "", f"🪎 Ingresos: {formatear_monto(total_ingresos)}", ""]
    for cat, monto in sorted(por_categoria.items(), key=lambda x: -x[1]):
        porcentaje = (monto / total_ingresos) * 100
        emoji_cat = CATEGORIA_EMOJIS.get(cat, "❓")
        lineas.append(f"  {emoji_cat} {cat}: {porcentaje:.1f}% ({formatear_monto(monto)})")
    porcentaje_ahorro = (ahorro / total_ingresos) * 100
    lineas.append("")
    lineas.append(f"  🛡️ Reservas intactas: {porcentaje_ahorro:.1f}% ({formatear_monto(ahorro)})")

    return "\n".join(lineas)

def generar_texto_balance(periodo: str, total_ingresos: float, total_gastos: float):
    etiqueta_periodo = ETIQUETAS_PERIODO.get(periodo, "en el período")
    balance = total_ingresos - total_gastos

    lineas = [
        f"⚖️ Balance de arcas {etiqueta_periodo}",
        "",
        f"🪎 Botín recaudado: {formatear_monto(total_ingresos)}",
        f"🪽 Oro gastado: {formatear_monto(total_gastos)}",
        f"🧮 Tesoro neto: {formatear_monto(balance)}",
    ]
    if total_ingresos > 0:
        pct_gastado = (total_gastos / total_ingresos) * 100
        lineas.append(f"📜 Has consumido el {pct_gastado:.1f}% de tu botín")
    else:
        lineas.append("🧌 No tienes ingresos registrados en este período.")

    return "\n".join(lineas)

def formatear_confirmacion_ingreso(datos: dict) -> str:
    if not datos.get("monto"):
        return "🧌 El heraldo no logró registrar ese botín. Inténtalo de nuevo."
    fecha_str, hora_str = formatear_fecha_hora_actual()
    categoria = datos.get("categoria", "Objeto encontrado")
    emoji_categoria = INGRESO_EMOJIS.get(categoria, "❓")
    return (
        "📯 Transacción registrada\n"
        f"• 🪎 Monto: {formatear_monto(datos.get('monto'))}\n"
        f"• 🔖 Categoría: {categoria} {emoji_categoria}\n"
        f"• 🪶 Descripción: {datos.get('descripcion')}\n"
        f"• 🌔 Fecha: {fecha_str}\n"
        f"• ⌛️ Hora: {hora_str}\n"
        f"• 🀄️ Tipo: Botín"
    )

# --- EXPORTAR EXCEL ---
PALABRAS_EXPORTAR = ["exportar", "exporta"]

def detectar_solicitud_exportar(texto: str) -> bool:
    texto_norm = normalizar(texto)
    return any(p in texto_norm for p in PALABRAS_EXPORTAR)

MESES_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
}
# Solo para mostrar la fecha en las confirmaciones de transacciones (ej. "23 de sep de 2026").
# MESES_ES se mantiene completo porque de ahí sale MESES_ALIAS, usado para detectar qué mes
# menciona el usuario en un informe/exportación (ej. "resumen de septiembre").
MESES_ES_ABREV = {
    1: "ene", 2: "feb", 3: "mar", 4: "abr", 5: "may", 6: "jun",
    7: "jul", 8: "ago", 9: "sep", 10: "oct", 11: "nov", 12: "dic",
}
MESES_ALIAS = {normalizar(nombre): num for num, nombre in MESES_ES.items()}

def detectar_mes_especifico(texto: str):
    texto_norm = normalizar(texto)
    for nombre_norm, num in MESES_ALIAS.items():
        if re.search(rf"\b{nombre_norm}\b", texto_norm):
            return num
    return None

ORDINALES_TRIMESTRE = {"primer": 1, "primero": 1, "segundo": 2, "tercer": 3, "tercero": 3, "cuarto": 4}

def detectar_trimestre_especifico(texto: str):
    texto_norm = normalizar(texto)
    m = re.search(r"\bq([1-4])\b", texto_norm)
    if m:
        return int(m.group(1))
    m2 = re.search(r"\btrimestre\s*([1-4])\b", texto_norm)
    if m2:
        return int(m2.group(1))
    for palabra, num in ORDINALES_TRIMESTRE.items():
        if re.search(rf"\b{palabra}\b", texto_norm):
            return num
    return None

def calcular_rango_mes(mes_num: int, anio: int = None):
    anio = anio or datetime.now(ZONA_HORARIA).year
    desde = datetime(anio, mes_num, 1, tzinfo=ZONA_HORARIA)
    if mes_num == 12:
        hasta = datetime(anio + 1, 1, 1, tzinfo=ZONA_HORARIA)
    else:
        hasta = datetime(anio, mes_num + 1, 1, tzinfo=ZONA_HORARIA)
    ahora = datetime.now(ZONA_HORARIA)
    if desde <= ahora < hasta:
        hasta = ahora + timedelta(minutes=1)
    return desde, hasta

def calcular_rango_trimestre(trimestre_num: int, anio: int = None):
    anio = anio or datetime.now(ZONA_HORARIA).year
    mes_inicio = (trimestre_num - 1) * 3 + 1
    desde = datetime(anio, mes_inicio, 1, tzinfo=ZONA_HORARIA)
    mes_fin = mes_inicio + 3
    if mes_fin > 12:
        hasta = datetime(anio + 1, mes_fin - 12, 1, tzinfo=ZONA_HORARIA)
    else:
        hasta = datetime(anio, mes_fin, 1, tzinfo=ZONA_HORARIA)
    ahora = datetime.now(ZONA_HORARIA)
    if desde <= ahora < hasta:
        hasta = ahora + timedelta(minutes=1)
    return desde, hasta

def preparar_exportacion(texto: str):
    """Determina el rango de fechas y el nombre base del archivo según lo pedido en el mensaje."""
    ahora = datetime.now(ZONA_HORARIA)
    trimestre_especifico = detectar_trimestre_especifico(texto)
    mes_especifico = detectar_mes_especifico(texto)
    periodo_exp = detectar_periodo_generico(texto, default=None)

    if trimestre_especifico is not None or periodo_exp == "trimestral":
        q_num = trimestre_especifico or ((ahora.month - 1) // 3) + 1
        desde, hasta = calcular_rango_trimestre(q_num)
        nombre_archivo_base = f"gastos_trimestre_Q{q_num}_{desde.year}"
        etiqueta_caption = f"Trimestre Q{q_num} {desde.year}"

    elif mes_especifico is not None or periodo_exp in ("mensual", None):
        mes_num = mes_especifico or ahora.month
        desde, hasta = calcular_rango_mes(mes_num)
        nombre_archivo_base = f"gastos_mes_{MESES_ES[mes_num]}_{desde.year}"
        etiqueta_caption = f"{MESES_ES[mes_num].capitalize()} {desde.year}"

    else:
        desde, hasta = calcular_rango_fechas(periodo_exp)
        fecha_archivo = ahora.strftime("%Y%m%d")
        nombre_archivo_base = f"gastos_{periodo_exp}_{fecha_archivo}"
        etiqueta_caption = ETIQUETAS_PERIODO.get(periodo_exp, "")

    return desde, hasta, nombre_archivo_base, etiqueta_caption

def generar_excel_gastos(gastos: list, ingresos: list) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter

    fuente_encabezado = Font(bold=True, color="FFFFFF")
    relleno_encabezado = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    relleno_encabezado_ingresos = PatternFill(start_color="548235", end_color="548235", fill_type="solid")
    fuente_subtitulo = Font(bold=True, size=12)

    def fila_encabezado(ws, fila, encabezados, relleno=relleno_encabezado):
        for col_num, texto_col in enumerate(encabezados, start=1):
            celda = ws.cell(row=fila, column=col_num, value=texto_col)
            celda.font = fuente_encabezado
            celda.fill = relleno
            celda.alignment = Alignment(horizontal="center")

    wb = Workbook()
    ws = wb.active
    ws.title = "Libro Mayor"

    total_gastos = sum(float(g.get("monto", 0)) for g in gastos)
    total_ingresos = sum(float(i.get("monto", 0)) for i in ingresos)

    por_categoria_gasto = {}
    for g in gastos:
        cat = g.get("categoria", "Miscelánea")
        por_categoria_gasto[cat] = por_categoria_gasto.get(cat, 0.0) + float(g.get("monto", 0))

    # --- Sección 1: Detalle de ingresos ---
    fila = 1
    fila_encabezado(ws, fila, ["Fecha", "Categoría", "Monto", "Descripción"], relleno=relleno_encabezado_ingresos)
    fila += 1
    for i in ingresos:
        try:
            fecha_str = datetime.fromisoformat(i["fecha"]).astimezone(ZONA_HORARIA).strftime("%Y-%m-%d %H:%M")
        except Exception:
            fecha_str = i.get("fecha", "")
        ws.cell(row=fila, column=1, value=fecha_str)
        ws.cell(row=fila, column=2, value=i.get("categoria", "Miscelánea"))
        ws.cell(row=fila, column=3, value=float(i.get("monto", 0)))
        ws.cell(row=fila, column=4, value=i.get("descripcion", ""))
        fila += 1
    ws.cell(row=fila, column=1, value="TOTAL").font = Font(bold=True)
    ws.cell(row=fila, column=3, value=total_ingresos).font = Font(bold=True)

    # --- Sección 2: Detalle de gastos ---
    fila += 3
    fila_encabezado(ws, fila, ["Fecha", "Categoría", "Monto", "Descripción"])
    fila += 1
    for g in gastos:
        try:
            fecha_str = datetime.fromisoformat(g["fecha"]).astimezone(ZONA_HORARIA).strftime("%Y-%m-%d %H:%M")
        except Exception:
            fecha_str = g.get("fecha", "")
        ws.cell(row=fila, column=1, value=fecha_str)
        ws.cell(row=fila, column=2, value=g.get("categoria", "Miscelánea"))
        ws.cell(row=fila, column=3, value=float(g.get("monto", 0)))
        ws.cell(row=fila, column=4, value=g.get("descripcion", ""))
        fila += 1
    ws.cell(row=fila, column=1, value="TOTAL").font = Font(bold=True)
    ws.cell(row=fila, column=3, value=total_gastos).font = Font(bold=True)

    # --- Sección 3: Resumen de gastos por categoría (% sobre el total de ingresos) ---
    fila += 3
    ws.cell(row=fila, column=1, value="Resumen de gastos por categoría").font = fuente_subtitulo
    fila += 1
    fila_encabezado(ws, fila, ["Categoría", "Total", "% de ingresos"])
    fila += 1
    for categoria, monto in sorted(por_categoria_gasto.items(), key=lambda x: -x[1]):
        porcentaje = (monto / total_ingresos) if total_ingresos else 0
        ws.cell(row=fila, column=1, value=categoria)
        ws.cell(row=fila, column=2, value=monto)
        celda_pct = ws.cell(row=fila, column=3, value=porcentaje)
        celda_pct.number_format = "0.0%"
        fila += 1
    ws.cell(row=fila, column=1, value="TOTAL GASTOS").font = Font(bold=True)
    ws.cell(row=fila, column=2, value=total_gastos).font = Font(bold=True)
    celda_pct_total = ws.cell(row=fila, column=3, value=(total_gastos / total_ingresos) if total_ingresos else 0)
    celda_pct_total.number_format = "0.0%"
    celda_pct_total.font = Font(bold=True)

    # --- Sección 4: Balance ---
    fila += 3
    balance = total_ingresos - total_gastos
    ws.cell(row=fila, column=1, value="BALANCE").font = fuente_subtitulo
    ws.cell(row=fila, column=2, value=balance).font = fuente_subtitulo
    if total_ingresos:
        celda_pct_balance = ws.cell(row=fila, column=3, value=total_gastos / total_ingresos)
        celda_pct_balance.number_format = "0.0%"
        celda_pct_balance.font = fuente_subtitulo

        FORMATO_EUR = '#,##0.00\\ "€"'
    for fila_celdas in ws.iter_rows():
        for celda in fila_celdas:
            if isinstance(celda.value, (int, float)) and celda.number_format != "0.0%":
                celda.number_format = FORMATO_EUR

    for i, ancho in enumerate([18, 22, 14, 35], start=1):
        ws.column_dimensions[get_column_letter(i)].width = ancho

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()

async def subir_documento_whatsapp(contenido: bytes, filename: str, mime_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", intentos: int = 3):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    data = {"messaging_product": "whatsapp"}
    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client_http:
                files = {"file": (filename, contenido, mime_type)}
                respuesta = await client_http.post(url, headers=headers, data=data, files=files)
                if respuesta.status_code == 200:
                    return respuesta.json().get("id")
                print(f"⚠️ Error subiendo documento a WhatsApp: {respuesta.status_code} - {respuesta.text}")
                return None
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout) as e:
            ultimo_error = e
            print(f"⏳ Intento {intento}/{intentos} falló subiendo documento ({type(e).__name__}), reintentando...")
            await asyncio.sleep(2 * intento)
    print(f"⚠️ Todos los intentos fallaron subiendo documento: {type(ultimo_error).__name__}: {ultimo_error}")
    return None

async def enviar_documento_whatsapp(numero_destino: str, media_id: str, filename: str, caption: str = ""):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "document",
        "document": {"id": media_id, "filename": filename, "caption": caption},
    }
    respuesta = await request_con_reintentos("POST", url, headers, json_payload=payload)
    if respuesta is not None and respuesta.status_code != 200:
        print(f"⚠️ Error enviando documento a WhatsApp: {respuesta.status_code} - {respuesta.text}")
    return respuesta

async def enviar_mensaje_whatsapp(numero_destino: str, texto: str):
    """Envía el mensaje y devuelve el wamid (id del mensaje en WhatsApp) si se pudo obtener,
    o None. El wamid se usa para poder asociar la confirmación de una transacción y así
    reconocerla luego si el usuario responde ('desliza') sobre ese mensaje."""
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
    if respuesta is None:
        return None
    if respuesta.status_code != 200:
        print(f"⚠️ Error enviando mensaje a WhatsApp: {respuesta.status_code} - {respuesta.text}")
        return None
    try:
        return respuesta.json()["messages"][0]["id"]
    except (KeyError, IndexError, ValueError):
        return None

def formatear_confirmacion(datos: dict) -> str:
    if datos.get("categoria") == "Error":
        return "🧌 Los escribas no comprendieron esa transacción. ¿Puedes narrarla de otra forma?"
    fecha_str, hora_str = formatear_fecha_hora_actual()
    categoria = datos.get("categoria", "Miscelánea")
    emoji_categoria = CATEGORIA_EMOJIS.get(categoria, "❓")
    return (
        "📯 Transacción registrada\n"
        f"• 🪎 Monto: {formatear_monto(datos.get('monto'))}\n"
        f"• 🔖 Categoría: {categoria} {emoji_categoria}\n"
        f"• 🪶 Descripción: {datos.get('descripcion')}\n"
        f"• 🌔 Fecha: {fecha_str}\n"
        f"• ⌛️ Hora: {hora_str}\n"
        f"• 🀄️ Tipo: Desembolso"
    )

def formatear_confirmacion_lote(gastos: list, ingresos: list) -> str:
    total = len(gastos) + len(ingresos)
    if total == 0:
        return "🧌 No pude leer ningún movimiento en ese pergamino. ¿Tienes una imagen más clara?"

    fecha_str, hora_str = formatear_fecha_hora_actual()
    lineas = [f"📯 {total} Transacciones registradas", f"🌔 {fecha_str} — ⏳ {hora_str}", ""]

    if gastos:
        total_gastos = sum(float(g.get("monto", 0)) for g in gastos)
        lineas.append(f"🪽 Gastos ({formatear_monto_corto(total_gastos)}):")
        for g in gastos:
            emoji_cat = CATEGORIA_EMOJIS.get(g.get("categoria", "Miscelánea"), "❓")
            lineas.append(f". {emoji_cat} {g.get('descripcion','')} — {g.get('categoria','')} - {formatear_monto_corto(g.get('monto', 0))}")
        lineas.append("")

    if ingresos:
        total_ingresos = sum(float(i.get("monto", 0)) for i in ingresos)
        lineas.append(f"🪎 Ingresos ({formatear_monto_corto(total_ingresos)}):")
        for i in ingresos:
            emoji_cat = INGRESO_EMOJIS.get(i.get("categoria", "Miscelánea"), "❓")
            lineas.append(f". {emoji_cat} {i.get('descripcion','')} — {i.get('categoria','')} - {formatear_monto_corto(i.get('monto', 0))}")

    return "\n".join(lineas).rstrip()

# --- 5. ELIMINACIÓN DE TRANSACCIONES ---
async def manejar_eliminacion(numero_remitente: str, descripcion_buscada: str, tipo_sugerido: str = "desconocido"):
    """Busca el gasto/ingreso más reciente que coincida con la descripción (o el más reciente
    de todos si la descripción viene vacía, ej. "elimina el último gasto") y lo borra. Si
    'tipo_sugerido' es "gasto" o "ingreso" restringe la búsqueda a esa tabla; si es
    "desconocido" busca en ambas y se queda con la coincidencia más reciente de las dos."""
    candidatos_gasto = []
    candidatos_ingreso = []
    if tipo_sugerido != "ingreso":
        candidatos_gasto = await buscar_gastos_por_descripcion(numero_remitente, descripcion_buscada)
    if tipo_sugerido != "gasto":
        candidatos_ingreso = await buscar_ingresos_por_descripcion(numero_remitente, descripcion_buscada)

    candidato = None
    es_ingreso = False
    if candidatos_gasto and candidatos_ingreso:
        if candidatos_gasto[0]["fecha"] >= candidatos_ingreso[0]["fecha"]:
            candidato = candidatos_gasto[0]
        else:
            candidato = candidatos_ingreso[0]
            es_ingreso = True
    elif candidatos_gasto:
        candidato = candidatos_gasto[0]
    elif candidatos_ingreso:
        candidato = candidatos_ingreso[0]
        es_ingreso = True

    if not candidato:
        texto_busq = f' que coincida con "{descripcion_buscada}"' if descripcion_buscada else ""
        await enviar_mensaje_whatsapp(numero_remitente, f"🧌 No hallé ningún registro en los libros{texto_busq}.")
        return

    if es_ingreso:
        ok = await eliminar_ingreso(candidato["id"])
        emoji_cat = INGRESO_EMOJIS.get(candidato.get("categoria"), "❓")
    else:
        ok = await eliminar_gasto(candidato["id"])
        emoji_cat = CATEGORIA_EMOJIS.get(candidato.get("categoria"), "❓")

    total_candidatos = len(candidatos_gasto) + len(candidatos_ingreso)
    tipo_log = "ingreso" if es_ingreso else "gasto"
    print(f"🧙🏻‍♂️ Eliminación de {tipo_log}: '{candidato.get('descripcion')}' ({numero_remitente})")

    if ok:
        extra = (
            f"\n\n(Había {total_candidatos - 1} coincidencia(s) más sin eliminar; "
            "sé más específico si quieres borrar otra)"
        ) if total_candidatos > 1 else ""
        await enviar_mensaje_whatsapp(
            numero_remitente,
            f"🧙🏻‍♂️ Registro eliminado de las arcas\n"
            f"• {candidato.get('descripcion')} ({formatear_monto_corto(candidato.get('monto'))}) 🪶\n"
            f"• Categoria: {candidato.get('categoria')}{extra} {emoji_cat}"
        )
    else:
        await enviar_mensaje_whatsapp(numero_remitente, "🧌 El archivero no pudo eliminar ese registro. Intenta de nuevo en un momento.")

async def manejar_respuesta_a_transaccion(numero_remitente: str, wamid_original: str, texto: str) -> bool:
    """Se llama cuando el mensaje entrante es una RESPUESTA (el usuario deslizó/citó un mensaje
    de confirmación). Busca a qué gasto o ingreso corresponde ese wamid y, si lo encuentra,
    interpreta el texto como una orden de eliminar esa transacción o de cambiarle la categoría.
    Devuelve True si el mensaje quedó gestionado por esta vía (aunque no encontrara la
    transacción o no entendiera la orden), y False si el wamid no corresponde a ninguna
    transacción conocida, para que el mensaje se procese de forma normal."""
    gasto = await buscar_gasto_por_wamid(numero_remitente, wamid_original)
    ingreso = None
    if not gasto:
        ingreso = await buscar_ingreso_por_wamid(numero_remitente, wamid_original)
    if not gasto and not ingreso:
        return False  # no es una respuesta a una confirmación de transacción conocida

    registro = gasto or ingreso
    es_ingreso = ingreso is not None

    # --- ¿Pide eliminar esta transacción? ---
    if detectar_solicitud_eliminar(texto):
        if es_ingreso:
            ok = await eliminar_ingreso(registro["id"])
            emoji_cat = INGRESO_EMOJIS.get(registro.get("categoria"), "❓")
        else:
            ok = await eliminar_gasto(registro["id"])
            emoji_cat = CATEGORIA_EMOJIS.get(registro.get("categoria"), "❓")
        print(f"🧙🏻‍♂️ Eliminación por respuesta: '{registro.get('descripcion')}' ({numero_remitente})")
        if ok:
            await enviar_mensaje_whatsapp(
                numero_remitente,
                f"🧙🏻‍♂️ Registro eliminado de las arcas\n"
                f"• {registro.get('descripcion')} ({formatear_monto_corto(registro.get('monto'))}) 🪶\n"
                f"• Categoria: {registro.get('categoria')} {emoji_cat}"
            )
        else:
            await enviar_mensaje_whatsapp(numero_remitente, "🧌 El archivero no pudo eliminar ese registro. Intenta de nuevo en un momento.")
        return True

    # --- Si no, se interpreta como un cambio de categoría ---
    categorias_validas = CATEGORIAS_INGRESO if es_ingreso else CATEGORIAS
    categoria_nueva = resolver_categoria(texto.strip(), categorias_validas)
    if not categoria_nueva and not es_ingreso:
        categoria_nueva = detectar_categoria_informe(texto)
    if not categoria_nueva:
        categoria_nueva = await extraer_categoria_con_ia(texto, categorias_validas)

    if not categoria_nueva:
        await enviar_mensaje_whatsapp(
            numero_remitente,
            "🧌 No reconocí esa categoría. Responde con el nombre de una categoría válida, "
            "o escribe \"elimina\" para borrar este registro."
        )
        return True

    if es_ingreso:
        ok = await actualizar_categoria_ingreso(registro["id"], categoria_nueva)
        emoji_cat = INGRESO_EMOJIS.get(categoria_nueva, "❓")
    else:
        ok = await actualizar_categoria_gasto(registro["id"], categoria_nueva)
        emoji_cat = CATEGORIA_EMOJIS.get(categoria_nueva, "❓")

    print(f"📝 Corrección de categoría por respuesta: '{registro.get('descripcion')}' -> {categoria_nueva} ({numero_remitente})")
    if ok:
        await enviar_mensaje_whatsapp(
            numero_remitente,
            f"🧙🏻‍♂️ Registro reclasificado\n"
            f"• {registro.get('descripcion')} ({formatear_monto_corto(registro.get('monto'))}) 🪶\n"
            f"• Ahora está en: {categoria_nueva} {emoji_cat}"
        )
    else:
        await enviar_mensaje_whatsapp(numero_remitente, "🧌 El archivero no pudo reclasificar ese registro. Intenta de nuevo en un momento.")
    return True

async def cerrar_ciclo_y_registrar_ahorro_si_corresponde(numero_remitente: str, datos_ingreso: dict):
    """Se llama justo ANTES de guardar un nuevo ingreso. Si ese ingreso es un 'Botín principal'
    y ya existía uno anterior, cierra el ciclo que termina ahora: calcula el balance total
    (todos los ingresos, incluido ese Botín anterior, menos todos los gastos) desde la fecha
    del Botín anterior hasta este instante, lo guarda en la tabla 'ahorros', avisa al usuario,
    y envía por WhatsApp el Excel de ese ciclo recién cerrado. Si es el primer Botín principal
    que registra el usuario, no hay ciclo anterior que cerrar y no hace nada."""
    if datos_ingreso.get("categoria") != "Botín principal":
        return

    ultimo_botin = await obtener_ultimo_botin_principal(numero_remitente)
    if not ultimo_botin:
        return  # primer Botín principal: no hay ciclo previo que cerrar

    desde = datetime.fromisoformat(ultimo_botin["fecha"]).astimezone(ZONA_HORARIA)
    hasta = datetime.now(ZONA_HORARIA)

    gastos_ciclo = await obtener_gastos(numero_remitente, desde, hasta)
    ingresos_ciclo = await obtener_ingresos(numero_remitente, desde, hasta)
    total_gastos = sum(float(g["monto"]) for g in gastos_ciclo)
    total_ingresos = sum(float(i["monto"]) for i in ingresos_ciclo)
    balance = total_ingresos - total_gastos

    await guardar_ahorro(numero_remitente, balance, desde, hasta)
    print(f"🏛️ Ciclo cerrado para {numero_remitente}: balance {balance:.2f} ({desde.date()} - {hasta.date()})")

    if balance >= 0:
        mensaje_fortuna = f"🏛️ Has sumado {formatear_monto(balance)} a tu fortuna."
    else:
        mensaje_fortuna = f"🏛️ Has restado {formatear_monto(abs(balance))} a tu fortuna."
    await enviar_mensaje_whatsapp(numero_remitente, mensaje_fortuna)

    # Exportar automáticamente el resumen (Excel) del ciclo que se acaba de cerrar
    etiqueta_ciclo = f"{desde.strftime('%d %b %Y')} – {hasta.strftime('%d %b %Y')}"
    nombre_archivo = f"gastos_ciclo_{desde.strftime('%Y%m%d')}_{hasta.strftime('%Y%m%d')}.xlsx"
    contenido_excel = generar_excel_gastos(gastos_ciclo, ingresos_ciclo)
    media_id = await subir_documento_whatsapp(contenido_excel, nombre_archivo)
    if media_id:
        caption = f"🧧 Libro de cuentas — Ciclo cerrado ({etiqueta_ciclo})"
        await enviar_documento_whatsapp(numero_remitente, media_id, nombre_archivo, caption)
    else:
        await enviar_mensaje_whatsapp(numero_remitente, "🧌 El escriba no ha podido preparar el pergamino del ciclo cerrado, pero tu fortuna quedó registrada.")

async def registrar_gasto_y_confirmar(numero_remitente: str, datos: dict):
    """Guarda el gasto, envía la confirmación y asocia el wamid del mensaje enviado al
    registro guardado, para poder reconocerlo después si el usuario responde sobre él."""
    if datos.get("categoria") == "Error":
        await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion(datos))
        return
    registro = await guardar_gasto(numero_remitente, datos)
    wamid = await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion(datos))
    if registro and wamid:
        await asociar_wamid_gasto(registro["id"], wamid)
    await enviar_alerta_ahorro_si_corresponde(numero_remitente, datos.get("monto", 0))

async def registrar_ingreso_y_confirmar(numero_remitente: str, datos: dict):
    """Guarda el ingreso, envía la confirmación y asocia el wamid del mensaje enviado al
    registro guardado. Si es un Botín principal, primero cierra el ciclo anterior."""
    registro = None
    if datos.get("monto"):
        await cerrar_ciclo_y_registrar_ahorro_si_corresponde(numero_remitente, datos)
        registro = await guardar_ingreso(numero_remitente, datos)
    wamid = await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion_ingreso(datos))
    if registro and wamid:
        await asociar_wamid_ingreso(registro["id"], wamid)

# --- 6. RUTAS DEL WEBHOOK ---
async def procesar_mensaje_entrante(message: dict, numero_remitente: str):
    try:
        # Si el usuario respondió ("deslizó"/citó) un mensaje de confirmación de una
        # transacción, tratamos ese caso aparte: puede pedir eliminarla o cambiarle la
        # categoría, identificándola sin ambigüedad por el wamid del mensaje citado.
        contexto = message.get("context")
        if contexto and contexto.get("id") and message.get("type") == "text":
            manejado = await manejar_respuesta_a_transaccion(
                numero_remitente, contexto["id"], message["text"]["body"]
            )
            if manejado:
                return

        # Procesar mensajes de texto
        if message["type"] == "text":
            texto = message["text"]["body"]
            periodo = detectar_periodo_informe(texto)

            if detectar_solicitud_exportar(texto):
                # El usuario pidió exportar un Excel (ej. "exportar trimestre", "exportar julio", "exportar Q2")
                categoria = detectar_categoria_informe(texto)
                desde, hasta, nombre_archivo_base, etiqueta_caption = preparar_exportacion(texto)
                gastos = await obtener_gastos(numero_remitente, desde, hasta, categoria)
                ingresos = [] if categoria else await obtener_ingresos(numero_remitente, desde, hasta)
                print(f"🧧 Exportación '{nombre_archivo_base}'{' / ' + categoria if categoria else ''} solicitada por {numero_remitente} ({len(gastos)} gastos, {len(ingresos)} ingresos)")

                if not gastos and not ingresos:
                    await enviar_mensaje_whatsapp(numero_remitente, f"🧌 No hay movimientos en tus arcas durante {etiqueta_caption}.")
                else:
                    contenido_excel = generar_excel_gastos(gastos, ingresos)
                    nombre_archivo = f"{nombre_archivo_base}.xlsx"
                    media_id = await subir_documento_whatsapp(contenido_excel, nombre_archivo)
                    if media_id:
                        caption = f"🧧 Libro de cuentas\n • {etiqueta_caption}"
                        await enviar_documento_whatsapp(numero_remitente, media_id, nombre_archivo, caption)
                    else:
                        await enviar_mensaje_whatsapp(numero_remitente, "🧌 El escriba no ha podido preparar el pergamino. Inténtalo de nuevo en unos instantes.")
            elif detectar_solicitud_eliminar(texto):
                # El usuario pidió eliminar/borrar/cancelar un gasto o ingreso ya registrado.
                # OJO: esta comprobación va ANTES que la de "ingreso", porque un mensaje como
                # "elimina el ingreso de la nómina" contiene la palabra "ingreso" y si no,
                # caería por error en la rama de registrar un ingreso nuevo.
                datos_eliminar = await procesar_eliminacion_con_ia(texto)
                descripcion_buscada = datos_eliminar.get("descripcion_buscada", "") or ""
                tipo_sugerido = datos_eliminar.get("tipo", "desconocido")
                await manejar_eliminacion(numero_remitente, descripcion_buscada, tipo_sugerido)
            elif periodo:
                # El usuario pidió un resumen/informe (opcionalmente filtrado por categoría,
                # y opcionalmente con mes/trimestre específico, ej. "resumen de julio").
                desde, hasta, etiqueta = resolver_periodo(texto, periodo)

                if detectar_referencia_ingresos(texto):
                    categoria_ingreso = detectar_categoria_ingreso_informe(texto)
                    if categoria_ingreso:
                        ingresos = await obtener_ingresos(numero_remitente, desde, hasta, categoria_ingreso)
                        informe = generar_texto_informe_ingresos(etiqueta, ingresos, categoria_ingreso)
                        print(f"📜 Informe de ingresos '{etiqueta}' / {categoria_ingreso} generado para {numero_remitente} ({len(ingresos)} ingresos)")
                        await enviar_mensaje_whatsapp(numero_remitente, informe)
                    else:
                        gastos = await obtener_gastos(numero_remitente, desde, hasta)
                        ingresos = await obtener_ingresos(numero_remitente, desde, hasta)
                        informe = generar_texto_informe(etiqueta, gastos, ingresos)
                        print(f"📜 Informe general '{etiqueta}' generado para {numero_remitente} ({len(gastos)} gastos, {len(ingresos)} ingresos)")
                        await enviar_mensaje_whatsapp(numero_remitente, informe)
                else:
                    # Caso normal: informe de GASTOS, opcionalmente filtrado por categoría
                    # (ej. "cuánto he gastado en Taberna este mes")
                    categoria = detectar_categoria_informe(texto)
                    gastos = await obtener_gastos(numero_remitente, desde, hasta, categoria)
                    ingresos = [] if categoria else await obtener_ingresos(numero_remitente, desde, hasta)

                    porcentaje = None
                    if categoria:
                        ingresos_periodo = await obtener_ingresos(numero_remitente, desde, hasta)
                        total_ingresos_periodo = sum(float(i["monto"]) for i in ingresos_periodo)
                        total_categoria = sum(float(g["monto"]) for g in gastos)
                        if total_ingresos_periodo > 0:
                            porcentaje = (total_categoria / total_ingresos_periodo) * 100

                    informe = generar_texto_informe(etiqueta, gastos, ingresos, categoria, porcentaje)
                    print(f"📜 Informe '{etiqueta}'{' / ' + categoria if categoria else ''} generado para {numero_remitente} ({len(gastos)} gastos)")
                    await enviar_mensaje_whatsapp(numero_remitente, informe)
            elif detectar_solicitud_porcentaje(texto):
                # El usuario pidió un porcentaje (ej. "qué % de mis gastos/ingresos es ocio")
                categoria = detectar_categoria_informe(texto)
                periodo_pct = detectar_periodo_generico(texto, default="mensual")

                if categoria:
                    # Categoría concreta: mismo formato que la crónica, con el % junto al nombre
                    desde, hasta, etiqueta = resolver_periodo(texto, periodo_pct)
                    todos_gastos = await obtener_gastos(numero_remitente, desde, hasta)
                    gastos_cat = [g for g in todos_gastos if g.get("categoria") == categoria]
                    total_cat = sum(float(g["monto"]) for g in gastos_cat)

                    if detectar_referencia_ingresos(texto):
                        ingresos = await obtener_ingresos(numero_remitente, desde, hasta)
                        base = sum(float(i["monto"]) for i in ingresos)
                    else:
                        base = sum(float(g["monto"]) for g in todos_gastos)

                    porcentaje = (total_cat / base) * 100 if base > 0 else 0.0
                    mensaje_pct = generar_texto_informe(etiqueta, gastos_cat, [], categoria, porcentaje)
                else:
                    # Reparto general: formato de "Reparto del tesoro"
                    desde, hasta = calcular_rango_fechas(periodo_pct)
                    gastos = await obtener_gastos(numero_remitente, desde, hasta)
                    ingresos = await obtener_ingresos(numero_remitente, desde, hasta)
                    total_ingresos = sum(float(i["monto"]) for i in ingresos)
                    mensaje_pct = generar_texto_reparto(periodo_pct, gastos, total_ingresos)

                print(f"📜 Porcentaje {periodo_pct}{' / ' + categoria if categoria else ''} generado para {numero_remitente}")
                await enviar_mensaje_whatsapp(numero_remitente, mensaje_pct)
            elif detectar_solicitud_balance(texto):
                # El usuario pidió su balance (ingresos - gastos)
                periodo_bal = detectar_periodo_generico(texto, default="mensual")
                desde, hasta = calcular_rango_fechas(periodo_bal)
                gastos = await obtener_gastos(numero_remitente, desde, hasta)
                ingresos = await obtener_ingresos(numero_remitente, desde, hasta)
                total_gastos = sum(float(g["monto"]) for g in gastos)
                total_ingresos = sum(float(i["monto"]) for i in ingresos)
                mensaje_balance = generar_texto_balance(periodo_bal, total_ingresos, total_gastos)
                print(f"⚖️ Balance {periodo_bal} generado para {numero_remitente}")
                await enviar_mensaje_whatsapp(numero_remitente, mensaje_balance)
            elif detectar_solicitud_ingreso(texto):
                # El usuario registró un ingreso (ej. "ingreso de 1500 sueldo")
                datos_ingreso = await procesar_ingreso_con_ia(texto)
                print(f"📯 Ingreso registrado: {datos_ingreso}")
                await registrar_ingreso_y_confirmar(numero_remitente, datos_ingreso)
            else:
                # Ninguna palabra clave coincidió: en vez de asumir que es un gasto,
                # le preguntamos a Gemini qué quiso decir realmente.
                clasificacion = await clasificar_mensaje_libre_con_ia(texto)
                intencion = clasificacion.get("intencion")

                if intencion == "ingreso":
                    datos_ingreso = await procesar_ingreso_con_ia(texto)
                    print(f"📯 Ingreso registrado (vía clasificador): {datos_ingreso}")
                    await registrar_ingreso_y_confirmar(numero_remitente, datos_ingreso)

                elif intencion == "corregir_categoria":
                    descripcion_buscada = clasificacion.get("descripcion_buscada", "")
                    categoria_nueva = resolver_categoria(clasificacion.get("categoria_nueva", ""), CATEGORIAS)

                    if not descripcion_buscada or not categoria_nueva:
                        await enviar_mensaje_whatsapp(numero_remitente, "🧌 El escriba no ha logrado interpretar tu petición. ¿Puedes reformularlo? (ej. \"pon el gasto de Jennifer en Refugio y suministros\")")
                    else:
                        candidatos = await buscar_gastos_por_descripcion(numero_remitente, descripcion_buscada)
                        if not candidatos:
                            await enviar_mensaje_whatsapp(numero_remitente, f"🧌 No hallé ningún registro en los libros que coincida con \"{descripcion_buscada}\".")
                        else:
                            gasto = candidatos[0]  # el más reciente
                            ok = await actualizar_categoria_gasto(gasto["id"], categoria_nueva)
                            emoji_cat = CATEGORIA_EMOJIS.get(categoria_nueva, "❓")
                            print(f"📝 Corrección de categoría: '{gasto.get('descripcion')}' -> {categoria_nueva} ({numero_remitente})")
                            if ok:
                                extra = f"\n\n(Había {len(candidatos) - 1} coincidencia(s) más sin modificar; sé más específico si quieres cambiar otra)" if len(candidatos) > 1 else ""
                                await enviar_mensaje_whatsapp(
                                    numero_remitente,
                                    f"🧙🏻‍♂️ Registro reclasificado\n"
                                    f"• {gasto.get('descripcion')} ({formatear_monto_corto(gasto.get('monto'))}) 🪶\n"
                                    f"• Ahora está en: {categoria_nueva}{extra} {emoji_cat}"
                                )
                            else:
                                await enviar_mensaje_whatsapp(numero_remitente, "🧌 El archivero no pudo reclasificar ese gasto. Intenta de nuevo en un momento.")

                elif intencion == "eliminar_transaccion":
                    descripcion_buscada = clasificacion.get("descripcion_buscada", "") or ""
                    tipo_sugerido = clasificacion.get("tipo", "desconocido")
                    await manejar_eliminacion(numero_remitente, descripcion_buscada, tipo_sugerido)

                elif intencion == "no_soportado":
                    respuesta = clasificacion.get("respuesta") or (
                        "🧌 No puedo ayudarte con eso. Puedo registrar tus gastos e ingresos "
                        "(por texto o foto), generar resúmenes, calcular porcentajes/balance, "
                        "corregir categorías, eliminar registros, y exportar tus datos a Excel."
                    )
                    print(f"🧌 Mensaje no soportado de {numero_remitente}: {texto!r}")
                    await enviar_mensaje_whatsapp(numero_remitente, respuesta)

                else:
                    # "gasto" (o clasificación no reconocida, por seguridad)
                    datos = await procesar_gasto_con_ia(texto)
                    print(f"📯 Gasto registrado (Texto): {datos}")
                    await registrar_gasto_y_confirmar(numero_remitente, datos)

        # Procesar fotos (recibo único o lista de transacciones bancarias)
        elif message["type"] == "image":
            media_id = message["image"]["id"]
            imagen = await descargar_imagen_whatsapp(media_id)
            transacciones = await procesar_imagen_transacciones_con_ia(imagen)
            print(f"📯 Transacciones detectadas en imagen ({len(transacciones)}): {transacciones}")

            gastos_guardados = []
            ingresos_guardados = []
            for t in transacciones:
                if t.get("tipo") == "Ingreso":
                    await cerrar_ciclo_y_registrar_ahorro_si_corresponde(numero_remitente, t)
                    await guardar_ingreso(numero_remitente, t)
                    ingresos_guardados.append(t)
                else:
                    await guardar_gasto(numero_remitente, t)
                    gastos_guardados.append(t)

            await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion_lote(gastos_guardados, ingresos_guardados))

            if gastos_guardados:
                total_gastos_lote = sum(float(g.get("monto", 0)) for g in gastos_guardados)
                await enviar_alerta_ahorro_si_corresponde(numero_remitente, total_gastos_lote)

    except Exception as e:
        print(f"⚠️ Error procesando/respondiendo mensaje: {type(e).__name__}: {e}")

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
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    if body.get("object") == "whatsapp_business_account":
        try:
            entry = body["entry"][0]
            changes = entry["changes"][0]
            value = changes["value"]

            if "messages" in value:
                message = value["messages"][0]
                numero_remitente = message["from"]
                message_id = message.get("id", "")

                if ya_procesado(message_id):
                    print(f"🔁 Mensaje duplicado ignorado: {message_id}")
                else:
                    # Se procesa en segundo plano; respondemos a Meta de inmediato.
                    background_tasks.add_task(procesar_mensaje_entrante, message, numero_remitente)

        except (KeyError, IndexError) as e:
            print(f"⚠️ Error parseando webhook: {e}")

    return {"status": "ok"}
