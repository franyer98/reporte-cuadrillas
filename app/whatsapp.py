"""Cliente mínimo de la WhatsApp Cloud API: responder mensajes y descargar fotos."""
import logging
import os
import time
import uuid

import httpx

logger = logging.getLogger("uvicorn.error")

from app.config import settings

GRAPH = "https://graph.facebook.com/v21.0"
HEADERS = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}

ERRORES_TRANSITORIOS = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)


def _con_reintentos(func, intentos: int = 3, espera_base: float = 1.5):
    """Ejecuta func() reintentando ante errores de red transitorios."""
    for intento in range(1, intentos + 1):
        try:
            return func()
        except ERRORES_TRANSITORIOS as e:
            if intento == intentos:
                raise
            espera = espera_base * intento
            logger.warning(
                f"WhatsApp: intento {intento}/{intentos} falló ({type(e).__name__}); "
                f"reintentando en {espera:.1f}s"
            )
            time.sleep(espera)


def enviar_texto(telefono: str, texto: str) -> None:
    """Responde al remitente (mensaje de servicio, gratuito)."""
    try:
        r = _con_reintentos(lambda: httpx.post(
            f"{GRAPH}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "to": telefono,
                "type": "text",
                "text": {"body": texto},
            },
            timeout=15,
        ))
        if r.status_code >= 400:
            logger.error(f"WHATSAPP SEND FALLÓ [{r.status_code}]: {r.text[:400]}")
        else:
            logger.info(f"WhatsApp send OK a {telefono}")
    except Exception as e:
        logger.error(f"WHATSAPP SEND EXCEPCIÓN: {e}")


def descargar_foto(media_id: str) -> bytes:
    """Descarga una imagen recibida y la devuelve comprimida (JPEG) para
    almacenarla en la base de datos de forma persistente."""
    import io

    from PIL import Image

    headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}
    meta = _con_reintentos(
        lambda: httpx.get(f"{GRAPH}/{media_id}", headers=headers, timeout=15)
    ).json()
    binario = _con_reintentos(
        lambda: httpx.get(meta["url"], headers=headers, timeout=30)
    ).content
    img = Image.open(io.BytesIO(binario))
    img.thumbnail((1024, 1024))  # tamaño máximo razonable para reportes
    salida = io.BytesIO()
    img.convert("RGB").save(salida, "JPEG", quality=80)
    return salida.getvalue()
