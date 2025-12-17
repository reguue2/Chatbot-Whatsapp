"""Plantilla Mako que usa Alembic al generar cada nueva migración.

Cada revisión creada contendrá esta cabecera seguida de los imports
esenciales para trabajar con SQLAlchemy.
"""

"""${message}"""

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

# Alembic siempre incluye estos imports básicos. Añade más según tu caso.
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    """Aplica los cambios de la revisión actual."""
    # Aquí Alembic insertará automáticamente las operaciones necesarias
    # para actualizar el esquema (create_table, add_column, etc.).
    pass


def downgrade() -> None:
    """Revierte los cambios introducidos en upgrade()."""
    # Este bloque recibe las operaciones inversas para dejar la base de
    # datos tal y como estaba antes de la migración correspondiente.
    pass
