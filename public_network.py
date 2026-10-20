"""Public-only egress shared by the HTTP collector and Chromium."""

from __future__ import annotations

import base64
import hmac
import ipaddress
import logging
import secrets
import select
import socket
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from playwright.sync_api import ProxySettings

log = logging.getLogger("webpagecheck")


class UnsafeURLError(ValueError):
    pass


_TRANSITION_NETWORKS = tuple(
    ipaddress.ip_network(prefix)
    for prefix in ("64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16", "::ffff:0:0/96")
)


def is_public_address(value: str | int) -> bool:
    address = ipaddress.ip_address(value)
    if address == ipaddress.ip_address("168.63.129.16"):
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        return not address.is_site_local and not any(address in network for network in _TRANSITION_NETWORKS)
    return address not in ipaddress.ip_network("192.0.0.0/24") and address not in ipaddress.ip_network("192.88.99.0/24")


def validate_public_url(url: str) -> urllib.parse.SplitResult:
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise UnsafeURLError("The URL contains invalid characters")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("The URL has an invalid host or port") from exc
    if parsed.scheme not in ("http", "https") or not host:
        raise UnsafeURLError("The URL must start with http:// or https://")
    if parsed.username is not None or parsed.password is not None or "%" in host:
        raise UnsafeURLError("URL credentials and scoped addresses are not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeURLError("The URL has an invalid port")
    host = host.rstrip(".").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local")):
        raise UnsafeURLError("Only public Internet destinations are allowed")
    try:
        address = str(ipaddress.ip_address(host))
    except ValueError:
        try:
            # inet_aton also recognizes decimal, octal and shortened IPv4 forms.
            address = socket.inet_ntoa(socket.inet_aton(host))
        except OSError:
            try:
                host.encode("idna")
            except UnicodeError as exc:
                raise UnsafeURLError("The URL has an invalid host") from exc
            return parsed
    if not is_public_address(address):
        raise UnsafeURLError("Only public Internet destinations are allowed")
    return parsed


def connect_public(host: str, port: int, timeout: float) -> socket.socket:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if not addresses or any(not is_public_address(info[4][0]) for info in addresses):
        raise UnsafeURLError("The destination resolves to a non-public Internet address")
    last_error: Optional[OSError] = None
    for family, kind, protocol, _, address in addresses:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(timeout)
            # Connect to the checked numeric address; never resolve the name again.
            connection.connect(address)
            return connection
        except OSError as exc:
            connection.close()
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError("No public destination address is available")


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, authorization: str, timeout: float) -> None:
        self.authorization = authorization
        self.egress_timeout = timeout
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(("127.0.0.1", 0), _ProxyHandler)


class _ProxyHandler(BaseHTTPRequestHandler):
    rbufsize = 0
    timeout = 30

    @property
    def proxy_server(self) -> _ProxyServer:
        if not isinstance(self.server, _ProxyServer):
            raise TypeError("Collector handler requires a public egress proxy")
        return self.server

    def log_message(self, format: str, *args: object) -> None:
        # Request paths and proxy credentials must not appear in access logs.
        return

    def _error(self, status: int, message: str) -> None:
        self.send_response(status)
        if status == 407:
            self.send_header("Proxy-Authenticate", 'Basic realm="webpagecheck-egress"')
        body = message.encode("utf-8")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _forward(self) -> None:
        server = self.proxy_server
        supplied = self.headers.get("Proxy-Authorization", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), server.authorization.encode("ascii")):
            self._error(407, "Proxy authentication required")
            return
        if not server.slots.acquire(blocking=False):
            self._error(503, "Too many collector connections")
            return
        upstream: Optional[socket.socket] = None
        try:
            if self.command == "CONNECT":
                parsed = validate_public_url("https://" + self.path)
                if parsed.path or parsed.query or parsed.fragment:
                    raise UnsafeURLError("Invalid CONNECT destination")
            else:
                parsed = validate_public_url(self.path)
                if parsed.scheme != "http":
                    raise UnsafeURLError("HTTPS requires CONNECT")
            host = parsed.hostname
            if host is None:
                raise UnsafeURLError("Missing destination host")
            upstream = connect_public(host, parsed.port or (443 if parsed.scheme == "https" else 80), server.egress_timeout)
        except UnsafeURLError as exc:
            log.warning("Collector connection blocked: %s", exc)
            self._error(403, str(exc))
            return
        except OSError as exc:
            log.warning("Collector connection failed: %s", exc)
            self._error(502, "The public destination could not be reached")
            return
        finally:
            if upstream is None:
                server.slots.release()
        assert upstream is not None
        try:
            if self.command == "CONNECT":
                self.send_response(200, "Connection established")
                self.end_headers()
            else:
                target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                headers = [f"{self.command} {target} HTTP/1.1"]
                headers.extend(
                    f"{name}: {value}"
                    for name, value in self.headers.items()
                    if name.lower() not in ("proxy-authorization", "proxy-connection", "connection")
                )
                headers.extend(("Connection: close", "", ""))
                upstream.sendall("\r\n".join(headers).encode("latin-1"))
            self.close_connection = True
            self._relay(upstream)
        except (OSError, TimeoutError) as exc:
            log.warning("Collector relay failed: %s", exc)
        finally:
            upstream.close()
            server.slots.release()

    def _relay(self, upstream: socket.socket) -> None:
        deadline = time.monotonic() + self.proxy_server.egress_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Collector connection timed out")
            ready, _, _ = select.select([self.connection, upstream], [], [], remaining)
            if not ready:
                raise TimeoutError("Collector connection timed out")
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                destination = upstream if source is self.connection else self.connection
                destination.sendall(data)

    do_CONNECT = _forward
    do_GET = _forward
    do_HEAD = _forward
    do_POST = _forward
    do_PUT = _forward
    do_PATCH = _forward
    do_DELETE = _forward
    do_OPTIONS = _forward


class _ForcedProxyHandler(urllib.request.ProxyHandler):
    def __init__(self, proxy: PublicProxy) -> None:
        self.proxy = proxy
        super().__init__({"http": proxy.url, "https": proxy.url})

    def proxy_open(self, req, proxy, type):
        # The standard handler honors NO_PROXY, including loopback exemptions.
        validate_public_url(req.full_url)
        req.add_header("Proxy-Authorization", self.proxy.authorization)
        req.set_proxy(self.proxy.address, "http")
        return None


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PublicProxy:
    def __init__(self, timeout: float = 30) -> None:
        self.timeout = timeout
        self.password = secrets.token_urlsafe(32)
        self.authorization = "Basic " + base64.b64encode(f"collector:{self.password}".encode("ascii")).decode("ascii")
        self.server: Optional[_ProxyServer] = None
        self.thread: Optional[threading.Thread] = None
        self.address = ""
        self.url = ""

    def __enter__(self) -> PublicProxy:
        self.server = _ProxyServer(self.authorization, self.timeout)
        self.address = f"127.0.0.1:{self.server.server_port}"
        self.url = "http://" + self.address
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join()

    def opener(self) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(_ForcedProxyHandler(self), _PublicRedirectHandler())

    def browser_settings(self) -> ProxySettings:
        return {"server": self.url, "username": "collector", "password": self.password, "bypass": "<-loopback>"}
