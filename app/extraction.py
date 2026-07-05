"""Extracción y corrección del reporte con Claude API.

Convierte el texto libre de la cuadrilla (con errores ortográficos,
abreviaciones, etc.) en datos estructurados con redacción profesional,
SIN alterar cantidades, lugares ni hechos reportados.
"""
import json

import anthropic

from app.config import settings

PROMPT = """Eres un asistente que procesa reportes diarios de cuadrillas de campo.

Recibirás el texto crudo de un reporte enviado por WhatsApp. Suele tener mala \
ortografía, abreviaciones y redacción informal.

Tu tarea:
1. Corregir ortografía y redactar profesionalmente, SIN inventar información \
ni alterar cantidades, unidades, lugares o hechos.
2. Extraer las actividades realizadas como lista estructurada.
3. Extraer novedades/pendientes si los hay.

Responde ÚNICAMENTE con JSON válido, sin markdown ni texto adicional:
{
  "texto_corregido": "reporte completo con redacción profesional",
  "actividades": [
    {"descripcion": "...", "cantidad": "40 metros" }
  ],
  "novedades": "pendientes o novedades, o cadena vacía"
}

Reporte crudo:
"""


def extraer_reporte(texto_crudo: str) -> dict:
    # Modo validación gratuito: sin IA, el texto pasa tal cual al Excel
    if not settings.LLM_ENABLED or not settings.ANTHROPIC_API_KEY:
        return {"texto_corregido": texto_crudo, "actividades": [], "novedades": ""}
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    respuesta = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": PROMPT + texto_crudo}],
    )
    contenido = respuesta.content[0].text.strip()
    contenido = contenido.removeprefix("```json").removesuffix("```").strip()
    try:
        datos = json.loads(contenido)
    except json.JSONDecodeError:
        # Degradación elegante: si el LLM falla, se conserva el texto original
        datos = {"texto_corregido": texto_crudo, "actividades": [], "novedades": ""}
    datos.setdefault("texto_corregido", texto_crudo)
    datos.setdefault("actividades", [])
    datos.setdefault("novedades", "")
    return datos
