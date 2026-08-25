import os
import json
import google.generativeai as genai
from PIL import Image

# Configuración del modelo Gemini
genai.configure(api_key=os.environ["GEMINI_API_KEY"])

SYSTEM_INSTRUCTION = """
Eres un asistente experto en finanzas personales. Tu trabajo es analizar mensajes de texto, 
imágenes de tickets/recibos o fragmentos de extractos bancarios y extraer los datos del gasto.

Categorías disponibles exclusivamente:
- Vivienda
- Servicios
- Alimentación
- Transporte
- Comida fuera
- Entretenimiento
- Suscripciones
- Compras
- Salud y Bienestar
- Finanzas / Pagos
- Varios

Debes responder ÚNICAMENTE en formato JSON estricto con la siguiente estructura:
{
  "monto": float,
  "moneda": "EUR" | "USD" | "MXN" etc,
  "categoria": "Nombre de la categoría",
  "comercio": "Nombre del establecimiento o desconocido",
  "fecha": "YYYY-MM-DD",
  "concepto": "Descripción corta del gasto"
}
"""

model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=SYSTEM_INSTRUCTION,
    generation_config={"response_mime_type": "application/json"}
)

def procesar_gasto_texto(texto_usuario):
    response = model.generate_content(texto_usuario)
    return json.loads(response.text)

def procesar_gasto_imagen(ruta_imagen):
    imagen = Image.open(ruta_imagen)
    response = model.generate_content(["Extrae los datos de este ticket o recibo:", imagen])
    return json.loads(response.text)

# --- Ejemplo de uso ---
# resultado_texto = procesar_gasto_texto("Ayer gasté 18.50 euros en el Bar Pepito comiendo con un amigo")
# print(resultado_texto)
