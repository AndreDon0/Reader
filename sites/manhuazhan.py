import json
import logging
import re
from base64 import b64decode
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin, urlsplit
from zipfile import ZipFile

from sqlalchemy import select

from models import Author, Book, BookStatus, Chapter, Tag
from sites.base_site import BaseSite
from utils.cbz import image_suffix
from utils.custom_request import request, request_soup

logger = logging.getLogger(__name__)

BOOK_PATH = re.compile(r"/comic/([0-9]+)/?")
UNAVAILABLE_BOOK_PATH = re.compile(r"/err/comic/?")
CHAPTER_PATH = re.compile(r"/chapter/([0-9]+)-([0-9]+)\.html")
NO_CHAPTER_LABEL = "待浏览"
IMAGE_DOMAINS = ("bzcdn.net", "baozimh.com", "baozimhcn.com")

# The site's reader selects a key by cid % 10. DATA is Base64 around an
# XOR-obfuscated Base64 string; the inner string decodes to ordered page JSON.
PAGE_KEYS = (
    b"4-bXd9iN", b"4-RXyjry", b"4-oYvwVy", b"4-4ZY57U", b"4-mbJpU7",
    b"4-6MM2Ei", b"4-54TiQr", b"4-Ph5xx9", b"4-bYgePR", b"4-Z9A3bW",
)
PAGE_DATA = re.compile(
    r"""\bvar\s+cid\s*=\s*([0-9]+)\s*;\s*var\s+DATA\s*=\s*(['"])([A-Za-z0-9+/=]+)\2"""
)


class Manhuazhan(BaseSite):
    name = "Manhuazhan"
    url = "https://www.manhuazhan.com/"
    language = "chinese"

    @classmethod
    def _site_path(cls, href: str, pattern: re.Pattern[str]) -> re.Match[str] | None:
        parsed = urlsplit(urljoin(cls.url, href))
        if parsed.scheme == "https" and parsed.netloc == urlsplit(cls.url).netloc:
            return pattern.fullmatch(parsed.path)
        return None

    def new(self) -> list[str]:
        soup = request_soup(self.url, timeout=10)
        if soup is None:
            return []

        book_urls = []
        for link in soup.select("#weekcon dd > a[href]"):
            latest_chapter = link.parent.select_one("span")
            if latest_chapter and latest_chapter.get_text(strip=True) == NO_CHAPTER_LABEL:
                continue
            if match := self._site_path(link["href"], BOOK_PATH):
                book_urls.append(urljoin(self.url, f"/comic/{match[1]}"))
        return list(dict.fromkeys(book_urls))

    def process_book(self, book_url: str) -> bool | None:
        try:
            with self.session.begin_nested():
                return self._process_book(book_url)
        except Exception:
            logger.exception("Cannot process book %s; book changes rolled back", book_url)
            return False

    def _process_book(self, book_url: str) -> bool | None:
        match = self._site_path(book_url, BOOK_PATH)
        if match is None:
            return False
        comic_id = match[1]
        book_url = urljoin(self.url, f"/comic/{comic_id}")
        soup = request_soup(book_url, timeout=10)
        if soup is None:
            return False
        source_url = getattr(soup, "source_url", None) or book_url
        if self._site_path(source_url, UNAVAILABLE_BOOK_PATH):
            return None
        source = self._site_path(source_url, BOOK_PATH)
        if source is None or source[1] != comic_id:
            logger.warning("Unexpected page returned for %s", book_url)
            return False

        title = soup.select_one("h1")
        cover = soup.select_one(".d-vod-pic img")
        description = soup.select_one(".d-des")
        if title is None or not title.get_text(strip=True) or cover is None or not cover.get("src"):
            logger.warning("Book metadata missing for %s", book_url)
            return False

        links = soup.select("#playlist .d-player-list a[href]")
        if not links:
            if soup.select_one("#playlist .d-player-list") is not None:
                return None
            logger.warning("Chapter list missing for %s", book_url)
            return False

        chapters = {}
        for link in links:
            chapter = self._site_path(link["href"], CHAPTER_PATH)
            name = link.get_text(" ", strip=True)
            if chapter and chapter[1] == comic_id and name:
                chapters[urljoin(self.url, chapter[0])] = name
        if not chapters:
            logger.warning("No usable chapter links for %s", book_url)
            return False

        metadata = {}
        for row in soup.select(".d-vod-type > p"):
            label = row.select_one("span")
            if label:
                metadata[label.get_text(strip=True).rstrip("：:")] = row

        def field(label):
            row = metadata.get(label)
            return row.get_text(strip=True).removeprefix(label).lstrip("：: ") if row else ""

        book = self.session.scalar(select(Book).where(Book.url == book_url))
        if book is None:
            book = Book(url=book_url, language=self.language)
        book.title = title.get_text(strip=True)
        book.cover_url = urljoin(book_url, cover.get("data-original") or cover["src"])
        book.description = description.get_text("\n", strip=True) if description else ""
        book.status = {
            "连载": BookStatus.ONGOING,
            "连载中": BookStatus.ONGOING,
            "完结": BookStatus.COMPLETED,
            "已完结": BookStatus.COMPLETED,
        }.get(field("状态"), BookStatus.UNKNOWN)

        nickname = field("作者")
        if nickname and len(nickname) <= 50:
            book.author = (
                self.session.scalar(select(Author).where(Author.nickname == nickname))
                or Author(nickname=nickname)
            )
        if tag_row := metadata.get("类型"):
            names = dict.fromkeys(tag.get_text(strip=True) for tag in tag_row.select("a"))
            book.tags = [
                self.session.scalar(select(Tag).where(Tag.name == name)) or Tag(name=name)
                for name in names if 0 < len(name) <= 50
            ]

        saved = {chapter.url: chapter for chapter in book.chapters}
        # An older copy of the catalogue can lack chapters we already know.
        # Keep their position until the source supplies the complete list again.
        complete_list = saved.keys() <= chapters.keys()
        next_number = max((chapter.number for chapter in saved.values()), default=0) + 1
        for number, (url, name) in enumerate(chapters.items(), start=1):
            if url in saved:
                saved[url].name = name
                if complete_list:
                    saved[url].number = number
            else:
                book.chapters.append(Chapter(url=url, name=name, number=number if complete_list else next_number))
                next_number += 1
        book.chapters_count = len(book.chapters)
        if book.id_book is None:
            book.site = self.site
            self.session.add(book)
        return True

    @staticmethod
    def _page_urls(soup, chapter_id: str, chapter_url: str) -> list[str]:
        payload = next((
            match for script in soup.select("script:not([src])")
            if (match := PAGE_DATA.search(script.string or ""))
        ), None)
        if payload is None or payload[1] != chapter_id:
            raise ValueError("Chapter image list missing")
        key = PAGE_KEYS[int(chapter_id) % len(PAGE_KEYS)]
        encoded = bytes(b ^ key[i % len(key)] for i, b in enumerate(b64decode(payload[3], validate=True)))
        pages = json.loads(b64decode(encoded, validate=True))
        if not isinstance(pages, list) or not pages:
            raise ValueError("Chapter image list is empty or invalid")

        urls = []
        for page in pages:
            href = page.get("url") if isinstance(page, dict) else None
            if not isinstance(href, str) or not href.strip():
                raise ValueError("Chapter image URL missing")
            url = urljoin(chapter_url, href)
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or parsed.netloc != host
                    or not any(host == domain or host.endswith("." + domain) for domain in IMAGE_DOMAINS)):
                raise ValueError("Unexpected chapter image host")
            urls.append(url)
        return urls

    @staticmethod
    def _download_page(url: str, chapter_url: str, number: int) -> tuple[bytes, str]:
        for _ in range(2):
            response = request(url, headers={"Referer": chapter_url}, timeout=15, allow_redirects=False)
            if response is None:
                continue
            with response:
                if response.status_code in (502, 503, 504):
                    continue
                if response.status_code != 200:
                    raise ValueError(f"Could not download page {number} (HTTP {response.status_code})")
                image = response.content
                if suffix := image_suffix(image):
                    return image, suffix
                raise ValueError(f"Invalid image for page {number}")
        raise ValueError(f"Could not download page {number} after two attempts")

    def process_chapter(self, chapter_url: str, *, data_root: Path | None = None) -> bool:
        try:
            with self.session.begin_nested():
                return self._process_chapter(chapter_url, data_root)
        except Exception:
            logger.exception("Cannot process chapter %s; chapter changes rolled back", chapter_url)
            return False

    def _process_chapter(self, chapter_url: str, data_root: Path | None) -> bool:
        match = self._site_path(chapter_url, CHAPTER_PATH)
        if match is None:
            return False
        chapter = self.session.scalar(
            select(Chapter).join(Chapter.book).where(Chapter.url == chapter_url, Book.site == self.site)
        )
        if chapter is None:
            logger.warning("Chapter not found in database: %s", chapter_url)
            return False

        soup = request_soup(chapter_url, timeout=10)
        if soup is None:
            return False
        source = self._site_path(getattr(soup, "source_url", None) or chapter_url, CHAPTER_PATH)
        if source is None or source[0] != match[0]:
            logger.warning("Unexpected page returned for %s", chapter_url)
            return False
        urls = self._page_urls(soup, match[2], chapter_url)

        relative = Path("data") / self.name / str(chapter.id_book) / f"{chapter.id_chapter}.cbz"
        path = (data_root or Path(__file__).resolve().parents[1]) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as temporary:
            tmp_path = Path(temporary.name)
        try:
            with ZipFile(tmp_path, "w") as archive:
                for number, url in enumerate(urls, start=1):
                    image, suffix = self._download_page(url, chapter_url, number)
                    archive.writestr(f"{number:05d}{suffix}", image)
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)

        chapter.file_path = str(relative)
        logger.info("Cached chapter %s (%d pages)", chapter_url, len(urls))
        return True
