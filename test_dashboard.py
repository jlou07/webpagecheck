import itertools
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from monitor import Monitor
from store import Store


@unittest.skipUnless(os.environ.get("WPC_BROWSER_TESTS") == "1", "Set WPC_BROWSER_TESTS=1 to run Chromium")
class DashboardBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch()
        cls.addClassCleanup(cls.browser.close)
        page = cls.browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.set_content("<h1>First check</h1><p>The page is being watched.</p>")
            cls.first_image = page.screenshot(type="jpeg")
            page.set_content("<h1>Latest check</h1><p>This is the latest capture.</p>")
            cls.second_image = page.screenshot(type="jpeg")
        finally:
            page.close()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "config.json")
        self.watch = self.store.create_watch(
            {"name": 'Sample <b>page</b> "quoted"', "url": "https://example.com", "mode": "change"}
        )
        self.store.create_watch({"name": "Pending page", "url": "https://example.org", "mode": "change"})
        unavailable = self.store.create_watch(
            {"name": "No capture", "url": "https://example.net", "mode": "change"}
        )
        self.store.set_watch_state(unavailable["id"], {"last_checked_ts": 900.0, "last_status": "ok"})
        clock = itertools.count(1000)
        self.monitor = Monitor(
            self.store, None, fetch=lambda url, timeout: "<p>Unchanged</p>", clock=lambda: float(next(clock))
        )
        capture = mock.patch("monitor.capture_screenshot_or_none", return_value=self.first_image)
        self.capture = capture.start()
        self.addCleanup(capture.stop)
        self.monitor.check_watch(self.watch["id"])
        server = make_server("127.0.0.1", 0, create_app(self.store, self.monitor, "browser-test"), threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"
        context = self.browser.new_context(
            http_credentials={"username": "test", "password": "browser-test"},
            viewport={"width": 1440, "height": 1000},
        )
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.errors = []
        self.requests = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("request", lambda request: self.requests.append(request.url))
        self.page.goto(f"{self.url}/?scoutTheme=light")
        expect(self.page.locator("#watches tr")).to_have_count(3)

    def test_thumbnail_opens_full_size_authenticated_image(self):
        image = self.page.locator(".watch-thumbnail")
        expect(image).to_be_visible()
        image.evaluate("img => img.decode()")
        self.assertEqual(image.evaluate("img => img.naturalWidth"), 1280)
        link = self.page.locator(".thumbnail-link")
        self.assertEqual(link.get_attribute("target"), "_blank")
        self.assertIn("opens in a new tab", link.get_attribute("aria-label"))
        self.assertEqual(self.page.locator("#watches b").count(), 0)
        expect(self.page.locator("#watches tr").first).to_contain_text(self.watch["name"])
        with self.page.expect_popup() as opened:
            link.click()
        popup = opened.value
        try:
            popup.wait_for_load_state()
            response = popup.request.get(popup.url)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["content-type"], "image/jpeg")
            self.assertIn("no-store", response.headers["cache-control"])
            self.assertEqual(response.body(), self.first_image)
        finally:
            popup.close()
        self.assertFalse(any("mosaic.jpg" in url for url in self.requests))
        self.assertEqual(self.errors, [])

    def test_check_now_refreshes_thumbnail_without_an_alert(self):
        link = self.page.locator(".thumbnail-link")
        previous = link.get_attribute("href")
        self.capture.return_value = self.second_image
        self.page.locator("#watches tr").first.get_by_role("button", name="Check now", exact=True).click()
        expect(link).not_to_have_attribute("href", previous)
        self.page.locator(".watch-thumbnail").evaluate("img => img.decode()")
        self.assertEqual(self.store.get_watch_image(self.watch["id"]), self.second_image)
        self.assertEqual(self.store.list_alerts(), [])
        self.assertEqual(self.errors, [])

    def test_scan_does_not_replace_last_check_thumbnail(self):
        previous = self.store.get_watch(self.watch["id"])
        self.capture.return_value = self.second_image
        self.page.locator("#watches tr").first.get_by_role(
            "button", name="Scan (analyze only, no email sent)", exact=True
        ).click()
        expect(self.page.locator("#scan-card .page-capture")).to_be_visible()
        self.assertEqual(self.store.get_watch(self.watch["id"]), previous)
        self.assertEqual(self.store.get_watch_image(self.watch["id"]), self.first_image)
        self.assertEqual(self.store.list_alerts(), [])
        self.assertEqual(self.errors, [])

    def test_mobile_layout_and_system_dark_theme(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)
        region = self.page.get_by_role("region", name="Watched pages table")
        self.assertTrue(region.evaluate("el => el.scrollWidth > el.clientWidth"))
        expect(self.page.locator("th.last-check")).to_be_hidden()
        self.page.locator(".thumbnail-link").scroll_into_view_if_needed()
        expect(self.page.locator(".watch-thumbnail")).to_be_visible()
        self.page.emulate_media(color_scheme="dark")
        self.page.goto(self.url)
        expect(self.page.locator("html")).to_have_attribute("data-theme", "dark")
        expect(self.page.locator("#watches tr")).to_have_count(3)
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)
        self.assertEqual(self.errors, [])

    def test_missing_images_show_explicit_placeholder_and_error(self):
        expect(self.page.locator("#watches tr").nth(1)).to_contain_text("Not checked yet")
        expect(self.page.locator("#watches tr").nth(2)).to_contain_text("Capture unavailable")
        self.page.route(
            "**/api/watches/*/image?*",
            lambda route: route.fulfill(status=404, json={"error": "Image not found"}),
        )
        self.page.reload()
        expect(self.page.locator("#watches tr").first).to_contain_text("Capture unavailable")
        expect(self.page.locator("#toast")).to_contain_text("Could not load the last check capture")
        self.assertEqual(self.page.locator(".thumbnail-link").count(), 0)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
