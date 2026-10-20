import base64
import contextlib
import datetime
import http.client
import ipaddress
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app import create_app, main
from monitor import Monitor, capture_screenshot, chromium_sandbox_enabled, fetch_html
from public_network import PublicProxy, UnsafeURLError, connect_public, is_public_address, validate_public_url
from store import Store, ValidationError, validate_watch


class PublicURLTests(unittest.TestCase):
    def test_rejects_non_public_and_ambiguous_urls(self):
        urls = [
            "http://127.0.0.1/", "http://10.1.2.3/", "http://172.16.1.1/", "http://192.168.1.1/",
            "http://169.254.169.254/", "http://168.63.129.16/", "http://100.100.100.200/", "http://0.0.0.0/", "http://224.0.0.1/",
            "http://[::1]/", "http://[::]/", "http://[fc00::1]/", "http://[fe80::1]/",
            "http://[ff02::1]/", "http://[fec0::1]/", "http://[::ffff:127.0.0.1]/",
            "http://[64:ff9b::a00:1]/", "http://[2002:7f00:1::]/", "http://[fe80::1%25eth0]/",
            "http://localhost/", "http://LOCALHOST./", "http://a.localhost/", "http://printer.local/",
            "http://2130706433/", "http://0x7f000001/", "http://0177.0.0.1/", "http://127.1/",
            "http://user:password@example.com/", "http://example.com:0/", "http://example.com:65536/",
            "http://example.com:wrong/", "http://[broken/", "http:///", "file:///etc/passwd",
            "ftp://example.com/", "http://example.com\\@127.0.0.1/", "http://example.com/\r\nHost: localhost",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                validate_watch({"url": url})

    def test_accepts_public_urls_without_resolving_at_registration(self):
        with mock.patch("public_network.socket.getaddrinfo") as resolve:
            for url in ("https://example.com/path?q=x", "http://8.8.8.8:8080/", "https://[2606:4700:4700::1111]/"):
                with self.subTest(url=url):
                    self.assertEqual(validate_watch({"url": url})["url"], url)
            resolve.assert_not_called()

    def test_special_address_ranges_are_not_public(self):
        for address in ("192.0.0.8", "192.88.99.1", "240.0.0.1", "198.18.0.1", "203.0.113.1"):
            with self.subTest(address=address):
                self.assertFalse(is_public_address(address))

    def test_existing_private_watch_cannot_bypass_fetch_validation(self):
        with mock.patch("public_network.connect_public") as connect:
            with self.assertRaises(UnsafeURLError):
                fetch_html("http://169.254.169.254/metadata/identity/oauth2/token", 1)
            connect.assert_not_called()

    def test_api_rejects_private_create_and_update_without_changing_watch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "config.json")
            client = create_app(store, Monitor(store, None), "test-password").test_client()
            auth = {"Authorization": "Basic " + base64.b64encode(b"user:test-password").decode("ascii")}
            response = client.post("/api/watches", headers=auth, json={"url": "http://127.0.0.1/"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("public Internet", response.get_json()["error"])
            self.assertEqual(store.list_watches(), [])
            watch = store.create_watch({"url": "https://example.com/"})
            response = client.put(
                f"/api/watches/{watch['id']}", headers=auth, json={"url": "http://10.0.0.1/"}
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(store.get_watch(watch["id"])["url"], "https://example.com/")


def _address(value, port=80):
    family = socket.AF_INET6 if ipaddress.ip_address(value).version == 6 else socket.AF_INET
    target = (value, port, 0, 0) if family == socket.AF_INET6 else (value, port)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", target


class DNSConnectionTests(unittest.TestCase):
    def test_rejects_private_and_mixed_dns_before_opening_socket(self):
        for addresses in (
            [_address("127.0.0.1")],
            [_address("169.254.169.254")],
            [_address("8.8.8.8"), _address("10.0.0.1")],
            [_address("8.8.8.8"), _address("::1")],
            [],
        ):
            with self.subTest(addresses=addresses):
                with mock.patch("public_network.socket.getaddrinfo", return_value=addresses), mock.patch(
                    "public_network.socket.socket"
                ) as factory:
                    with self.assertRaises(UnsafeURLError):
                        connect_public("example.com", 80, 2)
                    factory.assert_not_called()

    def test_connection_is_pinned_to_checked_numeric_address(self):
        for address in ("8.8.8.8", "2606:4700:4700::1111"):
            with self.subTest(address=address):
                result = _address(address, 443)
                with mock.patch("public_network.socket.getaddrinfo", return_value=[result]) as resolve, mock.patch(
                    "public_network.socket.socket"
                ) as factory:
                    connection = connect_public("example.com", 443, 2)
                    self.assertIs(connection, factory.return_value)
                    resolve.assert_called_once()
                    connection.connect.assert_called_once_with(result[4])
                    connection.settimeout.assert_called_once_with(2)

    def test_dns_is_revalidated_for_every_connection(self):
        with mock.patch(
            "public_network.socket.getaddrinfo",
            side_effect=[[_address("8.8.8.8")], [_address("127.0.0.1")]],
        ), mock.patch("public_network.socket.socket") as factory:
            connect_public("rebind.example", 80, 2)
            with self.assertRaises(UnsafeURLError):
                connect_public("rebind.example", 80, 2)
            self.assertEqual(factory.call_count, 1)

    def test_failed_socket_is_closed_before_trying_next_address(self):
        first, second = mock.Mock(), mock.Mock()
        first.connect.side_effect = OSError("unreachable")
        with mock.patch(
            "public_network.socket.getaddrinfo", return_value=[_address("8.8.8.8"), _address("1.1.1.1")]
        ), mock.patch("public_network.socket.socket", side_effect=[first, second]):
            self.assertIs(connect_public("example.com", 80, 2), second)
        first.close.assert_called_once()
        second.connect.assert_called_once_with(("1.1.1.1", 80))

    def test_dns_failure_is_explicit(self):
        with mock.patch("public_network.socket.getaddrinfo", side_effect=socket.gaierror("not found")):
            with self.assertRaises(socket.gaierror):
                connect_public("missing.example", 80, 2)


class _Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, dict(self.headers)))
        if self.path == "/private-redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/forbidden")
            self.end_headers()
            return
        if self.path == "/public-redirect":
            self.send_response(302)
            self.send_header("Location", "http://other.example/final")
            self.end_headers()
            return
        if self.path == "/ftp-redirect":
            self.send_response(302)
            self.send_header("Location", "ftp://example.com/secret")
            self.end_headers()
            return
        if self.path == "/browser":
            body = (
                '<!doctype html><html><body><h1>Public page</h1>'
                f'<img src="http://127.0.0.1:{self.server.server_port}/forbidden">'
                f'<iframe src="http://localhost:{self.server.server_port}/forbidden"></iframe>'
                '<img src="http://private.example/forbidden">'
                f'<script>fetch("http://2130706433:{self.server.server_port}/forbidden").catch(()=>{{}});'
                f'new WebSocket("ws://127.0.0.1:{self.server.server_port}/forbidden");'
                '</script></body></html>'
            ).encode()
        else:
            body = "Public result: caf\u00e9".encode("latin-1")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=iso-8859-1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.seen.append((self.path, dict(self.headers)))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@contextlib.contextmanager
def _origin(tls=False):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    server.daemon_threads = True
    server.seen = []
    with contextlib.ExitStack() as cleanup:
        cleanup.callback(server.server_close)
        if tls:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID

            directory = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "public.example")])
            now = datetime.datetime.now(datetime.timezone.utc)
            certificate = (
                x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("public.example")]), critical=False)
                .sign(key, hashes.SHA256())
            )
            certificate_file, key_file = directory / "certificate.pem", directory / "key.pem"
            certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_file.write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            ))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate_file, key_file)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


@contextlib.contextmanager
def _simulated_public_transport(origin):
    real_connect = connect_public
    real_resolve = socket.getaddrinfo

    def connect(host, port, timeout):
        if host in ("public.example", "other.example"):
            return socket.create_connection(origin.server_address, timeout)
        return real_connect(host, port, timeout)

    def resolve(host, port, *args, **kwargs):
        if host == "private.example":
            return [_address("127.0.0.1", port)]
        return real_resolve(host, port, *args, **kwargs)

    with mock.patch("public_network.connect_public", side_effect=connect) as connection, mock.patch(
        "public_network.socket.getaddrinfo", side_effect=resolve
    ):
        yield connection


class ProxyIntegrationTests(unittest.TestCase):
    def test_https_still_rejects_untrusted_certificates(self):
        with _origin(tls=True) as origin, _simulated_public_transport(origin):
            with self.assertRaises(urllib.error.URLError) as error:
                fetch_html("https://public.example/", 3)
            self.assertIsInstance(error.exception.reason, ssl.SSLCertVerificationError)
            self.assertEqual(origin.seen, [])

    def test_http_public_redirect_charset_and_auth_header_stripping(self):
        with _origin() as origin, _simulated_public_transport(origin) as connect:
            text = fetch_html("http://public.example/public-redirect", 3)
            self.assertEqual(text, "Public result: caf\u00e9")
            self.assertEqual([call.args[0] for call in connect.call_args_list], ["public.example", "other.example"])
            self.assertEqual([path for path, _ in origin.seen], ["/public-redirect", "/final"])
            for _, headers in origin.seen:
                self.assertNotIn("Proxy-Authorization", headers)
                self.assertEqual(headers["Accept-Language"], "en-US,en;q=0.8")

    def test_http_body_limit_is_preserved(self):
        with _origin() as origin, _simulated_public_transport(origin), mock.patch("monitor.MAX_BODY_BYTES", 6):
            self.assertEqual(fetch_html("http://public.example/", 3), "Public")

    def test_private_and_non_http_redirects_are_rejected_without_private_contact(self):
        with _origin() as origin, _simulated_public_transport(origin):
            for path in ("/private-redirect", "/ftp-redirect"):
                with self.subTest(path=path), self.assertRaises(UnsafeURLError):
                    fetch_html("http://public.example" + path, 3)
            self.assertNotIn("/forbidden", [path for path, _ in origin.seen])

    def test_no_proxy_and_environment_proxy_cannot_bypass_policy(self):
        with _origin() as origin, _simulated_public_transport(origin) as connect, mock.patch.dict(
            os.environ, {"NO_PROXY": "*", "no_proxy": "*", "HTTP_PROXY": "http://127.0.0.1:1"}
        ):
            self.assertIn("Public result", fetch_html("http://public.example/", 3))
            connect.assert_called_once()
            with self.assertRaises(urllib.error.HTTPError) as error:
                fetch_html("http://private.example/", 3)
            self.assertEqual(error.exception.code, 403)
            self.assertEqual(len(origin.seen), 1)

    def test_proxy_requires_authentication_before_resolving_destination(self):
        with PublicProxy(2) as proxy, mock.patch("public_network.connect_public") as connect:
            client = http.client.HTTPConnection(proxy.address, timeout=2)
            try:
                client.request("GET", "http://example.com/")
                response = client.getresponse()
                self.assertEqual(response.status, 407)
                response.read()
            finally:
                client.close()
            connect.assert_not_called()

    def test_connect_tunnel_rejects_private_literal_and_private_dns(self):
        with _origin() as origin, _simulated_public_transport(origin), PublicProxy(2) as proxy:
            for target in ("127.0.0.1:80", "private.example:443"):
                with self.subTest(target=target):
                    client = http.client.HTTPConnection(proxy.address, timeout=2)
                    try:
                        client.request("CONNECT", target, headers={"Proxy-Authorization": proxy.authorization})
                        response = client.getresponse()
                        self.assertEqual(response.status, 403)
                        response.read()
                    finally:
                        client.close()
            self.assertEqual(origin.seen, [])

    def test_forwarding_preserves_post_body(self):
        with _origin() as origin, _simulated_public_transport(origin), PublicProxy(3) as proxy:
            client = http.client.HTTPConnection(proxy.address, timeout=3)
            try:
                client.request(
                    "POST", "http://public.example/echo", body=b"request body",
                    headers={"Proxy-Authorization": proxy.authorization},
                )
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"request body")
            finally:
                client.close()


class BrowserConfigurationTests(unittest.TestCase):
    def test_sandbox_defaults_to_enabled(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CHROMIUM_SANDBOX", None)
            self.assertTrue(chromium_sandbox_enabled())

    def test_invalid_sandbox_setting_is_rejected_before_launch(self):
        for value in ("", "0", "yes", "disabled", "typo"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"CHROMIUM_SANDBOX": value}):
                with mock.patch("playwright.sync_api.sync_playwright") as playwright:
                    with self.assertRaisesRegex(ValueError, "CHROMIUM_SANDBOX"):
                        capture_screenshot("https://example.com")
                    playwright.assert_not_called()

    def test_invalid_sandbox_setting_prevents_app_startup(self):
        with mock.patch.dict(os.environ, {"UI_PASSWORD": "test-password", "CHROMIUM_SANDBOX": "typo"}):
            with mock.patch("app.Store") as store, self.assertLogs("webpagecheck", level="ERROR"):
                self.assertEqual(main(), 2)
                store.assert_not_called()

    def test_sandbox_choice_preserves_proxy_and_no_udp_bypass(self):
        for value in ("true", "false", " TRUE "):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"CHROMIUM_SANDBOX": value}):
                with mock.patch("playwright.sync_api.sync_playwright") as playwright:
                    engine = playwright.return_value.__enter__.return_value
                    browser = engine.chromium.launch.return_value
                    browser.version = "140.0"
                    page = browser.new_context.return_value.new_page.return_value
                    page.evaluate.return_value = 10000
                    page.screenshot.return_value = b"jpeg"
                    self.assertEqual(capture_screenshot("https://example.com"), b"jpeg")
                    settings = engine.chromium.launch.call_args.kwargs
                    self.assertEqual(settings["chromium_sandbox"], value.strip().lower() == "true")
                    self.assertNotIn("--no-sandbox", settings["args"])
                    self.assertIn("--disable-quic", settings["args"])
                    self.assertIn("--force-webrtc-ip-handling-policy=disable_non_proxied_udp", settings["args"])
                    self.assertEqual(settings["proxy"]["bypass"], "<-loopback>")
                    self.assertTrue(settings["proxy"]["password"])
                    self.assertEqual(browser.new_context.call_args.kwargs["service_workers"], "block")
                    self.assertFalse(browser.new_context.call_args.kwargs["accept_downloads"])
                    self.assertEqual(browser.new_context.call_args.kwargs["locale"], "en-US")
                    page.set_viewport_size.assert_called_once_with({"width": 1280, "height": 4000})
                    browser.close.assert_called_once()

    def test_browser_is_closed_when_navigation_fails(self):
        with mock.patch("playwright.sync_api.sync_playwright") as playwright:
            browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value
            browser.version = "140.0"
            browser.new_context.return_value.new_page.return_value.goto.side_effect = RuntimeError("blocked")
            with self.assertRaisesRegex(RuntimeError, "blocked"):
                capture_screenshot("https://example.com")
            browser.close.assert_called_once()

    def test_private_initial_url_is_rejected_before_browser_launch(self):
        with mock.patch("playwright.sync_api.sync_playwright") as playwright:
            with self.assertRaises(UnsafeURLError):
                capture_screenshot("http://127.0.0.1/")
            playwright.assert_not_called()


@unittest.skipUnless(os.environ.get("WPC_BROWSER_TESTS") == "1", "Set WPC_BROWSER_TESTS=1 to run Chromium")
class BrowserIntegrationTests(unittest.TestCase):
    def test_real_chromium_blocks_private_subresources_and_websockets(self):
        for value in ("true", "false"):
            with self.subTest(sandbox=value), mock.patch.dict(os.environ, {"CHROMIUM_SANDBOX": value}):
                with _origin() as origin, _simulated_public_transport(origin):
                    image = capture_screenshot("http://public.example/browser")
                    self.assertTrue(image.startswith(b"\xff\xd8\xff"))
                    self.assertIn("/browser", [path for path, _ in origin.seen])
                    self.assertNotIn("/forbidden", [path for path, _ in origin.seen])

    def test_real_chromium_does_not_follow_redirect_into_private_service(self):
        for value in ("true", "false"):
            with self.subTest(sandbox=value), mock.patch.dict(os.environ, {"CHROMIUM_SANDBOX": value}):
                with _origin() as origin, _simulated_public_transport(origin):
                    image = capture_screenshot("http://public.example/private-redirect")
                    self.assertTrue(image.startswith(b"\xff\xd8\xff"))
                    self.assertEqual([path for path, _ in origin.seen], ["/private-redirect"])


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell is required")
class DeploymentSecurityTests(unittest.TestCase):
    def _run_deployment(self, fail=False, sandbox=True):
        script = r"""
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1')
$env:PATH = ''
$global:ParameterPath = ''
$global:ObservedPassword = ''
$global:FailureMode = FAILURE_MODE
$global:ExpectedSandbox = 'SANDBOX_VALUE'
function global:az {
    $global:LASTEXITCODE = 0
    if (($args -join ' ') -match 'deployment group create') {
        if ($args -notcontains "chromiumSandbox=$global:ExpectedSandbox") { throw 'Sandbox option was not passed to Bicep' }
        $fileArgument = @($args | Where-Object { "$_".StartsWith('@') })[0]
        if (-not $fileArgument) { throw 'Missing secure parameters file' }
        $global:ParameterPath = "$fileArgument".Substring(1)
        $global:ObservedPassword = (Get-Content -LiteralPath $global:ParameterPath -Raw | ConvertFrom-Json).uiPassword.value
        if (($args -join ' ').Contains($global:ObservedPassword)) { throw 'Password in process arguments' }
        $rules = (Get-Acl -LiteralPath $global:ParameterPath).Access
        $expected = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        foreach ($rule in $rules) {
            if ($rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $expected) {
                throw 'Secret file is readable by another principal'
            }
        }
        if ($global:FailureMode) { $global:LASTEXITCODE = 9 }
    }
    if (($args -join ' ') -match 'deployment group show') {
        '{"acrName":{"value":"registry"},"acrLoginServer":{"value":"registry.example"},"appUrl":{"value":"https://app.example"}}'
    }
}
$failed = $false
try {
    $captured = @(& DEPLOY_SCRIPT -ResourceGroup 'rg-test' -ChromiumSandbox:SANDBOX_SETTING 6>&1) -join "`n"
} catch {
    $failed = $true
    $captured = $_.ToString()
}
if (-not $global:ObservedPassword) { throw "Secret file was not inspected: $captured" }
if ($captured.Contains($global:ObservedPassword)) { throw 'Password leaked in output' }
if (Test-Path -LiteralPath $global:ParameterPath) { throw 'Secret file was not cleaned' }
if (Test-Path -LiteralPath (Split-Path $global:ParameterPath)) { throw 'Secret directory was not cleaned' }
if ($failed -ne $global:FailureMode) { throw "Unexpected failure: $captured" }
Write-Output 'Deployment secret handling OK'
"""
        path = str(Path(__file__).with_name("deploy.ps1")).replace("'", "''")
        script = script.replace("DEPLOY_SCRIPT", "'" + path + "'").replace("FAILURE_MODE", "$true" if fail else "$false")
        script = script.replace("SANDBOX_VALUE", "true" if sandbox else "false")
        script = script.replace("SANDBOX_SETTING", "$true" if sandbox else "$false")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Deployment secret handling OK", result.stdout)

    def test_password_is_not_logged_or_passed_on_command_line_and_temp_file_is_removed(self):
        self._run_deployment()

    def test_failure_is_redacted_and_temp_file_is_removed(self):
        self._run_deployment(fail=True)

    def test_explicit_sandbox_compatibility_setting_reaches_bicep(self):
        self._run_deployment(sandbox=False)


if __name__ == "__main__":
    unittest.main()
