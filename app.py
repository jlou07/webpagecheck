"""Web interface and API for configuring web page monitoring."""

from __future__ import annotations

import hmac
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash

from ai import evaluator_from_env
from monitor import Monitor, channel_status, chromium_sandbox_enabled, send_all
from store import Store, ValidationError, split_emails, validate_login

log = logging.getLogger("webpagecheck")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _json_body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("The request body must be a JSON object")
    return data


def _settings_view(store: Store, email_connection_string: Optional[str], ai_enabled: bool) -> dict[str, Any]:
    settings = store.get_settings()
    return {
        "email_sender": settings["email_sender"],
        "email_recipient": settings["email_recipient"],
        "email_enabled": settings.get("email_enabled", True),
        "email_connection_configured": bool(email_connection_string),
        "channels": channel_status(settings, email_connection_string),
        "ai_enabled": ai_enabled,
    }


def _credentials_valid(store: Store, ui_password: str, username: str, password: str) -> bool:
    stored = store.get_login()
    if stored is None:
        return hmac.compare_digest(password.encode("utf-8"), ui_password.encode("utf-8"))
    return hmac.compare_digest(username.encode("utf-8"), stored["username"].encode("utf-8")) and check_password_hash(
        stored["password_hash"], password
    )


def _login_view(store: Store) -> dict[str, Any]:
    stored = store.get_login()
    return {"username": stored["username"] if stored else "", "configured": stored is not None}


def create_app(
    store: Store,
    monitor: Monitor,
    ui_password: str,
    email_connection_string: Optional[str] = None,
) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    @app.before_request
    def require_login() -> Optional[Response]:
        if request.path == "/healthz":
            return None
        auth = request.authorization
        if auth and _credentials_valid(store, ui_password, auth.username or "", auth.password or ""):
            return None
        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="webpagecheck"'},
        )

    @app.errorhandler(ValidationError)
    def handle_validation(exc: ValidationError):
        return jsonify({"error": str(exc)}), 400

    @app.get("/healthz")
    def healthz():
        return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "dashboard.html")

    @app.get("/api/watches")
    def list_watches():
        return jsonify(store.list_watches())

    @app.get("/api/watches/<watch_id>/image")
    def watch_image(watch_id: str):
        image = store.get_watch_image(watch_id)
        if image is None:
            return jsonify({"error": "Image not found"}), 404
        return Response(image, mimetype="image/jpeg", headers={"Cache-Control": "private, no-store"})

    @app.post("/api/watches")
    def create_watch():
        watch = store.create_watch(_json_body())
        return jsonify(watch), 201

    @app.put("/api/watches/<watch_id>")
    def update_watch(watch_id: str):
        watch = store.update_watch(watch_id, _json_body())
        if watch is None:
            return jsonify({"error": "Page not found"}), 404
        return jsonify(watch)

    @app.delete("/api/watches/<watch_id>")
    def delete_watch(watch_id: str):
        if not store.delete_watch(watch_id):
            return jsonify({"error": "Page not found"}), 404
        return "", 204

    @app.post("/api/watches/<watch_id>/check")
    def check_watch(watch_id: str):
        watch = monitor.check_watch(watch_id)
        if watch is None:
            return jsonify({"error": "Page not found"}), 404
        return jsonify(watch)

    @app.get("/api/settings")
    def get_settings():
        return jsonify(_settings_view(store, email_connection_string, monitor.ai_enabled))

    @app.put("/api/settings")
    def update_settings():
        store.update_settings(_json_body())
        return jsonify(_settings_view(store, email_connection_string, monitor.ai_enabled))

    @app.post("/api/settings/test")
    def test_settings():
        settings = store.get_settings()
        status = channel_status(settings, email_connection_string)
        if not any(status.values()):
            return jsonify({"error": "No notification channel is configured"}), 400
        title = "Test webpagecheck"
        message = "If you can read this, notifications are working."
        recipients = split_emails(settings.get("email_recipient", ""))
        alert = {"ts": time.time(), "title": f"URGENT: {title}", "body": message, "recipients": recipients}
        if not settings.get("email_enabled", True):
            store.add_alert({**alert, "status": "simulated", "errors": []})
            return jsonify({"channels": status, "errors": [], "simulated": True})
        errors = send_all(
            settings,
            email_connection_string,
            title,
            message,
            "",
            urgent=True,
        )
        store.add_alert({**alert, "status": "failed" if errors else "sent", "errors": errors})
        return jsonify({"channels": status, "errors": errors})

    @app.get("/api/login")
    def get_login():
        return jsonify(_login_view(store))

    @app.put("/api/login")
    def update_login():
        data = _json_body()
        username, password = validate_login(data.get("username"), data.get("password"))
        store.set_login(username, password)
        return jsonify(_login_view(store))

    @app.post("/api/watches/<watch_id>/scan")
    def scan_watch(watch_id: str):
        try:
            result = monitor.scan_watch(watch_id)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            log.exception("Scan failed for %s", watch_id)
            return jsonify({"error": f"Scan failed: {exc}"}), 502
        if result is None:
            return jsonify({"error": "Page not found"}), 404
        return jsonify(result)

    @app.get("/api/alerts")
    def list_alerts():
        return jsonify(store.list_alerts())

    @app.get("/api/alerts/<alert_id>/image")
    def alert_image(alert_id: str):
        image = store.get_alert_image(alert_id)
        if image is None:
            return jsonify({"error": "Image not found"}), 404
        return Response(image, mimetype="image/jpeg")

    return app


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    ui_password = os.environ.get("UI_PASSWORD", "")
    if not ui_password:
        log.error("UI_PASSWORD is required to protect the interface")
        return 2

    try:
        sandbox = chromium_sandbox_enabled()
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    if not sandbox:
        log.warning("Chromium sandbox explicitly disabled; public-only egress remains enforced")

    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    email_connection_string = os.environ.get("ACS_CONNECTION_STRING", "").strip() or None
    port = int(os.environ.get("PORT", "8080"))

    store = Store(data_dir / "config.json")
    monitor = Monitor(store, email_connection_string, evaluate=evaluator_from_env())
    app = create_app(store, monitor, ui_password, email_connection_string)

    monitor.start()
    log.info(
        "Web interface on port %s, data in %s, AI %s",
        port,
        data_dir,
        "active" if monitor.ai_enabled else "disabled",
    )

    from waitress import serve

    serve(app, host="0.0.0.0", port=port, threads=4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
