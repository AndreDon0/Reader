"""Local reader tests using an isolated database/cache and no network access."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from models import Author, Base, Book, BookAltTitle, BookStatus, BookType, Chapter, Site
from web import cached_text, create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "data").mkdir()
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        with self.factory.begin() as connection:
            site = Site(name="NOVGO", url="https://novgo.net/")
            author = Author(nickname="Example Author")
            self.book = Book(title="First story <script>alert(1)</script>", description="A story about learning.",
                             chapters_count=8, language="english", status=BookStatus.ONGOING,
                             type=BookType.NOVEL, url="https://novgo.net/first", cover_url="javascript:alert(1)",
                             site=site, author=author, alt_titles=[BookAltTitle(title="Hidden Alias")])
            second = Book(title="100% story", description="Second story", chapters_count=1,
                          language="english", status=BookStatus.COMPLETED, site=site,
                          url="https://novgo.net/second", cover_url="")
            third = Book(title="100X story", description="Third story", chapters_count=0,
                         language="english", site=site, url="https://novgo.net/third", cover_url="")
            connection.add_all([self.book, second, third])
            connection.flush()
            self.book_id, self.second_id = self.book.id_book, second.id_book
            chapters = [Chapter(number=n, name=f"Chapter {n}", url=f"https://novgo.net/first/chapter-{n}",
                                book=self.book) for n in (1, 3, 8)]
            connection.add_all(chapters)
            connection.add(Chapter(number=2, name="Other book chapter", url="https://novgo.net/second/2", book=second))
            connection.flush()
            self.chapter_ids = [item.id_chapter for item in chapters]
            chapters[0].file_path = "data/first.txt"
            (self.root / chapters[0].file_path).write_text(
                "Chapter 1\n\nFirst paragraph.\n\n<script>untrusted text</script>\n", encoding="utf-8")
        self.loader = Mock(side_effect=self.save_chapter)
        self.app = create_app({"TESTING": True, "SECRET_KEY": "offline-tests",
                               "SESSION_FACTORY": self.factory, "DATA_ROOT": self.root,
                               "BOOKS_PER_PAGE": 2, "CHAPTERS_PER_PAGE": 2,
                               "CHAPTER_LOADER": self.loader})
        self.client = self.app.test_client()

    def save_chapter(self, connection, chapter):
        chapter.file_path = f"data/{chapter.id_chapter}.txt"
        (self.root / chapter.file_path).write_text(f"{chapter.name}\n\nDownloaded paragraph.\n", encoding="utf-8")
        return True

    def token(self, chapter_id):
        page = self.client.get(f"/chapters/{chapter_id}")
        parsed = BeautifulSoup(page.data, "lxml")
        return parsed.select_one('input[name="csrf_token"]')["value"]

    def test_library_is_paginated_and_uses_real_counts(self):
        result = self.client.get("/")
        self.assertEqual(result.status_code, 200)
        page = BeautifulSoup(result.data, "lxml")
        self.assertEqual(len(page.select(".book-card")), 2)
        self.assertIn("Page 1 of 2", page.get_text())
        result = self.client.get("/?page=2")
        self.assertEqual(len(BeautifulSoup(result.data, "lxml").select(".book-card")), 1)

    def test_search_matches_title_author_and_alternative_title(self):
        for query in ("First story", "Example Author", "Hidden Alias"):
            with self.subTest(query=query):
                result = self.client.get("/", query_string={"q": query})
                page = BeautifulSoup(result.data, "lxml")
                self.assertEqual(len(page.select(".book-card")), 1)
                self.assertIn("First story", page.select_one(".book-card h3").get_text())

    def test_search_treats_percent_as_literal_and_filters_status(self):
        page = BeautifulSoup(self.client.get("/", query_string={"q": "100%"}).data, "lxml")
        self.assertEqual(len(page.select(".book-card")), 1)
        self.assertIn("100% story", page.select_one(".book-card h3").get_text())
        result = self.client.get("/?status=completed")
        self.assertIn(b"100% story", result.data)
        self.assertNotIn(b"100X story", result.data)

    def test_empty_search_and_invalid_page_values_are_usable(self):
        result = self.client.get("/?q=does-not-exist&page=999")
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"No matching books", result.data)
        for page in ("bad", "-10", "999999999999"):
            self.assertEqual(self.client.get("/", query_string={"page": page}).status_code, 200)

    def test_book_page_lists_chapters_with_pagination_and_escaped_metadata(self):
        result = self.client.get(f"/books/{self.book_id}")
        page = BeautifulSoup(result.data, "lxml")
        self.assertEqual(len(page.select(".chapter-list li")), 2)
        self.assertIn("First story <script>alert(1)</script>", page.h1.get_text())
        self.assertNotIn(b"<script>alert(1)</script>", result.data)
        self.assertNotIn(b"javascript:", result.data)
        result = self.client.get(f"/books/{self.book_id}?page=2")
        self.assertIn(b"Chapter 8", result.data)
        self.assertNotIn(b"Chapter 1</span>", result.data)

    def test_cached_reader_escapes_text_and_uses_real_neighbor_numbers(self):
        result = self.client.get(f"/chapters/{self.chapter_ids[0]}")
        page = BeautifulSoup(result.data, "lxml")
        self.assertEqual(result.status_code, 200)
        self.assertIn("First paragraph.", page.select_one(".chapter-body").get_text())
        self.assertIn(b"&lt;script&gt;untrusted text&lt;/script&gt;", result.data)
        self.assertEqual(page.select_one('a[rel="next"]')["href"], f"/chapters/{self.chapter_ids[1]}")
        self.assertIsNone(page.select_one('a[rel="prev"]'))
        self.loader.assert_not_called()

    def test_reader_neighbors_do_not_cross_books_and_list_returns_to_current_page(self):
        result = self.client.get(f"/chapters/{self.chapter_ids[2]}")
        page = BeautifulSoup(result.data, "lxml")
        self.assertEqual(page.select_one('a[rel="prev"]')["href"], f"/chapters/{self.chapter_ids[1]}")
        self.assertIsNone(page.select_one('a[rel="next"]'))
        self.assertIn(f"/books/{self.book_id}?page=2", result.get_data(as_text=True))

    def test_uncached_get_shows_load_action_without_network_or_writes(self):
        result = self.client.get(f"/chapters/{self.chapter_ids[1]}")
        self.assertIn(b"Load chapter", result.data)
        self.loader.assert_not_called()

    def test_loading_chapter_persists_path_and_subsequent_read_uses_cache(self):
        chapter_id = self.chapter_ids[1]
        result = self.client.post(f"/chapters/{chapter_id}/load", data={"csrf_token": self.token(chapter_id)})
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.location, f"/chapters/{chapter_id}")
        with self.factory() as connection:
            saved = connection.get(Chapter, chapter_id)
            self.assertEqual(saved.file_path, f"data/{chapter_id}.txt")
        self.assertIn(b"Downloaded paragraph.", self.client.get(result.location).data)
        self.loader.assert_called_once()

    def test_repeated_load_does_not_download_again(self):
        chapter_id = self.chapter_ids[1]
        token = self.token(chapter_id)
        for _ in range(2):
            self.assertEqual(self.client.post(f"/chapters/{chapter_id}/load", data={"csrf_token": token}).status_code, 303)
        self.loader.assert_called_once()

    def test_download_failure_returns_retry_page_without_exposing_exception(self):
        chapter_id = self.chapter_ids[1]
        token = self.token(chapter_id)
        self.loader.side_effect = RuntimeError("secret-token")
        with self.assertLogs("web", level="ERROR") as logs:
            result = self.client.post(f"/chapters/{chapter_id}/load", data={"csrf_token": token})
        self.assertEqual(result.status_code, 502)
        self.assertIn(b"Please try again", result.data)
        self.assertNotIn(b"secret-token", result.data)
        self.assertNotIn("secret-token", " ".join(logs.output))
        with self.factory() as connection:
            self.assertIsNone(connection.get(Chapter, chapter_id).file_path)

    def test_load_requires_valid_csrf_token_including_non_ascii_input(self):
        for token in ("", "incorrect", "неверно"):
            with self.subTest(token=token):
                result = self.client.post(f"/chapters/{self.chapter_ids[1]}/load", data={"csrf_token": token})
                self.assertEqual(result.status_code, 400)
        self.loader.assert_not_called()

    def test_cache_path_cannot_read_files_outside_data_directory(self):
        secret = self.root / "secret.txt"
        secret.write_text("private-value", encoding="utf-8")
        self.assertIsNone(cached_text(str(secret), self.root))
        link = self.root / "data" / "escape.txt"
        link.symlink_to(secret)
        self.assertIsNone(cached_text("data/escape.txt", self.root))
        with self.factory.begin() as connection:
            connection.get(Chapter, self.chapter_ids[0]).file_path = str(secret)
        result = self.client.get(f"/chapters/{self.chapter_ids[0]}")
        self.assertNotIn(b"private-value", result.data)
        self.assertIn(b"Load chapter", result.data)

    def test_missing_cached_file_offers_reload(self):
        (self.root / "data" / "first.txt").unlink()
        result = self.client.get(f"/chapters/{self.chapter_ids[0]}")
        self.assertIn(b"Load chapter", result.data)

    def test_missing_book_chapter_and_route_return_404(self):
        for path in ("/books/99999", "/chapters/99999", "/unknown"):
            with self.subTest(path=path):
                result = self.client.get(path)
                self.assertEqual(result.status_code, 404)
                self.assertIn(b"Back to library", result.data)

    def test_unavailable_database_returns_helpful_503(self):
        self.app.config["SESSION_FACTORY"] = None
        with patch("web.database.initialize_database", return_value=False):
            result = self.client.get("/")
        self.assertEqual(result.status_code, 503)
        self.assertIn(b"database is unavailable", result.data)


if __name__ == "__main__":
    unittest.main()
