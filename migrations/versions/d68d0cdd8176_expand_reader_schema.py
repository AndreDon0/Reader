"""Expand reader schema

Revision ID: d68d0cdd8176
Revises: 8172c2e9bd99
Create Date: 2026-11-06 16:31:17.192826

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd68d0cdd8176'
down_revision: Union[str, Sequence[str], None] = '8172c2e9bd99'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    sa.Enum(
        "ONGOING", "COMPLETED", "CANCELLED", "UNKNOWN",
        name="book_status",
    ).create(op.get_bind())

    sa.Enum(
        "NOVEL", "MANGA", "MANHUA", "MANHWA", "WESTERN", "UNKNOWN",
        name="book_type",
    ).create(op.get_bind())

    op.create_table('author',
    sa.Column('id_author', sa.Integer(), nullable=False),
    sa.Column('nickname', sa.String(length=50), nullable=False),
    sa.PrimaryKeyConstraint('id_author'),
    sa.UniqueConstraint('nickname')
    )
    op.create_table('tag',
    sa.Column('id_tag', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.PrimaryKeyConstraint('id_tag'),
    sa.UniqueConstraint('name')
    )
    op.create_table('users',
    sa.Column('id_user', sa.Uuid(), nullable=False),
    sa.Column('username', sa.String(length=50), nullable=False),
    sa.PrimaryKeyConstraint('id_user'),
    sa.UniqueConstraint('username')
    )
    op.create_table('site_check',
    sa.Column('id_site', sa.Integer(), nullable=False),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('is_up', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['id_site'], ['site.id_site'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id_site', 'checked_at')
    )
    op.create_index('ix_site_check_site_time', 'site_check', ['id_site', 'checked_at'], unique=False)
    op.create_table('book_alt_title',
    sa.Column('id_alt_title', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('id_book', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['id_book'], ['book.id_book'], ),
    sa.PrimaryKeyConstraint('id_alt_title')
    )
    op.create_index('ix_book_alt_title_book', 'book_alt_title', ['id_book'], unique=False)
    op.create_table('book_tag',
    sa.Column('id_book', sa.Integer(), nullable=False),
    sa.Column('id_tag', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['id_book'], ['book.id_book'], ),
    sa.ForeignKeyConstraint(['id_tag'], ['tag.id_tag'], ),
    sa.PrimaryKeyConstraint('id_book', 'id_tag')
    )
    op.create_index('ix_book_tag_tag_book', 'book_tag', ['id_tag', 'id_book'], unique=False)
    op.create_table('followed',
    sa.Column('id_user', sa.Uuid(), nullable=False),
    sa.Column('id_book', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('READING', 'COMPLETED', 'DROPPED', 'PLANNING', name='followed_status'), nullable=False),
    sa.ForeignKeyConstraint(['id_book'], ['book.id_book'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['id_user'], ['users.id_user'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id_user', 'id_book')
    )
    op.create_index('ix_followed_book', 'followed', ['id_book'], unique=False)
    op.create_table('readed',
    sa.Column('id_user', sa.Uuid(), nullable=False),
    sa.Column('id_chapter', sa.Integer(), nullable=False),
    sa.Column('read_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['id_chapter'], ['chapter.id_chapter'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['id_user'], ['users.id_user'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id_user', 'id_chapter')
    )
    op.create_index('ix_read_chapter_chapter', 'readed', ['id_chapter'], unique=False)
    op.add_column('book', sa.Column('rating', sa.Double(), nullable=True))
    op.add_column('book', sa.Column('rated', sa.Integer(), nullable=True))
    op.add_column('book', sa.Column('rank', sa.Integer(), nullable=True))
    op.add_column('book', sa.Column('views', sa.Integer(), nullable=True))
    op.add_column('book', sa.Column('likes', sa.Integer(), nullable=True))
    op.add_column('book', sa.Column('bookmarked', sa.Integer(), nullable=True))
    op.add_column('book', sa.Column('chapters_count', sa.Integer(), nullable=False))
    op.add_column('book', sa.Column('published_on', sa.Date(), nullable=True))
    op.add_column('book', sa.Column('language', sa.String(length=25), nullable=False))
    op.add_column('book', sa.Column('status', sa.Enum('ONGOING', 'COMPLETED', 'CANCELLED', 'UNKNOWN', name='book_status'), nullable=False))
    op.add_column('book', sa.Column('type', sa.Enum('NOVEL', 'MANGA', 'MANHUA', 'MANHWA', 'WESTERN', 'UNKNOWN', name='book_type'), nullable=False))
    op.add_column('book', sa.Column('id_author', sa.Integer(), nullable=True))
    op.alter_column('book', 'url',
               existing_type=sa.VARCHAR(length=200),
               type_=sa.String(length=500),
               existing_nullable=False)
    op.alter_column('book', 'cover_url',
               existing_type=sa.VARCHAR(length=200),
               type_=sa.String(length=500),
               existing_nullable=False)
    op.drop_constraint(op.f('book_cover_url_key'), 'book', type_='unique')
    op.create_index('ix_book_author', 'book', ['id_author'], unique=False)
    op.create_index('ix_book_site', 'book', ['id_site'], unique=False)
    op.create_foreign_key(
        "fk_book_author",
        "book", "author",
        ["id_author"], ["id_author"],
        ondelete="SET NULL",
    )
    op.add_column('chapter', sa.Column('number', sa.Integer(), nullable=False))
    op.add_column('chapter', sa.Column('published_on', sa.DateTime(), nullable=True))
    op.add_column('chapter', sa.Column('file_path', sa.String(length=200), nullable=True))
    op.alter_column('chapter', 'url',
               existing_type=sa.VARCHAR(length=200),
               type_=sa.String(length=500),
               existing_nullable=False)
    op.create_index('ix_chapter_book_number', 'chapter', ['id_book', 'number'], unique=False)
    op.create_unique_constraint(
        "uq_chapter_file_path", "chapter", ["file_path"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "uq_chapter_file_path", "chapter", type_="unique",
    )
    op.drop_index('ix_chapter_book_number', table_name='chapter')
    op.alter_column('chapter', 'url',
               existing_type=sa.String(length=500),
               type_=sa.VARCHAR(length=200),
               existing_nullable=False)
    op.drop_column('chapter', 'file_path')
    op.drop_column('chapter', 'published_on')
    op.drop_column('chapter', 'number')
    op.drop_constraint(
        "fk_book_author", "book", type_="foreignkey",
    )
    op.drop_index('ix_book_site', table_name='book')
    op.drop_index('ix_book_author', table_name='book')
    op.create_unique_constraint(op.f('book_cover_url_key'), 'book', ['cover_url'], postgresql_nulls_not_distinct=False)
    op.alter_column('book', 'cover_url',
               existing_type=sa.String(length=500),
               type_=sa.VARCHAR(length=200),
               existing_nullable=False)
    op.alter_column('book', 'url',
               existing_type=sa.String(length=500),
               type_=sa.VARCHAR(length=200),
               existing_nullable=False)
    op.drop_column('book', 'id_author')
    op.drop_column('book', 'type')
    op.drop_column('book', 'status')
    op.drop_column('book', 'language')
    op.drop_column('book', 'published_on')
    op.drop_column('book', 'chapters_count')
    op.drop_column('book', 'bookmarked')
    op.drop_column('book', 'likes')
    op.drop_column('book', 'views')
    op.drop_column('book', 'rank')
    op.drop_column('book', 'rated')
    op.drop_column('book', 'rating')
    op.drop_index('ix_read_chapter_chapter', table_name='readed')
    op.drop_table('readed')
    op.drop_index('ix_followed_book', table_name='followed')
    op.drop_table('followed')
    op.drop_index('ix_book_tag_tag_book', table_name='book_tag')
    op.drop_table('book_tag')
    op.drop_index('ix_book_alt_title_book', table_name='book_alt_title')
    op.drop_table('book_alt_title')
    op.drop_index('ix_site_check_site_time', table_name='site_check')
    op.drop_table('site_check')
    op.drop_table('users')
    op.drop_table('tag')
    op.drop_table('author')
    op.execute("DROP TYPE book_status")
    op.execute("DROP TYPE book_type")
    op.execute("DROP TYPE followed_status")
