import logging
import re
from urllib.parse import urljoin
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from tqdm import tqdm
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Author, Book, BookStatus, BookType, Chapter, Tag, Language
from sites.base_site import BaseSite
from utils.custom_request import request_soup
from utils.db import get_or_create, transactional
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

    @transactional
    def process_book(self, book_url: str, session: Session) -> None:

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
            session,
            Author,
            nickname=nickname
        )

        tags = [
            get_or_create(session, Tag, name=name)
            for name in set(
                link.get_text(strip=True)
                for link in info[1].select("a")
            )
        ]

        status = status_map.get(info[3].get_text(strip=True).lower())

        rating = rated = None
        stars = soup.select_one(".vote.vote-summary").get_text(" ", strip=True)
        rating_match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*5", stars)
        rated_match = re.search(r"([\d,]+)\s+votes?", stars)
        rating = float(rating_match.group(1))
        rated = int(rated_match.group(1).replace(",", ""))

        book = get_or_create(
            session,
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
                "id_site": self.id_site,
                "author": author,
                "tags": tags,
            },
            url=book_url,
        )
        book.chapters_count = chapters_count

        saved = session.execute(
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
                session.add(chapter)
                page_urls.add(chapter_url)
            saved_urls.update(page_urls)
            logger.debug("Saved %d chapter links from page %d for %s", len(page_urls), page, book_url)
        logging.debug("Book %s processed", book_url)

    @transactional
    def process_chapter(self, chapter_url: str, session: Session) -> None:
        from sites.downloads import cache_chapter

        chapter = session.scalar(
            select(Chapter).join(Chapter.book)
            .where(Chapter.url == chapter_url, Book.id_site == self.id_site)
        )
        if chapter is None:
            logger.warning("Chapter not found in database: %s", chapter_url)
            return
        cache_chapter(session, chapter, chapter.book.site)

    @staticmethod
    def download_chapter(chapter_url: str, path: Path) -> str:
        source = urlsplit(chapter_url)
        if source.scheme not in {"http", "https"} or source.hostname not in {"novgo.net", "www.novgo.net"}:
            raise ValueError("Unexpected NOVGO chapter URL")
        soup = request_soup(chapter_url, timeout=3)
        heading = soup.select_one(".chapter-text") if soup is not None else None
        body = soup.select_one("#chapter-content.chapter-c") if soup is not None else None
        chapter_name = heading.get_text(strip=True) if heading else ""
        content = body.get_text("\n", strip=True) if body else ""
        if not content:
            raise ValueError("NOVGO returned no chapter text")

        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            tmp_path = Path(temporary.name)
            try:
                temporary.write(f"{chapter_name}\n\n{content}\n" if chapter_name else f"{content}\n")
                temporary.close()
                tmp_path.replace(path)
            finally:
                tmp_path.unlink(missing_ok=True)
        logging.debug("Chapter %s processed", chapter_url)
        return chapter_name


    def catalog(self) -> list[str]:
        soup = request_soup(self.url, timeout=1)
        genres = soup.select("#fwn-head-pc .fwn-genres-mega-list a[href]")

        links = []
        for genre in genres:
            genre_url = urljoin(self.url, genre["href"])
            soup = request_soup(genre_url, timeout=3)
            pages = (a.get_text(strip=True) for a in soup.select(".fwn-arch-jump.jump-pc a"))
            last_page = max((int(page) for page in pages if page.isdecimal()), default=1)

            logging.debug("Process genre %s. Found pages %d", genre.get_text(strip=True), last_page)
            for page in range(1, last_page + 1):
                soup = request_soup(genre_url, params={"page": page}, timeout=3)
                for link in soup.select(".fwn-rank-list .tit a[href]"):
                    links.append(urljoin(genre_url, link["href"]))

        return list(dict.fromkeys(links))


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
        for i in tqdm(range(state, len(links)), desc="Processing catalog"):
            try:
                self.process_book(links[i])
            except Exception as e:
                e.add_note(f"Failed to process book: {links[i]}")
                raise
            save_checkpoint("novgo", i + 1)
