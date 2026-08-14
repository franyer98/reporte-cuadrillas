"""Webhook de WhatsApp Cloud API + endpoints de administración."""
import hashlib
import hmac
import json
import logging
import secrets

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base, engine, get_db
from app.excel import generar_excel
from app.extraction import extraer_reporte
from app.models import Cuadrilla, Foto, MensajeProcesado, Reporte
from app.schedule import EstadoHorario, ahora_local, evaluar_horario
from app.whatsapp import descargar_foto, enviar_texto

security = HTTPBasic()


def verificar_admin(credenciales: HTTPBasicCredentials = Depends(security)) -> str:
    """Protege endpoints de administración con usuario/clave (HTTP Basic Auth).
    Si ADMIN_PASSWORD no está configurada, bloquea el acceso por defecto
    en vez de dejarlo abierto."""
    if not settings.ADMIN_PASSWORD:
        raise HTTPException(
            503,
            "Este endpoint requiere configurar ADMIN_PASSWORD en las variables "
            "de entorno del servidor antes de poder usarse.",
        )
    usuario_ok = secrets.compare_digest(credenciales.username, settings.ADMIN_USER)
    clave_ok = secrets.compare_digest(credenciales.password, settings.ADMIN_PASSWORD)
    if not (usuario_ok and clave_ok):
        raise HTTPException(401, "Credenciales inválidas",
                             headers={"WWW-Authenticate": "Basic"})
    return credenciales.username


def verificar_firma_whatsapp(cuerpo: bytes, firma_header: str | None) -> bool:
    """Verifica que el payload realmente venga de Meta usando HMAC-SHA256
    con el App Secret. Si WHATSAPP_APP_SECRET no está configurado, no
    verifica (modo compatibilidad) pero deja constancia en el log."""
    if not settings.WHATSAPP_APP_SECRET:
        logger.warning("WHATSAPP_APP_SECRET no configurado: firma del webhook NO verificada")
        return True
    if not firma_header or not firma_header.startswith("sha256="):
        return False
    firma_esperada = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(), cuerpo, hashlib.sha256
    ).hexdigest()
    firma_recibida = firma_header.removeprefix("sha256=")
    return hmac.compare_digest(firma_esperada, firma_recibida)

Base.metadata.create_all(bind=engine)

# Migración ligera: agregar columna de datos binarios si la tabla ya existía
try:
    from sqlalchemy import text as _text
    with engine.begin() as _conn:
        _conn.execute(_text("ALTER TABLE fotos ADD COLUMN IF NOT EXISTS datos BYTEA"))
except Exception:
    pass  # SQLite u otra situación: create_all ya la incluye en tablas nuevas
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
async def recibir(request: Request, db: Session = Depends(get_db)):
    cuerpo = await request.body()
    firma = request.headers.get("X-Hub-Signature-256")
    if not verificar_firma_whatsapp(cuerpo, firma):
        logger.error("Webhook rechazado: firma inválida")
        raise HTTPException(403, "Firma inválida")

    try:
        payload = json.loads(cuerpo)
        cambios = payload["entry"][0]["changes"][0]["value"]
        mensajes = cambios.get("messages", [])
    except (KeyError, IndexError, json.JSONDecodeError):
        return {"status": "ignored"}

    logger.info(f"Webhook: {len(mensajes)} mensaje(s) recibido(s)")
    for msg in mensajes:
        msg_id = msg.get("id")
        if msg_id and db.get(MensajeProcesado, msg_id):
            logger.info(f"Mensaje {msg_id} ya procesado antes; se ignora (reintento de WhatsApp)")
            continue

        logger.info(f"Mensaje de {msg.get('from')} tipo {msg.get('type')}")
        try:
            _procesar_mensaje(msg, db)
            if msg_id:
                db.add(MensajeProcesado(whatsapp_id=msg_id))
                db.commit()
        except Exception as e:
            logger.error(f"ERROR procesando mensaje: {e}")
            db.rollback()  # limpia la sesión por si la transacción quedó rota
            telefono = msg.get("from")
            if telefono:
                try:
                    enviar_texto(telefono,
                        "⚠️ Hubo un problema técnico procesando tu reporte y NO fue "
                        "registrado. Por favor reenvíalo en unos minutos. Si el "
                        "problema persiste, contacta a tu supervisor.")
                except Exception as e2:
                    logger.error(f"No se pudo notificar el error al usuario: {e2}")
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

        if reporte and texto.strip() == reporte.texto_original.strip().split("\n")[-1].strip():
            n_act = len(json.loads(reporte.actividades_json))
            enviar_texto(telefono,
                f"↩️ Este mensaje ya estaba registrado ({n_act} actividad(es) en tu reporte de hoy). "
                "No se duplicó.")
            return

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
        es_anexo = reporte.hora_recepcion != hora  # ya existía un reporte previo hoy
        if es_anexo and estado != EstadoHorario.A_TIEMPO:
            # El reporte del día conserva su estado original; esto es solo un anexo tardío
            aviso = (f"\nℹ️ Anexado fuera de horario ({hora[:5]}); tu reporte de hoy "
                     f"conserva la hora original ({reporte.hora_recepcion[:5]}).")
        elif estado == EstadoHorario.TARDIO:
            aviso = f"\n⚠️ Registrado como EXTEMPORÁNEO ({hora[:5]})."
        else:
            aviso = ""
        enviar_texto(telefono,
            f"✅ Reporte recibido, {cuadrilla.nombre}: {n_act} actividad(es) registradas."
            + aviso)

    elif msg.get("type") == "image":
        if not reporte:
            reporte = Reporte(
                cuadrilla_id=cuadrilla.id, fecha=fecha, hora_recepcion=hora,
                estado_horario=estado.value, texto_original="(solo fotos)",
            )
            db.add(reporte)
            db.commit()
        datos = descargar_foto(msg["image"]["id"])
        db.add(Foto(reporte_id=reporte.id, datos=datos))
        db.commit()
        enviar_texto(telefono, f"📷 Foto recibida y anexada a tu reporte de hoy ({len(reporte.fotos)} en total).")


# ---------- Administración (requiere usuario/clave) ----------
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
            <a class='btn btn-ver' href='/reportes/{fecha}'>👁️ Ver</a>
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
  .btn-ver {{ background: transparent; color: #8B9DFF; border: 1px solid #6C82F5; }}
  .vacio {{ color: #8A93A6; justify-content: center; }}
</style></head>
<body>
  <h1>📋 <span>Reporte</span> Cuadrillas — Historial</h1>
  <p style='color:#8A93A6'>Toca cualquier día para descargar su Excel con todos los reportes.</p>
  <ul>{items}</ul>
</body></html>"""
    return HTMLResponse(html)


@app.get("/reportes/{fecha}")
def ver_reporte(fecha: str, db: Session = Depends(get_db)):
    """Visualizador HTML del reporte del día: mismo contenido del Excel,
    pero para revisar rápido desde el navegador sin descargar nada."""
    import base64
    from fastapi.responses import HTMLResponse

    reportes = (
        db.query(Reporte).filter(Reporte.fecha == fecha).join(Cuadrilla)
        .order_by(Cuadrilla.nombre).all()
    )
    todas = db.query(Cuadrilla).order_by(Cuadrilla.nombre).all()
    reportaron = {r.cuadrilla_id for r in reportes}
    faltantes = [c.nombre for c in todas if c.id not in reportaron]

    def _fotos_html(fotos):
        imgs = ""
        for f in fotos[:6]:
            if f.datos:
                b64 = base64.b64encode(f.datos).decode()
                imgs += f"<img src='data:image/jpeg;base64,{b64}' class='foto' />"
        return f"<div class='fotos'>{imgs}</div>" if imgs else ""

    tarjetas = ""
    for r in reportes:
        actividades = json.loads(r.actividades_json or "[]")
        lista_act = "".join(
            f"<li>{a.get('descripcion', '')}"
            + (f" <b>({a['cantidad']})</b>" if a.get("cantidad") else "")
            + (f" — 📍 {a['lugar']}" if a.get("lugar") else "")
            + "</li>"
            for a in actividades
        ) or f"<li class='sin-estructurar'>{r.texto_corregido or r.texto_original}</li>"

        tardio = r.estado_horario == "TARDIO"
        tarjetas += f"""
        <div class='card {"tardio" if tardio else ""}'>
          <div class='card-head'>
            <span class='nombre'>{r.cuadrilla.nombre}</span>
            <span class='hora'>{r.hora_recepcion[:5]}{" ⚠️ EXTEMPORÁNEO" if tardio else ""}</span>
          </div>
          <ul class='actividades'>{lista_act}</ul>
          {f"<p class='novedades'>📝 {r.novedades}</p>" if r.novedades else ""}
          {_fotos_html(r.fotos)}
        </div>"""

    if faltantes:
        tarjetas += f"""
        <div class='card sin-reporte'>
          <div class='card-head'><span class='nombre'>⛔ Sin reporte</span></div>
          <p>{', '.join(faltantes)}</p>
        </div>"""

    html = f"""<!doctype html>
<html lang='es'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Reporte {fecha} — Cuadrillas</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0D1220; color: #E8ECF4;
         max-width: 720px; margin: 0 auto; padding: 24px 16px; }}
  h1 {{ font-size: 1.3rem; }} h1 span {{ color: #8B9DFF; }}
  .top {{ display: flex; justify-content: space-between; align-items: center;
          margin-bottom: 18px; flex-wrap: wrap; gap: 10px; }}
  .top a {{ color: #8B9DFF; text-decoration: none; font-size: .9rem; }}
  .btn {{ background: #6C82F5; color: #0D1220; font-weight: 600; text-decoration: none;
          padding: 8px 14px; border-radius: 10px; font-size: .9rem; }}
  .card {{ background: #1B2438; border: 1px solid #39465F; border-radius: 14px;
           padding: 16px; margin-bottom: 14px; }}
  .card.tardio {{ border-color: #C0392B; background: #241A1A; }}
  .card.sin-reporte {{ border-color: #C0392B; background: #241A1A; }}
  .card-head {{ display: flex; justify-content: space-between; align-items: center;
                margin-bottom: 8px; flex-wrap: wrap; gap: 6px; }}
  .nombre {{ font-weight: 700; font-size: 1.05rem; }}
  .hora {{ color: #8A93A6; font-size: .85rem; }}
  .actividades {{ margin: 0; padding-left: 20px; }}
  .actividades li {{ margin-bottom: 4px; line-height: 1.4; }}
  .sin-estructurar {{ color: #8A93A6; font-style: italic; }}
  .novedades {{ margin-top: 10px; color: #F5C77E; font-size: .9rem; }}
  .fotos {{ display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }}
  .foto {{ width: 90px; height: 90px; object-fit: cover; border-radius: 8px;
           border: 1px solid #39465F; }}
  .vacio {{ color: #8A93A6; text-align: center; padding: 30px 0; }}
</style></head>
<body>
  <div class='top'>
    <h1>📅 <span>Reporte</span> {fecha}</h1>
    <div>
      <a href='/reportes'>← Historial</a>
      &nbsp;·&nbsp;
      <a class='btn' href='/excel?fecha={fecha}'>⬇️ Excel</a>
    </div>
  </div>
  {tarjetas or "<p class='vacio'>No hay reportes registrados este día.</p>"}
</body></html>"""
    return HTMLResponse(html)


@app.api_route("/", methods=["GET", "HEAD"])
def health():
    return {"status": "ok", "app": "Reporte Cuadrillas"}
