from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# Este token lo inventas tú y lo pondrás en el panel de Meta Developer
VERIFY_TOKEN = "mi_token_secreto_finanzas_123"

@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    Paso 1: Meta hace una petición GET aquí para vincular tu app.
    """
    hub_mode = request.query_params.get("hub.mode")
    hub_challenge = request.query_params.get("hub.challenge")
    hub_verify_token = request.query_params.get("hub.verify_token")

    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        print("¡Webhook verificado por Meta!")
        # Meta exige que devuelvas el hub.challenge como número
        return int(hub_challenge)
    
    raise HTTPException(status_code=403, detail="Token de verificación inválido")

@app.post("/webhook")
async def receive_message(request: Request):
    """
    Paso 2: Aquí llegan los mensajes (texto, imágenes) que envíes a tu bot.
    """
    body = await request.json()
    
    # Verificamos que sea un evento de WhatsApp
    if body.get("object") == "whatsapp_business_account":
        try:
            entry = body["entry"][0]
            changes = entry["changes"][0]
            value = changes["value"]
            
            # Filtramos para asegurarnos de que es un mensaje de un usuario
            if "messages" in value:
                message = value["messages"][0]
                sender_phone = message["from"]
                
                if message["type"] == "text":
                    text = message["text"]["body"]
                    print(f"💰 Nuevo gasto recibido de {sender_phone}: {text}")
                    # Aquí enviaremos el texto a la IA
                    
                elif message["type"] == "image":
                    image_id = message["image"]["id"]
                    print(f"📸 Foto de recibo recibida de {sender_phone} (ID: {image_id})")
                    # Aquí descargaremos la foto y se la pasaremos a la IA con OCR
                    
        except (KeyError, IndexError):
            # Ignoramos eventos de estado (como "mensaje entregado" o "leído")
            pass 
            
    # Siempre hay que devolver un 200 OK rapidísimo, si no Meta reintentará el envío
    return {"status": "ok"}
