# 📋 Reporte Cuadrillas

> Automatización de reportes diarios de cuadrillas vía WhatsApp → Excel
> FastAPI · WhatsApp Cloud API · Claude API · openpyxl

## Cómo funciona

```
Cuadrilla envía texto + fotos por WhatsApp (chat 1:1 al número del bot)
   → Webhook FastAPI valida horario (corte 6:30 PM + 30 min tolerancia)
   → Claude corrige ortografía y extrae actividades estructuradas
   → Se guarda en base de datos (texto original preservado para auditoría)
   → GET /excel?fecha=YYYY-MM-DD genera el Excel del día con fotos y resumen
```

## Reglas de horario (configurables en .env)

| Hora de llegada | Resultado |
|---|---|
| Hasta las 18:30 | ✔ Registrado a tiempo |
| 18:31 – 19:00 | ⚠️ Registrado como EXTEMPORÁNEO (resaltado en rojo) |
| Después de 19:00 | ⛔ Rechazado, se avisa a la cuadrilla |

## Ejecución local

```bash
pip install -r requirements.txt
cp .env.example .env   # completa tus credenciales
uvicorn app.main:app --reload
```

Documentación interactiva: http://localhost:8000/docs

## Registro de cuadrillas

```bash
curl -X POST "http://localhost:8000/cuadrillas?nombre=Los%20Halcones&telefono=573001234567"
```

## Conexión con WhatsApp Cloud API (resumen)

1. Crea una app en developers.facebook.com (tipo Business) y agrega el producto **WhatsApp**
2. Meta te da un número de prueba gratis para desarrollar; en producción registras tu propio número
3. En la configuración del webhook pon la URL pública `https://tu-servidor/webhook` y el mismo `WHATSAPP_VERIFY_TOKEN` de tu `.env`, y suscríbete al campo `messages`
4. Copia el token de acceso y el `PHONE_NUMBER_ID` a tu `.env`
5. Despliega en Render/Railway (igual que cualquier FastAPI)

## Generar el Excel del día

```
GET /excel?fecha=2026-07-04   → descarga reporte_cuadrillas_2026-07-04.xlsx
```

Incluye: actividades corregidas y estructuradas, novedades, fotos en miniatura,
reportes tardíos resaltados y resumen (a tiempo / extemporáneos / sin reportar).

## Pendiente (siguiente fase)

- Envío automático del Excel por correo a múltiples destinatarios (cron diario)
- Recordatorio por WhatsApp a cuadrillas que no han reportado a las 5:00 PM

## Tests

```bash
python -m pytest tests -v
```
