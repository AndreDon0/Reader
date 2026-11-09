"""Local reader tests using an isolated database/cache and no network access."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from models import (Author, Base, Book, BookAltTitle, BookStatus, BookType, CacheRemoval, Chapter,
                    DownloadJob, Followed, FollowedStatus, ImportJob, JobState, ReadChapter, Site, User)
from sites.base_site import SearchResult, SourceSearchUnavailable
from sites.novgo import Novgo
from utils.jobs import enqueue_watched, recover_interrupted, remove_cached_file, run_download, run_import
from web import cached_text, create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "data").mkdir()
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def sqlite_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        with self.factory.begin() as connection:
            site = Site(name="NOVGO", url="https://novgo.net/")
            author = Author(nickname="Example Author")
            self.book = Book(title="First story <script>alert(1)</script>", description="A story about learning.",
                             chapters_count=8, language="english", status=BookStatus.ONGOING,
                             type=BookType.NOVEL, url="https://novgo.net/first.html", cover_url="javascript:alert(1)",
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
            self.users = [User(username=name, password_hash="unused", is_admin=name == "admin")
                          for name in ("reader", "other", "admin")]
            connection.add_all(self.users)
            connection.flush()
            self.user_ids = [user.id_user for user in self.users]
            self.chapter_ids = [item.id_chapter for item in chapters]
            chapters[0].file_path = "data/first.txt"
            (self.root / chapters[0].file_path).write_text(
                "Chapter 1\n\nFirst paragraph.\n\n<script>untrusted text</script>\n", encoding="utf-8")
        self.loader = self.enterContext(patch.object(Novgo, "process_chapter", autospec=True,
                                                   side_effect=self.save_chapter))
        self.enterContext(patch.dict("os.environ", {"READER_MIN_FREE_BYTES": "0"}))
        self.app = create_app({"TESTING": True, "SECRET_KEY": "offline-tests",
                               "SESSION_FACTORY": self.factory, "DATA_ROOT": self.root,
                               "BOOKS_PER_PAGE": 2, "CHAPTERS_PER_PAGE": 2})
        self.client = self.sign_in(self.user_ids[0])

    def sign_in(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(user=str(user_id), session_version=0, csrf_token="token")
        return client

    def save_chapter(self, site, url, *, data_root):
        chapter = site.session.scalar(select(Chapter).where(Chapter.url == url))
        chapter.file_path = f"data/{chapter.id_chapter}.txt"
        (self.root / chapter.file_path).write_text(f"{chapter.name}\n\nDownloaded paragraph.\n", encoding="utf-8")
        return True

    def token(self, chapter_id):
        page = self.client.get(f"/chapters/{chapter_id}")
        parsed = BeautifulSoup(page.data, "lxml")
        return parsed.select_one('input[name="csrf_token"]')["value"]

    def test_library_is_paginated_and_uses_real_counts(self):
        result = self.client.get("/catalog")
        self.assertEqual(result.status_code, 200)
        page = BeautifulSoup(result.data, "lxml")
        self.assertEqual(len(page.select(".book-card")), 2)
        self.assertIn("Page 1 of 2", page.get_text())
        result = self.client.get("/catalog?page=2")
        self.assertEqual(len(BeautifulSoup(result.data, "lxml").select(".book-card")), 1)

    def test_search_matches_title_author_and_alternative_title(self):
        for query in ("First story", "Example Author", "Hidden Alias"):
            with self.subTest(query=query):
                result = self.client.get("/catalog", query_string={"q": query})
                page = BeautifulSoup(result.data, "lxml")
                self.assertEqual(len(page.select(".book-card")), 1)
                self.assertIn("First story", page.select_one(".book-card h3").get_text())

    def test_search_treats_percent_as_literal_and_filters_status(self):
        page = BeautifulSoup(self.client.get("/catalog", query_string={"q": "100%"}).data, "lxml")
        self.assertEqual(len(page.select(".book-card")), 1)
        self.assertIn("100% story", page.select_one(".book-card h3").get_text())
        result = self.client.get("/catalog?status=completed")
        self.assertIn(b"100% story", result.data)
        self.assertNotIn(b"100X story", result.data)

    def test_empty_search_and_invalid_page_values_are_usable(self):
        result = self.client.get("/catalog?q=does-not-exist&page=999")
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"No matching titles", result.data)
        for page in ("bad", "-10", "999999999999"):
            self.assertEqual(self.client.get("/catalog", query_string={"page": page}).status_code, 200)

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
        self.assertIn(b"Download chapter", result.data)
        self.loader.assert_not_called()

    def test_loading_chapter_persists_path_and_subsequent_read_uses_cache(self):
        chapter_id = self.chapter_ids[1]
        result = self.client.post(f"/chapters/{chapter_id}/load", data={"csrf_token": self.token(chapter_id)})
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.location, f"/chapters/{chapter_id}")
        self.loader.assert_not_called()
        self.assertIn(b"Chapter queued", self.client.get(result.location).data)
        self.assertTrue(run_download(self.factory, self.root))
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
        self.assertTrue(run_download(self.factory, self.root))
        self.assertFalse(run_download(self.factory, self.root))
        self.loader.assert_called_once()

    def test_download_failure_returns_retry_page_without_exposing_exception(self):
        chapter_id = self.chapter_ids[1]
        token = self.token(chapter_id)
        self.loader.side_effect = RuntimeError("secret-token")
        self.client.post(f"/chapters/{chapter_id}/load", data={"csrf_token": token})
        with self.factory.begin() as connection:
            job = connection.get(DownloadJob, chapter_id)
            job.state = JobState.RUNNING
            job.attempts = 2
        recover_interrupted(self.factory)
        with self.assertLogs("utils.jobs", level="ERROR"):
            self.assertTrue(run_download(self.factory, self.root))
        result = self.client.get(f"/chapters/{chapter_id}")
        self.assertIn(b"Retry download", result.data)
        self.assertNotIn(b"secret-token", result.data)
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
        self.assertIn(b"Download chapter", result.data)

    def test_missing_cached_file_offers_reload(self):
        (self.root / "data" / "first.txt").unlink()
        result = self.client.get(f"/chapters/{self.chapter_ids[0]}")
        self.assertIn(b"Download chapter", result.data)

    def test_missing_book_chapter_and_route_return_404(self):
        for path in ("/books/99999", "/chapters/99999", "/unknown"):
            with self.subTest(path=path):
                result = self.client.get(path)
                self.assertEqual(result.status_code, 404)
                self.assertIn(b"Back to library", result.data)

    def test_unavailable_database_returns_helpful_503(self):
        self.app.config["SESSION_FACTORY"] = None
        with patch("web.database.initialize_database", return_value=False):
            result = self.client.get("/catalog")
        self.assertEqual(result.status_code, 503)
        self.assertIn(b"database is unavailable", result.data)

    def test_registration_libraries_and_admin_permissions(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/catalog").status_code, 302)
        token = BeautifulSoup(client.get("/register").data, "lxml").select_one("[name=csrf_token]")["value"]
        response = client.post("/register", data={"username": "new-reader", "password": "long-local-test-password",
                                                  "csrf_token": token})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(client.get("/settings/admin").status_code, 403)
        book = f"/books/{self.book_id}"
        token = BeautifulSoup(client.get(book).data, "lxml").select_one("[name=csrf_token]")["value"]
        self.assertEqual(client.post(book + "/follow", data={"status": "reading", "csrf_token": token}).status_code, 303)
        self.assertIn(b"First story", client.get("/my-library").data)
        self.assertNotIn(b"First story", self.client.get("/my-library").data)
        self.assertEqual(client.get("/").status_code, 200)
        admin = self.sign_in(self.user_ids[2])
        self.assertEqual(admin.get("/settings/admin").status_code, 200)
        self.assertEqual(admin.post(f"/settings/admin/users/{self.user_ids[2]}",
                                    data={"csrf_token": "token", "action": "delete", "confirm": "DELETE"}).status_code, 400)

    def test_progress_is_shared_across_sessions_but_not_accounts(self):
        url = f"/chapters/{self.chapter_ids[0]}"
        def view(client):
            return BeautifulSoup(client.get(url).data, "lxml").select_one("[data-view]")
        def save(client, token, anchor, sequence=1, finished=0):
            return client.post(url + "/progress", data={"csrf_token": "token", "view": token,
                                                        "anchor": anchor, "offset": ".4", "sequence": sequence,
                                                        "finished": finished})
        second = self.sign_in(self.user_ids[0])
        stranger = self.sign_in(self.user_ids[1])
        old = view(self.client)["data-view"]
        new = view(second)["data-view"]
        self.assertEqual(save(second, new, 1, finished=1).status_code, 204)
        self.assertEqual(save(self.client, old, 0, sequence=100).status_code, 204)
        self.assertEqual(save(stranger, new, 0).status_code, 400)
        self.assertEqual(view(self.client)["data-anchor"], "1")
        self.assertEqual(view(self.client)["data-finished"], "1")
        self.assertEqual(view(stranger)["data-anchor"], "0")
        self.assertIn(b"First story", self.client.get("/").data)

    def test_search_falls_back_and_two_users_share_a_title_import(self):
        remote = Novgo.url + "remote.html"
        first = Novgo.url + "first.html"
        with patch.object(Novgo, "search", return_value=[SearchResult("Remote", remote), SearchResult("Remote", remote),
                                                          SearchResult("Known", first)]):
            result = self.client.get("/search/sources/NOVGO?q=Remote")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.data.count(b"data-result-url="), 2)
            self.assertIn(f"/books/{self.book_id}".encode(), result.data)
        with patch("sites.senkuro.Senkuro.search", side_effect=SourceSearchUnavailable):
            self.assertIn(b"Source unavailable", self.client.get("/search/sources/Senkuro?q=Remote").data)
        self.assertIn(b"First story", self.client.get("/catalog?q=First").data)
        other = self.sign_in(self.user_ids[1])
        for client in (self.client, other):
            response = client.post("/imports", data={"csrf_token": "token", "source": "NOVGO", "url": remote})
            self.assertEqual(response.status_code, 303)
        def import_title(site, url):
            site.session.add(Book(title="Remote title", description="", chapters_count=0, language="english",
                                  url=url, cover_url="", type=BookType.NOVEL, site=site.site))
            return True
        with patch.object(Novgo, "process_book", autospec=True, side_effect=import_title) as loader:
            self.assertTrue(run_import(self.factory, Novgo))
            self.assertFalse(run_import(self.factory, Novgo))
            loader.assert_called_once()
        self.assertIn(b"Remote title", self.client.get("/my-library?status=planning").data)
        self.assertIn(b"Remote title", other.get("/my-library?status=planning").data)

    def test_auto_download_only_queues_new_releases_once(self):
        for user_id in self.user_ids[:2]:
            client = self.sign_in(user_id)
            self.assertEqual(client.post(f"/books/{self.book_id}/watch",
                                         data={"csrf_token": "token", "enabled": "1"}).status_code, 303)
            self.assertIn(b"Waiting for the next release", client.get(f"/books/{self.book_id}?page=2").data)
        with self.factory.begin() as db:
            book = db.get(Book, self.book_id)
            site_id = book.id_site
            for number in (2, 9):
                db.add(Chapter(book=book, number=number, name=f"Chapter {number}",
                               url=Novgo.url + f"watch-{number}", discovered_at=datetime.now(UTC) + timedelta(seconds=1)))
            db.flush()
            enqueue_watched(db, site_id)
            enqueue_watched(db, site_id)
        with self.factory() as db:
            self.assertEqual(list(db.scalars(select(Chapter.number).join(DownloadJob))), [9])

    def test_admin_hide_and_permanent_delete_cascade_and_clear_cache(self):
        chapter_id = self.chapter_ids[0]
        admin = self.sign_in(self.user_ids[2])
        url = f"/settings/admin/chapters/{chapter_id}"
        self.assertEqual(admin.post(url, data={"csrf_token": "token", "action": "save",
                                              "hidden": "1", "display_name": "Custom name"}).status_code, 303)
        self.assertEqual(self.client.get(f"/chapters/{chapter_id}").status_code, 404)
        self.assertIn(b"Custom name", admin.get(url).data)
        with self.factory.begin() as db:
            db.add(Followed(id_user=self.user_ids[0], id_book=self.book_id, status=FollowedStatus.READING))
            db.add(ReadChapter(id_user=self.user_ids[0], id_chapter=chapter_id))
        self.assertEqual(admin.post(f"/settings/admin/books/{self.book_id}",
                                    data={"csrf_token": "token", "action": "delete", "confirm": "DELETE"}).status_code, 303)
        with self.factory() as db:
            self.assertIsNone(db.get(Book, self.book_id))
            self.assertIsNone(db.get(ReadChapter, (self.user_ids[0], chapter_id)))
            self.assertIsNotNone(db.get(CacheRemoval, "data/first.txt"))
        self.assertTrue(remove_cached_file(self.factory, self.root))
        self.assertFalse((self.root / "data/first.txt").exists())


if __name__ == "__main__":
    unittest.main()
