import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL", "")
engine = None
SessionLocal = sessionmaker()


def initialize_database() -> bool:
    """Configure the worker lazily, retrying missing/invalid setup next cycle."""
    global DATABASE_URL, engine
    if engine is not None:
        return True

    load_dotenv()
    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    if not DATABASE_URL:
        logger.error("DATABASE_URL is missing; database work will retry next cycle")
        return False

    try:
        url = make_url(DATABASE_URL)
        connect_args = {"connect_timeout": 10} if url.get_backend_name() == "postgresql" else {}
        engine = create_engine(
            url, pool_pre_ping=True, hide_parameters=True, connect_args=connect_args,
        )
        SessionLocal.configure(bind=engine)
    except Exception as exc:
        # Engine/parser exceptions can contain credentials. Log only their type.
        engine = None
        logger.error("Cannot configure database (%s); will retry next cycle", type(exc).__name__)
        return False

    logger.info("Database engine configured")
    return True
