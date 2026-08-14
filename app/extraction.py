"""Extracción y corrección del reporte con IA (Gemini o Claude).

Convierte el texto libre de la cuadrilla (con errores ortográficos,
abreviaciones, etc.) en datos estructurados con redacción profesional,
SIN alterar cantidades, lugares ni hechos reportados.

Proveedores soportados (se usa el que tenga clave configurada):
- GEMINI_API_KEY  → Google Gemini (capa gratuita)
- ANTHROPIC_API_KEY → Claude API
"""
import json
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger("uvicorn.error")

ERRORES_TRANSITORIOS = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)


def _con_reintentos(func, intentos: int = 3, espera_base: float = 1.5):
    """Ejecuta func() reintentando ante errores de red transitorios
    (timeouts, caídas de conexión). No reintenta errores de otro tipo
    (ej. clave inválida, request mal formado)."""
    for intento in range(1, intentos + 1):
        try:
            return func()
        except ERRORES_TRANSITORIOS as e:
            if intento == intentos:
                raise
            espera = espera_base * intento
            logger.warning(
                f"IA: intento {intento}/{intentos} falló ({type(e).__name__}); "
                f"reintentando en {espera:.1f}s"
            )
            time.sleep(espera)

PROMPT = """Eres un asistente que procesa reportes diarios de cuadrillas de campo.

Recibirás el texto crudo de un reporte enviado por WhatsApp. Suele tener mala \
ortografía, abreviaciones y redacción informal.

Tu tarea:
1. Corregir ortografía y redactar profesionalmente, SIN inventar información \
ni alterar cantidades, unidades, lugares o hechos.
2. Extraer las actividades realizadas como lista estructurada. Cada actividad \
DEBE conservar el lugar donde se realizó (sector, barrio, dirección, vía) si el \
reporte lo menciona — el lugar es información crítica que nunca debe perderse.
3. Extraer novedades/pendientes si los hay.
4. Si el texto no describe trabajo (saludos, pruebas, emojis), devuelve \
actividades vacías y el texto corregido tal cual corresponda.

Responde ÚNICAMENTE con JSON válido, sin markdown ni texto adicional:
{
  "texto_corregido": "reporte completo con redacción profesional",
  "actividades": [
    {"descripcion": "...", "cantidad": "40 metros", "lugar": "sector norte"}
  ],
  "novedades": "pendientes o novedades, o cadena vacía"
}

Reporte crudo:
"""

FALLBACK = {"actividades": [], "novedades": ""}


def _parsear(contenido: str, texto_crudo: str) -> dict:
    contenido = contenido.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        datos = json.loads(contenido)
    except json.JSONDecodeError:
        logger.warning("IA devolvió JSON inválido; se conserva el texto original")
        datos = {"texto_corregido": texto_crudo, **FALLBACK}
    datos.setdefault("texto_corregido", texto_crudo)
    datos.setdefault("actividades", [])
    datos.setdefault("novedades", "")
    return datos


def _extraer_con_gemini(texto_crudo: str) -> dict:
    def _llamar():
        return httpx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash-lite:generateContent",
            headers={"x-goog-api-key": settings.GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": PROMPT + texto_crudo}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
            timeout=30,
        )

    r = _con_reintentos(_llamar)
    if r.status_code >= 400:
        logger.error(f"GEMINI FALLÓ [{r.status_code}]: {r.text[:300]}")
        return {"texto_corregido": texto_crudo, **FALLBACK}
    contenido = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _parsear(contenido, texto_crudo)


def _extraer_con_claude(texto_crudo: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    def _llamar():
        return client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": PROMPT + texto_crudo}],
        )

    errores_claude = (anthropic.APIConnectionError, anthropic.APITimeoutError,
                       anthropic.InternalServerError)
    respuesta = None
    for intento in range(1, 4):
        try:
            respuesta = _llamar()
            break
        except errores_claude as e:
            if intento == 3:
                raise
            espera = 1.5 * intento
            logger.warning(
                f"Claude: intento {intento}/3 falló ({type(e).__name__}); "
                f"reintentando en {espera:.1f}s"
            )
            time.sleep(espera)
    return _parsear(respuesta.content[0].text, texto_crudo)


def extraer_reporte(texto_crudo: str) -> dict:
    # Modo validación gratuito: sin IA, el texto pasa tal cual al Excel
    if not settings.LLM_ENABLED:
        return {"texto_corregido": texto_crudo, **FALLBACK}
    try:
        if settings.GEMINI_API_KEY:
            return _extraer_con_gemini(texto_crudo)
        if settings.ANTHROPIC_API_KEY:
            return _extraer_con_claude(texto_crudo)
        logger.warning("LLM_ENABLED=true pero no hay clave de IA configurada")
    except Exception as e:
        logger.error(f"IA EXCEPCIÓN: {e}")
    return {"texto_corregido": texto_crudo, **FALLBACK}
