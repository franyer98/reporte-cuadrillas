from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Cuadrilla(Base):
    """Registro de cuadrillas: mapea número de WhatsApp → nombre."""
    __tablename__ = "cuadrillas"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120))
    telefono: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # ej: 573001234567

    reportes: Mapped[list["Reporte"]] = relationship(back_populates="cuadrilla")


class Reporte(Base):
    __tablename__ = "reportes"

    id: Mapped[int] = mapped_column(primary_key=True)
    cuadrilla_id: Mapped[int] = mapped_column(ForeignKey("cuadrillas.id"), index=True)
    fecha: Mapped[str] = mapped_column(String(10), index=True)       # YYYY-MM-DD (día del reporte)
    hora_recepcion: Mapped[str] = mapped_column(String(8))           # HH:MM:SS local
    estado_horario: Mapped[str] = mapped_column(String(12))          # A_TIEMPO | TARDIO
    texto_original: Mapped[str] = mapped_column(Text)                # crudo, para auditoría
    texto_corregido: Mapped[str] = mapped_column(Text, default="")   # redacción profesional
    actividades_json: Mapped[str] = mapped_column(Text, default="[]")  # lista estructurada
    novedades: Mapped[str] = mapped_column(Text, default="")
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    cuadrilla: Mapped["Cuadrilla"] = relationship(back_populates="reportes")
    fotos: Mapped[list["Foto"]] = relationship(back_populates="reporte", cascade="all, delete-orphan")


class Foto(Base):
    __tablename__ = "fotos"

    id: Mapped[int] = mapped_column(primary_key=True)
    reporte_id: Mapped[int] = mapped_column(ForeignKey("reportes.id"), index=True)
    ruta_local: Mapped[str] = mapped_column(String(300), default="")
    datos: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)  # JPEG comprimido, persistente

    reporte: Mapped["Reporte"] = relationship(back_populates="fotos")


class MensajeProcesado(Base):
    """Registro de IDs de mensajes de WhatsApp ya procesados, para deduplicar
    reintentos del webhook (Meta puede reenviar el mismo mensaje si no
    responde a tiempo). El id de WhatsApp es único por mensaje."""
    __tablename__ = "mensajes_procesados"

    whatsapp_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    procesado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
