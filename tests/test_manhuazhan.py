"""Exercise comic imports, archives, and the local reader without network access."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

import requests
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import main
from models import Base, Book, BookStatus, Chapter
from sites.manhuazhan import Manhuazhan
from web import create_app

BOOK_URL = Manhuazhan.url + "comic/42"
CHAPTER_URL = Manhuazhan.url + "chapter/42-101.html"
PAGE_URLS = ("https://s2.bzcdn.net/002.jpg", "https://s2.bzcdn.net/001.png")
PAGE_BYTES = (b"\xff\xd8\xffjpeg fixture", b"\x89PNG\r\n\x1a\npng fixture")

# Fixed chapter payload with the two image URLs above, in reverse filename order.
PAYLOAD = (
    "Yx4hMR0yOAp9Rz0xGCIgSVdlH241E0sDeURnMRwHPBJWRGctIzIjD3lpEyE1BwIOblQ"
    "YYTUiARBQdRgrMAAdEFVlAGgaIj9PeFRrIjQDRxBRQBwzGwNHDG51Ay40LjMBeEMQLSMTOEBsfG9l"
)


def soup(html, url):
    result = BeautifulSoup(html, "lxml")
    result.source_url = url
    return result


def book_page(links):
    return soup(f"""
      <h1>示例漫画</h1>
      <div class="d-vod-pic"><img src="/cover.jpg"></div>
      <div class="d-vod-type">
        <p><span>作者：</span>作者名字</p><p><span>状态：</span><em>连载中</em></p>
        <p><span>类型：</span><a>奇幻</a></p>
      </div>
      <div class="d-des">简介</div>
      <div id="playlist"><div class="d-player-list">{links}</div></div>
    """, BOOK_URL)


def image_response(content):
    response = requests.Response()
    response.status_code = 200
    response._content = content
    response._content_consumed = True
    return response


class ManhuazhanTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.factory = sessionmaker(engine)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.html = self.enterContext(patch("sites.manhuazhan.request_soup"))
        self.images = self.enterContext(patch("sites.manhuazhan.request"))

    def import_book(self):
        self.html.return_value = book_page('<a href="/chapter/42-101.html">第一话</a>')
        with self.factory.begin() as db:
            self.assertTrue(Manhuazhan(db).process_book(BOOK_URL))
            return db.scalar(select(Chapter.id_chapter))

    def test_discovery_and_reimport_preserve_ids_and_follow_source_order(self):
        self.html.return_value = soup("""
          <div id="weekcon"><dl>
            <dd><span><a href="/chapter/42-101.html">第一话</a></span><a href="/comic/42">漫画</a></dd>
            <dd><a href="/comic/42/">重复</a><a href="https://other.example/comic/42">Other</a></dd>
          </dl></div>
        """, Manhuazhan.url)
        with self.factory.begin() as db:
            self.assertEqual(Manhuazhan(db).new(), [BOOK_URL])
        original_id = self.import_book()
        self.html.return_value = book_page(
            '<a href="/chapter/42-105.html">序章</a><a href="/chapter/42-101.html">第一话</a>'
        )
        with self.factory.begin() as db:
            self.assertTrue(Manhuazhan(db).process_book(BOOK_URL))
        with self.factory() as db:
            book = db.scalar(select(Book))
            self.assertEqual(book.chapters_count, 2)
            self.assertEqual(book.status, BookStatus.ONGOING)
            self.assertEqual(book.author.nickname, "作者名字")
            self.assertEqual([tag.name for tag in book.tags], ["奇幻"])
            self.assertEqual([(c.number, c.name) for c in book.chapters], [(1, "序章"), (2, "第一话")])
            self.assertEqual(book.chapters[1].id_chapter, original_id)

    def test_worker_defers_unavailable_book_and_imports_it_when_it_recovers(self):
        self.html.return_value = soup("Comic does not exist.", Manhuazhan.url + "err/comic")
        self.enterContext(patch.object(main, "initialize_database", return_value=True))
        self.enterContext(patch.object(main, "SessionLocal", self.factory))
        self.enterContext(patch.object(main, "SITE_CLASSES", (Manhuazhan,)))
        self.enterContext(patch.object(main, "check_vpn_pool"))
        self.enterContext(patch.object(Manhuazhan, "is_up", return_value=True))
        self.enterContext(patch.object(Manhuazhan, "new", return_value=[BOOK_URL]))
        clock = self.enterContext(patch.object(main.time, "monotonic", return_value=100))
        unavailable_until = {}

        with self.assertLogs(main.logger, level="INFO") as logs:
            main.run_once(unavailable_until)
            main.run_once(unavailable_until)
        self.assertIn("unavailable", "\n".join(logs.output))
        self.assertNotIn("Processing incomplete", "\n".join(logs.output))
        self.html.assert_called_once_with(BOOK_URL, timeout=10)
        with self.factory() as db:
            self.assertIsNone(db.scalar(select(Book)))

        self.html.return_value = book_page('<a href="/chapter/42-101.html">第一话</a>')
        clock.return_value += main.UNAVAILABLE_RETRY_SECONDS
        main.run_once(unavailable_until)
        self.assertEqual(self.html.call_count, 2)
        with self.factory() as db:
            self.assertEqual(db.scalar(select(Book.url)), BOOK_URL)

    def test_web_load_saves_and_reads_cbz_in_page_order(self):
        chapter_id = self.import_book()
        self.html.return_value = soup(f"<script>var cid=101; var DATA='{PAYLOAD}';</script>", CHAPTER_URL)
        self.images.side_effect = [image_response(PAGE_BYTES[0]), None, image_response(PAGE_BYTES[1])]
        app = create_app({"TESTING": True, "SESSION_FACTORY": self.factory, "DATA_ROOT": self.root})
        client = app.test_client()
        url = f"/chapters/{chapter_id}"
        page = BeautifulSoup(client.get(url).data, "lxml")
        token = page.select_one("[name=csrf_token]")["value"]
        self.assertEqual(client.post(url + "/load", data={"csrf_token": token}).status_code, 303)
        self.assertEqual([call.args[0] for call in self.images.call_args_list], [PAGE_URLS[0], PAGE_URLS[1], PAGE_URLS[1]])
        self.assertEqual(self.images.call_args.kwargs["headers"]["Referer"], CHAPTER_URL)
        with self.factory() as db:
            file_path = self.root / db.get(Chapter, chapter_id).file_path
            self.assertEqual(file_path.suffix, ".cbz")
            with ZipFile(file_path) as archive:
                self.assertEqual(archive.namelist(), ["00001.jpg", "00002.png"])
                self.assertEqual([archive.read(name) for name in archive.namelist()], list(PAGE_BYTES))

        reader = BeautifulSoup(client.get(url).data, "lxml")
        images = reader.select(".comic-pages img")
        self.assertEqual([img["alt"] for img in images], ["Page 1", "Page 2"])
        for image, mime, expected in zip(images, ("image/jpeg", "image/png"), PAGE_BYTES, strict=True):
            response = client.get(image["src"])
            self.assertEqual((response.status_code, response.mimetype, response.data), (200, mime, expected))
        self.assertEqual(client.get(url + "/pages/missing.jpg").status_code, 404)
        self.assertEqual(client.post(url + "/load", data={"csrf_token": token}).status_code, 303)
        self.assertEqual(self.images.call_count, 3)

    def test_failed_image_leaves_no_partial_archive_or_database_path(self):
        chapter_id = self.import_book()
        self.html.return_value = soup(f"<script>var cid=101; var DATA='{PAYLOAD}';</script>", CHAPTER_URL)
        self.images.side_effect = [image_response(PAGE_BYTES[0]), image_response(b"<html>error</html>")]
        with self.factory.begin() as db:
            with self.assertLogs("sites.manhuazhan", level="ERROR"):
                self.assertFalse(Manhuazhan(db).process_chapter(CHAPTER_URL, data_root=self.root))
            self.assertIsNone(db.get(Chapter, chapter_id).file_path)
        self.assertEqual(list(self.root.rglob("*.*")), [])
