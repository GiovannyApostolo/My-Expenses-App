import os
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

def procesar_con_gemini(texto):
    api_key = os.getenv("GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    
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
    
    # Muestra en logs la respuesta pura para depuración
    print("RESPUESTA RAW GEMINI:", data)
    
    if "candidates" not in data or not data["candidates"]:
        error_msg = data.get('error', {}).get('message', 'Sin respuesta válida de Gemini')
        raise ValueError(f"Error Gemini API: {error_msg}")
        
    texto_res = data['candidates'][0]['content']['parts'][0]['text']
    
    # Limpieza de bloque de código Markdown
    clean_json = texto_res.strip()
    if clean_json.startswith("```"):
        clean_json = clean_json.split("\n", 1)[-1]
        clean_json = clean_json.rsplit("```", 1)[0]
    clean_json = clean_json.strip()
    
    return json.loads(clean_json)

def enviar_mensaje_whatsapp(telefono, texto):
    phone_id = os.getenv("PHONE_NUMBER_ID")
    token = os.getenv("WHATSAPP_TOKEN")
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": texto}
    }
    res = requests.post(url, json=payload, headers=headers)
    print("STATUS ENVIO META:", res.status_code)
    print("RESPUESTA META:", res.text)

@app.route("/", methods=["GET"])
def home():
    return "Bot de Gastos Activo", 200

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        if mode == "subscribe" and token == os.getenv("WEBHOOK_VERIFY_TOKEN", "mi_token_secreto_123"):
            return challenge, 200
        return "Forbidden", 403

    elif request.method == "POST":
        data = request.get_json()
        
        try:
            entry = data.get("entry", [])[0]
            changes = entry.get("changes", [])[0]
            value = changes.get("value", {})
            
            if "messages" in value:
                mensaje_obj = value["messages"][0]
                remitente = mensaje_obj["from"]
                
                if mensaje_obj.get("type") == "text":
                    texto = mensaje_obj["text"]["body"]
                    print(f"--- NUEVO MENSAJE DE {remitente}: {texto} ---")
                    
                    gasto = procesar_con_gemini(texto)
                    
                    respuesta = (
                        f"📝 *Gasto registrado*\n"
                        f"• *Monto:* {gasto.get('monto')} {gasto.get('moneda', 'EUR')}\n"
                        f"• *Categoría:* {gasto.get('categoria')}\n"
                        f"• *Comercio:* {gasto.get('comercio')}\n"
                        f"• *Concepto:* {gasto.get('concepto')}"
                    )
                    
                    enviar_mensaje_whatsapp(remitente, respuesta)
        except Exception as e:
            print("❌ ERROR EN PROCESAMIENTO:", str(e))

        return jsonify({"status": "recibido"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
