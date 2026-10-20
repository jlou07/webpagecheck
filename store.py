"""Persistance JSON des pages surveillées et des réglages de notification."""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from werkzeug.security import generate_password_hash

from public_network import UnsafeURLError, validate_public_url

MODES = ("change", "keyword", "ai")
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 10080
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "email_sender": "",
    "email_recipient": "",
    "email_enabled": True,
}


class ValidationError(ValueError):
    pass


def _url_error(url: str) -> Optional[str]:
    if len(url) > 2048:
        return "The URL is too long"
    try:
        validate_public_url(url)
    except UnsafeURLError as exc:
        return str(exc)
    return None


MAX_EMAILS = 10
MAX_ALERTS = 10
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 200
MAX_LOGIN_LENGTH = 100
LEGACY_STATUS = {"simulé": "simulated", "envoyé": "sent", "échec": "failed", "non envoyé": "not sent"}


def split_emails(raw: Any) -> list[str]:
    """Découpe une liste ou une chaîne d'adresses séparées par des virgules, espaces ou points-virgules."""
    items = raw if isinstance(raw, list) else re.split(r"[,;\s]+", str(raw or ""))
    return [str(item).strip() for item in items if str(item).strip()]


def _parse_emails(raw: Any) -> list[str]:
    emails: list[str] = []
    for address in split_emails(raw):
        if not EMAIL_RE.match(address):
            raise ValidationError(f"Invalid email address: {address}")
        if address.casefold() not in (e.casefold() for e in emails):
            emails.append(address)
    if len(emails) > MAX_EMAILS:
        raise ValidationError(f"At most {MAX_EMAILS} email addresses")
    return emails


def validate_watch(data: dict[str, Any]) -> dict[str, Any]:
    """Valide une page surveillée et retourne les champs normalisés."""
    url = str(data.get("url", "")).strip()
    error = _url_error(url)
    if error:
        raise ValidationError(error)

    name = str(data.get("name", "")).strip() or url
    if len(name) > 100:
        raise ValidationError("The name must be at most 100 characters")

    mode = str(data.get("mode", "change")).strip()
    if mode not in MODES:
        raise ValidationError("Unknown mode")

    keyword = str(data.get("keyword", "") or "").strip()
    if mode == "keyword":
        if not keyword:
            raise ValidationError("A keyword is required in keyword mode")
        if len(keyword) > 200:
            raise ValidationError("The keyword must be at most 200 characters")
    else:
        keyword = ""

    condition = str(data.get("ai_condition", "") or "").strip()
    try:
        threshold = int(data.get("ai_threshold", 70))
    except (TypeError, ValueError):
        raise ValidationError("The threshold must be a whole number between 1 and 100")
    if mode == "ai":
        if not condition:
            raise ValidationError("A condition is required in AI mode")
        if len(condition) > 500:
            raise ValidationError("The condition must be at most 500 characters")
        if not 1 <= threshold <= 100:
            raise ValidationError("The threshold must be between 1 and 100 %")
    else:
        condition = ""
        threshold = 70

    try:
        interval = int(data.get("interval_minutes", 60))
    except (TypeError, ValueError):
        raise ValidationError("The interval must be a whole number of minutes")
    if not MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES:
        raise ValidationError(
            f"The interval must be between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES} minutes"
        )

    return {
        "name": name,
        "url": url,
        "mode": mode,
        "keyword": keyword or None,
        "ai_condition": condition or None,
        "ai_threshold": threshold,
        "interval_minutes": interval,
        "extra_emails": _parse_emails(data.get("extra_emails", [])),
        "enabled": bool(data.get("enabled", True)),
    }


def validate_settings(data: dict[str, Any], current: dict[str, str]) -> dict[str, str]:
    """Valide les réglages. Un champ absent conserve sa valeur actuelle."""
    merged = dict(current)

    if "email_sender" in data:
        sender = str(data["email_sender"]).strip()
        if sender and not EMAIL_RE.match(sender):
            raise ValidationError(f"Invalid email address: {sender}")
        merged["email_sender"] = sender

    if "email_recipient" in data:
        merged["email_recipient"] = ", ".join(_parse_emails(data["email_recipient"]))

    if "email_enabled" in data:
        value = data["email_enabled"]
        merged["email_enabled"] = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")

    return merged


def validate_login(username: Any, password: Any) -> tuple[str, str]:
    login = str(username or "").strip()
    if not login:
        raise ValidationError("The login is required")
    if len(login) > MAX_LOGIN_LENGTH:
        raise ValidationError(f"The login must be at most {MAX_LOGIN_LENGTH} characters")
    if ":" in login:
        raise ValidationError("The login cannot contain a colon")
    secret = str(password or "")
    if len(secret) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"The password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(secret) > MAX_PASSWORD_LENGTH:
        raise ValidationError(f"The password must be at most {MAX_PASSWORD_LENGTH} characters")
    return login, secret


def _last_alert_summary(alert: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": alert.get("ts"),
        "status": alert.get("status"),
        "title": str(alert.get("title", "")).removeprefix("URGENT: "),
    }


class Store:
    """Stocke les données dans un fichier JSON, écrit de façon atomique."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"settings": dict(DEFAULT_SETTINGS), "watches": [], "alerts": [], "login": None}
        with self._path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        settings = dict(DEFAULT_SETTINGS)
        # Ignore les anciens réglages (ntfy, SMS) encore présents dans le fichier.
        settings.update({k: v for k, v in data.get("settings", {}).items() if k in DEFAULT_SETTINGS})
        alerts = []
        for alert in data.get("alerts", []):
            image = alert.pop("image_b64", None)
            if image:
                path = self._alert_path(alert["id"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(image))
                alert["has_image"] = True
            alert["status"] = LEGACY_STATUS.get(alert.get("status"), alert.get("status"))
            alerts.append(alert)
        watches = data.get("watches", [])
        for watch in watches:
            if "last_alert" in watch:
                continue
            # Alerts saved before watch_id existed are matched by page name; only English titles are taken.
            for alert in reversed(alerts):
                title = str(alert.get("title", ""))
                if title.startswith("URGENT: ") and title.endswith(f": {watch.get('name', '')}"):
                    watch["last_alert"] = _last_alert_summary(alert)
                    break
        return {"settings": settings, "watches": watches, "alerts": alerts, "login": data.get("login")}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def list_watches(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._data["watches"])

    def get_watch(self, watch_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            for watch in self._data["watches"]:
                if watch["id"] == watch_id:
                    return copy.deepcopy(watch)
        return None

    def create_watch(self, data: dict[str, Any]) -> dict[str, Any]:
        fields = validate_watch(data)
        watch = {**fields, "id": uuid.uuid4().hex, "state": {}}
        with self._lock:
            self._data["watches"].append(watch)
            self._save()
        return copy.deepcopy(watch)

    def update_watch(self, watch_id: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        with self._lock:
            for index, watch in enumerate(self._data["watches"]):
                if watch["id"] != watch_id:
                    continue
                fields = validate_watch({**watch, **data})
                # Une nouvelle cible ou un nouveau mode repart d'un état initial.
                if any(fields[k] != watch.get(k) for k in ("url", "mode", "keyword", "ai_condition", "ai_threshold")):
                    fields_state: dict[str, Any] = {}
                    self._check_path(watch_id).unlink(missing_ok=True)
                else:
                    fields_state = watch.get("state", {})
                updated = {**watch, **fields, "id": watch_id, "state": fields_state}
                self._data["watches"][index] = updated
                self._save()
                return copy.deepcopy(updated)
        return None

    def delete_watch(self, watch_id: str) -> bool:
        with self._lock:
            before = len(self._data["watches"])
            self._data["watches"] = [w for w in self._data["watches"] if w["id"] != watch_id]
            if len(self._data["watches"]) == before:
                return False
            self._check_path(watch_id).unlink(missing_ok=True)
            self._save()
            return True

    def _check_path(self, watch_id: str) -> Path:
        return self._path.parent / "checks" / f"{watch_id}.jpg"

    def set_watch_state(
        self, watch_id: str, state: dict[str, Any], image_jpeg: Optional[bytes] = None
    ) -> None:
        with self._lock:
            for watch in self._data["watches"]:
                if watch["id"] == watch_id:
                    path = self._check_path(watch_id)
                    if image_jpeg:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".capture-", suffix=".tmp")
                        try:
                            with os.fdopen(fd, "wb") as fh:
                                fh.write(image_jpeg)
                                fh.flush()
                                os.fsync(fh.fileno())
                            os.replace(tmp, path)
                        except BaseException:
                            if os.path.exists(tmp):
                                os.remove(tmp)
                            raise
                    else:
                        path.unlink(missing_ok=True)
                    watch["state"] = {**copy.deepcopy(state), "has_image": bool(image_jpeg)}
                    self._save()
                    return

    def get_watch_image(self, watch_id: str) -> Optional[bytes]:
        with self._lock:
            watch = self.get_watch(watch_id)
            if watch is None or not watch.get("state", {}).get("has_image"):
                return None
            path = self._check_path(watch_id)
            return path.read_bytes() if path.exists() else None

    def _alert_path(self, alert_id: str) -> Path:
        return self._path.parent / "alerts" / f"{alert_id}.jpg"

    def add_alert(self, alert: dict[str, Any], image_jpeg: Optional[bytes] = None) -> None:
        """Ajoute une alerte à l'historique ; les plus anciennes et leurs images sont retirées."""
        entry = {**alert, "id": uuid.uuid4().hex, "has_image": bool(image_jpeg)}
        with self._lock:
            if image_jpeg:
                path = self._alert_path(entry["id"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(image_jpeg)
            alerts = self._data.get("alerts", []) + [entry]
            for dropped in alerts[:-MAX_ALERTS]:
                if dropped.get("has_image"):
                    self._alert_path(dropped["id"]).unlink(missing_ok=True)
            self._data["alerts"] = alerts[-MAX_ALERTS:]
            for watch in self._data["watches"]:
                if watch["id"] == entry.get("watch_id"):
                    watch["last_alert"] = _last_alert_summary(entry)
            self._save()

    def list_alerts(self) -> list[dict[str, Any]]:
        """Alertes les plus récentes d'abord."""
        with self._lock:
            return [dict(alert) for alert in reversed(self._data.get("alerts", []))]

    def get_alert_image(self, alert_id: str) -> Optional[bytes]:
        with self._lock:
            known = any(alert["id"] == alert_id and alert.get("has_image") for alert in self._data.get("alerts", []))
        path = self._alert_path(alert_id)
        return path.read_bytes() if known and path.exists() else None

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data["settings"])

    def update_settings(self, data: dict[str, Any]) -> dict[str, str]:
        with self._lock:
            merged = validate_settings(data, self._data["settings"])
            self._data["settings"] = merged
            self._save()
            return dict(merged)

    def get_login(self) -> Optional[dict[str, Any]]:
        with self._lock:
            login = self._data.get("login")
            return dict(login) if login else None

    def set_login(self, username: str, password: str) -> None:
        with self._lock:
            self._data["login"] = {"username": username, "password_hash": generate_password_hash(password)}
            self._save()
