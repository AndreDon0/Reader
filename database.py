import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

load_dotenv()

DATABASE_URL = str(URL.create(
    drivername="postgresql+psycopg",
    username="reader",
    password=os.environ["POSTGRES_PASSWORD"],
    host="127.0.0.1",
    port=60209,
    database="reader",
).render_as_string(hide_password=False))

engine = None
SessionLocal = sessionmaker()

try:
    engine = create_engine(
        DATABASE_URL,
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
