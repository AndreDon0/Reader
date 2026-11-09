import logging
import signal
import time
from datetime import UTC, datetime

from database import SessionLocal, initialize_database
from models import SiteCheck
from sites.registry import SITE_CLASSES
from utils.custom_request import check_vpn_pool, close_vpn_pools
from utils.logging_config import configure_logging


INTERVAL = 120
UNAVAILABLE_RETRY_SECONDS = 3600
logger = logging.getLogger(__name__)


def run_once(unavailable_until: dict[tuple[str, str], float] | None = None) -> None:
    if not initialize_database():
        return
    if unavailable_until is None:
        unavailable_until = {}
    now = time.monotonic()
    for key, deadline in list(unavailable_until.items()):
        if deadline <= now:
            del unavailable_until[key]

    if SITE_CLASSES:
        check_vpn_pool(SITE_CLASSES[0].url)
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
            key = (site_class.url, book_url)
            if key in unavailable_until:
                continue
            try:
                with SessionLocal.begin() as session:
                    completed = site_class(session).process_book(book_url)
                if completed is None:
                    unavailable_until[key] = time.monotonic() + UNAVAILABLE_RETRY_SECONDS
                    logger.info("Source reports %s unavailable; checking again in %d minutes if still listed",
                                book_url, UNAVAILABLE_RETRY_SECONDS // 60)
                elif completed:
                    logger.info("Processed %s", book_url)
                else:
                    logger.warning("Processing incomplete for %s; will retry when listed again", book_url)
            except Exception:
                logger.exception("Cannot process %s; transaction rolled back", book_url)


def main() -> None:
    configure_logging()
    logger.info("Reader worker started; polling every %s seconds", INTERVAL)
    unavailable_until = {}

    try:
        while True:
            start = time.monotonic()
            try:
                run_once(unavailable_until)
            except Exception:
                logger.exception("Worker cycle failed; will retry next cycle")
            remaining = INTERVAL - (time.monotonic() - start)
            if remaining < 0:
                logger.warning("Can't keep up! Behind by %.1f seconds", -remaining)
                time.sleep(1)
            else:
                time.sleep(remaining)
    finally:
        close_vpn_pools()


if __name__ == "__main__":
    # Route service termination through main()'s cleanup just like Ctrl-C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Stopped")
