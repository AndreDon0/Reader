import logging
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Site

from utils.custom_request import request

logger = logging.getLogger(__name__)


class BaseSite:
    name = ""
    url = ""
    language = ""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.site = session.scalar(
            select(Site).where(Site.url == self.url)
        )

        if self.site is None:
            self.site = Site(name=self.name, url=self.url)
            session.add(self.site)

    def is_up(self) -> bool:
        response = request(self.url, timeout=10)
        if response is None:
            logger.warning("Health request failed for %s", self.name)
            return False
        try:
            is_up = response.status_code == 200
            if not is_up:
                logger.warning("%s health check returned HTTP %s", self.name, response.status_code)
            return is_up
        finally:
            response.close()

    # Get new titles
    def new(self) -> list[str]:
        return []

    # Search title by title
    def search(self, title: str) -> list[str]:
        return []

    def process_book(self, url: str) -> bool | None:
        """Return True on success, False on failure, or None if the source marks it unavailable."""
        logger.warning("Book processing is not implemented for %s", self.name)
        return False

    # Process whole catalog
    def process_catalog() -> None:
        pass

    # Process chapter by url
    def process_chapter(self, url: str, *, data_root: Path | None = None) -> bool:
        logger.warning("Chapter processing is not implemented for %s", self.name)
        return False

    # Titles may contain characters such as "/" that affect file paths.
    @staticmethod
    def safe_name(value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
        return cleaned.strip(" .")[:60] or "untitled"
