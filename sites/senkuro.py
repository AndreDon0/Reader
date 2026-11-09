import logging
import re
from datetime import UTC, datetime
from urllib.parse import quote, unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from models import Book, BookAltTitle, BookStatus, BookType
from sites.base_site import BaseSite, SearchResult, SourceSearchUnavailable
from utils.custom_request import request

logger = logging.getLogger(__name__)

MANGA_FIELDS = """
            slug
            originalName { content }
            titles { lang content }
            alternativeNames { content }
            type status chapters score views
            cover { original { url } }
            localizations {
                lang
                description {
                    ... on TiptapNodeNestedBlock {
                        content { ... on TiptapNodeText { text } }
                    }
                    ... on TiptapNodeText { text }
                }
            }
"""

CATALOG_QUERY = """
query ReaderCatalog($after: String) {
    mangas(first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
        pageInfo { hasNextPage endCursor }
        edges { node {
""" + MANGA_FIELDS + """
        } }
    }
}
"""

TITLE_QUERY = "query ReaderTitle($slug: String!) { manga(slug: $slug) {" + MANGA_FIELDS + "} }"
SEARCH_QUERY = """
query ReaderSearch($query: String!) {
    mangaTachiyomiSearch(query: $query, offset: 0) {
        mangas { slug originalName { content } titles { lang content } }
    }
}
"""
BOOK_TYPES = {
    "MANGA": BookType.MANGA,
    "RU_MANGA": BookType.MANGA,
    "OEL_MANGA": BookType.MANGA,
    "MANHUA": BookType.MANHUA,
    "MANHWA": BookType.MANHWA,
    "COMICS": BookType.WESTERN,
}
BOOK_STATUSES = {
    "ONGOING": BookStatus.ONGOING,
    "FINISHED": BookStatus.COMPLETED,
    "CANCELLED": BookStatus.CANCELLED,
}


def _node_text(node: dict) -> str:
    return node.get("text") or "".join(_node_text(child) for child in node.get("content") or ())


def _description(entry: dict) -> str:
    for loc in entry.get("localizations") or ():
        if loc["lang"] == "RU":
            return "\n".join(filter(None, map(_node_text, loc["description"] or ())))
    return ""


class Senkuro(BaseSite):
    name = "Senkuro"
    url = "https://senkuro.me/"
    # This API serves the .me frontend; .me is the host for reader-facing links.
    api_url = "https://api.senkuro.com/graphql"
    language = "russian"
    use_vpn = False
    catalog_batches_per_cycle = 20
    title_path = re.compile(r"/manga/(?:[A-Za-z0-9_-]|%[A-Fa-f0-9]{2})+/?")

    @classmethod
    def _query(cls, document: str, *, timeout=10, **variables) -> dict:
        response = request(
            cls.api_url, method="POST", direct=True, timeout=timeout,
            headers={"Origin": cls.url.rstrip("/")},
            json={"query": document, "variables": variables},
        )
        if response is None:
            raise RuntimeError("Senkuro API request failed")
        with response:
            response.raise_for_status()
            payload = response.json()
        if payload.get("errors") or not isinstance(payload.get("data"), dict):
            raise ValueError("Senkuro returned an unsuccessful GraphQL response")
        return payload["data"]

    @classmethod
    def search(cls, title: str) -> list[SearchResult]:
        try:
            entries = cls._query(SEARCH_QUERY, query=title, timeout=4)["mangaTachiyomiSearch"]["mangas"]
            found = []
            for entry in entries:
                slug = entry.get("slug")
                name = cls._name(entry)
                if slug and name:
                    url = cls.canonical_title_url(f"/manga/{quote(slug, safe='')}")
                    if url:
                        found.append(SearchResult(name, url))
            return found
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SourceSearchUnavailable from exc

    def process_book(self, url: str) -> bool:
        url = self.canonical_title_url(url)
        if url is None:
            return False
        slug = unquote(urlsplit(url).path.rsplit("/", 1)[1])
        entry = self._query(TITLE_QUERY, slug=slug).get("manga")
        if not entry or entry.get("slug") != slug or not self._name(entry):
            return False
        book = self.session.scalar(select(Book).where(Book.url == url))
        self._save_book(book, url, entry)
        return True

    def is_up(self) -> bool:
        # The catalog uses the API, so check it rather than the frontend.
        return self._query("{ __typename }").get("__typename") == "Query"

    def process_catalog(self, cursor: str | None = None) -> str | None:
        page = self._query(CATALOG_QUERY, after=cursor)["mangas"]
        entries = page["edges"]
        info = page["pageInfo"]
        next_cursor = info["endCursor"] if info["hasNextPage"] else None
        if info["hasNextPage"] and (not entries or not next_cursor or next_cursor == cursor):
            raise ValueError("Senkuro catalog cursor did not advance")

        by_url = {}
        for edge in entries:
            entry = edge["node"]
            slug = entry.get("slug")
            if not slug:
                logger.warning("Skipping Senkuro catalog entry without a slug")
                continue
            by_url[f"{self.url}manga/{quote(slug, safe='')}"] = entry

        existing = {book.url: book for book in self.session.scalars(
            select(Book).where(Book.url.in_(by_url)).options(selectinload(Book.alt_titles)),
        )}
        for url, entry in by_url.items():
            self._save_book(existing.get(url), url, entry)
        logger.debug("Processed %d Senkuro catalog entries", len(by_url))
        if next_cursor is None:
            logger.info("Reached end of the Senkuro catalog")
        return next_cursor

    @staticmethod
    def _name(entry: dict) -> str | None:
        titles = entry.get("titles") or []
        return next((item["content"] for item in titles if item.get("lang") == "RU" and item.get("content")),
                    (entry.get("originalName") or {}).get("content") or
                    next((item["content"] for item in titles if item.get("content")), None))

    def _save_book(self, book: Book | None, url: str, entry: dict) -> None:
        titles = entry.get("titles") or []
        original = (entry.get("originalName") or {}).get("content")
        alternate = entry.get("alternativeNames") or []
        names = dict.fromkeys(name for name in (
            original, *(title.get("content") for title in titles + alternate),
        ) if name)
        title = self._name(entry) or next(iter(names), None)
        if title is None:
            logger.warning("Skipping Senkuro catalog entry without a title: %s", url)
            return

        if book is None:
            book = Book(url=url, site=self.site, language=self.language)
            self.session.add(book)
        cover = ((entry.get("cover") or {}).get("original") or {}).get("url") or ""
        self.update_metadata(book, title=title, description=_description(entry),
                             cover_url=cover if urlsplit(cover).scheme == "https" else "",
                             type=BOOK_TYPES.get(entry.get("type"), BookType.UNKNOWN),
                             status=BOOK_STATUSES.get(entry.get("status"), BookStatus.UNKNOWN))
        count = entry.get("chapters") or 0
        if count > (book.chapters_count or 0):
            book.last_release_at = datetime.now(UTC)
        book.chapters_count = count
        book.views = entry.get("views")
        score = entry.get("score")
        book.rating = score / 2 if score else None  # Senkuro uses 10, Reader uses 5.

        names.pop(title, None)
        saved_names = {alt.title: alt for alt in book.alt_titles}
        if names.keys() != saved_names.keys() and "alt_titles" not in (book.edited_fields or ()):
            book.alt_titles = [saved_names.get(name) or BookAltTitle(title=name) for name in names]
