import os
import json
from flask import Flask, request, jsonify
import google.generativeai as genai

app = Flask(__name__)

# Configurar API Key de Gemini
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

SYSTEM_INSTRUCTION = """
Eres un asistente experto en finanzas personales. Analiza el mensaje y extrae los datos del gasto.
Categorías válidas: Vivienda, Servicios, Alimentación, Transporte, Comida fuera, Entretenimiento, Suscripciones, Compras, Salud y Bienestar, Finanzas / Pagos, Varios.
Responde ÚNICAMENTE en JSON con la estructura:
{"monto": float, "moneda": "EUR", "categoria": "string", "comercio": "string", "fecha": "YYYY-MM-DD", "concepto": "string"}
"""

model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=SYSTEM_INSTRUCTION,
    generation_config={"response_mime_type": "application/json"}
)

@app.route("/", methods=["GET"])
def home():
    return "Bot de Gastos Activo", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    return jsonify({"status": "recibido"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
