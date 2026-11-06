"""Baseline existing schema

Revision ID: 8172c2e9bd99
Revises: 
Create Date: 2026-11-06 16:29:15.358632

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8172c2e9bd99'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "site",
        sa.Column("id_site", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("url", sa.String(200), nullable=False, unique=True),
    )

    op.create_table(
        "book",
        sa.Column("id_book", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(50), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("url", sa.String(200), nullable=False, unique=True),
        sa.Column("cover_url", sa.String(200), nullable=False, unique=True),
        sa.Column(
            "id_site",
            sa.Integer(),
            sa.ForeignKey("site.id_site"),
            nullable=False,
        ),
    )

    op.create_table(
        "chapter",
        sa.Column("id_chapter", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("url", sa.String(200), nullable=False, unique=True),
        sa.Column(
            "id_book",
            sa.Integer(),
            sa.ForeignKey("book.id_book"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("chapter")
    op.drop_table("book")
    op.drop_table("site")