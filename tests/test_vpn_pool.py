"""Offline routing tests: no subscription, VPN server or database is contacted."""

import os
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import Mock, patch

import requests

from utils import custom_request


class VPNPoolTests(unittest.TestCase):
    url = "https://example.com/book"
    subscription = "https://subscription.example/secret"

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        custom_request._server_pool.cache_clear()
        self.addCleanup(custom_request._server_pool.cache_clear)
        stack.enter_context(patch.dict(os.environ, {}, clear=True))
        stack.enter_context(patch.object(custom_request, "load_dotenv"))
        stack.enter_context(patch.object(custom_request.random, "shuffle"))
        self.clock = stack.enter_context(patch.object(custom_request.time, "monotonic", return_value=100))
        self.servers = [{"server": "a"}, {"server": "b"}, {"server": "c"}]
        self.load = stack.enter_context(patch.object(custom_request, "_servers", return_value=self.servers))
        self.which = stack.enter_context(patch.object(custom_request.shutil, "which", return_value="/fake/xray"))
        self.fetch = Mock()
        self.sessions = []
        self.routes = []
        self.closed_routes = []

        def session():
            result = Mock()
            result.request = self.fetch
            result.__enter__ = Mock(return_value=result)
            result.__exit__ = Mock(return_value=False)
            self.sessions.append(result)
            return result

        @contextmanager
        def proxy(server, executable):
            self.routes.append(server["server"])
            try:
                yield "http://127.0.0.1:12345"
            finally:
                self.closed_routes.append(server["server"])

        stack.enter_context(patch.object(custom_request.requests, "Session", side_effect=session))
        self.proxy = stack.enter_context(patch.object(custom_request, "_proxy", side_effect=proxy))

    def request(self, **kwargs):
        return custom_request.request(self.url, subscription_url=self.subscription, **kwargs)

    def test_failed_server_is_replaced_and_working_server_reused(self):
        result = Mock(status_code=200)
        self.fetch.side_effect = [requests.ReadTimeout("secret"), result, result]
        with self.assertLogs(custom_request.logger, level="WARNING") as logs:
            self.assertIs(self.request(timeout=7, params={"page": 2}), result)
        self.assertIs(self.request(), result)
        self.assertEqual(self.routes, ["a", "b", "b"])
        self.assertEqual(self.closed_routes, self.routes)
        self.load.assert_called_once_with(self.subscription)
        self.assertTrue(all(not session.trust_env for session in self.sessions))
        first = self.fetch.call_args_list[0]
        self.assertEqual(first.args, ("GET", self.url))
        self.assertEqual(first.kwargs["timeout"], 7)
        self.assertEqual(first.kwargs["params"], {"page": 2})
        self.assertTrue(all("proxies" in call.kwargs for call in self.fetch.call_args_list))
        self.assertNotIn("secret", " ".join(logs.output))

    def test_all_servers_tried_once_before_direct_fallback(self):
        result = Mock(status_code=200)
        self.fetch.side_effect = [requests.ConnectionError("secret")] * 3 + [result]
        with self.assertLogs(custom_request.logger, level="WARNING") as logs:
            self.assertIs(self.request(timeout=2, headers={"Accept": "text/html"}), result)
        self.assertEqual(self.routes, ["a", "b", "c"])
        self.assertEqual(self.closed_routes, self.routes)
        self.assertEqual(self.fetch.call_count, 4)
        self.fetch.assert_called_with("GET", self.url, timeout=2, headers={"Accept": "text/html"})
        self.assertTrue(all(not session.trust_env for session in self.sessions))
        self.assertIn("All 3 VPN servers failed; trying direct", " ".join(logs.output))
        self.assertNotIn("secret", " ".join(logs.output))

    def test_direct_failure_returns_none_after_exhausting_pool(self):
        self.fetch.side_effect = requests.ConnectionError("private credentials")
        with self.assertLogs(custom_request.logger, level="WARNING") as logs:
            self.assertIsNone(self.request())
        self.assertEqual(self.routes, ["a", "b", "c"])
        self.assertEqual(self.fetch.call_count, 4)
        self.assertNotIn("private", " ".join(logs.output))

    def test_xray_startup_failure_tries_next_server(self):
        result = Mock(status_code=200)
        working_proxy = self.proxy.side_effect

        def proxy(server, executable):
            if server == self.servers[0]:
                raise RuntimeError("private configuration")
            return working_proxy(server, executable)

        self.proxy.side_effect = proxy
        self.fetch.return_value = result
        with self.assertLogs(custom_request.logger, level="WARNING"):
            self.assertIs(self.request(), result)
        self.assertEqual(self.proxy.call_count, 2)
        self.assertEqual(self.routes, ["b"])
        self.fetch.assert_called_once()

    def test_unavailable_pool_uses_direct_connection(self):
        for mode in ("missing-xray", "invalid-subscription", "subscription-timeout"):
            with self.subTest(mode=mode):
                custom_request._server_pool.cache_clear()
                self.which.return_value = None if mode == "missing-xray" else "/fake/xray"
                self.load.side_effect = (requests.ReadTimeout("secret") if mode == "subscription-timeout"
                                         else ValueError("secret"))
                self.fetch.reset_mock()
                result = Mock(status_code=200)
                self.fetch.return_value = result
                with self.assertLogs(custom_request.logger, level="ERROR") as logs:
                    self.assertIs(self.request(), result)
                self.fetch.assert_called_once_with("GET", self.url, timeout=30)
                self.assertFalse(self.sessions[-1].trust_env)
                self.assertNotIn("secret", " ".join(logs.output))
        self.proxy.assert_not_called()

    def test_duplicate_servers_are_only_tried_once(self):
        self.load.return_value = [self.servers[0], dict(self.servers[0]), self.servers[1]]
        result = Mock(status_code=200)
        self.fetch.side_effect = [requests.ConnectionError(), requests.ConnectionError(), result]
        with self.assertLogs(custom_request.logger, level="WARNING"):
            self.assertIs(self.request(), result)
        self.assertEqual(self.routes, ["a", "b"])
        self.assertEqual(self.fetch.call_count, 3)

    def test_refresh_retains_working_server_and_removes_obsolete_entries(self):
        pool = custom_request._server_pool(self.subscription)
        pool.candidates(self.subscription)
        pool.succeeded(self.servers[1])
        self.clock.return_value = 100 + custom_request.POOL_REFRESH_SECONDS
        self.load.return_value = [self.servers[1], {"server": "new"}]
        self.assertEqual(pool.candidates(self.subscription), [{"server": "b"}, {"server": "new"}])
        self.assertEqual(self.load.call_count, 2)

    def test_refresh_failure_keeps_cached_servers_and_delays_refresh_retry(self):
        pool = custom_request._server_pool(self.subscription)
        pool.candidates(self.subscription)
        self.clock.return_value = 100 + custom_request.POOL_REFRESH_SECONDS
        self.load.side_effect = requests.ConnectionError("secret")
        with self.assertLogs(custom_request.logger, level="WARNING") as logs:
            self.assertEqual(pool.candidates(self.subscription), self.servers)
        self.assertEqual(pool.candidates(self.subscription), self.servers)
        self.assertEqual(self.load.call_count, 2)
        self.assertNotIn("secret", " ".join(logs.output))

    def test_subscriptions_have_independent_pools(self):
        first = custom_request._server_pool(self.subscription)
        second = custom_request._server_pool("https://other.example/secret")
        first.candidates(self.subscription)
        second.candidates("https://other.example/secret")
        first.failed(self.servers[0])
        self.assertEqual(second.servers, self.servers)
        self.assertNotEqual(first.servers, second.servers)

    def test_http_error_response_is_returned_without_rotating_servers(self):
        result = Mock(status_code=503)
        self.fetch.return_value = result
        self.assertIs(self.request(), result)
        self.assertEqual(self.routes, ["a"])
        self.fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
