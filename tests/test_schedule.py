from datetime import datetime
from zoneinfo import ZoneInfo

from app.schedule import EstadoHorario, evaluar_horario

TZ = ZoneInfo("America/Bogota")


def _en(h, m):
    return datetime(2026, 7, 4, h, m, tzinfo=TZ)


def test_a_tiempo():
    assert evaluar_horario(_en(10, 0)) == EstadoHorario.A_TIEMPO
    assert evaluar_horario(_en(18, 30)) == EstadoHorario.A_TIEMPO  # justo al corte


def test_tardio_dentro_de_tolerancia():
    assert evaluar_horario(_en(18, 31)) == EstadoHorario.TARDIO
    assert evaluar_horario(_en(19, 0)) == EstadoHorario.TARDIO  # límite de gracia


def test_rechazado():
    assert evaluar_horario(_en(19, 1)) == EstadoHorario.RECHAZADO
    assert evaluar_horario(_en(23, 0)) == EstadoHorario.RECHAZADO
