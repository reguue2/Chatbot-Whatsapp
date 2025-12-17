from sqlalchemy import (
    Column, Integer, String, Float, ForeignKey, Date, Time, Index, text, UniqueConstraint, JSON, Boolean
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.dialects.mysql import TIMESTAMP as MySQLTimestamp

Base = declarative_base()


class Peluqueria(Base):
    __tablename__ = 'peluquerias'
    id = Column(Integer, primary_key=True, autoincrement=True)
    nombre = Column(String(100))
    tipo_negocio = Column(String(50), default="peluquería")
    direccion = Column(String(150))

    dias_cerrados = Column(String(100))  # "lunes,domingo"
    horario = Column(String(200))  # "09:00-14:00,16:00-20:00"
    country_code = Column(String(2))  # ISO 3166-1 alpha-2, ej: "UY"
    tz = Column(String(64))  # zoneinfo: "America/Montevideo"
    currency_code = Column(String(3))  # ISO 4217: "UYU"
    locale = Column(String(10))  # "es_UY", "es_MX", ...
    telefono_peluqueria = Column(String(20))  # almacenar E.164 (+598..., +52...)
    cal_id = Column(String(120))  # Calendar ID
    api_key = Column(String(120), nullable=False, unique=True, index=True)  # para auth del webhook
    dias_cerrados_anio = Column(MutableDict.as_mutable(JSON), default=dict)
    info = Column(String(500))  # info libre
    num_peluqueros = Column(Integer, nullable=False, default=1)
    rango_reservas = Column(Integer, nullable=False, default=30)  # tamaño del slot en minutos
    min_avance_min  = Column(Integer, nullable=False, server_default="60")   # minutos mínima antelación
    max_avance_dias = Column(Integer, nullable=False, server_default="150")  # días máxima antelación
    wa_phone_number_id = Column(String(32), unique=True)
    wa_token = Column(String(512), nullable=False, default="")
    wa_business_id = Column(String(32), nullable=False, default="")
    enable_peluquero_selection = Column(Boolean, nullable=False, default=False)
    peluquero_selection_required = Column(Boolean, nullable=False, default=False)

    servicios = relationship("Servicio", back_populates="peluqueria", cascade="all, delete-orphan")
    reservas = relationship("Reserva", back_populates="peluqueria", cascade="all, delete-orphan")
    peluqueros = relationship("Peluquero", back_populates="peluqueria", cascade="all, delete-orphan")

class Peluquero(Base):
    __tablename__ = "peluqueros"
    id = Column(Integer, primary_key=True)
    peluqueria_id = Column(Integer, ForeignKey("peluquerias.id", ondelete="CASCADE"), nullable=False, index=True)
    nombre = Column(String(120), nullable=False)
    activo = Column(Boolean, nullable=False, default=True)
    orden = Column(Integer, nullable=False, default=100)

    peluqueria = relationship("Peluqueria", back_populates="peluqueros")
    reservas = relationship("Reserva", back_populates="peluquero")

    __table_args__ = (
        UniqueConstraint("peluqueria_id", "nombre", name="uq_peluquero_por_peluqueria"),
    )

class Servicio(Base):
    __tablename__ = 'servicios'
    id = Column(Integer, primary_key=True, autoincrement=True)
    peluqueria_id = Column(Integer, ForeignKey('peluquerias.id'), nullable=False, index=True)
    nombre = Column(String(120), nullable=False)
    descripcion = Column(String(255), nullable=True)
    precio = Column(Float, default=0.0)
    duracion_min = Column(Integer, nullable=False, default=30)

    peluqueria = relationship("Peluqueria", back_populates="servicios")
    reservas = relationship("Reserva", back_populates="servicio")


class Reserva(Base):
    __tablename__ = 'reservas'
    id = Column(Integer, primary_key=True, autoincrement=True)

    peluqueria_id = Column(Integer, ForeignKey('peluquerias.id'), nullable=False, index=True)
    servicio_id = Column(Integer, ForeignKey('servicios.id'), nullable=False, index=True)

    nombre_cliente = Column(String(100), nullable=False)
    telefono = Column(String(50), nullable=False, index=True)

    fecha = Column(Date, nullable=False, index=True)  # YYYY-MM-DD
    hora = Column(Time, nullable=False)  # HH:MM:SS

    estado = Column(String(20), nullable=False, default="confirmada", index=True)
    event_id = Column(String(256), nullable=True, index=True)
    peluquero_id = Column(Integer, ForeignKey("peluqueros.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(
        MySQLTimestamp(fsp=0),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at = Column(
        MySQLTimestamp(fsp=0),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP")
    )

    peluquero = relationship("Peluquero", back_populates="reservas")
    peluqueria = relationship("Peluqueria", back_populates="reservas")
    servicio = relationship("Servicio", back_populates="reservas", lazy="selectin")

    __table_args__ = (
        Index("ix_reservas_pelu_fecha", "peluqueria_id", "fecha"),
        Index("ix_reservas_pelu_fecha_hora", "peluqueria_id", "fecha", "hora"),
        Index("ix_reservas_pelu_fecha_estado", "peluqueria_id", "fecha", "estado"),
        Index("ix_reservas_peluquero_fecha", "peluquero_id", "fecha", "hora")
    )

