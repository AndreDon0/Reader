import logging
from collections.abc import Iterator

from sqlalchemy.orm import Session
from models import Site, Language

from utils.custom_request import request
from utils.db import get_or_create

logger = logging.getLogger(__name__)


class BaseSite:
    name = "not a site"
    url = ""
    language = Language.ENGLISH

    def __init__(self, session: Session) -> None:
        self.session = session
        self.site = get_or_create(
            session,
            Site,
            defaults={"name":self.name},
            url=self.url
        )

    def is_up(self) -> bool:
        return request(self.url, timeout=10).status_code == 200

    # Get new titles
    def new(self) -> list[str]:
        logger.warning("New is not implemented for %s", self.name)
        return []

    # Search books on site by title
    def search(self, title: str) -> list[str]:
        logger.warning("Search is not implemented for %s", self.name)
        return []

    def catalog(self) -> list[str]:
        logger.warning("Catalog is not implemented for %s", self.name)
        return []

    # Process catalog of site
    def process_catalog(self) -> None:
        logger.warning("Catalog processing is not implemented for %s", self.name)

    # Process book by url
    def process_book(self, book_url: str) -> None:
        logger.warning("Book processing is not implemented for %s", self.name)

    # Process chapter by url
    def process_chapter(self, chapter_url: str) -> None:
        logger.warning("Chapter processing is not implemented for %s", self.name)
