# db.py
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sentry_sdk
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from models import Base
from settings import settings

MYSQL_USER = settings.MYSQL_USER
MYSQL_PASS = settings.MYSQL_PASS
MYSQL_HOST = settings.MYSQL_HOST
MYSQL_DB   = settings.MYSQL_DB

DATABASE_URL = f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASS}@{MYSQL_HOST}:3306/{MYSQL_DB}?charset=utf8mb4"

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=15,         # ≈ 5 × cores
    max_overflow=30,
    pool_recycle=1800,
    pool_timeout=10
)

# --- DEFAULT GLOBAL (solo como respaldo si no estableces TZ por peluquería) ---
# En lugar de fijar Europe/Madrid, dejamos UTC para que no haya desfases
# cuando luego apliques la TZ específica de cada peluquería.
@event.listens_for(engine, "connect")
def set_default_timezone(dbapi_conn, conn_record):
    cur = dbapi_conn.cursor()
    try:
        cur.execute("SET time_zone = '+00:00'")  # UTC por defecto
    except Exception as e:
        sentry_sdk.capture_message(f"No se pudo fijar TZ por defecto (UTC): {e}")
    finally:
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# --- HELPER: fija la TZ de ESTA SESIÓN a la de la peluquería ---
def set_session_time_zone(db_session, tz_name: str | None, fallback_name: str | None = None):
    """
    Establece la zona horaria de la conexión MySQL asociada a 'db_session'.
    1) Intenta por nombre (requiere tablas de zona en MySQL).
    2) Si falla, calcula un offset dinámico (maneja verano/invierno) y lo aplica.
    Si tz_name es None, usa fallback_name; si tampoco, se queda en UTC (default listener).
    """
    tz = tz_name or fallback_name
    if not tz:
        return  # nos quedamos en UTC (por el listener)

    try:
        # 1) Por nombre (ideal si el server tiene tablas de zona cargadas)
        db_session.execute(text("SET time_zone = :tz"), {"tz": tz})
        return
    except Exception as e:
        # 2) Fallback: offset dinámico
        sentry_sdk.capture_message(f"Fallo SET time_zone '{tz}' por nombre. Intento offset. Error: {e}")

    try:
        now = datetime.now(ZoneInfo(tz))
        off: timedelta = now.utcoffset() or timedelta(0)
        total_min = int(off.total_seconds() // 60)
        sign = "+" if total_min >= 0 else "-"
        total_min = abs(total_min)
        hh, mm = divmod(total_min, 60)
        offset_str = f"{sign}{hh:02d}:{mm:02d}"
        db_session.execute(text("SET time_zone = :ofs"), {"ofs": offset_str})
    except Exception as e:
        # Si también falla, registramos pero no rompemos el flujo: seguiremos en UTC.
        sentry_sdk.capture_exception(e)


def init_db():
    Base.metadata.create_all(engine)

if __name__ == "__main__":
    init_db()
    print("¡Tablas creadas en la base de datos!")
