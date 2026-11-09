import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from sites.base_site import SourceSearchUnavailable
from sites.manhuazhan import Manhuazhan
from sites.novgo import Novgo
from sites.senkuro import Senkuro


class SourceSearchTests(unittest.TestCase):
    def test_novgo_extracts_only_valid_title_links(self):
        page = BeautifulSoup("""
          <title>Search: Test - NOVGO.NET</title>
          <div class="fwn-rank-list">
            <h3 class="tit"><a href="/a-title.html?q=tracking">A title</a></h3>
            <h3 class="tit"><a href="https://other.invalid/title.html">Outside</a></h3>
          </div>
        """, "lxml")
        with patch("sites.novgo.request_soup", return_value=page):
            results = Novgo.search("Test")
        self.assertEqual([(item.title, item.url) for item in results],
                         [("A title", "https://novgo.net/a-title.html")])

    def test_manhuazhan_posts_native_search_and_identifies_a_gate(self):
        page = BeautifulSoup('<title>搜索结果</title><main class="search-results">'
                             '<a href="/comic/42" title="测试漫画"></a></main>', "lxml")
        page.source_url = Manhuazhan.url + "search"
        gate = BeautifulSoup("<title>安全验证 - 漫画站</title>", "lxml")
        with patch("sites.manhuazhan.request_soup", side_effect=[page, gate]) as fetch:
            self.assertEqual(Manhuazhan.search("测试")[0].url, Manhuazhan.url + "comic/42")
            self.assertEqual(fetch.call_args.kwargs["data"], {"key": "测试"})
            with self.assertRaises(SourceSearchUnavailable):
                Manhuazhan.search("测试")

    def test_senkuro_uses_title_search_and_prefers_russian_name(self):
        data = {"mangaTachiyomiSearch": {"mangas": [{
            "slug": "example-title", "originalName": {"content": "Original"},
            "titles": [{"lang": "RU", "content": "Пример"}],
        }]}}
        with patch.object(Senkuro, "_query", return_value=data) as query:
            self.assertEqual(Senkuro.search("Пример")[0].title, "Пример")
            self.assertEqual(query.call_args.kwargs["query"], "Пример")
