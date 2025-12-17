"""Ensure columns min_avance_min and max_avance_dias exist on peluquerias."""

from alembic import op
import sqlalchemy as sa

# --- IDs de Alembic ---
revision = "994f4cad19a1"
down_revision = "674b8fb62c8a"  # ajusta si tu head es otro
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Añade las columnas si no existen (compatible con MySQL 5.7+/8)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("peluquerias")}

    if "min_avance_min" not in existing_cols:
        op.add_column(
            "peluquerias",
            sa.Column("min_avance_min", sa.Integer(), nullable=False, server_default=sa.text("60")),
        )
        # Quitamos el server_default para que no quede persistente en DB (opcional)
        try:
            op.alter_column(
                "peluquerias",
                "min_avance_min",
                server_default=None,
                existing_type=sa.Integer(),
                existing_nullable=False,
            )
        except Exception:
            pass

    if "max_avance_dias" not in existing_cols:
        op.add_column(
            "peluquerias",
            sa.Column("max_avance_dias", sa.Integer(), nullable=False, server_default=sa.text("150")),
        )
        try:
            op.alter_column(
                "peluquerias",
                "max_avance_dias",
                server_default=None,
                existing_type=sa.Integer(),
                existing_nullable=False,
            )
        except Exception:
            pass


def downgrade() -> None:
    """Elimina las columnas si existen (para rollback limpio)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("peluquerias")}

    if "max_avance_dias" in existing_cols:
        op.drop_column("peluquerias", "max_avance_dias")

    if "min_avance_min" in existing_cols:
        op.drop_column("peluquerias", "min_avance_min")
