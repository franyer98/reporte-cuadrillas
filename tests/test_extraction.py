import httpx
import pytest

from app.extraction import FALLBACK, _con_reintentos, _parsear


# ---------- _parsear ----------

def test_parsear_json_valido():
    contenido = '{"texto_corregido": "Se reparó la baranda.", "actividades": [{"descripcion": "Reparación de baranda"}], "novedades": ""}'
    datos = _parsear(contenido, "texto crudo")
    assert datos["texto_corregido"] == "Se reparó la baranda."
    assert len(datos["actividades"]) == 1
    assert datos["novedades"] == ""


def test_parsear_con_bloque_markdown():
    """La IA a veces envuelve el JSON en ```json ... ``` a pesar de la instrucción."""
    contenido = '```json\n{"texto_corregido": "ok", "actividades": [], "novedades": ""}\n```'
    datos = _parsear(contenido, "texto crudo")
    assert datos["texto_corregido"] == "ok"
    assert datos["actividades"] == []


def test_parsear_json_invalido_usa_fallback():
    contenido = "esto no es JSON válido {{{"
    datos = _parsear(contenido, "texto original de la cuadrilla")
    assert datos["texto_corregido"] == "texto original de la cuadrilla"
    assert datos["actividades"] == FALLBACK["actividades"]
    assert datos["novedades"] == FALLBACK["novedades"]


def test_parsear_completa_llaves_faltantes():
    """Si la IA omite alguna llave del JSON, no debe romper el flujo."""
    contenido = '{"texto_corregido": "solo esto"}'
    datos = _parsear(contenido, "crudo")
    assert datos["texto_corregido"] == "solo esto"
    assert datos["actividades"] == []
    assert datos["novedades"] == ""


# ---------- _con_reintentos ----------

def test_reintentos_exito_al_segundo_intento():
    llamadas = {"n": 0}

    def func():
        llamadas["n"] += 1
        if llamadas["n"] < 2:
            raise httpx.ConnectError("falla simulada")
        return "ok"

    resultado = _con_reintentos(func, intentos=3, espera_base=0.01)
    assert resultado == "ok"
    assert llamadas["n"] == 2


def test_reintentos_agota_intentos_y_lanza():
    llamadas = {"n": 0}

    def func():
        llamadas["n"] += 1
        raise httpx.TimeoutException("timeout simulado")

    with pytest.raises(httpx.TimeoutException):
        _con_reintentos(func, intentos=3, espera_base=0.01)
    assert llamadas["n"] == 3


def test_reintentos_no_reintenta_errores_no_transitorios():
    """Un error que no sea de red (ej. ValueError) debe propagarse
    inmediatamente, sin reintentar."""
    llamadas = {"n": 0}

    def func():
        llamadas["n"] += 1
        raise ValueError("error de lógica, no de red")

    with pytest.raises(ValueError):
        _con_reintentos(func, intentos=3, espera_base=0.01)
    assert llamadas["n"] == 1
