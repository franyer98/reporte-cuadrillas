"""Webhook de WhatsApp Cloud API + endpoints de administración."""
import json
import logging

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base, engine, get_db
from app.excel import generar_excel
from app.extraction import extraer_reporte
from app.models import Cuadrilla, Foto, Reporte
from app.schedule import EstadoHorario, ahora_local, evaluar_horario
from app.whatsapp import descargar_foto, enviar_texto

Base.metadata.create_all(bind=engine)
logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Reporte Cuadrillas", version="1.0.0")


# ---------- Verificación del webhook (Meta lo llama una vez al configurarlo) ----------
@app.get("/webhook")
def verificar(request: Request):
    params = request.query_params
    if (params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == settings.WHATSAPP_VERIFY_TOKEN):
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(403, "Token de verificación inválido")


# ---------- Recepción de mensajes ----------
@app.post("/webhook")
def recibir(payload: dict, db: Session = Depends(get_db)):
    try:
        cambios = payload["entry"][0]["changes"][0]["value"]
        mensajes = cambios.get("messages", [])
    except (KeyError, IndexError):
        return {"status": "ignored"}

    logger.info(f"Webhook: {len(mensajes)} mensaje(s) recibido(s)")
    for msg in mensajes:
        logger.info(f"Mensaje de {msg.get('from')} tipo {msg.get('type')}")
        try:
            _procesar_mensaje(msg, db)
        except Exception as e:
            logger.error(f"ERROR procesando mensaje: {e}")
    return {"status": "ok"}


def _procesar_mensaje(msg: dict, db: Session) -> None:
    telefono = msg.get("from", "")
    cuadrilla = db.query(Cuadrilla).filter(Cuadrilla.telefono == telefono).first()
    if not cuadrilla:
        enviar_texto(telefono,
            "❌ Este número no está registrado como cuadrilla. Contacta al administrador.")
        return

    momento = ahora_local()
    estado = evaluar_horario(momento)
    if estado == EstadoHorario.RECHAZADO:
        enviar_texto(telefono,
            f"⛔ El horario de reportes cerró a las {settings.REPORT_CUTOFF} "
            f"(tolerancia {settings.REPORT_GRACE_MINUTES} min). Tu reporte NO fue "
            "registrado. Contacta a tu supervisor.")
        return

    fecha = momento.strftime("%Y-%m-%d")
    hora = momento.strftime("%H:%M:%S")

    # Un reporte por cuadrilla por día: si ya existe, se le anexa
    reporte = (db.query(Reporte)
               .filter(Reporte.cuadrilla_id == cuadrilla.id, Reporte.fecha == fecha)
               .first())

    if msg.get("type") == "text":
        texto = msg["text"]["body"]
        datos = extraer_reporte(texto)
        if reporte:
            reporte.texto_original += "\n" + texto
            reporte.texto_corregido += "\n" + datos["texto_corregido"]
            previas = json.loads(reporte.actividades_json)
            reporte.actividades_json = json.dumps(previas + datos["actividades"], ensure_ascii=False)
            if datos["novedades"]:
                reporte.novedades = (reporte.novedades + "\n" + datos["novedades"]).strip()
        else:
            reporte = Reporte(
                cuadrilla_id=cuadrilla.id, fecha=fecha, hora_recepcion=hora,
                estado_horario=estado.value, texto_original=texto,
                texto_corregido=datos["texto_corregido"],
                actividades_json=json.dumps(datos["actividades"], ensure_ascii=False),
                novedades=datos["novedades"],
            )
            db.add(reporte)
        db.commit()

        n_act = len(json.loads(reporte.actividades_json))
        aviso_tardio = (f"\n⚠️ Registrado como EXTEMPORÁNEO ({hora[:5]})."
                        if estado == EstadoHorario.TARDIO else "")
        enviar_texto(telefono,
            f"✅ Reporte recibido, {cuadrilla.nombre}: {n_act} actividad(es) registradas."
            + aviso_tardio)

    elif msg.get("type") == "image":
        if not reporte:
            reporte = Reporte(
                cuadrilla_id=cuadrilla.id, fecha=fecha, hora_recepcion=hora,
                estado_horario=estado.value, texto_original="(solo fotos)",
            )
            db.add(reporte)
            db.commit()
        ruta = descargar_foto(msg["image"]["id"])
        db.add(Foto(reporte_id=reporte.id, ruta_local=ruta))
        db.commit()
        enviar_texto(telefono, f"📷 Foto recibida y anexada a tu reporte de hoy ({len(reporte.fotos)} en total).")


# ---------- Administración ----------
@app.post("/cuadrillas")
def registrar_cuadrilla(nombre: str, telefono: str, db: Session = Depends(get_db)):
    """Registra una cuadrilla: nombre + número de WhatsApp (ej. 573001234567)."""
    if db.query(Cuadrilla).filter(Cuadrilla.telefono == telefono).first():
        raise HTTPException(409, "Ese teléfono ya está registrado")
    c = Cuadrilla(nombre=nombre, telefono=telefono)
    db.add(c)
    db.commit()
    return {"id": c.id, "nombre": c.nombre, "telefono": c.telefono}


@app.get("/cuadrillas")
def listar_cuadrillas(db: Session = Depends(get_db)):
    return [{"id": c.id, "nombre": c.nombre, "telefono": c.telefono}
            for c in db.query(Cuadrilla).order_by(Cuadrilla.nombre)]


@app.get("/excel")
def descargar_excel(fecha: str = Query(..., description="YYYY-MM-DD"),
                    db: Session = Depends(get_db)):
    """Genera y descarga el Excel del día indicado."""
    ruta = generar_excel(db, fecha, salida=f"reporte_{fecha}.xlsx")
    return FileResponse(ruta, filename=f"reporte_cuadrillas_{fecha}.xlsx")


# TODO (fase correo): endpoint/cron que genere el Excel del día y lo envíe
# por SMTP a la lista de destinatarios — módulo omitido por ahora a pedido.




# ---------- Utilidad temporal: suscribir la app a la cuenta de WhatsApp Business ----------
@app.get("/admin/subscribe")
def suscribir_waba(waba_id: str):
    """Suscribe esta app a los webhooks de la cuenta de WhatsApp Business (WABA).
    Paso necesario una sola vez; requiere WHATSAPP_TOKEN válido."""
    import httpx as _httpx
    r = _httpx.post(
        f"https://graph.facebook.com/v21.0/{waba_id}/subscribed_apps",
        headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
        timeout=15,
    )
    estado = _httpx.get(
        f"https://graph.facebook.com/v21.0/{waba_id}/subscribed_apps",
        headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
        timeout=15,
    )
    return {"suscripcion": r.json(), "apps_suscritas": estado.json()}


@app.get("/")
def health():
    return {"status": "ok", "app": "Reporte Cuadrillas"}
