from sites.novgo import Novgo
from database import SessionLocal
from utils.logging_config import configure_logging

configure_logging()

site = Novgo(SessionLocal)
site.process_catalog()