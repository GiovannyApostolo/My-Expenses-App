import os
import json
import requests
import google.generativeai as genai
from flask import Flask, request, jsonify

app = Flask(__name__)

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

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

                    prompt = (
                        f"Extrae el gasto de este texto: '{texto}'. "
                        "Responde ÚNICAMENTE en formato JSON con las claves: "
                        "monto (numero), moneda (string), categoria (string), comercio (string), concepto (string)."
                    )

                    # Usamos el modelo alias flash compatible con la API REST de genai
                    model = genai.GenerativeModel("gemini-1.5-flash-latest")
                    res_gemini = model.generate_content(prompt)

                    clean_json = res_gemini.text.replace("```json", "").replace("```", "").strip()
                    gasto = json.loads(clean_json)

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
