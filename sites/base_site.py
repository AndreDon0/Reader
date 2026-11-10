import logging

from sqlalchemy.orm import Session, sessionmaker
from models import Site, Language, SiteCheck

from utils.custom_request import request
from utils.db import get_or_create

logger = logging.getLogger(__name__)


class BaseSite:
    name = "not a site"
    url = ""
    language = Language.ENGLISH

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

        with self.session_factory.begin() as session:
            site = get_or_create(
                session,
                Site,
                defaults={"name": self.name},
                url=self.url,
            )

            self.id_site = site.id_site

    def is_up(self) -> bool:
        up = request(self.url, timeout=10).status_code == 200

        with self.session_factory.begin() as session:
            site = get_or_create(
                session,
                SiteCheck,
                defaults={"is_up": up},
                id_site=self.id_site,
            )

            self.id_site = site.id_site
        return up
    
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
