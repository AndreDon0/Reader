import logging
import time
from datetime import UTC, datetime

from database import SessionLocal, initialize_database
from models import SiteCheck
from sites.novgo import Novgo
from utils.logging_config import configure_logging


SITE_CLASSES = (Novgo,)
INTERVAL = 60
logger = logging.getLogger(__name__)


def run_once() -> None:
    if not initialize_database():
        return
    for site_class in SITE_CLASSES:
        logger.info("Checking %s", site_class.name)
        try:
            with SessionLocal.begin() as session:
                site = site_class(session)
                try:
                    is_up = site.is_up()
                except Exception:
                    logger.exception("Health check failed for %s", site.name)
                    is_up = False

                session.add(SiteCheck(
                    site=site.site,
                    checked_at=datetime.now(UTC),
                    is_up=is_up,
                ))

            if not is_up:
                logger.warning("%s is unavailable; skipping until next cycle", site_class.name)
                continue

            with SessionLocal.begin() as session:
                book_urls = list(dict.fromkeys(site_class(session).new()))
            logger.info("Found %d book URLs on %s", len(book_urls), site_class.name)
        except Exception:
            logger.exception("Cannot check %s; transaction rolled back", site_class.name)
            continue

        for book_url in book_urls:
            try:
                with SessionLocal.begin() as session:
                    completed = site_class(session).process_book(book_url)
                if completed:
                    logger.info("Processed %s", book_url)
                else:
                    logger.warning("Processing incomplete for %s; will retry when listed again", book_url)
            except Exception:
                logger.exception("Cannot process %s; transaction rolled back", book_url)


def main() -> None:
    configure_logging()
    logger.info("Reader worker started; polling every %s seconds", INTERVAL)

    while True:
        start = time.monotonic()
        try:
            run_once()
        except Exception:
            logger.exception("Worker cycle failed; will retry next cycle")
        remaining = INTERVAL - (time.monotonic() - start)
        if remaining < 0:
            logger.warning("Can't keep up! Behind by %.1f seconds", -remaining)
            # Failed slow cycles must not cause a busy retry loop.
            time.sleep(1)
        else:
            time.sleep(remaining)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Stopped")
