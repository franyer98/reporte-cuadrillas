"""Reglas de horario para la recepción de reportes (opción 3: corte + tolerancia)."""
from datetime import datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

from app.config import settings


class EstadoHorario(str, Enum):
    A_TIEMPO = "A_TIEMPO"
    TARDIO = "TARDIO"        # dentro de la tolerancia → se acepta marcado
    RECHAZADO = "RECHAZADO"  # fuera de la tolerancia → no se registra


def _parse_hora(valor: str) -> time:
    h, m = valor.split(":")
    return time(int(h), int(m))


def ahora_local() -> datetime:
    return datetime.now(ZoneInfo(settings.TIMEZONE))


def evaluar_horario(momento: datetime | None = None) -> EstadoHorario:
    """Clasifica un reporte según la hora de llegada."""
    momento = momento or ahora_local()
    corte = _parse_hora(settings.REPORT_CUTOFF)
    minutos = momento.hour * 60 + momento.minute
    minutos_corte = corte.hour * 60 + corte.minute
    minutos_gracia = minutos_corte + settings.REPORT_GRACE_MINUTES

    if minutos <= minutos_corte:
        return EstadoHorario.A_TIEMPO
    if minutos <= minutos_gracia:
        return EstadoHorario.TARDIO
    return EstadoHorario.RECHAZADO
