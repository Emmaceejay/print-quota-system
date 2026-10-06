"""printers.duplex_default: queue prints two-sided unless a job asks otherwise

Revision ID: 9e2f4c1a7b30
Revises: 5c1e7a9d2b44
Create Date: 2026-10-06 12:00:00.000000+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '9e2f4c1a7b30'
down_revision = '5c1e7a9d2b44'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('printers', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('duplex_default', sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table('printers', schema=None) as batch_op:
        batch_op.drop_column('duplex_default')
