from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


def upsert(session, model):
    """Build the same conflict-aware insert for PostgreSQL and local SQLite tests."""
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    return insert(model)
