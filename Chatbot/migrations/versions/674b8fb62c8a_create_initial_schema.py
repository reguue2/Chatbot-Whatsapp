"""Create the initial database schema for the bot."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "674b8fb62c8a"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create base tables and indexes used by the application."""

    op.create_table(
        "peluquerias",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("nombre", sa.String(length=100), nullable=True),
        sa.Column("direccion", sa.String(length=150), nullable=True),
        sa.Column("dias_cerrados", sa.String(length=100), nullable=True),
        sa.Column("horario", sa.String(length=200), nullable=True),
        sa.Column("telefono_peluqueria", sa.String(length=9), nullable=True),
        sa.Column("cal_id", sa.String(length=120), nullable=True),
        sa.Column("api_key", sa.String(length=120), nullable=True),
        sa.Column("info", sa.String(length=500), nullable=True),
        sa.Column("num_peluqueros", sa.Integer(), nullable=False),
        sa.Column("rango_reservas", sa.Integer(), nullable=False),
        sa.Column(
            "min_avance_min",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
        sa.Column(
            "max_avance_dias",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("150"),
        ),
        sa.Column("wa_phone_number_id", sa.String(length=32), nullable=False),
        sa.Column("wa_token", sa.String(length=512), nullable=False),
        sa.Column("wa_business_id", sa.String(length=32), nullable=False),
        sa.UniqueConstraint("wa_phone_number_id"),
    )
    op.create_index("ix_peluquerias_api_key", "peluquerias", ["api_key"], unique=False)

    op.create_table(
        "servicios",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("peluqueria_id", sa.Integer(), sa.ForeignKey("peluquerias.id"), nullable=False),
        sa.Column("nombre", sa.String(length=120), nullable=False),
        sa.Column("precio", sa.Float(), nullable=True),
        sa.Column("duracion_min", sa.Integer(), nullable=False),
    )
    op.create_index("ix_servicios_peluqueria_id", "servicios", ["peluqueria_id"], unique=False)

    op.create_table(
        "reservas",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("peluqueria_id", sa.Integer(), sa.ForeignKey("peluquerias.id"), nullable=False),
        sa.Column("servicio_id", sa.Integer(), sa.ForeignKey("servicios.id"), nullable=False),
        sa.Column("nombre_cliente", sa.String(length=100), nullable=False),
        sa.Column("telefono", sa.String(length=50), nullable=False),
        sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("hora", sa.Time(), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("event_id", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            mysql.TIMESTAMP(fsp=0),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            mysql.TIMESTAMP(fsp=0),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_reservas_estado", "reservas", ["estado"], unique=False)
    op.create_index("ix_reservas_event_id", "reservas", ["event_id"], unique=False)
    op.create_index("ix_reservas_fecha", "reservas", ["fecha"], unique=False)
    op.create_index("ix_reservas_peluqueria_id", "reservas", ["peluqueria_id"], unique=False)
    op.create_index("ix_reservas_servicio_id", "reservas", ["servicio_id"], unique=False)
    op.create_index("ix_reservas_telefono", "reservas", ["telefono"], unique=False)
    op.create_index(
        "ix_reservas_pelu_fecha",
        "reservas",
        ["peluqueria_id", "fecha"],
        unique=False,
    )
    op.create_index(
        "ix_reservas_pelu_fecha_hora",
        "reservas",
        ["peluqueria_id", "fecha", "hora"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the application tables."""

    op.drop_index("ix_reservas_pelu_fecha_hora", table_name="reservas")
    op.drop_index("ix_reservas_pelu_fecha", table_name="reservas")
    op.drop_index("ix_reservas_telefono", table_name="reservas")
    op.drop_index("ix_reservas_servicio_id", table_name="reservas")
    op.drop_index("ix_reservas_peluqueria_id", table_name="reservas")
    op.drop_index("ix_reservas_fecha", table_name="reservas")
    op.drop_index("ix_reservas_event_id", table_name="reservas")
    op.drop_index("ix_reservas_estado", table_name="reservas")
    op.drop_table("reservas")

    op.drop_index("ix_servicios_peluqueria_id", table_name="servicios")
    op.drop_table("servicios")

    op.drop_index("ix_peluquerias_api_key", table_name="peluquerias")
    op.drop_table("peluquerias")
