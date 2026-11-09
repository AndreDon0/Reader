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

from models import Author, Book, BookStatus, BookType, Chapter, Tag
from sites.base_site import BaseSite, SearchResult, SourceSearchUnavailable
from utils.custom_request import request, request_soup

logger = logging.getLogger(__name__)


def _sitemap(url: str) -> ElementTree.Element:
    response = request(url, timeout=10)
    if response is None:
        raise RuntimeError("NOVGO sitemap request failed")
    with response:
        response.raise_for_status()
        return ElementTree.fromstring(response.content)


@lru_cache(maxsize=1)
def _catalog_urls(site_url: str, _hour: int) -> tuple[str, ...]:
    # Cache successful sitemap reads within the hour. The index points to
    # versioned files, so discover their names instead of hardcoding them.
    origin = urlsplit(site_url)
    index = _sitemap(urljoin(site_url, "sitemap.xml"))
    urls = set()
    for loc in index.findall("./{*}sitemap/{*}loc"):
        parsed = urlsplit(loc.text or "")
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            continue
        if not re.fullmatch(r"/sitemaps/novel-[^/]+\.xml", parsed.path):
            continue
        for entry in _sitemap(loc.text).findall("./{*}url/{*}loc"):
            title = urlsplit(entry.text or "")
            if ((title.scheme, title.netloc) == (origin.scheme, origin.netloc)
                    and re.fullmatch(r"/[^/]+\.html", title.path)):
                urls.add(urljoin(site_url, title.path))
    if not urls:
        raise ValueError("No novel URLs in the NOVGO sitemap")
    return tuple(sorted(urls))


class Novgo(BaseSite):
    name = "NOVGO"
    url = "https://novgo.net/"
    language = "english"
    catalog_batches_per_cycle = 3
    supports_downloads = True
    search_type = BookType.NOVEL
    title_path = re.compile(r"/[^/]+\.html/?")


    def __init__(self, session: Session) -> None:
        super().__init__(session)

    def process_catalog(self, cursor: str | None = None) -> str | None:
        urls = _catalog_urls(self.url, int(monotonic() // 3600))
        saved = set(self.session.scalars(select(Book.url).where(Book.site == self.site)))
        start = bisect_right(urls, cursor) if cursor is not None else 0
        book_url = next((urls[i] for i in range(start, len(urls)) if urls[i] not in saved), None)
        if book_url is None:
            return None

        # Leave a failed title eligible for the next sweep, but let this
        # sweep move on to the rest of the sitemap.
        try:
            with self.session.begin_nested() as transaction:
                if not self._process_book(book_url):
                    transaction.rollback()
                    logger.warning("NOVGO catalog title incomplete: %s", book_url)
        except Exception:
            logger.exception("Cannot import NOVGO catalog title %s; book changes rolled back", book_url)
        return book_url if book_url != urls[-1] else None

    def new(self) -> list[str]:
        soup = request_soup(self.url, timeout=10)
        if soup is None:
            return []

        latest_list = soup.select_one(".fwn-latest-list")
        if latest_list is None:
            logger.error("Latest releases list missing on %s", self.name)
            return []

        new = []
        for link in latest_list.select(".home-release-title-line a"):
            if not link.get("href"):
                logger.warning("Skipping release link without href on %s", self.name)
                continue
            new.append(urljoin(self.url, link["href"]))

        return new


    @classmethod
    def search(cls, title: str) -> list[SearchResult]:
        soup = request_soup(
            urljoin(cls.url, "search"),
            params={"keyword": title}, timeout=4,
        )
        if soup is None:
            raise SourceSearchUnavailable

        found = []
        for link in soup.select(".fwn-rank-list .tit a"):
            url = cls.canonical_title_url(link.get("href", ""))
            name = link.get_text(" ", strip=True)
            if url and name:
                found.append(SearchResult(name, url, cls.search_type))
        if found or (soup.title and soup.title.get_text(strip=True).startswith("Search:")):
            return found
        raise SourceSearchUnavailable


    def process_book(self, book_url: str) -> bool:
        """Roll back a failed book without poisoning the next book's session."""
        try:
            with self.session.begin_nested():
                return self._process_book(book_url)
        except Exception:
            logger.exception("Cannot process book %s; book changes rolled back", book_url)
            return False


    def _process_book(self, book_url: str) -> bool:
        existing = self.session.scalar(
            select(Book).where(Book.url == book_url)
        )

        soup = request_soup(book_url, timeout=10)
        if soup is None:
            return False

        title_node = soup.select_one("h1.tit")
        cover = soup.select_one(".m-imgtxt .pic img")
        description_node = soup.select_one("#novel-summary-inner")
        chapter_meta = soup.select_one("#list-chapter")
        info = soup.select(".m-imgtxt .txt > .item > .right")
        if (
            title_node is None or not title_node.get_text(strip=True)
            or cover is None or not cover.get("src")
            or description_node is None or chapter_meta is None or len(info) < 4
        ):
            logger.error("Required book metadata missing for %s; skipping", book_url)
            return False

        try:
            chapters_count = int(chapter_meta["data-total-chapters"])
            page_size = int(chapter_meta["data-page-size"])
            if chapters_count < 0 or page_size <= 0:
                raise ValueError("Invalid chapter count or page size")
        except (KeyError, TypeError, ValueError):
            logger.error("Invalid chapter pagination for %s; skipping", book_url)
            return False

        title = title_node.get_text(strip=True)
        cover_url = urljoin(
            getattr(soup, "source_url", None) or book_url,
            cover["src"],
        )
        description = description_node.get_text("\n", strip=True)

        # Your Book model supports one author: use the first listed name.
        author = None
        author_link = info[0].select_one("a")
        if author_link is not None:
            nickname = author_link.get_text(strip=True)
            author = self.session.scalar(
                select(Author).where(Author.nickname == nickname)
            )
            if author is None:
                author = Author(nickname=nickname)

        # Reuse tags because Tag.name is unique.
        tags = []
        tag_names = dict.fromkeys(
            link.get_text(strip=True)
            for link in info[1].select("a")
        )
        for name in tag_names:
            tag = self.session.scalar(
                select(Tag).where(Tag.name == name)
            )
            if tag is None:
                tag = Tag(name=name)
            tags.append(tag)

        status = {
            "ongoing": BookStatus.ONGOING,
            "completed": BookStatus.COMPLETED,
            "cancelled": BookStatus.CANCELLED,
        }.get(
            info[3].get_text(strip=True).lower(),
            BookStatus.UNKNOWN,
        )

        rating = rated = None
        summary = soup.select_one(".vote.vote-summary")
        if summary is not None:
            stars = summary.get_text(" ", strip=True)

            rating_match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*5", stars)
            rated_match = re.search(r"([\d,]+)\s+votes?", stars)

            if rating_match:
                rating = float(rating_match.group(1))
            if rated_match:
                rated = int(rated_match.group(1).replace(",", ""))

        book = existing

        if book is None:
            book = Book(
                title=title,
                description=description,
                url=book_url,
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
            )
            self.session.add(book)
        else:
            book.chapters_count = chapters_count

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

        if last_count >= chapters_count:
            logger.debug("Book already up to date: %s", book_url)
            return True

        page_count = (chapters_count + page_size - 1) // page_size
        first_page = last_count // page_size + 1
        completed = True

        for page in range(first_page, page_count + 1):
            page_soup = request_soup(book_url, params={"page": page}, timeout=10)
            if page_soup is None:
                logger.warning("Cannot fetch chapter page %d for %s; skipping page", page, book_url)
                completed = False
                continue

            chapter_list = page_soup.select_one("#idData.ul-list5")
            if chapter_list is None:
                logger.error("Chapter list missing on page %d for %s", page, book_url)
                completed = False
                continue

            first_number = (page - 1) * page_size + 1
            links = chapter_list.select("a")
            expected = min(page_size, chapters_count - first_number + 1)
            if len(links) != expected:
                logger.error("Expected %d chapter links on page %d for %s, got %d; skipping page",
                             expected, page, book_url, len(links))
                completed = False
                continue

            page_urls = set()
            try:
                with self.session.begin_nested():
                    for number, link in enumerate(links, start=first_number):
                        name = link.get("title") or link.get_text(strip=True)
                        if not link.get("href") or not name:
                            logger.warning("Invalid chapter link %d on %s; skipping", number, book_url)
                            completed = False
                            continue
                        chapter_url = urljoin(
                            getattr(page_soup, "source_url", None) or book_url, link["href"],
                        )
                        if chapter_url in saved_urls or chapter_url in page_urls:
                            continue

                        chapter = Chapter(number=number, url=chapter_url, name=name, book=book,
                                          discovered_at=datetime.now(UTC))
                        self.session.add(chapter)
                        page_urls.add(chapter_url)
                saved_urls.update(page_urls)
                if page_urls:
                    book.last_release_at = datetime.now(UTC)
                logger.debug("Saved %d chapter links from page %d for %s", len(page_urls), page, book_url)
            except Exception:
                logger.exception("Cannot save chapter page %d for %s; page rolled back", page, book_url)
                completed = False
        return completed


    def process_chapter(self, chapter_url: str, *, data_root: Path | None = None) -> bool:
        try:
            with self.session.begin_nested():
                return self._process_chapter(chapter_url, data_root)
        except Exception:
            logger.exception("Cannot process chapter %s; chapter changes rolled back", chapter_url)
            return False


    def _process_chapter(self, chapter_url: str, data_root: Path | None) -> bool:
        chapter = self.session.scalar(
            select(Chapter).where(Chapter.url == chapter_url)
        )
        if chapter is None:
            logger.warning("Chapter not found in database: %s", chapter_url)
            return False

        soup = request_soup(chapter_url, timeout=10)
        if soup is None:
            return False

        title_node = soup.select_one(".chapter-text")
        content_node = soup.select_one("#chapter-content.chapter-c")
        if title_node is None or content_node is None:
            logger.error("Chapter title or content missing for %s; skipping", chapter_url)
            return False
        chapter_name = title_node.get_text(strip=True)
        content = content_node.get_text("\n", strip=True)
        if not chapter_name or not content:
            logger.error("Chapter title or content empty for %s; skipping", chapter_url)
            return False

        # novgo.py is inside Reader/sites/, so parents[1] is Reader/.
        base = data_root or Path(__file__).resolve().parents[1]

        book_folder = (
            base
            / "data"
            / self.name
            / f"{chapter.id_book}-{self.safe_name(chapter.book.title)}"
        )
        file_path = book_folder / (
            f"{chapter.number:05d}-{chapter.id_chapter}-"
            f"{self.safe_name(chapter_name)}.txt"
        )

        file_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = file_path.with_suffix(".txt.tmp")
        try:
            temporary_path.write_text(f"{chapter_name}\n\n{content}\n", encoding="utf-8")
            temporary_path.replace(file_path)
        finally:
            temporary_path.unlink(missing_ok=True)

        chapter.name = chapter_name
        chapter.file_path = str(file_path.relative_to(base))
        logger.info("Cached chapter %s", chapter_url)
        return True
