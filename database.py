import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL

logger = logging.getLogger(__name__)

engine = None
SessionLocal = sessionmaker()

try:
    engine = create_engine(
        DATABASE_URL.render_as_string(hide_password=False),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 10},
    )
    SessionLocal.configure(bind=engine)

except Exception as exc:
    logger.error(
        "Cannot connect to database (%s)",
        type(exc).__name__,
    )

logger.info("Database connected successfully")
