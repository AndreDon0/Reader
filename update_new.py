import logging
import time

from database import SessionLocal
from sites.base_site import BaseSite
from sites.registry import SITE_CLASSES
from utils.custom_request import check_vpn_pool, close_vpn_pools
from utils.logging_config import configure_logging
from utils.pool import PriorityPool

logger = logging.getLogger(__name__)

INTERVAL = 300
pool = PriorityPool()
sites = [site(SessionLocal) for site in SITE_CLASSES]

def process_site(site: BaseSite) -> None:
    if site.is_up():
        new = site.new()
        for book_url in new:
            pool.submit(site.process_book, book_url)


def iter() -> None:
    start_time = time.time()
    check_vpn_pool(probe_url="https://github.com/")

    for site in sites:
        process_site(site)

    sleep_time = INTERVAL - (time.time() - start_time)
    if sleep_time < 0:
        logger.error("Can't keep up! Behind by %.1f seconds", sleep_time)
    else:
        time.sleep(sleep_time)


def main() -> None:
    configure_logging()
    logger.info("Polling each site every %s seconds", INTERVAL)

    try:
        while True:
            iter()
            logging.info("Going to next iteration. Borrowed tasks count %d", pool.pending)
    finally:
        pool.shutdown()
        close_vpn_pools()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Stopped")
