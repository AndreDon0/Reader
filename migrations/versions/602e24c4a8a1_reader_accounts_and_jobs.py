"""Persist Reader accounts, progress, discovery dates and background jobs."""

import sqlalchemy as sa
from alembic import op

revision = "602e24c4a8a1"
down_revision = "9f47c7e2a630"
branch_labels = None
depends_on = None


def timestamp():
    return sa.DateTime(timezone=True)


def flag(name):
    return sa.Column(name, sa.Boolean(), server_default=sa.false(), nullable=False)


def integer(name, type_=sa.Integer):
    return sa.Column(name, type_(), server_default="0", nullable=False)


def string_list(name):
    return sa.Column(name, sa.JSON(), server_default=sa.text("'[]'"), nullable=False)


def job_fields():
    return (
        sa.Column("state", sa.String(10), server_default="queued", nullable=False),
        integer("attempts"),
        sa.Column("available_at", timestamp(), server_default=sa.func.now(), nullable=False),
    )


def upgrade():
    for column in (
        flag("is_admin"), flag("disabled"), integer("session_version"),
        sa.Column("primary_language", sa.String(25)), string_list("secondary_languages"),
        string_list("book_types"),
    ):
        op.add_column("users", column)
    for column in (flag("hidden"), string_list("edited_fields"), sa.Column("last_release_at", timestamp())):
        op.add_column("book", column)

    # Historical imports have no discovery date. Set the default for new rows only.
    op.add_column("chapter", sa.Column("discovered_at", timestamp()))
    op.alter_column("chapter", "discovered_at", server_default=sa.func.now())
    op.add_column("chapter", flag("hidden"))
    op.add_column("chapter", sa.Column("display_name", sa.String()))
    op.execute("""
        UPDATE book AS b SET last_release_at = releases.last_release AT TIME ZONE 'UTC'
        FROM (SELECT id_book, max(published_on) AS last_release
              FROM chapter WHERE published_on IS NOT NULL GROUP BY id_book) AS releases
        WHERE b.id_book = releases.id_book
    """)
    for column in (
        sa.Column("followed_at", timestamp(), server_default=sa.func.now(), nullable=False),
        sa.Column("watch_since", timestamp()),
        sa.Column("watch_after_id", sa.Integer()), sa.Column("watch_after_number", sa.Integer()),
    ):
        op.add_column("followed", column)
    for column in (
        integer("anchor"), sa.Column("anchor_offset", sa.Float(), server_default="0", nullable=False),
        flag("finished"), integer("view_stamp", sa.BigInteger), integer("save_sequence"),
    ):
        op.add_column("readed", column)
    op.execute("UPDATE readed SET finished = TRUE")

    for table in ("chapter", "book_alt_title", "book_tag"):
        name = f"{table}_id_book_fkey"
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, "book", ["id_book"], ["id_book"], ondelete="CASCADE")

    op.create_table("auth_attempt", sa.Column("key", sa.String(80), primary_key=True),
                    sa.Column("window", sa.BigInteger(), nullable=False), sa.Column("count", sa.Integer(), nullable=False))
    op.create_table(
        "download_job", sa.Column("id_chapter", sa.Integer(),
                                  sa.ForeignKey("chapter.id_chapter", ondelete="CASCADE"), primary_key=True),
        *job_fields(),
    )
    op.create_table(
        "import_job", sa.Column("url", sa.String(500), primary_key=True),
        sa.Column("source", sa.String(50), nullable=False), *job_fields(),
        sa.Column("id_book", sa.Integer(), sa.ForeignKey("book.id_book", ondelete="CASCADE")),
    )
    op.create_table(
        "import_request",
        sa.Column("url", sa.String(500), sa.ForeignKey("import_job.url", ondelete="CASCADE"), primary_key=True),
        sa.Column("id_user", sa.Uuid(), sa.ForeignKey("users.id_user", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table("cache_removal", sa.Column("path", sa.String(1000), primary_key=True))
    op.create_index("ix_book_catalog_release", "book", ["last_release_at", "id_book"])
    op.create_index("ix_chapter_book_discovered", "chapter", ["id_book", "discovered_at"])
    op.create_index("ix_read_user_time", "readed", ["id_user", "read_at"])
    op.create_index("ix_followed_watch", "followed", ["id_book"], postgresql_where=sa.text("watch_since IS NOT NULL"))
    op.create_index("ix_download_due", "download_job", ["state", "available_at"])
    op.create_index("ix_import_due", "import_job", ["source", "state", "available_at"])
    op.create_index("ix_import_request_user", "import_request", ["id_user"])
    op.create_index("ix_auth_attempt_window", "auth_attempt", ["window"])


def downgrade():
    for table in ("cache_removal", "import_request", "import_job", "download_job", "auth_attempt"):
        op.drop_table(table)
    for name, table in (
        ("ix_followed_watch", "followed"), ("ix_read_user_time", "readed"),
        ("ix_chapter_book_discovered", "chapter"), ("ix_book_catalog_release", "book"),
    ):
        op.drop_index(name, table_name=table)
    for table in ("chapter", "book_alt_title", "book_tag"):
        name = f"{table}_id_book_fkey"
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, "book", ["id_book"], ["id_book"])
    for table, columns in (
        ("readed", ("anchor", "anchor_offset", "finished", "view_stamp", "save_sequence")),
        ("followed", ("followed_at", "watch_since", "watch_after_id", "watch_after_number")),
        ("chapter", ("discovered_at", "hidden", "display_name")),
        ("book", ("hidden", "edited_fields", "last_release_at")),
        ("users", ("is_admin", "disabled", "session_version", "primary_language", "secondary_languages", "book_types")),
    ):
        for column in columns:
            op.drop_column(table, column)
