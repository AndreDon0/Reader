import logging
import re
from bisect import bisect_right
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from time import monotonic
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Author, Book, BookStatus, BookType, Chapter, Tag, Language
from sites.base_site import BaseSite
from utils.custom_request import request_soup
from utils.db import get_or_create
from config import FILES_PATH

logger = logging.getLogger(__name__)

status_map = {
    "ongoing": BookStatus.ONGOING,
    "completed": BookStatus.COMPLETED,
    "cancelled": BookStatus.CANCELLED,
}

class Novgo(BaseSite):
    name = "NOVGO"
    url = "https://novgo.net/"
    language = Language.ENGLISH

    def __init__(self, session: Session) -> None:
        super().__init__(session)

    def new(self) -> list[str]:
        soup = request_soup(self.url, timeout=1)

        new = []
        for link in soup.select(".fwn-latest-list .home-release-title-line a"):
            new.append(urljoin(self.url, link["href"]))

        return new


    def search(self, title: str) -> list[str]:
        soup = request_soup(
            urljoin(self.url, "search"),
            params={"keyword": title}, timeout=5,
        )

        found = []
        for link in soup.select(".fwn-rank-list .tit a"):
            found.append(urljoin(self.url, link["href"]))
        return found


    def process_book(self, book_url: str) -> None:

        soup = request_soup(book_url, timeout=2)

        chapter_meta = soup.select_one("#list-chapter")
        chapters_count = int(chapter_meta["data-total-chapters"])
        page_size = int(chapter_meta["data-page-size"])

        title = soup.select_one("h1.tit").get_text(strip=True)
        cover_url = urljoin(book_url, soup.select_one(".m-imgtxt .pic img")["src"])
        description = soup.select_one("#novel-summary-inner").get_text("\n", strip=True)

        info = soup.select(".m-imgtxt .txt > .item > .right")

        nickname = info[0].select_one("a").get_text(strip=True)
        author = get_or_create(
            self.session,
            Author,
            filters={"nickname":nickname}
        )

        tags = [
            get_or_create(self.session, Tag, name=name)
            for name in set(
                link.get_text(strip=True)
                for link in info[1].select("a")
            )
        ]

        status = status_map[info[3].get_text(strip=True).lower()]

        rating = rated = None
        stars = soup.select_one(".vote.vote-summary").get_text(" ", strip=True)
        rating_match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*5", stars)
        rated_match = re.search(r"([\d,]+)\s+votes?", stars)
        rating = float(rating_match.group(1))
        rated = int(rated_match.group(1).replace(",", ""))

        book = get_or_create(
            self.session,
            Book,
            title=title,
            description=description,
            cover_url=cover_url,
            chapters_count=chapters_count,
            language=self.language,
            type=BookType.NOVEL,
            status=status,
            rating=rating,
            rated=rated,
            site=self.site,
            author=author,
            tags=tags,
            filters={"url":book_url}
        )

        saved = self.session.execute(
            select(Chapter.number, Chapter.url)
            .where(Chapter.book == book)
        ).all()

        saved_numbers = {number for number, _ in saved}
        saved_urls = {chapter_url for _, chapter_url in saved}

        # Find the last chapter before the first gap.
        # For saved chapters [1, 2, 4], resume after chapter 2.
        last_count = 0
        while last_count + 1 in saved_numbers:
            last_count += 1

        page_count = (chapters_count + page_size - 1) // page_size
        first_page = last_count // page_size + 1

        for page in range(first_page, page_count + 1):
            page_soup = request_soup(book_url, params={"page": page}, timeout=5)

            first_number = (page - 1) * page_size + 1
            links = page_soup.select_one("#idData.ul-list5").select("a")

            page_urls = set()
            with self.session.begin_nested():
                for number, link in enumerate(links, start=first_number):
                    name = link.get("title") or link.get_text(strip=True)
                    chapter_url = urljoin(getattr(page_soup, "source_url", None) or book_url, link["href"])
                    if chapter_url in saved_urls or chapter_url in page_urls:
                        continue

                    chapter = Chapter(
                        number=number, 
                        url=chapter_url, 
                        name=name, 
                        book=book,
                    )
                    self.session.add(chapter)
                    page_urls.add(chapter_url)
            saved_urls.update(page_urls)
            logger.debug("Saved %d chapter links from page %d for %s", len(page_urls), page, book_url)


    def process_chapter(self, chapter_url: str) -> None:
        chapter = self.session.scalar(
            select(Chapter).where(Chapter.url == chapter_url)
        )
        if chapter is None:
            logger.warning("Chapter not found in database: %s", chapter_url)

        soup = request_soup(chapter_url, timeout=3)

        chapter_name = soup.select_one(".chapter-text").get_text(strip=True)
        content = soup.select_one("#chapter-content.chapter-c").get_text("\n", strip=True)

        path = FILES_PATH / f"{self.site.id_site}" / f"{chapter.id_book}" / f"{chapter.id_chapter}-"

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{chapter_name}\n\n{content}\n", encoding="utf-8")
        chapter.name = chapter_name
