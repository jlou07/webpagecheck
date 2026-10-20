"""Check monitored pages and send email notifications using Azure Communication Services."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time
import urllib.request
from html import escape as html_escape
from html.parser import HTMLParser
from typing import Any, Callable, Optional

from store import Store, split_emails
from ai import Evaluator
from public_network import PublicProxy, validate_public_url

log = logging.getLogger("webpagecheck")

USER_AGENT = "Mozilla/5.0 (compatible; webpagecheck/2.0)"
MAX_BODY_BYTES = 5 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 30
SCHEDULER_TICK_SECONDS = 15
SCREENSHOT_TIMEOUT_MS = 30000
SCREENSHOT_MAX_HEIGHT = 4000

Fetch = Callable[[str, float], str]
# notify(title, message, click_url) -> list of delivery errors
Notify = Callable[[str, str, str], list[str]]
Clock = Callable[[], float]


class _TextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def extract_text(html: str) -> str:
    """Return visible page text with normalized whitespace."""
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())


def fetch_html(url: str, timeout: float) -> str:
    validate_public_url(url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
    )
    with PublicProxy(timeout) as proxy:
        with proxy.opener().open(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(MAX_BODY_BYTES)
    return raw.decode(charset, errors="replace")


def send_email(
    connection_string: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    urgent: bool = False,
    image_jpeg: Optional[bytes] = None,
) -> None:
    """Send an email using Azure Communication Services; raise on failure."""
    from azure.communication.email import EmailClient
    from azure.core.exceptions import HttpResponseError

    client = EmailClient.from_connection_string(connection_string)
    content: dict[str, Any] = {"subject": subject, "plainText": body}
    message: dict[str, Any] = {
        "senderAddress": sender,
        "recipients": {"to": [{"address": recipient}]},
        "content": content,
    }
    if image_jpeg:
        text_html = html_escape(body).replace("\n", "<br>")
        content["html"] = (
            f'<p>{text_html}</p><p><img src="cid:page-capture" alt="Page capture" style="max-width:100%"></p>'
        )
        message["attachments"] = [
            {
                "name": "capture.jpg",
                "contentType": "image/jpeg",
                "contentInBase64": base64.b64encode(image_jpeg).decode("ascii"),
                "contentId": "page-capture",
            }
        ]
    if urgent:
        content["subject"] = f"URGENT: {subject}"
        message["headers"] = {"X-Priority": "1", "X-MSMail-Priority": "High"}
    try:
        client.begin_send(message).result()
    except HttpResponseError as exc:
        if exc.status_code == 429 or "TooManyRequests" in str(exc):
            raise RuntimeError(
                "Azure send limit reached (5 emails per minute and 10 per hour on the Azure domain). "
                "Try again later"
            ) from exc
        raise


def chromium_sandbox_enabled() -> bool:
    value = os.environ.get("CHROMIUM_SANDBOX", "true").strip().lower()
    if value not in ("true", "false"):
        raise ValueError("CHROMIUM_SANDBOX must be true or false")
    return value == "true"


def capture_screenshot(url: str) -> bytes:
    """Capture a JPEG using Playwright Chromium; raise on failure."""
    from playwright.sync_api import sync_playwright

    validate_public_url(url)
    sandbox = chromium_sandbox_enabled()
    with PublicProxy(SCREENSHOT_TIMEOUT_MS / 1000) as proxy, sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            chromium_sandbox=sandbox,
            proxy=proxy.browser_settings(),
            args=[
                "--disable-dev-shm-usage", "--disable-gpu", "--disable-blink-features=AutomationControlled",
                "--disable-quic", "--dns-prefetch-disable",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ],
        )
        try:
            # A standard browser User-Agent reduces headless Chromium blocking.
            user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{browser.version} Safari/537.36"
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 900}, user_agent=user_agent, locale="en-US",
                service_workers="block", accept_downloads=False,
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = context.new_page()
            page.goto(url, wait_until="load", timeout=SCREENSHOT_TIMEOUT_MS)
            # Cap the height to avoid exhausting container memory on long pages.
            height = page.evaluate("document.documentElement.scrollHeight")
            page.set_viewport_size({"width": 1280, "height": min(max(height, 900), SCREENSHOT_MAX_HEIGHT)})
            return page.screenshot(type="jpeg", quality=70)
        finally:
            browser.close()


def capture_screenshot_or_none(url: str) -> Optional[bytes]:
    """Attempt a capture without preventing alert delivery on failure."""
    try:
        return capture_screenshot(url)
    except Exception:
        log.exception("Screenshot failed for %s", url)
        return None


def channel_status(settings: dict[str, str], email_connection_string: Optional[str]) -> dict[str, bool]:
    return {
        "email": bool(email_connection_string and settings.get("email_sender") and settings.get("email_recipient")),
    }


def _email_recipients(settings: dict[str, str], extra_emails: Optional[list[str]]) -> list[str]:
    """Return primary and extra recipients without duplicates."""
    recipients: list[str] = []
    for address in [*split_emails(settings.get("email_recipient", "")), *(extra_emails or [])]:
        if address.casefold() not in {r.casefold() for r in recipients}:
            recipients.append(address)
    return recipients


def send_all(
    settings: dict[str, str],
    email_connection_string: Optional[str],
    title: str,
    message: str,
    click_url: str,
    extra_emails: Optional[list[str]] = None,
    urgent: bool = False,
    image_jpeg: Optional[bytes] = None,
) -> list[str]:
    """Email the alert to each recipient and return delivery errors."""
    errors: list[str] = []
    recipients = _email_recipients(settings, extra_emails)
    if email_connection_string and settings.get("email_sender") and recipients:
        body = f"{message}\n\n{click_url}" if click_url else message
        for recipient in recipients:
            try:
                send_email(
                    email_connection_string,
                    settings["email_sender"],
                    recipient,
                    title,
                    body,
                    urgent=urgent,
                    image_jpeg=image_jpeg,
                )
            except Exception as exc:
                log.exception("Email send failed for %s", recipient)
                errors.append(f"email ({recipient}) : {exc}")
    return errors


def run_check(
    watch: dict[str, Any],
    state: dict[str, Any],
    now: float,
    fetch: Fetch,
    notify: Notify,
    evaluate: Optional[Evaluator] = None,
) -> dict[str, Any]:
    """Check a page and return its new state without persisting it."""
    new = dict(state)
    new["last_checked_ts"] = now
    new["notify_errors"] = []
    url = watch["url"]
    name = watch["name"]

    try:
        html = fetch(url, HTTP_TIMEOUT_SECONDS)
    except Exception as exc:
        log.warning("Could not read %s: %s", url, exc)
        new["last_status"] = "error"
        new["last_error"] = str(exc)
        if not state.get("failing"):
            new["failing"] = True
            new["notify_errors"] = notify(f"Page unreachable: {name}", f"The page could not be loaded: {exc}", url)
        return new

    new["last_status"] = "ok"
    new["last_error"] = None
    notify_errors: list[str] = []
    if state.get("failing"):
        new["failing"] = False
        notify_errors += notify(f"Page reachable again: {name}", "The page is reachable again.", url)

    text = extract_text(html)
    if watch["mode"] == "ai":
        if evaluate is None:
            new["last_status"] = "error"
            new["last_error"] = "Azure OpenAI is not configured"
            new["notify_errors"] = notify_errors
            return new
        try:
            probability, reason = evaluate(watch["ai_condition"], text)
        except Exception as exc:
            log.exception("AI evaluation failed for %s", url)
            new["last_status"] = "error"
            new["last_error"] = f"AI evaluation failed: {exc}"
            new["notify_errors"] = notify_errors
            return new

        percent = round(probability * 100)
        above = percent >= watch["ai_threshold"]
        new["ai_probability"] = percent
        new["ai_reason"] = reason
        new["ai_above"] = above
        if above and not state.get("ai_above"):
            new["last_changed_ts"] = now
            notify_errors += notify(
                f"Condition detected ({percent} %): {name}",
                f"{watch['ai_condition']}\n\n{reason}",
                url,
            )
    elif watch["mode"] == "keyword":
        keyword = watch["keyword"] or ""
        present = keyword.casefold() in text.casefold()
        previous = bool(state.get("keyword_present"))
        new["keyword_present"] = present
        if present != previous:
            new["last_changed_ts"] = now
            if present:
                notify_errors += notify(f"Keyword found: {name}", f'"{keyword}" is present on this page.', url)
            else:
                notify_errors += notify(f"Keyword gone: {name}", f'"{keyword}" is no longer present on this page.', url)
    else:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        previous_hash = state.get("last_hash")
        new["last_hash"] = digest
        if previous_hash is not None and digest != previous_hash:
            new["last_changed_ts"] = now
            notify_errors += notify(f"Page changed: {name}", "The content of this page has changed.", url)

    new["notify_errors"] = notify_errors
    return new


class Monitor:
    """Run checks on demand or according to each page's schedule."""

    def __init__(
        self,
        store: Store,
        email_connection_string: Optional[str],
        evaluate: Optional[Evaluator] = None,
        fetch: Fetch = fetch_html,
        clock: Clock = time.time,
        tick_seconds: float = SCHEDULER_TICK_SECONDS,
    ) -> None:
        self._store = store
        self._email_connection_string = email_connection_string
        self._evaluate = evaluate
        self._fetch = fetch
        self._clock = clock
        self._tick = tick_seconds
        self._check_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def ai_enabled(self) -> bool:
        return self._evaluate is not None

    def _notify(
        self,
        title: str,
        message: str,
        click_url: str,
        image: Optional[bytes],
        extra_emails: Optional[list[str]] = None,
        watch_id: Optional[str] = None,
    ) -> list[str]:
        settings = self._store.get_settings()
        recipients = _email_recipients(settings, extra_emails)
        _, errors = self._dispatch(settings, title, message, click_url, extra_emails, recipients, image, watch_id)
        return errors

    def _dispatch(
        self,
        settings: dict[str, Any],
        title: str,
        message: str,
        click_url: str,
        extra_emails: Optional[list[str]],
        recipients: list[str],
        image: Optional[bytes],
        watch_id: Optional[str] = None,
    ) -> tuple[str, list[str]]:
        """Send or simulate an alert and return its status and delivery errors."""
        body = f"{message}\n\n{click_url}" if click_url else message
        alert = {"ts": self._clock(), "title": f"URGENT: {title}", "body": body, "recipients": recipients, "watch_id": watch_id}
        if not settings.get("email_enabled", True):
            self._store.add_alert({**alert, "status": "simulated", "errors": []}, image)
            return "simulated", []
        if not (self._email_connection_string and settings.get("email_sender") and recipients):
            self._store.add_alert({**alert, "status": "not sent", "errors": ["email not configured"]}, image)
            return "not sent", []
        errors = send_all(
            settings,
            self._email_connection_string,
            title,
            message,
            click_url,
            extra_emails=extra_emails,
            urgent=True,
            image_jpeg=image,
        )
        status = "failed" if errors else "sent"
        self._store.add_alert({**alert, "status": status, "errors": errors}, image)
        return status, errors

    def scan_watch(self, watch_id: str) -> Optional[dict[str, Any]]:
        """Analyzes a page on demand without changing its alert state. Nothing is sent."""
        watch = self._store.get_watch(watch_id)
        if watch is None:
            return None
        url, name, mode = watch["url"], watch["name"], watch["mode"]
        text = extract_text(self._fetch(url, HTTP_TIMEOUT_SECONDS))
        image = capture_screenshot_or_none(url)
        if mode == "ai":
            if self._evaluate is None:
                raise RuntimeError("Azure OpenAI is not configured")
            probability, reason = self._evaluate(watch["ai_condition"], text)
            percent = round(probability * 100)
            threshold = watch["ai_threshold"]
            triggered = percent >= threshold
            result = {"probability": percent, "threshold": threshold, "reason": reason}
            title = f"Condition detected ({percent} %): {name}"
            message = f"{watch['ai_condition']}\n\n{reason}"
        elif mode == "keyword":
            keyword = watch["keyword"] or ""
            present = keyword.casefold() in text.casefold()
            triggered = present
            result = {"keyword": keyword, "present": present}
            title = f"Keyword found: {name}"
            message = f'"{keyword}" is present on this page.'
        else:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            previous = watch.get("state", {}).get("last_hash")
            changed = None if previous is None else digest != previous
            triggered = bool(changed)
            result = {"changed": changed}
            title = f"Page changed: {name}"
            message = "The content of this page has changed."
        recipients = _email_recipients(self._store.get_settings(), watch.get("extra_emails"))
        mail = {"subject": f"URGENT: {title}", "body": f"{message}\n\n{url}", "recipients": recipients}
        return {
            "name": name,
            "url": url,
            "mode": mode,
            "checked_at": self._clock(),
            "image": base64.b64encode(image).decode("ascii") if image else None,
            "result": result,
            "triggered": triggered,
            "mail": mail,
        }

    def check_watch(self, watch_id: str) -> Optional[dict[str, Any]]:
        """Check a page immediately; return the updated watch or None if missing."""
        with self._check_lock:
            watch = self._store.get_watch(watch_id)
            if watch is None:
                return None
            now = self._clock()
            image = capture_screenshot_or_none(watch["url"])
            new_state = run_check(
                watch,
                watch.get("state", {}),
                now,
                self._fetch,
                lambda title, message, url: self._notify(
                    title, message, url, image, watch.get("extra_emails"), watch_id
                ),
                self._evaluate,
            )
            self._store.set_watch_state(watch_id, new_state, image)
            return self._store.get_watch(watch_id)


    def run_due(self) -> int:
        """Check pages whose interval has elapsed and return the check count."""
        now = self._clock()
        checked = 0
        for watch in self._store.list_watches():
            if not watch["enabled"]:
                continue
            last = watch.get("state", {}).get("last_checked_ts")
            if last is not None and now - last < watch["interval_minutes"] * 60:
                continue
            try:
                self.check_watch(watch["id"])
            except Exception:
                log.exception("Error while checking %s", watch["url"])
            checked += 1
        return checked

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:
                log.exception("Scheduler error")
            self._stop.wait(self._tick)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
