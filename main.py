import logging
import signal
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event

from sqlalchemy import exists, select

from database import SessionLocal, initialize_database
from models import Book, Followed, SiteCheck, User
from sites.base_site import BaseSite
from sites.registry import SITE_CLASSES
from utils.custom_request import check_vpn_pool, close_vpn_pools
from utils.logging_config import configure_logging
from utils.jobs import enqueue_watched, recover_interrupted, run_cache_worker, run_import


INTERVAL = 120
UNAVAILABLE_RETRY_SECONDS = 3600
CATALOG_BATCH_DELAY = 0.5
logger = logging.getLogger(__name__)


@dataclass
class SiteState:
    catalog_cursor: str | None = None
    watch_cursor: int = 0
    unavailable_until: dict[str, float] = field(default_factory=dict)


def run_site_once(site_class: type[BaseSite], state: SiteState, stop: Event) -> None:
    deadline = time.monotonic() + INTERVAL
    logger.info("Checking %s", site_class.name)
    with SessionLocal.begin() as session:
        site = site_class(session)
        session.flush()
        site_id = site.site.id_site
        try:
            is_up = site.is_up()
        except Exception:
            logger.exception("Health check failed for %s", site.name)
            is_up = False

        session.add(SiteCheck(site=site.site, checked_at=datetime.now(UTC), is_up=is_up))

    if site_class.title_path is not None:
        for _ in range(3):
            if stop.is_set() or time.monotonic() >= deadline or not run_import(SessionLocal, site_class):
                break
    if not is_up:
        logger.warning("%s is unavailable; skipping until next cycle", site_class.name)
        return

    now = time.monotonic()
    state.unavailable_until = {url: until for url, until in state.unavailable_until.items() if until > now}

    # Run at least one catalog batch even if recent releases fill the cycle.
    # A failed batch rolls back and the next cycle retries its cursor.
    try:
        for batch in range(site_class.catalog_batches_per_cycle):
            if stop.is_set():
                break
            if batch > 0 and (stop.wait(CATALOG_BATCH_DELAY) or time.monotonic() >= deadline):
                break
            with SessionLocal.begin() as session:
                cursor = site_class(session).process_catalog(state.catalog_cursor)
            state.catalog_cursor = cursor
            if cursor is None:
                break
    except Exception:
        logger.exception("Cannot import catalog batch for %s; will retry next cycle", site_class.name)

    if stop.is_set():
        return
    with SessionLocal.begin() as session:
        watched = []
        if site_class.supports_downloads:
            watched = session.execute(
                select(Book.id_book, Book.url)
                .where(Book.id_site == site_id, Book.hidden.is_(False), Book.id_book > state.watch_cursor,
                       exists(select(Followed.id_book).join(User, User.id_user == Followed.id_user)
                              .where(Followed.id_book == Book.id_book, Followed.watch_since.is_not(None),
                                     User.disabled.is_(False))))
                .order_by(Book.id_book).limit(12)
            ).all()
            state.watch_cursor = watched[-1].id_book if len(watched) == 12 else 0
        book_urls = list(dict.fromkeys([row.url for row in watched] + site_class(session).new()))
    logger.info("Found %d recent book URLs on %s", len(book_urls), site_class.name)

    for book_url in book_urls:
        if stop.is_set():
            break
        if book_url in state.unavailable_until:
            continue
        try:
            with SessionLocal.begin() as session:
                completed = site_class(session).process_book(book_url)
            if completed is None:
                state.unavailable_until[book_url] = time.monotonic() + UNAVAILABLE_RETRY_SECONDS
                logger.info("Source reports %s unavailable; checking again in %d minutes if still listed",
                            book_url, UNAVAILABLE_RETRY_SECONDS // 60)
            elif completed:
                logger.info("Processed %s", book_url)
            else:
                logger.warning("Processing incomplete for %s; will retry when listed again", book_url)
        except Exception:
            logger.exception("Cannot process %s; transaction rolled back", book_url)
    if site_class.supports_downloads and not stop.is_set():
        with SessionLocal.begin() as session:
            enqueue_watched(session, site_id)


def run_periodically(name: str, task: Callable[[], None], stop: Event) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            task()
        except Exception:
            logger.exception("%s cycle failed; will retry next cycle", name)
        remaining = INTERVAL - (time.monotonic() - started)
        if remaining < 0:
            logger.warning("%s cycle exceeded its interval by %.1fs", name, -remaining)
        stop.wait(remaining if remaining > 0 else 1)


def run_site(site_class: type[BaseSite], stop: Event) -> None:
    state = SiteState()
    run_periodically(site_class.name, lambda: run_site_once(site_class, state, stop), stop)


def main() -> None:
    configure_logging()
    logger.info("Reader worker started; polling each site every %s seconds", INTERVAL)
    stop = Event()

    try:
        while not initialize_database():
            stop.wait(INTERVAL)

        if not SITE_CLASSES:
            return
        vpn_probe_url = next((site.url for site in SITE_CLASSES if site.use_vpn), None)
        recover_interrupted(SessionLocal)
        with ThreadPoolExecutor(max_workers=len(SITE_CLASSES) + bool(vpn_probe_url) + 1) as executor:
            try:
                executor.submit(run_cache_worker, SessionLocal, stop)
                if vpn_probe_url:
                    executor.submit(run_periodically, "VPN health", lambda: check_vpn_pool(vpn_probe_url), stop)
                for site_class in SITE_CLASSES:
                    executor.submit(run_site, site_class, stop)
                stop.wait()
            finally:
                stop.set()
    finally:
        close_vpn_pools()


if __name__ == "__main__":
    # Route service termination through main()'s cleanup just like Ctrl-C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Stopped")
