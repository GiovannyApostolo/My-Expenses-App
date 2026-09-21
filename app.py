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
    "Ocio y restauración",
    "Supermercado",
    "Vehiculo y transporte",
    "Suscripciones",
    "Vivienda y servicios",
    "Compras",
    "Regalos",
    "Mascota",
    "Salud y cuidado personal",
    "Educación",
    "Finanzas",
    "Gastos hormiga",
    "Tabaco",
    "Otros",
]
CATEGORIAS_TEXTO = ", ".join(CATEGORIAS)
ACLARACION_CATEGORIAS = (
    "Distingue bien entre estas categorías que se prestan a confusión:\n"
    "- 'Ocio y restauración': comer/beber fuera de casa (restaurantes, bares, cafeterías, "
    "comida a domicilio) y entretenimiento puntual (cine, conciertos, videojuegos, salidas).\n"
    "- 'Suscripciones': CUALQUIER pago recurrente/periódico, sea de entretenimiento o no "
    "(Netflix, Spotify, Tidal, iCloud/Apple Cloud, Google Cloud, ChatGPT Plus, Suno, hosting, "
    "dominios, gimnasio con cuota mensual, etc.).\n"
    "- 'Gastos hormiga': gastos pequeños, impulsivos y cotidianos hechos en la calle o al paso "
    "(un refresco, un café rápido, chicles, prensa, una chocolatina), distintos de una comida "
    "completa en restaurante (que va en 'Ocio y restauración') o de la compra grande de "
    "supermercado.\n"
    "_ 'Tabaco': unicamente gastos en tabaco y cigarros.\n"
    "- 'Regalos': regalos para otras personas (cumpleanos, navidad, aniversarios, etc.), "
    "distintos a otro tipo de compras (que va en 'Compras') "
)

CATEGORIA_EMOJIS = {
    "Ocio y restauración": "🎪",
    "Supermercado": "🥖",
    "Vehiculo y transporte": "🐫",
    "Suscripciones": "🔮",
    "Vivienda y servicios": "🏰",
    "Compras": "🏺",
    "Regalos": "💎",
    "Mascota": "🐴",
    "Salud y cuidado personal": "🍵",
    "Educación": "📖",
    "Finanzas": "🪙",
    "Gastos hormiga": "🐜",
    "Tabaco": "🍂",
    "Otros": "❓",
}

CATEGORIAS_INGRESO = ["Sueldo", "Freelance", "Extras", "Regalo", "Otros"]
CATEGORIAS_INGRESO_TEXTO = ", ".join(CATEGORIAS_INGRESO)
INGRESO_EMOJIS = {
    "Sueldo": "🪓",
    "Freelance": "⚔️",
    "Extras": "✨",
    "Regalo": "💎",
    "Otros": "❓",
    "Reintegros": "🫱🏼‍🫲🏽",
}

def formatear_fecha_hora_actual():
    ahora = datetime.now(ZONA_HORARIA)
    fecha_str = f"{ahora.day} de {MESES_ES[ahora.month]} de {ahora.year}"
    hora_str = ahora.strftime("%H:%M")
    return fecha_str, hora_str

MONEDA_SIMBOLO = "€"
MONEDA_CODIGO = "EUR"

def formatear_monto_corto(monto) -> str:
    try:
        return f"{MONEDA_SIMBOLO}{float(monto):.2f}"
    except (TypeError, ValueError):
        return f"{MONEDA_SIMBOLO}{monto}"

def formatear_monto(monto) -> str:
    try:
        return f"{MONEDA_SIMBOLO}{float(monto):.2f} {MONEDA_CODIGO}"
    except (TypeError, ValueError):
        return f"{MONEDA_SIMBOLO}{monto} {MONEDA_CODIGO}"

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
async def generar_con_reintentos(contenido, intentos=3):
    ultimo_error = None
    for modelo_actual in (MODELO, MODELO_RESPALDO):
        for intento in range(1, intentos + 1):
            try:
                respuesta = client.models.generate_content(
                    model=modelo_actual,
                    contents=contenido,
                    config=GENERATION_CONFIG,
                )
                return json.loads(respuesta.text)
            except Exception as e:
                ultimo_error = e
                es_saturacion = "503" in str(e) or "UNAVAILABLE" in str(e) or "429" in str(e)
                if es_saturacion and intento < intentos:
                    print(f"⏳ {modelo_actual} saturado, intento {intento}/{intentos}, reintentando...")
                    await asyncio.sleep(3 * intento)
                    continue
                break
        es_saturacion_final = "503" in str(ultimo_error) or "UNAVAILABLE" in str(ultimo_error)
        if modelo_actual == MODELO and es_saturacion_final:
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

async def clasificar_mensaje_libre_con_ia(texto_usuario: str) -> dict:
    """Se usa solo cuando ninguna palabra clave (informe/porcentaje/balance/exportar/ingreso)
    coincidió con el mensaje. Decide si realmente es un gasto, un ingreso, una corrección de
    categoría, o algo fuera del alcance del bot, en vez de asumir por defecto que es un gasto."""
    prompt_sistema = f"""
    Eres el clasificador de intención de un bot de finanzas personales por WhatsApp. El bot
    SOLO puede: registrar gastos, registrar ingresos, corregir la categoría de un gasto ya
    registrado, generar resúmenes/informes, calcular porcentajes y balance, y exportar datos
    a Excel. No hace nada más (no agenda, no da consejos generales, no chatea de temas ajenos
    a las finanzas personales del usuario).

    Analiza el mensaje del usuario:
    - Si describe un GASTO real (algo que compró, pagó o gastó, con o sin monto explícito),
      responde: {{"intencion": "gasto"}}
    - Si describe un INGRESO real (dinero que recibió: sueldo, freelance, regalo, venta, reintegro, etc.),
      responde: {{"intencion": "ingreso"}}
    - Si pide CAMBIAR/CORREGIR la categoría de un gasto que ya registró antes (identificándolo
      por su descripción o nombre, ej. "pon el gasto de Jennifer González en Vivienda"),
      responde: {{"intencion": "corregir_categoria", "descripcion_buscada": "el texto que
      identifica el gasto original", "categoria_nueva": "una de [{CATEGORIAS_TEXTO}]"}}
    - Si es cualquier otra cosa (saludo, pregunta general, petición fuera del alcance del bot,
      o un mensaje ambiguo sin relación clara a lo anterior), responde:
      {{"intencion": "no_soportado", "respuesta": "..."}} donde "respuesta" es un mensaje breve,
      amable y en español, explicando que no puedes ayudar con eso, y recordando brevemente
      qué sí puedes hacer (registrar gastos e ingresos por texto o foto, corregir categorías,
      generar resúmenes, calcular porcentajes/balance, y exportar a Excel).

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
    url = f"{SUPABASE_URL}/rest/v1/ingresos"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    payload = {
        "numero": numero,
        "monto": datos.get("monto", 0.0),
        "categoria": datos.get("categoria", "Otros"),
        "descripcion": datos.get("descripcion", ""),
    }
    respuesta = await request_con_reintentos("POST", url, headers, json_payload=payload)
    if respuesta is None or respuesta.status_code not in (200, 201):
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        texto = respuesta.text if respuesta else ""
        print(f"⚠️ Error guardando ingreso en Supabase: {codigo} - {texto}")

async def obtener_ingresos(numero: str, desde: datetime, hasta: datetime):
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
    respuesta = await request_con_reintentos("GET", url, headers, params=params)
    if respuesta is None or respuesta.status_code != 200:
        codigo = respuesta.status_code if respuesta else "sin respuesta"
        print(f"⚠️ Error consultando ingresos en Supabase: {codigo}")
        return []
    return respuesta.json()

# --- 4. DETECCIÓN Y GENERACIÓN DE INFORMES ---
def normalizar(texto: str) -> str:
    texto = texto.lower()
    texto = "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")
    return texto

PALABRAS_INFORME = ["resumen", "informe", "reporte", "detalle", "detallame", "detalla", "muestrame", "cuanto gaste", "cuanto he gastado"]
PERIODOS = {
    "diario": ["diario", "diarios", "de hoy", "hoy"],
    "semanal": ["semanal", "semanales", "semana"],
    "mensual": ["mensual", "mensuales", "mes"],
    "trimestral": ["trimestral", "trimestrales", "trimestre"],
    "anual": ["anual", "anuales", "ano"],
}

# Alias -> nombre exacto de categoría (debe coincidir con CATEGORIAS)
ALIASES_CATEGORIA = {
    "restaurantes": "Ocio y restauración", "restaurante": "Ocio y restauración",
    "restauracion": "Ocio y restauración", "ocio": "Ocio y restauración",
    "supermercado": "Supermercado", "super": "Supermercado",
    "transporte": "Vehiculo y transporte", "vehiculo": "Vehiculo y transporte", "gasolina": "Vehiculo y transporte",
    "suscripciones": "Suscripciones", "suscripcion": "Suscripciones", "subscripciones": "Suscripciones", "subscripcion": "Suscripciones",
    "vivienda": "Vivienda y servicios", "servicios": "Vivienda y servicios", "alquiler": "Vivienda y servicios",
    "compras": "Compras",
    "regalos": "Regalos", "regalo": "Regalos", "cumpleanos": "Regalos",
    "salud": "Salud y cuidado personal", "cuidado personal": "Salud y cuidado personal",
    "educacion": "Educación",
    "finanzas": "Finanzas",
    "hormigas": "Gastos hormiga", "hormiga": "Gastos hormiga",
    "tabaco": "Tabaco", "cigarros": "Tabaco",
    "otros": "Otros",
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

PALABRAS_PORCENTAJE = ["porcentaje", "%", "que parte de mis gastos", "que fraccion"]

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

def detectar_categoria_informe(texto: str):
    texto_norm = normalizar(texto)
    # Alias más largos primero, para que "vivienda" no se coma casos más específicos, etc.
    for alias in sorted(ALIASES_CATEGORIA.keys(), key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", texto_norm):
            return ALIASES_CATEGORIA[alias]
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
    return f". {descripcion} - {formatear_monto_corto(item.get('monto', 0))}"

def generar_texto_informe(etiqueta: str, gastos: list, ingresos: list, categoria: str = None) -> str:
    # --- Informe filtrado por una sola categoría de gasto (sin sección de ingresos/balance) ---
    if categoria:
        emoji_cat = CATEGORIA_EMOJIS.get(categoria, "❓")
        titulo = f"📜 Resumen de {etiqueta} — {emoji_cat} {categoria}"
        if not gastos:
            return f"{titulo}\n\nNo tienes gastos registrados en este período. 🎊"
        total = sum(float(g["monto"]) for g in gastos)
        lineas = [titulo, "", f"🪽 Total: {formatear_monto(total)}", ""]
        for g in sorted(gastos, key=lambda x: x.get("fecha", "")):
            lineas.append(formatear_linea_transaccion(g))
        return "\n".join(lineas)

    # --- Informe general: gastos por categoría + ingresos por categoría + balance ---
    titulo = f"📜 Resumen de {etiqueta}"
    total_gastos = sum(float(g["monto"]) for g in gastos)
    total_ingresos = sum(float(i["monto"]) for i in ingresos)

    lineas = [titulo, "", f"🪽 Total gastos: {formatear_monto(total_gastos)}"]

    if gastos:
        por_categoria = {}
        for g in gastos:
            por_categoria.setdefault(g.get("categoria", "Otros"), []).append(g)
        lineas.append("")
        lineas.append("Por categoría:")
        for cat, lista in sorted(por_categoria.items(), key=lambda kv: -sum(float(x["monto"]) for x in kv[1])):
            total_cat = sum(float(x["monto"]) for x in lista)
            emoji_cat = CATEGORIA_EMOJIS.get(cat, "❓")
            lineas.append(f"{emoji_cat} {cat}: {formatear_monto_corto(total_cat)}")
            for g in sorted(lista, key=lambda x: x.get("fecha", "")):
                lineas.append(formatear_linea_transaccion(g))

    lineas.append("")
    lineas.append(f"🪎 Total ingresos: {formatear_monto(total_ingresos)}")

    if ingresos:
        por_categoria_ing = {}
        for i in ingresos:
            cat = i.get("categoria", "Otros")
            por_categoria_ing[cat] = por_categoria_ing.get(cat, 0.0) + float(i["monto"])
        lineas.append("")
        lineas.append("Por categoría:")
        for cat, total_cat in sorted(por_categoria_ing.items(), key=lambda x: -x[1]):
            emoji_cat = INGRESO_EMOJIS.get(cat, "❓")
            lineas.append(f"{emoji_cat} {cat}: {formatear_monto_corto(total_cat)}")

    balance = total_ingresos - total_gastos
    signo = "+" if balance >= 0 else "-"
    lineas.append("")
    if total_ingresos > 0:
        pct_gastado = (total_gastos / total_ingresos) * 100
        lineas.append(f"🧮 Balance: {signo}{formatear_monto_corto(abs(balance))} ({pct_gastado:.1f}% de tus ingresos gastado)")
    else:
        lineas.append(f"🧮 Balance: {signo}{formatear_monto_corto(abs(balance))}")

    return "\n".join(lineas)

def generar_texto_porcentaje(periodo: str, gastos: list, categoria: str = None):
    etiqueta_periodo = ETIQUETAS_PERIODO.get(periodo, "en el período")

    if not gastos:
        return f"📜 No tienes gastos registrados {etiqueta_periodo}. 🎊"

    total = sum(float(g["monto"]) for g in gastos)
    if total == 0:
        return f"📜 No tienes gastos registrados {etiqueta_periodo}. 🎊"

    if categoria:
        monto_categoria = sum(float(g["monto"]) for g in gastos if g.get("categoria") == categoria)
        porcentaje = (monto_categoria / total) * 100
        return (
            f"📜 {categoria} representa el {porcentaje:.1f}% de tus gastos {etiqueta_periodo}\n"
            f"({monto_categoria:.2f} de {total:.2f} en total)"
        )

    # Sin categoría específica: desglose de porcentaje por cada categoría
    por_categoria = {}
    for g in gastos:
        cat = g.get("categoria", "Otros")
        por_categoria[cat] = por_categoria.get(cat, 0.0) + float(g["monto"])

    lineas = [f"📜 Distribución de gastos {etiqueta_periodo}", "", f"🪎 Total: {total:.2f}", ""]
    for cat, monto in sorted(por_categoria.items(), key=lambda x: -x[1]):
        porcentaje = (monto / total) * 100
        lineas.append(f"  🔖 {cat}: {porcentaje:.1f}% ({monto:.2f})")

    return "\n".join(lineas)

def generar_texto_porcentaje_ingresos(periodo: str, gastos: list, total_ingresos: float, categoria: str = None):
    etiqueta_periodo = ETIQUETAS_PERIODO.get(periodo, "en el período")

    if total_ingresos <= 0:
        return f"⚠️ No tienes ingresos registrados {etiqueta_periodo}, así que no puedo calcular el porcentaje sobre tus ingresos."

    if categoria:
        monto_categoria = sum(float(g["monto"]) for g in gastos if g.get("categoria") == categoria)
        porcentaje = (monto_categoria / total_ingresos) * 100
        return (
            f"📜 {categoria} representa el {porcentaje:.1f}% de tus ingresos {etiqueta_periodo}\n"
            f"({monto_categoria:.2f} de {total_ingresos:.2f} de ingresos)"
        )

    por_categoria = {}
    for g in gastos:
        cat = g.get("categoria", "Otros")
        por_categoria[cat] = por_categoria.get(cat, 0.0) + float(g["monto"])

    total_gastos = sum(por_categoria.values())
    ahorro = total_ingresos - total_gastos

    lineas = [f"📜 Gastos {etiqueta_periodo} sobre tus ingresos", "", f"🪎 Ingresos: {total_ingresos:.2f}", ""]
    for cat, monto in sorted(por_categoria.items(), key=lambda x: -x[1]):
        porcentaje = (monto / total_ingresos) * 100
        lineas.append(f"  🔖 {cat}: {porcentaje:.1f}% ({monto:.2f})")
    porcentaje_ahorro = (ahorro / total_ingresos) * 100
    lineas.append("")
    lineas.append(f"  🛡️ Restante/Ahorro: {porcentaje_ahorro:.1f}% ({ahorro:.2f})")

    return "\n".join(lineas)

def generar_texto_balance(periodo: str, total_ingresos: float, total_gastos: float):
    etiqueta_periodo = ETIQUETAS_PERIODO.get(periodo, "en el período")
    balance = total_ingresos - total_gastos

    lineas = [
        f"💼 Balance {etiqueta_periodo}",
        "",
        f"🪎 Ingresos: {total_ingresos:.2f}",
        f"🪽 Gastos: {total_gastos:.2f}",
        f"🧮 Balance: {balance:.2f}",
    ]
    if total_ingresos > 0:
        pct_gastado = (total_gastos / total_ingresos) * 100
        lineas.append(f"📜 Has gastado el {pct_gastado:.1f}% de tus ingresos")
    else:
        lineas.append("🧌 No tienes ingresos registrados en este período.")

    return "\n".join(lineas)

def formatear_confirmacion_ingreso(datos: dict) -> str:
    if not datos.get("monto"):
        return "⚠️ No pude procesar ese ingreso. ¿Puedes intentar describirlo de otra forma?"
    fecha_str, hora_str = formatear_fecha_hora_actual()
    categoria = datos.get("categoria", "Otros")
    emoji_categoria = INGRESO_EMOJIS.get(categoria, "❓")
    return (
        "✅ Transacción Registrada\n"
        f"• 🪎 Monto: {formatear_monto(datos.get('monto'))}\n"
        f"• 🔖 Categoría: {emoji_categoria} {categoria}\n"
        f"• 🪶 Descripción: {datos.get('descripcion')}\n"
        f"• 📅 Fecha: {fecha_str}\n"
        f"• ⌛️ Hora: {hora_str}\n"
        f"• 🔄 Tipo: Ingreso"
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
    ws.title = "Resumen"

    total_gastos = sum(float(g.get("monto", 0)) for g in gastos)
    total_ingresos = sum(float(i.get("monto", 0)) for i in ingresos)

    por_categoria_gasto = {}
    for g in gastos:
        cat = g.get("categoria", "Otros")
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
        ws.cell(row=fila, column=2, value=i.get("categoria", "Otros"))
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
        ws.cell(row=fila, column=2, value=g.get("categoria", "Otros"))
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
    fecha_str, hora_str = formatear_fecha_hora_actual()
    categoria = datos.get("categoria", "Otros")
    emoji_categoria = CATEGORIA_EMOJIS.get(categoria, "❓")
    return (
        "✅ Transacción Registrada\n"
        f"• 🪎 Monto: {formatear_monto(datos.get('monto'))}\n"
        f"• 🔖 Categoría: {emoji_categoria} {categoria}\n"
        f"• 🪶 Descripción: {datos.get('descripcion')}\n"
        f"• 📅 Fecha: {fecha_str}\n"
        f"• ⌛️ Hora: {hora_str}\n"
        f"• 🔄 Tipo: Gasto"
    )

def formatear_confirmacion_lote(gastos: list, ingresos: list) -> str:
    total = len(gastos) + len(ingresos)
    if total == 0:
        return "⚠️ No identifiqué ninguna transacción en esa imagen. ¿Puedes intentar con una foto más clara?"

    fecha_str, hora_str = formatear_fecha_hora_actual()
    lineas = [f"✅ {total} transacciones registradas", f"📅 {fecha_str} — ⏳ {hora_str}", ""]

    if gastos:
        total_gastos = sum(float(g.get("monto", 0)) for g in gastos)
        lineas.append(f"🪽 Gastos ({formatear_monto_corto(total_gastos)}):")
        for g in gastos:
            emoji_cat = CATEGORIA_EMOJIS.get(g.get("categoria", "Otros"), "❓")
            lineas.append(f". {emoji_cat} {g.get('descripcion','')} — {g.get('categoria','')} - {formatear_monto_corto(g.get('monto', 0))}")
        lineas.append("")

    if ingresos:
        total_ingresos = sum(float(i.get("monto", 0)) for i in ingresos)
        lineas.append(f"🪎 Ingresos ({formatear_monto_corto(total_ingresos)}):")
        for i in ingresos:
            emoji_cat = INGRESO_EMOJIS.get(i.get("categoria", "Otros"), "❓")
            lineas.append(f". {emoji_cat} {i.get('descripcion','')} — {i.get('categoria','')} - {formatear_monto_corto(i.get('monto', 0))}")

    return "\n".join(lineas).rstrip()

# --- 6. RUTAS DEL WEBHOOK ---
async def procesar_mensaje_entrante(message: dict, numero_remitente: str):
    try:
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
                    await enviar_mensaje_whatsapp(numero_remitente, f"No tienes gastos ni ingresos registrados en {etiqueta_caption} para exportar.")
                else:
                    contenido_excel = generar_excel_gastos(gastos, ingresos)
                    nombre_archivo = f"{nombre_archivo_base}.xlsx"
                    media_id = await subir_documento_whatsapp(contenido_excel, nombre_archivo)
                    if media_id:
                        caption = f"🧧 Gastos — {etiqueta_caption} ({len(gastos)} registros)"
                        await enviar_documento_whatsapp(numero_remitente, media_id, nombre_archivo, caption)
                    else:
                        await enviar_mensaje_whatsapp(numero_remitente, "⚠️ No pude generar el archivo de exportación. Intenta de nuevo en un momento.")
            elif periodo:
                # El usuario pidió un resumen/informe (opcionalmente filtrado por categoría,
                # y opcionalmente con mes/trimestre específico, ej. "resumen de julio")
                categoria = detectar_categoria_informe(texto)
                desde, hasta, etiqueta = resolver_periodo(texto, periodo)
                gastos = await obtener_gastos(numero_remitente, desde, hasta, categoria)
                ingresos = [] if categoria else await obtener_ingresos(numero_remitente, desde, hasta)
                informe = generar_texto_informe(etiqueta, gastos, ingresos, categoria)

                print(f"📜 Informe '{etiqueta}'{' / ' + categoria if categoria else ''} generado para {numero_remitente} ({len(gastos)} gastos)")
                await enviar_mensaje_whatsapp(numero_remitente, informe)
            elif detectar_solicitud_porcentaje(texto):
                # El usuario pidió un porcentaje (ej. "qué % de mis gastos/ingresos es ocio")
                categoria = detectar_categoria_informe(texto)
                periodo_pct = detectar_periodo_generico(texto, default="mensual")
                desde, hasta = calcular_rango_fechas(periodo_pct)
                gastos = await obtener_gastos(numero_remitente, desde, hasta)  # sin filtro: necesitamos el total

                if detectar_referencia_ingresos(texto):
                    ingresos = await obtener_ingresos(numero_remitente, desde, hasta)
                    total_ingresos = sum(float(i["monto"]) for i in ingresos)
                    mensaje_pct = generar_texto_porcentaje_ingresos(periodo_pct, gastos, total_ingresos, categoria)
                else:
                    mensaje_pct = generar_texto_porcentaje(periodo_pct, gastos, categoria)

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
                print(f"💼 Balance {periodo_bal} generado para {numero_remitente}")
                await enviar_mensaje_whatsapp(numero_remitente, mensaje_balance)
            elif detectar_solicitud_ingreso(texto):
                # El usuario registró un ingreso (ej. "ingreso de 1500 sueldo")
                datos_ingreso = await procesar_ingreso_con_ia(texto)
                print(f"✅ Ingreso registrado: {datos_ingreso}")
                if datos_ingreso.get("monto"):
                    await guardar_ingreso(numero_remitente, datos_ingreso)
                await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion_ingreso(datos_ingreso))
            else:
                # Ninguna palabra clave coincidió: en vez de asumir que es un gasto,
                # le preguntamos a Gemini qué quiso decir realmente.
                clasificacion = await clasificar_mensaje_libre_con_ia(texto)
                intencion = clasificacion.get("intencion")

                if intencion == "ingreso":
                    datos_ingreso = await procesar_ingreso_con_ia(texto)
                    print(f"✅ Ingreso registrado (vía clasificador): {datos_ingreso}")
                    if datos_ingreso.get("monto"):
                        await guardar_ingreso(numero_remitente, datos_ingreso)
                    await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion_ingreso(datos_ingreso))

                elif intencion == "corregir_categoria":
                    descripcion_buscada = clasificacion.get("descripcion_buscada", "")
                    categoria_nueva = resolver_categoria(clasificacion.get("categoria_nueva", ""), CATEGORIAS)

                    if not descripcion_buscada or not categoria_nueva:
                        await enviar_mensaje_whatsapp(numero_remitente, "⚠️ No entendí bien qué gasto o categoría quieres cambiar. ¿Puedes reformularlo? (ej. \"pon el gasto de Jennifer en Vivienda y servicios\")")
                    else:
                        candidatos = await buscar_gastos_por_descripcion(numero_remitente, descripcion_buscada)
                        if not candidatos:
                            await enviar_mensaje_whatsapp(numero_remitente, f"⚠️ No encontré ningún gasto que coincida con \"{descripcion_buscada}\".")
                        else:
                            gasto = candidatos[0]  # el más reciente
                            ok = await actualizar_categoria_gasto(gasto["id"], categoria_nueva)
                            emoji_cat = CATEGORIA_EMOJIS.get(categoria_nueva, "❓")
                            print(f"🪶 Corrección de categoría: '{gasto.get('descripcion')}' -> {categoria_nueva} ({numero_remitente})")
                            if ok:
                                extra = f"\n\n(Había {len(candidatos) - 1} coincidencia(s) más sin modificar; sé más específico si quieres cambiar otra)" if len(candidatos) > 1 else ""
                                await enviar_mensaje_whatsapp(
                                    numero_remitente,
                                    f"✅ Categoría actualizada\n"
                                    f"• 🪶 {gasto.get('descripcion')} ({formatear_monto_corto(gasto.get('monto'))})\n"
                                    f"• {emoji_cat} Ahora está en: {categoria_nueva}{extra}"
                                )
                            else:
                                await enviar_mensaje_whatsapp(numero_remitente, "⚠️ No pude actualizar la categoría. Intenta de nuevo en un momento.")

                elif intencion == "no_soportado":
                    respuesta = clasificacion.get("respuesta") or (
                        "🧌 No puedo ayudarte con eso. Puedo registrar tus gastos e ingresos "
                        "(por texto o foto), generar resúmenes, calcular porcentajes/balance, "
                        "y exportar tus datos a Excel."
                    )
                    print(f"🧌 Mensaje no soportado de {numero_remitente}: {texto!r}")
                    await enviar_mensaje_whatsapp(numero_remitente, respuesta)

                else:
                    # "gasto" (o clasificación no reconocida, por seguridad)
                    datos = await procesar_gasto_con_ia(texto)
                    print(f"✅ Gasto registrado (Texto): {datos}")
                    if datos.get("categoria") != "Error":
                        await guardar_gasto(numero_remitente, datos)
                    await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion(datos))

        # Procesar fotos (recibo único o lista de transacciones bancarias)
        elif message["type"] == "image":
            media_id = message["image"]["id"]
            imagen = await descargar_imagen_whatsapp(media_id)
            transacciones = await procesar_imagen_transacciones_con_ia(imagen)
            print(f"✅ Transacciones detectadas en imagen ({len(transacciones)}): {transacciones}")

            gastos_guardados = []
            ingresos_guardados = []
            for t in transacciones:
                if t.get("tipo") == "Ingreso":
                    await guardar_ingreso(numero_remitente, t)
                    ingresos_guardados.append(t)
                else:
                    await guardar_gasto(numero_remitente, t)
                    gastos_guardados.append(t)

            await enviar_mensaje_whatsapp(numero_remitente, formatear_confirmacion_lote(gastos_guardados, ingresos_guardados))

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
