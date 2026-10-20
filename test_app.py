import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from azure.core.exceptions import HttpResponseError

from ai import build_messages, parse_answer
from app import create_app
from monitor import Monitor, capture_screenshot_or_none, channel_status, extract_text, run_check, send_all, send_email
from store import Store, ValidationError, validate_settings, validate_watch

PASSWORD = "secret-test"


def _watch(**overrides):
    base = {
        "id": "w1",
        "name": "Summit",
        "url": "https://summit.microsoft.com/en-us/",
        "mode": "ai",
        "keyword": None,
        "ai_condition": "Les inscriptions sont ouvertes",
        "ai_threshold": 70,
        "interval_minutes": 60,
        "enabled": True,
    }
    base.update(overrides)
    return base


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message, url):
        self.calls.append(title)
        return []


class ValidationTests(unittest.TestCase):
    def test_ai_mode_requires_condition(self):
        with self.assertRaises(ValidationError):
            validate_watch({"url": "https://example.com", "mode": "ai"})

    def test_ai_threshold_range(self):
        with self.assertRaises(ValidationError):
            validate_watch({"url": "https://example.com", "mode": "ai", "ai_condition": "x", "ai_threshold": 0})
        with self.assertRaises(ValidationError):
            validate_watch({"url": "https://example.com", "mode": "ai", "ai_condition": "x", "ai_threshold": 101})

    def test_non_ai_mode_clears_condition(self):
        fields = validate_watch({"url": "https://example.com", "mode": "change", "ai_condition": "x"})
        self.assertIsNone(fields["ai_condition"])

    def test_rejects_non_http_url(self):
        with self.assertRaises(ValidationError):
            validate_watch({"url": "ftp://example.com", "mode": "change"})

    def test_settings_rejects_invalid_email(self):
        with self.assertRaises(ValidationError):
            validate_settings({"email_recipient": "pas-un-email"}, {})
        with self.assertRaises(ValidationError):
            validate_settings({"email_sender": "pas-un-email"}, {})

    def test_settings_accepts_several_recipients(self):
        merged = validate_settings({"email_recipient": "a@x.com; B@y.org a@x.com"}, {})
        self.assertEqual(merged["email_recipient"], "a@x.com, B@y.org")

    def test_watch_extra_emails_are_parsed_and_deduplicated(self):
        fields = validate_watch(
            {"url": "https://example.com", "mode": "change", "extra_emails": "ami@x.com, Ami@x.com; collegue@y.org"}
        )
        self.assertEqual(fields["extra_emails"], ["ami@x.com", "collegue@y.org"])

    def test_watch_extra_emails_rejects_invalid_address(self):
        with self.assertRaises(ValidationError):
            validate_watch({"url": "https://example.com", "mode": "change", "extra_emails": "pas-un-email"})

    def test_parse_answer_clamps_probability(self):
        self.assertEqual(parse_answer('{"probability": 1.7, "reason": "ok"}'), (1.0, "ok"))
        self.assertEqual(parse_answer('{"probability": -1, "reason": ""}'), (0.0, ""))

    def test_messages_truncate_long_pages(self):
        messages = build_messages("cond", "x" * 50000)
        self.assertLess(len(messages[1]["content"]), 13000)

    def test_extract_text_skips_scripts(self):
        self.assertEqual(extract_text("<p>Bonjour</p><script>var a=1;</script>"), "Bonjour")


class EmailRecipientTests(unittest.TestCase):
    SETTINGS = {"email_sender": "DoNotReply@x.azurecomm.net", "email_recipient": "moi@x.com"}

    def test_send_all_mails_main_and_extra_recipients_once_each(self):
        with mock.patch("monitor.send_email") as mail:
            errors = send_all(self.SETTINGS, "conn", "Titre", "détail", "https://example.com", ["ami@x.com", "MOI@x.com"])
        self.assertEqual(errors, [])
        self.assertEqual([c.args[2] for c in mail.call_args_list], ["moi@x.com", "ami@x.com"])

    def test_send_all_mails_extra_recipients_without_main_one(self):
        settings = {"email_sender": "DoNotReply@x.azurecomm.net", "email_recipient": ""}
        with mock.patch("monitor.send_email") as mail:
            send_all(settings, "conn", "Titre", "", "", ["ami@x.com"])
        self.assertEqual([c.args[2] for c in mail.call_args_list], ["ami@x.com"])

    def test_send_all_reports_failure_per_address(self):
        with mock.patch("monitor.send_email", side_effect=[None, RuntimeError("boîte pleine")]):
            errors = send_all(self.SETTINGS, "conn", "Titre", "", "", ["ami@x.com"])
        self.assertEqual(errors, ["email (ami@x.com) : boîte pleine"])

    def test_monitor_passes_watch_extra_emails_to_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "config.json")
            store.update_settings({"email_sender": "DoNotReply@x.azurecomm.net", "email_recipient": "moi@x.com"})
            watch = store.create_watch(
                {"url": "https://example.com", "mode": "keyword", "keyword": "ouvertes", "extra_emails": "ami@x.com"}
            )
            monitor = Monitor(store, "conn", evaluate=None, fetch=lambda url, timeout: "Inscriptions ouvertes")
            with mock.patch("monitor.capture_screenshot_or_none", return_value=None), mock.patch(
                "monitor.send_all", return_value=[]
            ) as sender:
                monitor.check_watch(watch["id"])
        self.assertEqual(sender.call_args.kwargs["extra_emails"], ["ami@x.com"])
        self.assertTrue(sender.call_args.kwargs["urgent"])


    def test_send_all_mails_each_recipient_of_a_list(self):
        settings = {"email_sender": "DoNotReply@x.azurecomm.net", "email_recipient": "a@x.com, b@y.com"}
        with mock.patch("monitor.send_email") as mail:
            errors = send_all(settings, "conn", "Titre", "", "")
        self.assertEqual(errors, [])
        self.assertEqual([c.args[2] for c in mail.call_args_list], ["a@x.com", "b@y.com"])

    def test_channel_status_needs_connection_sender_and_recipient(self):
        full = {"email_sender": "DoNotReply@x.azurecomm.net", "email_recipient": "a@x.com, b@y.com"}
        self.assertTrue(channel_status(full, "endpoint=https://x;accesskey=abc")["email"])
        self.assertFalse(channel_status(full, None)["email"])
        self.assertFalse(channel_status({"email_sender": "DoNotReply@x.azurecomm.net", "email_recipient": ""}, "endpoint=x")["email"])


    def test_urgent_mail_gets_urgent_subject_and_priority_headers(self):
        with mock.patch("azure.communication.email.EmailClient") as client_cls:
            send_email("conn", "DoNotReply@x.azurecomm.net", "a@x.com", "Titre", "corps", urgent=True)
        sent = client_cls.from_connection_string.return_value.begin_send.call_args.args[0]
        self.assertEqual(sent["content"]["subject"], "URGENT: Titre")
        self.assertEqual(sent["headers"]["X-Priority"], "1")

    def test_normal_mail_is_not_urgent(self):
        with mock.patch("azure.communication.email.EmailClient") as client_cls:
            send_email("conn", "DoNotReply@x.azurecomm.net", "a@x.com", "Titre", "corps")
        sent = client_cls.from_connection_string.return_value.begin_send.call_args.args[0]
        self.assertEqual(sent["content"]["subject"], "Titre")
        self.assertNotIn("headers", sent)


    def test_mail_with_capture_embeds_inline_image(self):
        with mock.patch("azure.communication.email.EmailClient") as client_cls:
            send_email("conn", "DoNotReply@x.azurecomm.net", "a@x.com", "Titre", "corps\nligne 2", image_jpeg=b"jpeg-bytes")
        sent = client_cls.from_connection_string.return_value.begin_send.call_args.args[0]
        self.assertIn('src="cid:page-capture"', sent["content"]["html"])
        self.assertEqual(sent["content"]["plainText"], "corps\nligne 2")
        attachment = sent["attachments"][0]
        self.assertEqual(attachment["contentId"], "page-capture")
        self.assertEqual(base64.b64decode(attachment["contentInBase64"]), b"jpeg-bytes")

    def test_capture_failure_returns_none_instead_of_raising(self):
        with mock.patch("monitor.capture_screenshot", side_effect=RuntimeError("chromium absent")):
            self.assertIsNone(capture_screenshot_or_none("https://example.com"))

    def test_check_and_alert_share_one_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "config.json")
            store.update_settings({"email_sender": "DoNotReply@x.azurecomm.net", "email_recipient": "moi@x.com"})
            watch = store.create_watch({"url": "https://example.com", "mode": "keyword", "keyword": "ouvertes"})
            monitor = Monitor(store, "conn", evaluate=None, fetch=lambda url, timeout: "Inscriptions ouvertes")
            with mock.patch("monitor.capture_screenshot_or_none", return_value=b"jpeg") as capture, mock.patch(
                "monitor.send_all", return_value=[]
            ) as sender:
                monitor.check_watch(watch["id"])
        capture.assert_called_once_with("https://example.com")
        self.assertEqual(sender.call_args.kwargs["image_jpeg"], b"jpeg")

    def test_check_takes_screenshot_without_email_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "config.json")
            watch = store.create_watch({"url": "https://example.com", "mode": "keyword", "keyword": "ouvertes"})
            monitor = Monitor(store, None, evaluate=None, fetch=lambda url, timeout: "Inscriptions ouvertes")
            with mock.patch("monitor.capture_screenshot_or_none", return_value=b"jpeg") as capture:
                monitor.check_watch(watch["id"])
            self.assertEqual(store.get_watch_image(watch["id"]), b"jpeg")
        capture.assert_called_once_with("https://example.com")


    def test_too_many_requests_explains_the_quota(self):
        error = HttpResponseError(message="(TooManyRequests)  - Please try again after 0 seconds.")
        with mock.patch("azure.communication.email.EmailClient") as client_cls:
            client_cls.from_connection_string.return_value.begin_send.return_value.result.side_effect = error
            with self.assertRaisesRegex(RuntimeError, "Azure send limit reached"):
                send_email("conn", "DoNotReply@x.azurecomm.net", "a@x.com", "Titre", "corps")


class AlertHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "config.json")
        self.store.update_settings({"email_sender": "DoNotReply@x.azurecomm.net", "email_recipient": "moi@x.com"})
        self.watch = self.store.create_watch({"url": "https://example.com", "mode": "keyword", "keyword": "ouvertes"})

    def tearDown(self):
        self._tmp.cleanup()

    def _trigger_alert(self, email_connection_string):
        monitor = Monitor(self.store, email_connection_string, evaluate=None, fetch=lambda url, timeout: "Inscriptions ouvertes")
        monitor.check_watch(self.watch["id"])

    def test_disabled_email_records_simulated_alert_without_sending(self):
        self.store.update_settings({"email_enabled": False})
        with mock.patch("monitor.capture_screenshot_or_none", return_value=b"jpeg"), mock.patch("monitor.send_all") as sender:
            self._trigger_alert("conn")
        sender.assert_not_called()
        alert = self.store.list_alerts()[0]
        self.assertEqual(alert["status"], "simulated")
        self.assertTrue(alert["has_image"])
        self.assertEqual(self.store.get_alert_image(alert["id"]), b"jpeg")

    def test_enabled_email_records_sent_alert(self):
        with mock.patch("monitor.capture_screenshot_or_none", return_value=None), mock.patch("monitor.send_all", return_value=[]):
            self._trigger_alert("conn")
        self.assertEqual(self.store.list_alerts()[0]["status"], "sent")


    def test_failed_email_records_failed_alert(self):
        with mock.patch("monitor.capture_screenshot_or_none", return_value=None), mock.patch(
            "monitor.send_all", return_value=["email (moi@x.com) : refusé"]
        ):
            self._trigger_alert("conn")
        alert = self.store.list_alerts()[0]
        self.assertEqual(alert["status"], "failed")
        self.assertEqual(alert["errors"], ["email (moi@x.com) : refusé"])

    def test_only_the_last_alerts_are_kept(self):
        for index in range(12):
            self.store.add_alert({"ts": float(index), "title": f"t{index}", "body": "b", "recipients": [], "status": "simulated", "errors": []})
        alerts = self.store.list_alerts()
        self.assertEqual(len(alerts), 10)
        self.assertEqual(alerts[0]["title"], "t11")

    def test_email_enabled_accepts_booleans_and_strings(self):
        self.assertFalse(validate_settings({"email_enabled": "false"}, {})["email_enabled"])
        self.assertTrue(validate_settings({"email_enabled": True}, {"email_enabled": False})["email_enabled"])

    def test_test_notification_is_simulated_when_email_is_disabled(self):
        self.store.update_settings({"email_enabled": False})
        app = create_app(self.store, Monitor(self.store, None, evaluate=None), PASSWORD, "endpoint=https://x;accesskey=abc")
        auth = {"Authorization": "Basic " + base64.b64encode(f"u:{PASSWORD}".encode()).decode()}
        with mock.patch("app.send_all") as sender:
            response = app.test_client().post("/api/settings/test", headers=auth)
        sender.assert_not_called()
        self.assertTrue(response.get_json()["simulated"])
        self.assertEqual(self.store.list_alerts()[0]["status"], "simulated")


    def test_alert_images_are_stored_as_files_not_in_config(self):
        self.store.add_alert({"ts": 1.0, "title": "t", "body": "b", "recipients": [], "status": "simulated", "errors": []}, b"jpeg-bytes")
        config_text = (Path(self._tmp.name) / "config.json").read_text(encoding="utf-8")
        self.assertNotIn("image_b64", config_text)
        self.assertEqual(self.store.get_alert_image(self.store.list_alerts()[0]["id"]), b"jpeg-bytes")

    def test_legacy_inline_images_are_moved_to_files(self):
        legacy = {
            "settings": {},
            "watches": [],
            "alerts": [{"id": "abc123", "ts": 1.0, "title": "t", "body": "b", "recipients": [], "status": "simulated", "errors": [], "image_b64": base64.b64encode(b"old-jpeg").decode("ascii")}],
        }
        path = Path(self._tmp.name) / "legacy.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        store = Store(path)
        self.assertTrue(store.list_alerts()[0]["has_image"])
        self.assertEqual(store.get_alert_image("abc123"), b"old-jpeg")


class LastCheckCaptureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.json"
        self.store = Store(self.path)
        self.watch = self.store.create_watch({"url": "https://example.com", "mode": "change"})
        self.monitor = Monitor(
            self.store, None, fetch=lambda url, timeout: "<p>Unchanged</p>", clock=lambda: 100.0
        )
        self.client = create_app(self.store, self.monitor, PASSWORD).test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(f"u:{PASSWORD}".encode()).decode()}
        self.image_url = f"/api/watches/{self.watch['id']}/image"
        self.capture = mock.patch("monitor.capture_screenshot_or_none", return_value=b"first-jpeg")
        self.capture_mock = self.capture.start()
        self.addCleanup(self.capture.stop)

    def test_check_without_alert_persists_capture_across_restart(self):
        result = self.monitor.check_watch(self.watch["id"])
        self.assertTrue(result["state"]["has_image"])
        self.assertEqual(result["state"]["last_checked_ts"], 100.0)
        self.assertEqual(self.store.list_alerts(), [])
        self.capture_mock.assert_called_once_with(self.watch["url"])
        reloaded = Store(self.path)
        self.assertEqual(reloaded.get_watch_image(self.watch["id"]), b"first-jpeg")
        self.assertNotIn("first-jpeg", self.path.read_text(encoding="utf-8"))

    def test_every_check_replaces_capture_even_without_content_change(self):
        self.monitor.check_watch(self.watch["id"])
        self.capture_mock.return_value = b"second-jpeg"
        self.monitor.check_watch(self.watch["id"])
        self.assertEqual(self.store.get_watch_image(self.watch["id"]), b"second-jpeg")
        self.assertEqual(self.store.list_alerts(), [])
        self.assertEqual(len(list((self.path.parent / "checks").iterdir())), 1)

    def test_capture_failure_clears_previous_image_but_check_still_succeeds(self):
        self.monitor.check_watch(self.watch["id"])
        self.capture_mock.return_value = None
        result = self.monitor.check_watch(self.watch["id"])
        self.assertEqual(result["state"]["last_status"], "ok")
        self.assertFalse(result["state"]["has_image"])
        self.assertIsNone(self.store.get_watch_image(self.watch["id"]))
        self.assertEqual(self.client.get(self.image_url, headers=self.auth).status_code, 404)
        self.assertEqual(list((self.path.parent / "checks").iterdir()), [])

    def test_scheduled_check_also_saves_capture(self):
        self.assertEqual(self.monitor.run_due(), 1)
        self.assertEqual(self.store.get_watch_image(self.watch["id"]), b"first-jpeg")
        self.assertEqual(self.monitor.run_due(), 0)
        self.capture_mock.assert_called_once()

    def test_failed_check_does_not_reuse_an_old_capture(self):
        self.monitor.check_watch(self.watch["id"])
        self.capture_mock.return_value = None
        monitor = Monitor(
            self.store, None, fetch=mock.Mock(side_effect=RuntimeError("page unavailable")), clock=lambda: 200.0
        )
        result = monitor.check_watch(self.watch["id"])
        self.assertEqual(result["state"]["last_status"], "error")
        self.assertEqual(result["state"]["last_checked_ts"], 200.0)
        self.assertFalse(result["state"]["has_image"])
        self.assertIsNone(self.store.get_watch_image(self.watch["id"]))

    def test_scan_leaves_last_check_capture_and_state_unchanged(self):
        checked = self.monitor.check_watch(self.watch["id"])
        self.capture_mock.return_value = b"scan-jpeg"
        result = self.monitor.scan_watch(self.watch["id"])
        self.assertEqual(base64.b64decode(result["image"]), b"scan-jpeg")
        self.assertEqual(self.store.get_watch(self.watch["id"]), checked)
        self.assertEqual(self.store.get_watch_image(self.watch["id"]), b"first-jpeg")

    def test_image_route_requires_authentication_and_does_not_cache(self):
        self.monitor.check_watch(self.watch["id"])
        self.assertEqual(self.client.get(self.image_url).status_code, 401)
        response = self.client.get(self.image_url, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.data, b"first-jpeg")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.capture_mock.return_value = b"second-jpeg"
        self.monitor.check_watch(self.watch["id"])
        self.assertEqual(self.client.get(self.image_url, headers=self.auth).data, b"second-jpeg")

    def test_missing_and_not_yet_checked_images_return_404(self):
        self.assertEqual(self.client.get(self.image_url, headers=self.auth).status_code, 404)
        self.assertEqual(self.client.get("/api/watches/unknown/image", headers=self.auth).status_code, 404)

    def test_edit_preserves_capture_unless_check_state_is_reset(self):
        self.monitor.check_watch(self.watch["id"])
        self.store.update_watch(self.watch["id"], {"name": "Renamed", "enabled": False})
        self.assertEqual(self.store.get_watch_image(self.watch["id"]), b"first-jpeg")
        self.store.update_watch(self.watch["id"], {"url": "https://example.org"})
        self.assertEqual(self.store.get_watch(self.watch["id"])["state"], {})
        self.assertIsNone(self.store.get_watch_image(self.watch["id"]))
        self.assertEqual(list((self.path.parent / "checks").iterdir()), [])

    def test_delete_removes_capture_file(self):
        self.monitor.check_watch(self.watch["id"])
        self.store.delete_watch(self.watch["id"])
        self.assertIsNone(self.store.get_watch_image(self.watch["id"]))
        self.assertEqual(list((self.path.parent / "checks").iterdir()), [])


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "config.json")
        self.store.update_settings({"email_sender": "DoNotReply@x.azurecomm.net", "email_recipient": "moi@x.com", "email_enabled": False})
        self.watch = self.store.create_watch(
            {"url": "https://example.com", "mode": "ai", "ai_condition": "Inscriptions ouvertes", "ai_threshold": 70}
        )
        self.monitor = Monitor(
            self.store,
            "conn",
            evaluate=lambda condition, text: (0.9, "Le texte annonce l'ouverture"),
            fetch=lambda url, timeout: "<p>Inscriptions ouvertes</p>",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_shows_ai_result_and_mail_without_changing_state(self):
        before = dict(self.store.get_watch(self.watch["id"])["state"])
        with mock.patch("monitor.capture_screenshot_or_none", return_value=b"jpeg"), mock.patch("monitor.send_all") as sender:
            result = self.monitor.scan_watch(self.watch["id"])
        sender.assert_not_called()
        self.assertEqual(result["result"]["probability"], 90)
        self.assertEqual(base64.b64decode(result["image"]), b"jpeg")
        self.assertTrue(result["triggered"])
        self.assertEqual(result["mail"]["subject"], f"URGENT: Condition detected (90 %): {self.watch['name']}")
        self.assertEqual(self.store.get_watch(self.watch["id"])["state"], before)
        self.assertEqual(self.store.list_alerts(), [])


    def test_change_scan_has_no_reference_on_first_scan(self):
        watch = self.store.create_watch({"url": "https://example.org", "mode": "change"})
        monitor = Monitor(self.store, None, evaluate=None, fetch=lambda url, timeout: "<p>Bonjour</p>")
        with mock.patch("monitor.capture_screenshot_or_none", return_value=None):
            result = monitor.scan_watch(watch["id"])
        self.assertIsNone(result["result"]["changed"])
        self.assertFalse(result["triggered"])

    def test_scan_route_returns_result_and_404_for_unknown_page(self):
        app = create_app(self.store, self.monitor, PASSWORD, "conn")
        auth = {"Authorization": "Basic " + base64.b64encode(f"u:{PASSWORD}".encode()).decode()}
        client = app.test_client()
        with mock.patch("monitor.capture_screenshot_or_none", return_value=None):
            response = client.post(f"/api/watches/{self.watch['id']}/scan", headers=auth)
            missing = client.post("/api/watches/inconnu/scan", headers=auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("mail", response.get_json())
        self.assertEqual(missing.status_code, 404)


class AiTransitionTests(unittest.TestCase):
    def setUp(self):
        self.notify = RecordingNotifier()
        self.page = "Inscriptions fermées"

        def fetch(url, timeout):
            return self.page

        self.fetch = fetch
        self.scores = []

        def evaluate(condition, text):
            return self.scores.pop(0), "raison"

        self.evaluate = evaluate

    def _run(self, state):
        return run_check(_watch(), state, 0.0, self.fetch, self.notify, self.evaluate)

    def test_notifies_once_when_crossing_threshold(self):
        state = {}
        self.scores = [0.2, 0.9, 0.95]
        state = self._run(state)
        self.assertEqual(self.notify.calls, [])
        state = self._run(state)
        self.assertEqual(len(self.notify.calls), 1)
        state = self._run(state)
        self.assertEqual(len(self.notify.calls), 1, "pas de répétition tant que la condition reste vraie")

    def test_notifies_again_after_condition_drops_and_returns(self):
        state = {}
        self.scores = [0.9, 0.1, 0.9]
        state = self._run(state)
        state = self._run(state)
        state = self._run(state)
        self.assertEqual(len(self.notify.calls), 2)

    def test_stores_probability_and_reason(self):
        self.scores = [0.42]
        state = self._run({})
        self.assertEqual(state["ai_probability"], 42)
        self.assertEqual(state["ai_reason"], "raison")
        self.assertFalse(state["ai_above"])

    def test_missing_evaluator_is_reported(self):
        state = run_check(_watch(), {}, 0.0, self.fetch, self.notify, None)
        self.assertEqual(state["last_status"], "error")
        self.assertEqual(self.notify.calls, [])

    def test_evaluator_exception_is_reported(self):
        def broken(condition, text):
            raise RuntimeError("quota dépassé")

        state = run_check(_watch(), {}, 0.0, self.fetch, self.notify, broken)
        self.assertEqual(state["last_status"], "error")
        self.assertIn("quota dépassé", state["last_error"])


class StoreAndApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self.store = Store(self.path)
        self.monitor = Monitor(self.store, None, evaluate=None)
        self.app = create_app(self.store, self.monitor, PASSWORD, None)
        self.client = self.app.test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(f"u:{PASSWORD}".encode()).decode()}

    def tearDown(self):
        self._tmp.cleanup()

    def test_healthz_is_public(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_api_requires_password(self):
        self.assertEqual(self.client.get("/api/watches").status_code, 401)
        bad = {"Authorization": "Basic " + base64.b64encode(b"u:wrong").decode()}
        self.assertEqual(self.client.get("/api/watches", headers=bad).status_code, 401)

    def test_create_list_update_delete(self):
        created = self.client.post(
            "/api/watches",
            headers=self.auth,
            json={"url": "https://summit.microsoft.com/en-us/", "mode": "ai", "ai_condition": "Inscriptions ouvertes"},
        )
        self.assertEqual(created.status_code, 201)
        watch_id = created.get_json()["id"]

        listed = self.client.get("/api/watches", headers=self.auth).get_json()
        self.assertEqual(len(listed), 1)

        updated = self.client.put(f"/api/watches/{watch_id}", headers=self.auth, json={"interval_minutes": 30})
        self.assertEqual(updated.get_json()["interval_minutes"], 30)

        self.assertEqual(self.client.delete(f"/api/watches/{watch_id}", headers=self.auth).status_code, 204)
        self.assertEqual(self.client.delete(f"/api/watches/{watch_id}", headers=self.auth).status_code, 404)

    def test_invalid_input_returns_400(self):
        response = self.client.post("/api/watches", headers=self.auth, json={"url": "pas une url", "mode": "change"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_settings_roundtrip_with_several_recipients(self):
        self.client.put("/api/settings", headers=self.auth, json={"email_recipient": "a@x.com; b@y.org"})
        data = self.client.get("/api/settings", headers=self.auth).get_json()
        self.assertEqual(data["email_recipient"], "a@x.com, b@y.org")
        self.assertFalse(data["channels"]["email"])
        self.assertNotIn("ntfy_topic", data)
        self.assertFalse(data["ai_enabled"])

    def test_test_notification_without_channel_is_400(self):
        response = self.client.post("/api/settings/test", headers=self.auth)
        self.assertEqual(response.status_code, 400)

    def test_legacy_settings_are_ignored_on_load(self):
        self.path.write_text(
            json.dumps({"settings": {"sms_recipient": "+33612345678", "ntfy_topic": "x", "email_recipient": "a@x.com"}, "watches": []}),
            encoding="utf-8",
        )
        settings = Store(self.path).get_settings()
        self.assertNotIn("sms_recipient", settings)
        self.assertNotIn("ntfy_topic", settings)
        self.assertEqual(settings["email_recipient"], "a@x.com")

    def test_store_persists_to_json(self):
        self.store.create_watch({"url": "https://example.com", "mode": "change"})
        reloaded = Store(self.path)
        self.assertEqual(len(reloaded.list_watches()), 1)
        json.loads(self.path.read_text(encoding="utf-8"))


class SiteLoginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self.store = Store(self.path)
        self.monitor = Monitor(self.store, None, evaluate=None)
        self.client = create_app(self.store, self.monitor, PASSWORD, None).test_client()

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _basic(username, password):
        return {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}

    def test_deployment_password_is_used_until_a_login_is_saved(self):
        status = self.client.get("/api/login", headers=self._basic("u", PASSWORD)).get_json()
        self.assertEqual(status, {"username": "", "configured": False})
        self.assertEqual(self.client.get("/api/watches", headers=self._basic("anyone", PASSWORD)).status_code, 200)

    def test_saved_login_replaces_deployment_password(self):
        response = self.client.put(
            "/api/login", headers=self._basic("u", PASSWORD), json={"username": "jean", "password": "correct-horse-1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"username": "jean", "configured": True})
        self.assertEqual(self.client.get("/api/watches", headers=self._basic("jean", "correct-horse-1")).status_code, 200)
        self.assertEqual(self.client.get("/api/watches", headers=self._basic("jean", PASSWORD)).status_code, 401)
        self.assertEqual(self.client.get("/api/watches", headers=self._basic("bob", "correct-horse-1")).status_code, 401)

    def test_password_is_stored_hashed_and_survives_restart(self):
        self.client.put(
            "/api/login", headers=self._basic("u", PASSWORD), json={"username": "jean", "password": "correct-horse-1"}
        )
        self.assertNotIn("correct-horse-1", self.path.read_text(encoding="utf-8"))
        reloaded = Store(self.path)
        restarted = create_app(reloaded, Monitor(reloaded, None, evaluate=None), PASSWORD, None).test_client()
        self.assertEqual(restarted.get("/api/watches", headers=self._basic("jean", "correct-horse-1")).status_code, 200)

    def test_login_status_never_returns_the_password(self):
        self.client.put(
            "/api/login", headers=self._basic("u", PASSWORD), json={"username": "jean", "password": "correct-horse-1"}
        )
        status = self.client.get("/api/login", headers=self._basic("jean", "correct-horse-1")).get_json()
        self.assertEqual(status, {"username": "jean", "configured": True})

    def test_invalid_login_is_rejected(self):
        auth = self._basic("u", PASSWORD)
        bodies = (
            {"username": "", "password": "long-enough"},
            {"username": "a:b", "password": "long-enough"},
            {"username": "jean", "password": "short"},
        )
        for body in bodies:
            self.assertEqual(self.client.put("/api/login", headers=auth, json=body).status_code, 400)
        self.assertFalse(self.client.get("/api/login", headers=auth).get_json()["configured"])


class LastAlertTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self.store = Store(self.path)
        self.watch = self.store.create_watch({"name": "Example page", "url": "https://example.com/", "mode": "keyword", "keyword": "Signal"})

    def tearDown(self):
        self._tmp.cleanup()

    def _add_alert(self, status="sent"):
        self.store.add_alert(
            {
                "ts": 5.0,
                "title": "URGENT: Keyword found: Example page",
                "body": "b",
                "recipients": [],
                "status": status,
                "errors": [],
                "watch_id": self.watch["id"],
            }
        )

    def test_add_alert_records_last_alert_on_its_watch(self):
        self._add_alert()
        self.assertEqual(
            self.store.get_watch(self.watch["id"])["last_alert"],
            {"ts": 5.0, "status": "sent", "title": "Keyword found: Example page"},
        )

    def test_last_alert_survives_state_changes_and_edits(self):
        self._add_alert()
        self.store.set_watch_state(self.watch["id"], {"keyword_present": True})
        self.store.update_watch(self.watch["id"], {"interval_minutes": 30})
        self.assertEqual(self.store.get_watch(self.watch["id"])["last_alert"]["status"], "sent")

    def test_simulated_check_records_last_alert(self):
        self.store.update_settings({"email_enabled": False})
        monitor = Monitor(self.store, None, evaluate=None, fetch=lambda url, timeout: "<p>Signal is here</p>")
        with mock.patch("monitor.capture_screenshot_or_none", return_value=None):
            monitor.check_watch(self.watch["id"])
        last = self.store.get_watch(self.watch["id"])["last_alert"]
        self.assertEqual((last["status"], last["title"]), ("simulated", "Keyword found: Example page"))

    def test_legacy_alerts_are_matched_by_page_name_on_load(self):
        legacy = {
            "settings": {},
            "watches": [
                {"id": "w1", "name": "Example page", "url": "https://example.com/", "mode": "keyword", "keyword": "Signal", "interval_minutes": 2, "enabled": True, "state": {}},
                {"id": "w2", "name": "MVP Summit", "url": "https://summit.microsoft.com/en-us/", "mode": "ai", "ai_condition": "Open", "ai_threshold": 70, "interval_minutes": 1, "enabled": True, "state": {}},
            ],
            "alerts": [
                {"id": "a1", "ts": 1.0, "title": "URGENT: Keyword found: Example page", "body": "b", "recipients": [], "status": "simulated", "errors": []},
                {"id": "a2", "ts": 2.0, "title": "URGENT : Mot-clé trouvé : MVP Summit", "body": "b", "recipients": [], "status": "simulé", "errors": []},
            ],
        }
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        reloaded = Store(self.path)
        self.assertEqual(reloaded.get_watch("w1")["last_alert"], {"ts": 1.0, "status": "simulated", "title": "Keyword found: Example page"})
        self.assertNotIn("last_alert", reloaded.get_watch("w2"))


if __name__ == "__main__":
    unittest.main()
