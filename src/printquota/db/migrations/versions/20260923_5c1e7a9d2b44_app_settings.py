"""app_settings: configuration saved from the web console

Revision ID: 5c1e7a9d2b44
Revises: a4b93b2d06f0
Create Date: 2026-09-23 17:00:00.000000+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '5c1e7a9d2b44'
down_revision = 'a4b93b2d06f0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_by', sa.String(length=128), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )


def downgrade() -> None:
    op.drop_table('app_settings')
