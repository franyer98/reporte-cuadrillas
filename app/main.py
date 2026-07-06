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






# ---------- Historial: página con todos los días de reportes ----------
@app.get("/reportes")
def historial_reportes(db: Session = Depends(get_db)):
    """Página HTML con el historial de días que tienen reportes y su Excel."""
    from fastapi.responses import HTMLResponse
    from sqlalchemy import func

    filas = (
        db.query(
            Reporte.fecha,
            func.count(Reporte.id),
            func.sum(func.case((Reporte.estado_horario == "TARDIO", 1), else_=0))
            if False else func.count(Reporte.id),
        )
        .group_by(Reporte.fecha)
        .order_by(Reporte.fecha.desc())
        .all()
    )
    # Conteo de tardíos por fecha
    tardios = dict(
        db.query(Reporte.fecha, func.count(Reporte.id))
        .filter(Reporte.estado_horario == "TARDIO")
        .group_by(Reporte.fecha)
        .all()
    )

    items = "".join(
        f"""<li>
            <span class='fecha'>📅 {fecha}</span>
            <span class='meta'>{total} reporte(s){f" · ⚠️ {tardios[fecha]} tardío(s)" if tardios.get(fecha) else ""}</span>
            <a class='btn' href='/excel?fecha={fecha}'>⬇️ Descargar Excel</a>
        </li>"""
        for fecha, total, _ in filas
    ) or "<li class='vacio'>Aún no hay reportes registrados.</li>"

    html = f"""<!doctype html>
<html lang='es'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Historial de Reportes — Cuadrillas</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0D1220; color: #E8ECF4;
         max-width: 640px; margin: 0 auto; padding: 24px 16px; }}
  h1 {{ font-size: 1.4rem; }} h1 span {{ color: #8B9DFF; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ background: #1B2438; border: 1px solid #39465F; border-radius: 14px;
        padding: 14px 16px; margin-bottom: 10px; display: flex;
        align-items: center; gap: 12px; flex-wrap: wrap; }}
  .fecha {{ font-weight: 600; }}
  .meta {{ color: #8A93A6; font-size: .9rem; flex: 1; }}
  .btn {{ background: #6C82F5; color: #0D1220; font-weight: 600; text-decoration: none;
          padding: 8px 14px; border-radius: 10px; font-size: .9rem; }}
  .vacio {{ color: #8A93A6; justify-content: center; }}
</style></head>
<body>
  <h1>📋 <span>Reporte</span> Cuadrillas — Historial</h1>
  <p style='color:#8A93A6'>Toca cualquier día para descargar su Excel con todos los reportes.</p>
  <ul>{items}</ul>
</body></html>"""
    return HTMLResponse(html)


@app.get("/")
def health():
    return {"status": "ok", "app": "Reporte Cuadrillas"}
