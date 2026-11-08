"""Offline lifecycle/routing tests; never contact a real subscription or VPN."""

import io
import json
import os
import subprocess
import threading
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests

from utils import custom_request


def response(status=200):
    result = requests.Response()
    result.status_code = status
    result._content = b"OK"
    result._content_consumed = True
    result.close = Mock()
    return result


class VPNPoolTests(unittest.TestCase):
    url = "https://example.com/book"
    probe_url = "https://probe.example/health"
    subscription = "https://subscription.example/secret"

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        custom_request.close_vpn_pools()
        stack.enter_context(patch.dict(os.environ, {}, clear=True))
        stack.enter_context(patch.object(custom_request, "load_dotenv"))
        self.clock = stack.enter_context(patch.object(custom_request.time, "monotonic", return_value=100))
        self.servers = [{"server": "a"}, {"server": "b"}, {"server": "c"}]
        self.load = stack.enter_context(patch.object(custom_request, "_servers", return_value=self.servers))
        self.which = stack.enter_context(patch.object(custom_request.shutil, "which", return_value="/fake/xray"))
        self.sessions, self.started, self.closed, self.processes = [], [], [], []
        self.proxy_names, self.events, self.responses = {}, [], []
        self.fail_health, self.fail_requests = set(), set()
        self.fail_direct, self.request_status = False, 200

        def session():
            result = Mock(proxies={})

            def fetch(method, url, **kwargs):
                route = self.proxy_names.get(result.proxies.get("https"), "direct")
                health = url in (self.probe_url, "https://example.com/")
                self.events.append((route, method, url, kwargs))
                if route in (self.fail_health if health else self.fail_requests) or (route == "direct" and self.fail_direct):
                    raise requests.ReadTimeout("secret credentials")
                item = response(200 if health else self.request_status)
                self.responses.append((route, health, item))
                return item

            result.request.side_effect = fetch
            result.__enter__ = Mock(return_value=result)
            result.__exit__ = Mock(side_effect=lambda *args: result.close())
            self.sessions.append(result)
            return result

        @contextmanager
        def proxy(servers, executable):
            generation = len(self.started)
            self.started.append(list(servers))
            process = Mock()
            process.poll.return_value = None
            self.processes.append(process)
            proxies = []
            for index, server in enumerate(servers):
                proxy_url = f"http://127.0.0.1:{12000 + generation * 100 + index}"
                self.proxy_names[proxy_url] = server["server"]
                proxies.append(proxy_url)
            try:
                yield process, proxies
            finally:
                self.closed.append(generation)

        stack.enter_context(patch.object(custom_request.requests, "Session", side_effect=session))
        self.proxy = stack.enter_context(patch.object(custom_request, "_proxy", side_effect=proxy))
        # Close resources while their owning mocks are still installed.
        stack.callback(custom_request.close_vpn_pools)

    def check(self):
        return custom_request.check_vpn_pool(self.probe_url, subscription_url=self.subscription)

    def request(self, **kwargs):
        return custom_request.request(self.url, subscription_url=self.subscription, **kwargs)

    def page_routes(self):
        return [route for route, _, url, _ in self.events if url == self.url]

    def test_process_and_sessions_survive_requests_and_cycle_checks(self):
        self.assertEqual(self.check(), 3)
        original = list(self.sessions)
        for _ in range(5):
            self.assertEqual(self.request().status_code, 200)
        self.assertEqual(self.page_routes(), ["a", "b", "c", "a", "b"])
        self.assertEqual(self.check(), 3)
        self.assertEqual(self.sessions, original)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.closed, [])
        self.load.assert_called_once_with(self.subscription)
        for session in self.sessions:
            self.assertFalse(session.trust_env)
            session.close.assert_not_called()
        for _, health, item in self.responses:
            if health:
                item.close.assert_called_once()

    def test_bad_health_route_is_excluded_from_requests(self):
        self.fail_health.add("b")
        self.assertEqual(self.check(), 2)
        for _ in range(4):
            self.request()
        self.assertEqual(self.page_routes(), ["a", "c", "a", "c"])

    def test_request_failure_disables_route_until_next_cycle_then_recovers(self):
        self.check()
        self.fail_requests.add("a")
        with self.assertLogs(custom_request.logger, level="WARNING") as logs:
            self.assertEqual(self.request(timeout=7, params={"page": 2}).status_code, 200)
        self.assertEqual(self.page_routes(), ["a", "b"])
        for _ in range(3):
            self.request()
        self.assertNotIn("a", self.page_routes()[2:])
        self.fail_requests.clear()
        self.assertEqual(self.check(), 3)
        for _ in range(3):
            self.request()
        self.assertIn("a", self.page_routes()[-3:])
        self.assertEqual(len(self.started), 1)
        self.assertNotIn("secret", " ".join(logs.output))
        first = next(event for event in self.events if event[2] == self.url)
        self.assertEqual(first[3], {"timeout": 7, "params": {"page": 2}})

    def test_exhausted_routes_fall_back_without_repeating_dead_routes(self):
        self.check()
        self.fail_requests.update(("a", "b", "c"))
        with self.assertLogs(custom_request.logger, level="WARNING") as logs:
            self.assertEqual(self.request().status_code, 200)
            self.assertEqual(self.request().status_code, 200)
        self.assertEqual(self.page_routes(), ["a", "b", "c", "direct", "direct"])
        self.assertNotIn("secret", " ".join(logs.output))
        self.assertIn("No working VPN routes remain; trying direct", " ".join(logs.output))
        self.assertTrue(all(not session.trust_env for session in self.sessions))

    def test_all_down_routes_are_rechecked_without_restarting_process(self):
        self.fail_health.update(("a", "b", "c"))
        self.assertEqual(self.check(), 0)
        with self.assertLogs(custom_request.logger, level="ERROR"):
            self.request()
            self.request()
        self.assertEqual(len(self.events), 5)  # Three probes, two direct requests.
        self.fail_health.clear()
        self.assertEqual(self.check(), 3)
        self.request()
        self.assertEqual(self.page_routes(), ["direct", "direct", "a"])
        self.assertEqual(len(self.started), 1)

    def test_direct_failure_returns_none_with_redacted_logging(self):
        self.fail_health.update(("a", "b", "c"))
        self.check()
        self.fail_direct = True
        with self.assertLogs(custom_request.logger, level="ERROR") as logs:
            self.assertIsNone(self.request())
        self.assertNotIn("secret", " ".join(logs.output))

    def test_standalone_requests_initialize_and_probe_once(self):
        self.assertEqual(self.request().status_code, 200)
        self.assertEqual(self.request().status_code, 200)
        self.assertEqual(len(self.events), 5)
        self.assertEqual(self.page_routes(), ["a", "b"])
        self.assertEqual(len(self.started), 1)

    def test_unchanged_subscription_preserves_process_and_sessions(self):
        self.check()
        original = list(self.sessions)
        self.clock.return_value += custom_request.POOL_REFRESH_SECONDS
        self.load.return_value = list(reversed(self.servers))
        self.check()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.sessions, original)
        self.assertEqual(self.load.call_count, 2)

    def test_changed_subscription_replaces_process_and_closes_old_sessions(self):
        self.check()
        original = list(self.sessions)
        self.clock.return_value += custom_request.POOL_REFRESH_SECONDS
        self.load.return_value = [{"server": "b"}, {"server": "new"}]
        self.assertEqual(self.check(), 2)
        self.assertEqual(len(self.started), 2)
        self.assertEqual(self.closed, [0])
        for session in original:
            session.close.assert_called_once()
        self.request()
        self.request()
        self.assertEqual(self.page_routes(), ["b", "new"])

    def test_failed_refresh_retains_routes_and_backs_off(self):
        self.check()
        self.clock.return_value += custom_request.POOL_REFRESH_SECONDS
        self.load.side_effect = requests.ConnectionError("secret")
        with self.assertLogs(custom_request.logger, level="WARNING") as logs:
            self.assertEqual(self.check(), 3)
        self.assertEqual(self.check(), 3)
        self.assertEqual(self.load.call_count, 2)
        self.assertEqual(len(self.started), 1)
        self.assertNotIn("secret", " ".join(logs.output))

    def test_invalid_replacement_retains_working_process(self):
        self.check()
        self.clock.return_value += custom_request.POOL_REFRESH_SECONDS
        self.load.return_value = [{"server": "bad"}]
        self.proxy.side_effect = RuntimeError("secret configuration")
        with self.assertLogs(custom_request.logger, level="WARNING"):
            self.assertEqual(self.check(), 3)
        self.assertEqual(self.closed, [])
        self.assertEqual(self.request().status_code, 200)

    def test_dead_process_restarts_and_is_probed_before_reuse(self):
        self.check()
        self.processes[0].poll.return_value = 1
        self.assertEqual(self.request().status_code, 200)
        self.assertEqual(len(self.started), 2)
        self.assertEqual(self.closed, [0])
        self.assertEqual(len(self.events), 7)

    def test_initial_subscription_failure_uses_direct_and_backs_off(self):
        self.load.side_effect = ValueError("secret subscription")
        with self.assertLogs(custom_request.logger, level="ERROR") as logs:
            self.assertEqual(self.request().status_code, 200)
            self.assertEqual(self.request().status_code, 200)
        self.load.assert_called_once()
        self.proxy.assert_not_called()
        self.assertEqual(self.page_routes(), ["direct", "direct"])
        self.assertNotIn("secret", " ".join(logs.output))

    def test_missing_xray_uses_direct_and_health_check_returns_zero(self):
        self.which.return_value = None
        with self.assertLogs(custom_request.logger, level="ERROR"):
            self.assertEqual(self.check(), 0)
            self.assertEqual(self.request().status_code, 200)
        self.assertEqual(self.page_routes(), ["direct"])
        self.load.assert_not_called()
        self.proxy.assert_not_called()

    def test_no_subscription_health_check_is_noop(self):
        self.assertEqual(custom_request.check_vpn_pool(self.probe_url), 0)
        self.which.assert_not_called()
        self.assertEqual(self.sessions, [])

    def test_duplicate_outbounds_have_one_route(self):
        self.load.return_value = [self.servers[0], dict(self.servers[0]), self.servers[1]]
        self.assertEqual(self.check(), 2)
        self.assertEqual(len(self.started[0]), 2)

    def test_subscription_and_executable_select_independent_pools(self):
        first = custom_request._server_pool(self.subscription, "/fake/xray")
        self.assertIs(first, custom_request._server_pool(self.subscription, "/fake/xray"))
        self.assertIsNot(first, custom_request._server_pool("other subscription", "/fake/xray"))
        self.assertIsNot(first, custom_request._server_pool(self.subscription, "/other/xray"))

    def test_shutdown_closes_sessions_and_process_once(self):
        self.check()
        custom_request.close_vpn_pools()
        custom_request.close_vpn_pools()
        self.assertEqual(self.closed, [0])
        self.assertFalse(custom_request._POOLS)
        for session in self.sessions:
            session.close.assert_called_once()

    def test_http_error_response_is_returned_without_retrying(self):
        self.check()
        self.request_status = 503
        self.assertEqual(self.request().status_code, 503)
        self.assertEqual(self.page_routes(), ["a"])

    def test_health_probes_run_concurrently_with_worker_limit(self):
        pool = custom_request._server_pool(self.subscription, "/fake/xray")
        pool._ensure_started()
        gate, lock = threading.Event(), threading.Lock()
        active = peak = 0

        def fetch(method, url, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    gate.set()
            self.assertTrue(gate.wait(timeout=1), "health probes were serialized")
            with lock:
                active -= 1
            return response()

        for session in self.sessions:
            session.request.side_effect = fetch
        with patch.object(custom_request, "HEALTH_WORKERS", 2):
            self.assertEqual(self.check(), 3)
        self.assertEqual(peak, 2)


class XrayProcessTests(unittest.TestCase):
    class ConfigInput(io.BytesIO):
        def close(self):
            if not self.closed:
                self.config = json.loads(self.getvalue())
            super().close()

    def test_local_ports_route_to_separate_outbounds_and_close_on_exit(self):
        process = Mock()
        process.stdin = self.ConfigInput()
        process.poll.return_value = None
        outbounds = [{"protocol": "vless", "settings": {"id": "first"}},
                     {"protocol": "vless", "settings": {"id": "second"}}]
        with patch.object(custom_request.subprocess, "Popen", return_value=process) as start:
            with patch.object(custom_request.socket, "create_connection"):
                with custom_request._proxy(outbounds, "/fake/xray") as (running, proxies):
                    self.assertIs(running, process)
                    process.terminate.assert_not_called()
                    self.assertEqual(len(set(proxies)), 2)
                    config = process.stdin.config
                    for index, proxy in enumerate(proxies):
                        self.assertEqual(config["inbounds"][index]["listen"], "127.0.0.1")
                        self.assertEqual(config["inbounds"][index]["port"], urlsplit(proxy).port)
                        rule = config["routing"]["rules"][index]
                        self.assertEqual(rule["inboundTag"], [config["inbounds"][index]["tag"]])
                        self.assertEqual(rule["outboundTag"], config["outbounds"][index]["tag"])
                        self.assertEqual(config["outbounds"][index]["settings"], outbounds[index]["settings"])
        start.assert_called_once()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        self.assertTrue(all("tag" not in outbound for outbound in outbounds))

    def test_stuck_process_is_killed_during_shutdown(self):
        process = Mock()
        process.stdin = self.ConfigInput()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("xray", 5), 0]
        with patch.object(custom_request.subprocess, "Popen", return_value=process):
            with patch.object(custom_request.socket, "create_connection"):
                with custom_request._proxy([{}], "/fake/xray"):
                    pass
        process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
