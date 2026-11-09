"""Fetch pages through a VLESS server pool, with direct access as a fallback."""

import atexit
import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from threading import RLock
from urllib.parse import parse_qsl, unquote, urlsplit
from dotenv import load_dotenv

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
POOL_REFRESH_SECONDS = 300
HEALTH_TIMEOUT = 5
HEALTH_WORKERS = 8
_POOLS = {}
_POOLS_LOCK = RLock()


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


class _Tunnel:
    def __init__(self, proxy: str):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies = {"http": proxy, "https": proxy}
        self.healthy = False

    def check(self, url: str, timeout: float) -> str | None:
        response = None
        try:
            response = self.session.request("GET", url, timeout=timeout)
            response.raise_for_status()
        except (requests.RequestException, OSError) as exc:
            self.healthy = False
            self.session.close()
            return type(exc).__name__
        else:
            self.healthy = True
            return None
        finally:
            if response is not None:
                response.close()


class _ServerPool:
    """Own one Xray process and a reusable HTTP session for each VPN route."""

    def __init__(self, subscription_url: str, executable: str):
        self.subscription_url = subscription_url
        self.executable = executable
        self.servers = []
        self.tunnels = []
        self.refresh_at = 0
        self.process = None
        self.resources = ExitStack()
        self.checked = False
        self.next_tunnel = 0
        self.lock = RLock()

    def _start(self, servers: list[dict]):
        # Start the replacement before releasing the existing working process.
        with ExitStack() as resources:
            process, proxies = resources.enter_context(_proxy(servers, self.executable))
            tunnels = []
            for proxy in proxies:
                tunnel = _Tunnel(proxy)
                resources.callback(tunnel.session.close)
                tunnels.append(tunnel)
            self.resources.close()
            self.resources = resources.pop_all()
        self.process, self.tunnels, self.servers = process, tunnels, servers
        self.checked = False
        self.next_tunnel = 0
        logger.info("VPN started: %d persistent routes in one Xray process", len(tunnels))

    def _ensure_started(self):
        now = time.monotonic()
        if now >= self.refresh_at:
            self.refresh_at = now + 60  # Also back off failed initial subscriptions.
            try:
                unique = {json.dumps(server, sort_keys=True): server
                          for server in _servers(self.subscription_url)}
                servers = list(unique.values())
                if not servers:
                    raise ValueError("No VPN servers available")
                old = {json.dumps(server, sort_keys=True) for server in self.servers}
                if set(unique) != old:
                    self._start(servers)
                self.refresh_at = now + POOL_REFRESH_SECONDS
            except Exception as exc:
                if not self.servers:
                    raise
                logger.warning("VPN pool refresh failed (%s); using cached routes", type(exc).__name__)
        if not self.servers:
            raise RuntimeError("VPN subscription unavailable; waiting before retry")
        if self.process is None or self.process.poll() is not None:
            self._start(self.servers)

    def _check(self, url: str, timeout: float) -> int:
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(HEALTH_WORKERS, len(self.tunnels))) as executor:
            results = list(executor.map(lambda tunnel: tunnel.check(url, timeout), self.tunnels))
        self.checked = True
        for index, failure in enumerate(results, start=1):
            if failure:
                logger.debug("VPN route %d health check failed (%s)", index, failure)
        healthy = sum(tunnel.healthy for tunnel in self.tunnels)
        logger.info("VPN health check: %d/%d routes working (%.1fs)",
                    healthy, len(self.tunnels), time.monotonic() - started)
        return healthy

    def check(self, url: str, timeout: float) -> int:
        with self.lock:
            self._ensure_started()
            return self._check(url, timeout)

    def request(self, method: str, url: str, timeout, **kwargs):
        # Session objects are not shared between simultaneous health probes.
        # This lock also prevents a process refresh while a request is running.
        with self.lock:
            self._ensure_started()
            if not self.checked:
                origin = urlsplit(url)
                self._check(f"{origin.scheme}://{origin.netloc}/", HEALTH_TIMEOUT)
            candidates = [tunnel for tunnel in self.tunnels if tunnel.healthy]
            if candidates:
                offset = self.next_tunnel % len(candidates)
                candidates = candidates[offset:] + candidates[:offset]
                self.next_tunnel += 1
            for attempt, tunnel in enumerate(candidates, start=1):
                try:
                    return tunnel.session.request(method, url, timeout=timeout, **kwargs)
                except (requests.RequestException, OSError) as exc:
                    tunnel.healthy = False
                    tunnel.session.close()
                    logger.warning("VPN route attempt %d/%d failed (%s); disabled until next health check",
                                   attempt, len(candidates), type(exc).__name__)
            return None

    def close(self):
        with self.lock:
            self.resources.close()
            self.process = None
            self.tunnels = []
            self.checked = False


def _server_pool(subscription_url: str, executable: str) -> _ServerPool:
    with _POOLS_LOCK:
        key = (subscription_url, executable)
        if key not in _POOLS:
            _POOLS[key] = _ServerPool(subscription_url, executable)
        return _POOLS[key]


def close_vpn_pools():
    """Close reusable sessions and terminate owned Xray processes."""
    with _POOLS_LOCK:
        for pool in _POOLS.values():
            pool.close()
        _POOLS.clear()


atexit.register(close_vpn_pools)


@contextmanager
def _proxy(outbounds: list[dict], xray_path: str):
    # Reserve all ports together so two routes cannot receive the same port.
    with ExitStack() as listeners:
        ports = []
        for _ in outbounds:
            listener = listeners.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            ports.append(listener.getsockname()[1])

        config = {"log": {"loglevel": "none"}, "inbounds": [], "outbounds": [],
                  "routing": {"rules": []}}
        for index, (outbound, port) in enumerate(zip(outbounds, ports)):
            inbound_tag, outbound_tag = f"in-{index}", f"out-{index}"
            config["inbounds"].append({
                "tag": inbound_tag, "listen": "127.0.0.1", "port": port,
                "protocol": "http", "settings": {},
            })
            config["outbounds"].append({**outbound, "tag": outbound_tag})
            config["routing"]["rules"].append({
                "type": "field", "inboundTag": [inbound_tag], "outboundTag": outbound_tag,
            })
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
        pending = set(ports)
        while pending:
            if process.poll() is not None:
                raise RuntimeError("Xray failed to start; check its version and subscription")
            for port in list(pending):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        pending.remove(port)
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Xray did not start within 10 seconds") from None
            if pending:
                time.sleep(0.05)

        yield process, [f"http://127.0.0.1:{port}" for port in ports]
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
    direct: bool = False,
    timeout: float | tuple[float, float] = 30,
    **kwargs,
) -> requests.Response | None:
    """Fetch a URL through the VPN pool, then directly if every server fails.

    Set direct=True to bypass both the VPN pool and environment proxies.
    Without a subscription, use a normal requests.request call. To use a VPN,
    install Xray (`brew install xray` on macOS) and supply the subscription using
    VPN_SUBSCRIPTION_URL or subscription_url; optionally set XRAY_PATH.
    Other keyword arguments (headers, params, cookies, json, data, etc.) are
    passed to requests. With a VPN, streaming and custom proxies are not
    supported. Sessions and Xray routes stay alive until close_vpn_pools().

    Subscription servers are refreshed every five minutes. check_vpn_pool()
    tests every route at the start of each worker cycle; standalone use probes
    on first access too. Requests rotate through healthy routes, disabling any
    failed route until the next check. If no working routes remain (or VPN
    setup fails), log an error and try the local connection with environment
    proxies disabled. Return None if that request also fails. HTTP error
    responses are returned for the caller to inspect. Timeouts apply to each
    attempt, rather than the entire operation.

    Example::

        response = request("https://example.com", timeout=15)
        if response is not None:
            logger.info("HTTP status: %s", response.status_code)
    """
    try:
        if direct:
            if "proxies" in kwargs:
                raise ValueError("Direct requests cannot specify a proxy")
            return _request_direct(method, url, timeout, **kwargs)
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
        response = _server_pool(subscription_url, executable).request(method, url, timeout, **kwargs)
    except Exception as exc:
        logger.error("VPN pool unavailable (%s); trying direct connection", type(exc).__name__)
    else:
        if response is not None:
            return response
        logger.error("No working VPN routes remain; trying direct connection")

    return _request_direct(method, url, timeout, **kwargs)


def _request_direct(method, url, timeout, **kwargs):
    with requests.Session() as session:
        # Do not reuse HTTP_PROXY/HTTPS_PROXY/ALL_PROXY.
        session.trust_env = False
        return session.request(method, url, timeout=timeout, **kwargs)


def check_vpn_pool(
    probe_url: str,
    *,
    subscription_url: str | None = None,
    xray_path: str | None = None,
    timeout: float = HEALTH_TIMEOUT,
) -> int:
    """Probe every tunnel through HTTPS; return the number of usable routes.

    Call once per worker cycle, including when no routes worked last time.
    Failed routes can recover here. Health probes never fall back to direct.
    """
    try:
        load_dotenv()
        subscription_url = subscription_url or os.environ.get("VPN_SUBSCRIPTION_URL")
        if not subscription_url:
            return 0
        executable = shutil.which(xray_path or os.environ.get("XRAY_PATH", "xray"))
        if not executable:
            raise RuntimeError("Xray not found; install it or set XRAY_PATH")
        return _server_pool(subscription_url, executable).check(probe_url, timeout)
    except Exception as exc:
        logger.error("VPN health check unavailable (%s)", type(exc).__name__)
        return 0


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
