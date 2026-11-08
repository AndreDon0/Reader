"""Fetch pages through a VLESS server pool, with direct access as a fallback."""

import base64
import json
import logging
import os
import random
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from functools import lru_cache
from urllib.parse import parse_qsl, unquote, urlsplit
from dotenv import load_dotenv

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
POOL_REFRESH_SECONDS = 300


def _outbound(uri: str) -> dict:
    """Convert a VLESS TCP/gRPC + REALITY share link to an Xray outbound."""
    try:
        server = urlsplit(uri)
        address, port, user_id = server.hostname, server.port, server.username
        if server.scheme != "vless" or not all((address, port, user_id)):
            raise ValueError
    except ValueError:
        raise ValueError("Invalid VLESS server entry in subscription") from None

    options = dict(parse_qsl(server.query))
    network = options.get("type", "tcp")
    if network not in {"tcp", "raw", "grpc"}:
        raise ValueError("Only TCP and gRPC VLESS transports are supported")
    if options.get("security") != "reality" or not options.get("pbk"):
        raise ValueError("Expected a VLESS + REALITY server with a public key")
    if options.get("headerType", "none") != "none":
        raise ValueError("TCP header obfuscation is not supported")

    stream = {
        "network": network,
        "security": "reality",
        "realitySettings": {
            "serverName": options.get("sni", address),
            "fingerprint": options.get("fp", "chrome"),
            "publicKey": options["pbk"],
            "shortId": options.get("sid", ""),
        },
    }
    if network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": options.get("serviceName", ""),
            "multiMode": options.get("mode") == "multi",
        }

    return {
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": address,
                "port": port,
                "users": [{
                    "id": unquote(user_id),
                    "encryption": options.get("encryption", "none"),
                    "flow": options.get("flow", ""),
                }],
            }],
        },
        "streamSettings": stream,
    }


def _servers(subscription_url: str) -> list[dict]:
    # Keep subscription credentials out of exception messages and stored files.
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(subscription_url, timeout=20)
            response.raise_for_status()
            content = response.text.strip()
    except requests.RequestException:
        raise requests.ConnectionError("Could not fetch VPN subscription") from None

    if not content.startswith("vless://"):
        try:
            content = "".join(content.split())
            content = base64.b64decode(
                content + "=" * (-len(content) % 4), altchars=b"-_"
            ).decode("utf-8")
        except (ValueError, UnicodeError):
            raise ValueError("Subscription is not a VLESS URI list") from None

    servers = []
    for index, line in enumerate(content.splitlines(), start=1):
        if not line.strip().startswith("vless://"):
            continue
        try:
            servers.append(_outbound(line.strip()))
        except ValueError:
            logger.warning("Skipping invalid or unsupported VPN entry %d", index)
    if not servers:
        raise ValueError("No VLESS servers found in subscription")
    return servers


class _ServerPool:
    """Keep working servers first and subscription data in memory only."""

    def __init__(self):
        self.servers = []
        self.refresh_at = 0

    def candidates(self, subscription_url: str) -> list[dict]:
        now = time.monotonic()
        if now >= self.refresh_at or not self.servers:
            try:
                # Different subscription labels can describe the same outbound.
                unique = {json.dumps(server, sort_keys=True): server
                          for server in _servers(subscription_url)}
                servers = list(unique.values())
                if not servers:
                    raise ValueError("No VPN servers available")
                random.shuffle(servers)
                # Keep the previous working order across subscription refreshes.
                previous = [server for server in self.servers if server in servers]
                self.servers = previous + [server for server in servers if server not in previous]
                self.refresh_at = now + POOL_REFRESH_SECONDS
                logger.info("VPN pool loaded: %d servers", len(self.servers))
            except Exception as exc:
                if not self.servers:
                    raise
                self.refresh_at = now + 60
                logger.warning("VPN pool refresh failed (%s); using cached servers", type(exc).__name__)
        return list(self.servers)

    def failed(self, server: dict):
        self.servers.remove(server)
        self.servers.append(server)

    def succeeded(self, server: dict):
        self.servers.remove(server)
        self.servers.insert(0, server)


@lru_cache(maxsize=8)
def _server_pool(subscription_url: str) -> _ServerPool:
    return _ServerPool()


@contextmanager
def _proxy(outbound: dict, xray_path: str):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    config = {
        "log": {"loglevel": "none"},
        "inbounds": [{
            "listen": "127.0.0.1", "port": port,
            "protocol": "http", "settings": {},
        }],
        "outbounds": [outbound],
    }
    process = subprocess.Popen(
        [xray_path, "run", "-config", "stdin:", "-format", "json"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Pass credentials through stdin instead of a config file or CLI argument.
        try:
            process.stdin.write(json.dumps(config).encode())
            process.stdin.close()
        except BrokenPipeError:
            raise RuntimeError("Xray exited while loading its configuration") from None

        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                raise RuntimeError("Xray failed to start; check its version and subscription")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Xray did not start within 10 seconds") from None
                time.sleep(0.05)

        yield f"http://127.0.0.1:{port}"
    finally:
        if not process.stdin.closed:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def request(
    url: str,
    method: str = "GET",
    *,
    subscription_url: str | None = None,
    xray_path: str | None = None,
    timeout: float | tuple[float, float] = 30,
    **kwargs,
) -> requests.Response | None:
    """Fetch a URL through the VPN pool, then directly if every server fails.

    Without a subscription, use a normal requests.request call. To use a VPN,
    install Xray (`brew install xray` on macOS) and supply the subscription using
    VPN_SUBSCRIPTION_URL or subscription_url; optionally set XRAY_PATH.
    Other keyword arguments (headers, params, cookies, json, data, etc.) are
    passed to requests. With a VPN, streaming and custom proxies are not
    supported because the local proxy is stopped before this function returns.

    Subscription servers are cached for five minutes. Each VPN server is tried
    at most once per call; the last working server is preferred on later calls.
    If all servers fail (or VPN setup fails), log an error and try the local
    connection with environment proxies disabled. Return None only if that
    request also fails. HTTP error responses are returned for the caller to
    inspect; request_soup also checks their status before parsing. The timeout
    applies to each attempt, so exhausting the pool can take longer than it.

    Example::

        response = request("https://example.com", timeout=15)
        if response is not None:
            logger.info("HTTP status: %s", response.status_code)
    """
    try:
        load_dotenv()
        subscription_url = subscription_url or os.environ.get("VPN_SUBSCRIPTION_URL")
        if not subscription_url:
            response = requests.request(method, url, timeout=timeout, **kwargs)
        else:
            if kwargs.get("stream") or "proxies" in kwargs:
                logger.error("VPN requests do not support streaming or custom proxies")
                return None
            response = _request_with_vpn(
                url, method, subscription_url, xray_path, timeout, **kwargs,
            )
        logger.debug("HTTP request finished with status %s", response.status_code)
        return response
    except Exception as exc:
        # Third-party errors may include an explicit subscription URL or UUID.
        # Log the type only, never subscription material or request payloads.
        logger.error("Request failed (%s)", type(exc).__name__)
        return None


def _request_with_vpn(url, method, subscription_url, xray_path, timeout, **kwargs):
    try:
        executable = shutil.which(xray_path or os.environ.get("XRAY_PATH", "xray"))
        if not executable:
            raise RuntimeError("Xray not found; install it or set XRAY_PATH")
        pool = _server_pool(subscription_url)
        candidates = pool.candidates(subscription_url)
    except Exception as exc:
        logger.error("VPN pool unavailable (%s); trying direct connection", type(exc).__name__)
    else:
        for attempt, outbound in enumerate(candidates, start=1):
            try:
                with _proxy(outbound, executable) as proxy, requests.Session() as session:
                    session.trust_env = False
                    response = session.request(
                        method, url, timeout=timeout,
                        proxies={"http": proxy, "https": proxy}, **kwargs,
                    )
            except (requests.RequestException, OSError, RuntimeError, subprocess.SubprocessError) as exc:
                pool.failed(outbound)
                logger.warning("VPN server attempt %d/%d failed (%s); trying next route",
                               attempt, len(candidates), type(exc).__name__)
            else:
                pool.succeeded(outbound)
                return response
        logger.error("All %d VPN servers failed; trying direct connection", len(candidates))

    with requests.Session() as session:
        # A direct fallback must not reuse HTTP_PROXY/HTTPS_PROXY/ALL_PROXY.
        session.trust_env = False
        return session.request(method, url, timeout=timeout, **kwargs)


def request_soup(url: str, *args, **kwargs) -> BeautifulSoup | None:
    """Return parsed HTML, or None after logging a request/status/parse failure."""
    response = request(url, *args, **kwargs)
    if response is None:
        return None
    try:
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "lxml")
        # Retain the final URL so relative covers/links follow redirects.
        soup.source_url = response.url
        return soup
    except requests.HTTPError:
        logger.warning("Cannot parse page: HTTP %s", response.status_code)
        return None
    except Exception as exc:
        logger.error("Cannot parse page (%s)", type(exc).__name__)
        return None
    finally:
        response.close()
