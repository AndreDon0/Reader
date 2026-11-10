import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL

logger = logging.getLogger(__name__)


engine = create_engine(
    DATABASE_URL.render_as_string(hide_password=False),
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    hide_parameters=True,
    connect_args={"connect_timeout": 10},
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=True,
    expire_on_commit=False,
)

logger.info("Database connected successfully")
