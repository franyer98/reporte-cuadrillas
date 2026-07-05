"""Cliente mínimo de la WhatsApp Cloud API: responder mensajes y descargar fotos."""
import os
import uuid

import httpx

from app.config import settings

GRAPH = "https://graph.facebook.com/v21.0"
HEADERS = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}


def enviar_texto(telefono: str, texto: str) -> None:
    """Responde al remitente (mensaje de servicio, gratuito)."""
    httpx.post(
        f"{GRAPH}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages",
        headers=HEADERS,
        json={
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "text",
            "text": {"body": texto},
        },
        timeout=15,
    )


def descargar_foto(media_id: str) -> str:
    """Descarga una imagen recibida y devuelve la ruta local."""
    meta = httpx.get(f"{GRAPH}/{media_id}", headers=HEADERS, timeout=15).json()
    binario = httpx.get(meta["url"], headers=HEADERS, timeout=30).content
    os.makedirs(settings.FOTOS_DIR, exist_ok=True)
    ruta = os.path.join(settings.FOTOS_DIR, f"{uuid.uuid4().hex}.jpg")
    with open(ruta, "wb") as f:
        f.write(binario)
    return ruta
