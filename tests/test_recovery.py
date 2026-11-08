"""Offline tests for failure recovery; never use the configured database/VPN."""

import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import database
import main
from models import Base, Book, Chapter, Site, SiteCheck
from sites.base_site import BaseSite
from sites.novgo import Novgo
from utils import custom_request, password
from utils.logging_config import RedactingFormatter, configure_logging


def soup(html):
    return BeautifulSoup(html, "lxml")


def book_page(total=6, size=2):
    return soup(f"""
        <h1 class="tit">Example</h1>
        <div class="m-imgtxt">
          <div class="pic"><img src="cover.jpg"></div>
          <div class="txt">
            <div class="item"><div class="right"><a>Author</a></div></div>
            <div class="item"><div class="right"><a>Fantasy</a></div></div>
            <div class="item"><div class="right">English</div></div>
            <div class="item"><div class="right">Ongoing</div></div>
          </div>
        </div>
        <div id="novel-summary-inner">Description</div>
        <div id="list-chapter" data-total-chapters="{total}" data-page-size="{size}"></div>
    """)


def chapter_page(*numbers):
    links = "".join(f'<a href="/chapter-{n}">Chapter {n}</a>' for n in numbers)
    return soup(f'<ul id="idData" class="ul-list5">{links}</ul>')


def response(status=200):
    result = requests.Response()
    result.status_code = status
    result.url = "https://redirect.example/books/example/"
    result._content = b"<h1>Example</h1>"
    result._content_consumed = True
    return result


class RequestRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        dotenv = patch.object(custom_request, "load_dotenv")
        dotenv.start()
        self.addCleanup(dotenv.stop)

    def test_direct_request_preserves_http_error_response(self):
        result = response(503)
        with patch.object(custom_request.requests, "request", return_value=result) as fetch:
            self.assertIs(custom_request.request("https://example.com", stream=True), result)
        fetch.assert_called_once_with("GET", "https://example.com", timeout=30, stream=True)

    def test_connection_error_is_logged_without_exception_secrets(self):
        with patch.object(custom_request.requests, "request", side_effect=requests.ConnectionError("secret")):
            with self.assertLogs(custom_request.logger, level="ERROR") as logs:
                self.assertIsNone(custom_request.request("https://example.com"))
        self.assertIn("ConnectionError", " ".join(logs.output))
        self.assertNotIn("secret", " ".join(logs.output))

    def test_http_error_is_not_parsed(self):
        with patch.object(custom_request, "request", return_value=response(503)):
            with self.assertLogs(custom_request.logger, level="WARNING"):
                self.assertIsNone(custom_request.request_soup("https://example.com"))

    def test_soup_preserves_redirect_and_closes_response(self):
        result = response()
        result.close = Mock()
        with patch.object(custom_request, "request", return_value=result):
            parsed = custom_request.request_soup("https://example.com")
        self.assertEqual(parsed.source_url, result.url)
        self.assertEqual(parsed.h1.text, "Example")
        result.close.assert_called_once()

    def test_missing_response_is_not_parsed(self):
        with patch.object(custom_request, "request", return_value=None):
            self.assertIsNone(custom_request.request_soup("https://example.com"))

    def test_parser_failure_is_logged_and_response_closed(self):
        result = response()
        result.close = Mock()
        with patch.object(custom_request, "request", return_value=result):
            with patch.object(custom_request, "BeautifulSoup", side_effect=ValueError("bad HTML")):
                with self.assertLogs(custom_request.logger, level="ERROR"):
                    self.assertIsNone(custom_request.request_soup("https://example.com"))
        result.close.assert_called_once()

    def test_vpn_failures_never_send_direct_request(self):
        for mode in ("missing-xray", "invalid-subscription", "unsupported-stream"):
            with self.subTest(mode=mode):
                with patch.object(custom_request.requests, "request") as direct:
                    with patch.object(custom_request.shutil, "which", return_value=None if mode == "missing-xray" else "/fake/xray"):
                        with patch.object(custom_request, "_servers", side_effect=ValueError("private subscription")):
                            with self.assertLogs(custom_request.logger, level="ERROR") as logs:
                                self.assertIsNone(custom_request.request(
                                    "https://example.com", subscription_url="https://private.example/secret",
                                    stream=mode == "unsupported-stream",
                                ))
                    direct.assert_not_called()
                self.assertNotIn("private", " ".join(logs.output))

    def test_invalid_subscription_entry_does_not_discard_valid_servers(self):
        result = response()
        result._content = (
            b"vless://bad\n"
            b"vless://example-id@vpn.example:443?security=reality&pbk=example-key&type=tcp"
        )
        session = Mock()
        session.get.return_value = result
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        with patch.object(custom_request.requests, "Session", return_value=session):
            with self.assertLogs(custom_request.logger, level="WARNING"):
                servers = custom_request._servers("https://subscription.example/secret")
        self.assertEqual(len(servers), 1)
        self.assertFalse(session.trust_env)

    def test_xray_cleanup_after_failed_request(self):
        process = Mock()
        process.stdin = io.BytesIO()
        process.poll.return_value = None
        with patch.object(custom_request.subprocess, "Popen", return_value=process):
            with patch.object(custom_request.socket, "create_connection"):
                with self.assertRaisesRegex(RuntimeError, "request failed"):
                    with custom_request._proxy({}, "/fake/xray"):
                        raise RuntimeError("request failed")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        self.assertTrue(process.stdin.closed)


class SiteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", hide_parameters=True)
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)()
        self.site = Novgo(self.session)
        self.session.commit()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.session.close)
        self.url = "https://novgo.net/book/example/"

    def saved_numbers(self):
        return list(self.session.scalars(select(Chapter.number).order_by(Chapter.number)))

    def test_bad_page_is_skipped_and_gap_is_retried(self):
        with patch("sites.novgo.request_soup", side_effect=[
            book_page(), chapter_page(1, 2), None, chapter_page(5, 6),
        ]):
            with self.assertLogs("sites.novgo", level="WARNING"):
                self.assertFalse(self.site.process_book(self.url))
        self.session.commit()
        self.assertEqual(self.saved_numbers(), [1, 2, 5, 6])
        with patch("sites.novgo.request_soup", side_effect=[
            book_page(), chapter_page(3, 4), chapter_page(5, 6),
        ]):
            self.assertTrue(self.site.process_book(self.url))
        self.session.commit()
        self.assertEqual(self.saved_numbers(), [1, 2, 3, 4, 5, 6])

    def test_failed_page_database_write_preserves_other_pages(self):
        original_add = self.session.add

        def add_with_failure(item):
            original_add(item)
            if isinstance(item, Chapter) and item.number == 3:
                original_add(Chapter(number=3, name="Duplicate", url=item.url, book=item.book))

        with patch.object(self.session, "add", side_effect=add_with_failure):
            with patch("sites.novgo.request_soup", side_effect=[
                book_page(), chapter_page(1, 2), chapter_page(3, 4), chapter_page(5, 6),
            ]):
                with self.assertLogs("sites.novgo", level="ERROR"):
                    self.assertFalse(self.site.process_book(self.url))
        self.session.commit()
        self.assertEqual(self.saved_numbers(), [1, 2, 5, 6])

    def test_bad_book_rolls_back_and_next_book_can_succeed(self):
        with patch("sites.novgo.request_soup", side_effect=RuntimeError("unexpected parser error")):
            with self.assertLogs("sites.novgo", level="ERROR"):
                self.assertFalse(self.site.process_book(self.url))
        with patch("sites.novgo.request_soup", side_effect=[book_page(2), chapter_page(1, 2)]):
            self.assertTrue(self.site.process_book(self.url))
        self.session.commit()
        self.assertEqual(self.saved_numbers(), [1, 2])

    def test_missing_or_invalid_metadata_is_not_saved(self):
        for page in (soup(""), book_page(size=0), book_page(total=-1)):
            with self.subTest(page=str(page)):
                with patch("sites.novgo.request_soup", return_value=page):
                    with self.assertLogs("sites.novgo", level="ERROR"):
                        self.assertFalse(self.site.process_book(self.url))
        self.assertIsNone(self.session.scalar(select(Book)))

    def test_missing_chapter_list_does_not_stop_later_pages(self):
        with patch("sites.novgo.request_soup", side_effect=[
            book_page(4), soup(""), chapter_page(3, 4),
        ]):
            with self.assertLogs("sites.novgo", level="ERROR"):
                self.assertFalse(self.site.process_book(self.url))
        self.session.commit()
        self.assertEqual(self.saved_numbers(), [3, 4])

    def test_invalid_link_keeps_numbering_of_later_chapters(self):
        page = soup('<ul id="idData" class="ul-list5"><a>Broken</a><a href="/chapter-2">Two</a></ul>')
        with patch("sites.novgo.request_soup", side_effect=[book_page(2), page]):
            with self.assertLogs("sites.novgo", level="WARNING"):
                self.assertFalse(self.site.process_book(self.url))
        self.assertEqual(self.saved_numbers(), [2])

    def test_redirected_book_cover_uses_final_url(self):
        page = book_page(0)
        page.source_url = "https://redirect.example/books/example/"
        with patch("sites.novgo.request_soup", return_value=page):
            self.assertTrue(self.site.process_book(self.url))
        self.assertEqual(self.session.scalar(select(Book)).cover_url,
                         "https://redirect.example/books/example/cover.jpg")

    def test_missing_lists_and_failed_requests_return_empty_results(self):
        for page in (None, soup("")):
            with patch("sites.novgo.request_soup", return_value=page):
                with patch("sites.novgo.logger"):
                    self.assertEqual(self.site.new(), [])
                    self.assertEqual(self.site.search("Example"), [])

    def test_missing_chapter_returns_false(self):
        with self.assertLogs("sites.novgo", level="WARNING"):
            self.assertFalse(self.site.process_chapter("https://novgo.net/missing"))

    def test_chapter_io_failure_preserves_existing_cache_and_session(self):
        with patch("sites.novgo.request_soup", side_effect=[book_page(2), chapter_page(1, 2)]):
            self.assertTrue(self.site.process_book(self.url))
        self.session.commit()
        chapter_url = "https://novgo.net/chapter-1"
        page = soup('<div class="chapter-text">One</div><div id="chapter-content" class="chapter-c">Content</div>')
        with tempfile.TemporaryDirectory() as directory:
            with patch("sites.novgo.__file__", str(Path(directory) / "sites" / "novgo.py")):
                with patch("sites.novgo.request_soup", return_value=page):
                    self.assertTrue(self.site.process_chapter(chapter_url))
                    self.session.commit()
                    chapter = self.session.scalar(select(Chapter).where(Chapter.url == chapter_url))
                    cache_path = Path(directory) / chapter.file_path
                    original_content = cache_path.read_text()
                    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
                        with self.assertLogs("sites.novgo", level="ERROR"):
                            self.assertFalse(self.site.process_chapter(chapter_url))
                    self.session.commit()
                    self.assertEqual(cache_path.read_text(), original_content)
                    self.assertEqual(list(Path(directory).rglob("*.tmp")), [])


class WorkerRecoveryTests(unittest.TestCase):
    def test_cycle_failure_is_logged_and_worker_runs_again(self):
        with patch.object(main, "configure_logging"):
            with patch.object(main, "run_once", side_effect=[RuntimeError("bad cycle"), KeyboardInterrupt]) as cycle:
                with patch.object(main.time, "sleep") as sleep:
                    with self.assertLogs(main.logger, level="ERROR"):
                        with self.assertRaises(KeyboardInterrupt):
                            main.main()
        self.assertEqual(cycle.call_count, 2)
        sleep.assert_called_once()

    def test_failed_book_does_not_stop_other_books_or_sites(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        processed = []

        class BrokenSite(BaseSite):
            name, url = "Broken", "https://broken.example/"

            def is_up(self):
                raise requests.ConnectionError("offline")

        class WorkingSite(BaseSite):
            name, url = "Working", "https://working.example/"

            def is_up(self):
                return True

            def new(self):
                return ["bad", "good", "good"]

            def process_book(self, url):
                if url == "bad":
                    self.session.add(Site(name="Must roll back", url="https://rollback.example/"))
                    raise ValueError("invalid book")
                processed.append(url)
                return True

        with patch.object(main, "initialize_database", return_value=True):
            with patch.object(main, "SessionLocal", factory):
                with patch.object(main, "SITE_CLASSES", (BrokenSite, WorkingSite)):
                    with self.assertLogs(main.logger, level="WARNING"):
                        main.run_once()
        self.assertEqual(processed, ["good"])
        with factory() as session:
            checks = list(session.scalars(select(SiteCheck).order_by(SiteCheck.id_site)))
            self.assertEqual([check.is_up for check in checks], [False, True])
            self.assertIsNone(session.scalar(select(Site).where(Site.name == "Must roll back")))

    def test_missing_then_fixed_database_configuration_can_retry(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(database, "load_dotenv"):
                with patch.object(database, "engine", None):
                    with patch.object(database, "DATABASE_URL", ""):
                        with patch.object(database, "SessionLocal", sessionmaker()):
                            with self.assertLogs(database.logger, level="ERROR"):
                                self.assertFalse(database.initialize_database())
                            os.environ["DATABASE_URL"] = "sqlite://"
                            self.assertTrue(database.initialize_database())
                            self.addCleanup(database.engine.dispose)
                            self.assertIsNotNone(database.SessionLocal.kw["bind"])

    def test_database_error_is_redacted_and_can_retry(self):
        with patch.dict(os.environ, {"DATABASE_URL": "private-invalid-url"}):
            with patch.object(database, "load_dotenv"):
                with patch.object(database, "engine", None):
                    with patch.object(database, "DATABASE_URL", ""):
                        with self.assertLogs(database.logger, level="ERROR") as logs:
                            self.assertFalse(database.initialize_database())
        self.assertNotIn("private-invalid-url", " ".join(logs.output))


class LoggingAndPasswordTests(unittest.TestCase):
    def test_url_secrets_are_removed_from_tracebacks(self):
        private = "https://subscription.example/private-token"
        with patch.dict(os.environ, {"VPN_SUBSCRIPTION_URL": private}):
            try:
                raise ValueError(f"{private} https://user:password@example.com/path?token=private-query#private-fragment")
            except ValueError:
                import sys
                record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Failure", (), sys.exc_info())
            rendered = RedactingFormatter("%(message)s").format(record)
        for secret in ("private-token", "user:password", "private-query", "private-fragment"):
            self.assertNotIn(secret, rendered)
        self.assertIn("ValueError", rendered)

    def test_logging_setup_is_idempotent_and_writes_file(self):
        root = logging.getLogger()
        previous_level = root.level
        self.addCleanup(root.setLevel, previous_level)
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "reader.log"
            with patch.dict(os.environ, {"LOG_FILE": str(log_path), "LOG_LEVEL": "INFO"}):
                with patch("sys.stderr", io.StringIO()):
                    configure_logging()
                    configure_logging()
                    owned = [h for h in root.handlers if getattr(h, "_reader_handler", False)]
                    try:
                        logging.getLogger("test").info("one event")
                        self.assertEqual(len(owned), 2)
                        self.assertEqual(log_path.read_text().count("one event"), 1)
                    finally:
                        for handler in owned:
                            root.removeHandler(handler)
                            handler.close()

    def test_unwritable_log_path_keeps_console_logging(self):
        root = logging.getLogger()
        previous_level = root.level
        with patch.dict(os.environ, {"LOG_FILE": "unwritable/reader.log", "LOG_LEVEL": "invalid"}):
            with patch("utils.logging_config.Path.mkdir", side_effect=PermissionError("denied")):
                with patch("sys.stderr", io.StringIO()) as console:
                    try:
                        configure_logging()
                        self.assertIn("continuing with console", console.getvalue())
                        self.assertIn("Invalid LOG_LEVEL", console.getvalue())
                    finally:
                        for handler in root.handlers[:]:
                            if getattr(handler, "_reader_handler", False):
                                root.removeHandler(handler)
                                handler.close()
                        root.setLevel(previous_level)

    def test_invalid_hash_and_mismatch_fail_closed(self):
        hashed = password.hash_password("example password")
        self.assertTrue(password.verify_password(hashed, "example password"))
        self.assertFalse(password.verify_password(hashed, "wrong password"))
        with self.assertLogs(password.logger, level="ERROR"):
            self.assertFalse(password.verify_password("invalid", "example password"))

    def test_hashing_failure_returns_none(self):
        hasher = Mock()
        hasher.hash.side_effect = password.HashingError("failure")
        with patch.object(password, "hasher", hasher):
            with self.assertLogs(password.logger, level="ERROR"):
                self.assertIsNone(password.hash_password("example password"))


if __name__ == "__main__":
    unittest.main()
