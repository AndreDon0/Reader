import logging
import re
from urllib.parse import urljoin
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Author, Book, BookStatus, BookType, Chapter, Tag, Language
from sites.base_site import BaseSite
from utils.custom_request import request_soup
from utils.db import get_or_create
from config import FILES_PATH
from utils.logging_config import check_none
from utils.checkpoint import save_checkpoint, load_checkpoint

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
        chapters_count = check_none(int(chapter_meta["data-total-chapters"]), "chapters_count")
        page_size = check_none(int(chapter_meta["data-page-size"]), "page_size", level=logging.ERROR)

        title = check_none(soup.select_one("h1.tit").get_text(strip=True), "title", level=logging.ERROR)
        cover_url = check_none(urljoin(book_url, soup.select_one(".m-imgtxt .pic img")["src"]), "cover_url", level=logging.ERROR)
        description = check_none(soup.select_one("#novel-summary-inner").get_text("\n", strip=True), "description", level=logging.ERROR)

        info = soup.select(".m-imgtxt .txt > .item > .right")

        nickname = check_none(info[0].select_one("a").get_text(strip=True), "nickname", level=logging.ERROR)
        author = get_or_create(
            self.session,
            Author,
            nickname=nickname
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
            defaults={
                "title": title,
                "description": description,
                "cover_url": cover_url,
                "chapters_count": chapters_count,
                "language": self.language,
                "type": BookType.NOVEL,
                "status": status,
                "rating": rating,
                "rated": rated,
                "site": self.site,
                "author": author,
                "tags": tags,
            },
            url=book_url,
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

        self.session.commit()


    def process_chapter(self, chapter_url: str) -> None:
        chapter = self.session.scalar(
            select(Chapter).where(Chapter.url == chapter_url)
        )
        if chapter is None:
            logger.warning("Chapter not found in database: %s", chapter_url)

        soup = request_soup(chapter_url, timeout=3)

        chapter_name = check_none(soup.select_one(".chapter-text").get_text(strip=True), "chapter_name")
        content = check_none(soup.select_one("#chapter-content.chapter-c").get_text("\n", strip=True), "content", level=logging.ERROR)

        path = FILES_PATH / f"{self.site.id_site}" / f"{chapter.id_book}" / f"{chapter.id_chapter}"

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{chapter_name}\n\n{content}\n", encoding="utf-8")
        chapter.name = chapter_name

        self.session.commit()


    def catalog(self) -> list[str]:
        soup = request_soup(self.url, timeout=1)

        menu = check_none(soup.select_one("#fwn-head-pc .fwn-genres-mega-list"), "menu", level=logging.ERROR)

        genres_url = []
        links = []
        for a in menu.select("a[href]"):
    
            genres_url.append(urljoin(self.url, a["href"]))

        for genre_url in genres_url:
            tmp_soup = request_soup(genre_url, timeout=3)
            count = tmp_soup.select(".fwn-arch-jump .jump-pc a")[-1].get_text(strip=True)

            for i in (1, count + 1):
                soup = request_soup(
                    genre_url,
                    params={"page": i}, timeout=3,
                )

                link = soup.select(".ul-list1 .ul-list1-2 .ss-custom .rank-list .fwn-rank-list .con .tit a")["href"]
                links.append(link)

        return link


    def process_catalog(self) -> None:
        path = Path("tmp/novgo_catalog.json")
        if path.exists():
            with open("tmp/novgo_catalog.json", "r") as f:
                links = json.load(f)
        else:
            links = self.catalog()
            with open("tmp/novgo_catalog.json", "w") as f:
                json.dump(links, f)

        state = load_checkpoint("novgo", 0)
        for i in range(state, len(links)):
            self.process_book(links[i])
            save_checkpoint("novgo", i)
