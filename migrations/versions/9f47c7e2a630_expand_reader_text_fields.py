"""Expand reader text fields.

Revision ID: 9f47c7e2a630
Revises: 4b1da0a162e6
Create Date: 2026-11-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9f47c7e2a630'
down_revision: Union[str, Sequence[str], None] = '4b1da0a162e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Match the text lengths declared in the models."""
    op.alter_column(
        'book', 'title',
        existing_type=sa.String(length=50),
        type_=sa.String(),
        existing_nullable=False,
    )
    op.alter_column(
        'chapter', 'name',
        existing_type=sa.String(length=50),
        type_=sa.String(),
        existing_nullable=False,
    )
    op.alter_column(
        'chapter', 'file_path',
        existing_type=sa.String(length=200),
        type_=sa.String(length=1000),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Restore the old limits; PostgreSQL rejects values that no longer fit."""
    op.alter_column(
        'chapter', 'file_path',
        existing_type=sa.String(length=1000),
        type_=sa.String(length=200),
        existing_nullable=True,
    )
    op.alter_column(
        'chapter', 'name',
        existing_type=sa.String(),
        type_=sa.String(length=50),
        existing_nullable=False,
    )
    op.alter_column(
        'book', 'title',
        existing_type=sa.String(),
        type_=sa.String(length=50),
        existing_nullable=False,
    )
