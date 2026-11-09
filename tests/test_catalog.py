import json
import unittest
from threading import Event
from unittest.mock import patch

from requests import Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import main
from models import Base, Book, BookStatus, BookType
from sites.novgo import Novgo, _catalog_urls
from sites.senkuro import Senkuro


def response(content: bytes) -> Response:
    result = Response()
    result.status_code = 200
    result._content = content
    result._content_consumed = True
    return result


def graphql(**payload) -> Response:
    return response(json.dumps(payload).encode())


def senkuro_page(slug: str, next_cursor: str | None = None, chapters: int = 4) -> Response:
    return graphql(data={"mangas": {
        "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor},
        "edges": [{"node": {
            "slug": slug, "originalName": {"content": "Original"},
            "titles": [{"lang": "RU", "content": "Русское название"}],
            "alternativeNames": [{"content": "Alternate"}, {"content": "Original"}],
            "cover": {"original": {"url": "https://mirai.senkuro.net/example.jpeg"}},
            "chapters": chapters, "type": "MANHWA", "status": "FINISHED",
            "localizations": [{"lang": "RU", "description": [{"content": [{"text": "Описание"}]}]}],
        }}],
    }})


class CatalogTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.factory = sessionmaker(engine)

    def test_senkuro_retries_failed_cursor_then_finishes_and_updates_books(self):
        health = lambda: graphql(data={"__typename": "Query"})
        fetch = self.enterContext(patch("sites.senkuro.request", side_effect=[
            health(), senkuro_page("first", "page-two"), graphql(errors=[{"message": "temporary"}]),
            health(), senkuro_page("second"),
            health(), senkuro_page("first", "page-two", chapters=5), senkuro_page("second"),
        ]))
        self.enterContext(patch.object(main, "SessionLocal", self.factory))
        self.enterContext(patch.object(main, "CATALOG_BATCH_DELAY", 0))
        state, stop = main.SiteState(), Event()

        with self.assertLogs(main.logger, level="ERROR"):
            main.run_site_once(Senkuro, state, stop)
        self.assertEqual(state.catalog_cursor, "page-two")
        main.run_site_once(Senkuro, state, stop)
        self.assertIsNone(state.catalog_cursor)
        main.run_site_once(Senkuro, state, stop)

        self.assertEqual([call.kwargs["json"]["variables"]["after"] for call in fetch.call_args_list
                          if "after" in call.kwargs["json"]["variables"]],
                         [None, "page-two", "page-two", None, "page-two"])
        self.assertTrue(all(call.kwargs["direct"] for call in fetch.call_args_list))
        with self.factory() as session:
            first, second = list(session.scalars(select(Book).order_by(Book.url)))
            self.assertEqual(first.url, "https://senkuro.me/manga/first")
            self.assertEqual(first.title, "Русское название")
            self.assertEqual(first.description, "Описание")
            self.assertEqual(first.chapters_count, 5)
            self.assertEqual(first.type, BookType.MANHWA)
            self.assertEqual(first.status, BookStatus.COMPLETED)
            self.assertEqual({entry.title for entry in first.alt_titles}, {"Original", "Alternate"})
            self.assertEqual(second.url, "https://senkuro.me/manga/second")

    def test_novgo_follows_versioned_title_sitemap_and_moves_past_failed_title(self):
        index = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://novgo.net/sitemaps/static-current.xml</loc></sitemap>
          <sitemap><loc>https://novgo.net/sitemaps/novel-current.xml</loc></sitemap>
        </sitemapindex>"""
        urls = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://novgo.net/b.html</loc></url>
          <url><loc>https://novgo.net/a.html</loc></url>
          <url><loc>https://other.example/c.html</loc></url>
        </urlset>"""
        _catalog_urls.cache_clear()
        self.addCleanup(_catalog_urls.cache_clear)
        fetch = self.enterContext(patch("sites.novgo.request", side_effect=[response(index), response(urls)]))
        process = self.enterContext(patch.object(Novgo, "_process_book", side_effect=[False, True]))
        with self.factory.begin() as session, self.assertLogs("sites.novgo", level="WARNING"):
            cursor = Novgo(session).process_catalog()
        self.assertEqual(cursor, "https://novgo.net/a.html")
        with self.factory.begin() as session:
            self.assertIsNone(Novgo(session).process_catalog(cursor))
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         ["https://novgo.net/sitemap.xml", "https://novgo.net/sitemaps/novel-current.xml"])
        self.assertEqual([call.args[0] for call in process.call_args_list],
                         ["https://novgo.net/a.html", "https://novgo.net/b.html"])
