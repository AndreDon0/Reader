"""Fetch pages directly or through a random VLESS server using Xray."""

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
from urllib.parse import parse_qsl, unquote, urlsplit
from dotenv import load_dotenv

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


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
    """Fetch a URL directly or through a randomly chosen subscription server.

    Without a subscription, use a normal requests.request call. To use a VPN,
    install Xray (`brew install xray` on macOS) and supply the subscription using
    VPN_SUBSCRIPTION_URL or subscription_url; optionally set XRAY_PATH.
    Other keyword arguments (headers, params, cookies, json, data, etc.) are
    passed to requests. With a VPN, streaming and custom proxies are not
    supported because the local proxy is stopped before this function returns.

    VPN calls choose independently, so consecutive calls can use the same
    server or exit IP. Failures are logged and return None; VPN failures are
    never sent directly. HTTP error responses are returned for the caller to
    inspect; request_soup also checks their status before parsing.

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
            executable = shutil.which(xray_path or os.environ.get("XRAY_PATH", "xray"))
            if not executable:
                logger.error("Xray not found; install it or set XRAY_PATH")
                return None

            outbound = random.choice(_servers(subscription_url))
            with _proxy(outbound, executable) as proxy, requests.Session() as session:
                session.trust_env = False
                response = session.request(
                    method, url, timeout=timeout,
                    proxies={"http": proxy, "https": proxy}, **kwargs,
                )
        logger.debug("HTTP request finished with status %s", response.status_code)
        return response
    except Exception as exc:
        # Third-party errors may include an explicit subscription URL or UUID.
        # Log the type only, never subscription material or request payloads.
        logger.error("%s request failed (%s)", "VPN" if subscription_url else "Direct", type(exc).__name__)
        return None


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
