import os
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

def procesar_con_gemini(texto):
    api_key = os.getenv("GEMINI_API_KEY")
    # Endpoint v1 estable para evitar problemas con la versión v1beta
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={api_key}"
    
    prompt = (
        f"Extrae el gasto de este texto: '{texto}'. "
        "Responde ÚNICAMENTE en formato JSON plano con las claves exactas: "
        "monto (numero), moneda (string), categoria (string), comercio (string), concepto (string)."
    )
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
    }
    
    response = requests.post(url, json=payload, headers={"Content-Type": "application/json"})
    data = response.json()
    
    print("RESPUESTA RAW GEMINI:", data)
    
    if "candidates" not in data or not data["candidates"]:
        error_msg = data.get('error', {}).get('message', 'Sin respuesta válida de Gemini')
        raise ValueError(f"Error Gemini API: {error_msg}")
        
    texto_res = data['candidates'][0]['content']['parts'][0]['text']
    
    # Limpieza de bloques de código Markdown en la respuesta
    clean_json = texto_res.strip()
    if clean_json.startswith("```"):
        clean_json = clean_json.split("\n", 1)[-1]
        clean_json = clean_json.rsplit("
