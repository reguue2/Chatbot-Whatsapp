"""Alembic environment script.

Este archivo se encarga de preparar el contexto de migraciones
para que Alembic pueda generar y aplicar revisiones en MySQL.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Ajuste de rutas y carga de la Base declarativa
# ---------------------------------------------------------------------------
# Añadimos la carpeta de la aplicación al PYTHONPATH para poder importar
# los módulos existentes del proyecto (settings, models, etc.).
BASE_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BASE_DIR / "Bot_ia_secretaria_peluqueria"
if APP_DIR.exists() and str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Algunos entornos pueden no tener un módulo models/base.py físico.
# Creamos un módulo proxy que expone la misma Base que se declara en
# Bot_ia_secretaria_peluqueria/models.py para cumplir el requisito de
# importar desde ``models.base`` sin modificar la lógica existente.
if "models.base" not in sys.modules:
    models_module = importlib.import_module("models")
    base_proxy = types.ModuleType("models.base")
    base_proxy.Base = models_module.Base
    base_proxy.metadata = models_module.Base.metadata
    sys.modules["models.base"] = base_proxy

# Con el proxy listo, importamos la Base declarativa.
from models.base import Base  # type: ignore  # noqa: E402
from settings import settings  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Configuración general de Alembic
# ---------------------------------------------------------------------------
config = context.config

# Si se definió un fichero de configuración, registramos los loggers para
# que Alembic respete los niveles definidos en alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# target_metadata indica a Alembic de dónde obtener la metadata de las
# tablas. Así podrá detectar cambios en los modelos automáticamente.
target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Resolución de la URL de conexión
# ---------------------------------------------------------------------------
def get_database_url() -> str:
    """Construye la cadena de conexión a partir de settings."""
    try:
        return f"mysql+pymysql://{settings.MYSQL_USER}:{settings.MYSQL_PASS}@{settings.MYSQL_HOST}/{settings.MYSQL_DB}?charset=utf8mb4"
    except Exception as e:
        raise RuntimeError("No se pudo construir la URL de la base de datos a partir de settings") from e



# ---------------------------------------------------------------------------
# Modo offline: genera scripts SQL sin conectarse a la base de datos
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    """Ejecuta las migraciones en modo 'offline'."""

    database_url = get_database_url()

    # Configuramos el contexto con la URL directamente. literal_binds hace que
    # Alembic escriba los parámetros con valores concretos en el SQL generado.
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Modo online: aplica migraciones conectándose a la base de datos
# ---------------------------------------------------------------------------
def run_migrations_online() -> None:
    """Ejecuta las migraciones en modo 'online'."""

    database_url = get_database_url()

    # Tomamos la configuración de alembic.ini y la completamos con la URL.
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url

    # engine_from_config crea el Engine de SQLAlchemy usando NullPool para no
    # compartir conexiones entre procesos de migración.
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # Abrimos la conexión y ejecutamos las migraciones dentro de una transacción.
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


# Punto de entrada: Alembic decide si ejecuta modo online u offline.
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
