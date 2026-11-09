import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Book, BookType, Site

from utils.custom_request import request

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    type: BookType | None = None


class SourceSearchUnavailable(Exception):
    pass


class BaseSite:
    name = ""
    url = ""
    language = ""
    use_vpn = True
    catalog_batches_per_cycle = 0
    supports_downloads = False
    search_type: BookType | None = None
    title_path: re.Pattern | None = None

    def __init__(self, session: Session) -> None:
        self.session = session
        self.site = session.scalar(
            select(Site).where(Site.url == self.url)
        )

        if self.site is None:
            self.site = Site(name=self.name, url=self.url)
            session.add(self.site)

    def is_up(self) -> bool:
        response = request(self.url, timeout=10, direct=not self.use_vpn)
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

    @classmethod
    def search(cls, title: str) -> list[SearchResult]:
        raise SourceSearchUnavailable

    @classmethod
    def canonical_title_url(cls, value: str) -> str | None:
        try:
            url = urlsplit(urljoin(cls.url, value))
            if (url.scheme != "https" or url.netloc != urlsplit(cls.url).netloc
                    or not cls.title_path or not cls.title_path.fullmatch(url.path)):
                return None
            canonical = urljoin(cls.url, url.path.rstrip("/"))
            return canonical if len(canonical) <= 500 else None
        except ValueError:
            return None

    @staticmethod
    def update_metadata(book: Book, **fields) -> None:
        for name, value in fields.items():
            if name not in (book.edited_fields or ()):
                setattr(book, name, value)

    def process_book(self, url: str) -> bool | None:
        """Return True on success, False on failure, or None if the source marks it unavailable."""
        logger.warning("Book processing is not implemented for %s", self.name)
        return False

    def process_catalog(self, cursor: str | None = None) -> str | None:
        """Import a catalog batch in the current transaction.

        Return the next cursor, or None when the full catalog is covered.
        Sites without a complete catalog leave catalog_batches_per_cycle at 0.
        """
        return None

    # Process chapter by url
    def process_chapter(self, url: str, *, data_root: Path | None = None) -> bool:
        logger.warning("Chapter processing is not implemented for %s", self.name)
        return False

    # Titles may contain characters such as "/" that affect file paths.
    @staticmethod
    def safe_name(value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
        return cleaned.strip(" .")[:60] or "untitled"
