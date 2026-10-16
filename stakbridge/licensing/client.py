"""Calls to the licence server. Every call answers
    {"ok": True, "data": {...}}  or
    {"ok": False, "error": code, "message": text, "network_error": bool, "status": http status, "body": {...}}
network_error means the server could not be reached (offline grace applies); otherwise the server said no.
"""

import base64
from typing import Callable
from urllib.parse import urljoin

TIMEOUT = 10.0
UPLOAD_TIMEOUT = 180.0  # a week of logs can be several MB even compressed
REDIRECTS = (301, 302, 303, 307, 308)

# (url, json payload, timeout) -> (http status, json body or {}). Raises OSError when the server can't be reached.
Transport = Callable[[str, dict, float], tuple[int, dict]]


def requests_transport(url: str, payload: dict, timeout: float) -> tuple[int, dict]:
    """POST that stays a POST across redirects (an http:// address behind an https redirect)."""
    import requests

    try:
        for _ in range(5):
            response = requests.post(url, json=payload, timeout=timeout, allow_redirects=False)
            location = response.headers.get("Location")
            if response.status_code not in REDIRECTS or not location:
                break
            url = urljoin(url, location)
    except requests.RequestException as exc:
        raise OSError(str(exc)) from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


class LicenseClient:
    def __init__(self, server_url: str, transport: Transport | None = None):
        self.server_url = server_url
        self.transport = transport or requests_transport

    def _post(self, path: str, payload: dict, timeout: float = TIMEOUT) -> dict:
        try:
            status, body = self.transport(f"{self.server_url}{path}", payload, timeout)
        except OSError as exc:
            return {"ok": False, "error": "network_error", "network_error": True, "status": None, "body": {},
                    "message": f"Can't reach the licence server ({self.server_url}). Check the internet connection. ({exc})"}
        if status >= 400:
            return {"ok": False, "error": body.get("error", "server_error"), "network_error": False, "status": status,
                    "body": body, "message": body.get("message") or f"The licence server answered HTTP {status}"}
        return {"ok": True, "data": body}

    def login(self, username: str, password: str, fingerprint: str) -> dict:
        return self._post("/api/auth/login", {"username": username, "password": password,
                                              "device_fingerprint": fingerprint})

    def refresh(self, token: str, admin_id: str | None) -> dict:
        # With admin_id the server can answer "admin_deleted" instead of a plain "invalid_session".
        return self._post("/api/auth/refresh", {"token": token, "admin_id": admin_id})

    def logout(self, token: str) -> dict:
        return self._post("/api/auth/logout", {"token": token})

    def change_password(self, token: str, current: str, new: str) -> dict:
        return self._post("/api/auth/change-password", {"token": token, "current_password": current,
                                                        "new_password": new})

    def forgot_request(self, email: str) -> dict:
        return self._post("/api/auth/forgot-password", {"email": email})

    def forgot_verify(self, email: str, code: str) -> dict:
        return self._post("/api/auth/forgot-password/verify", {"email": email, "code": code})

    def forgot_reset(self, email: str, reset_token: str, new: str, confirm: str) -> dict:
        return self._post("/api/auth/forgot-password/reset", {"email": email, "reset_token": reset_token,
                                                              "new_password": new, "confirm_password": confirm})

    def reserve_clients(self, token: str, count: int) -> dict:
        return self._post("/api/clients/reserve", {"token": token, "client_count": count})

    def telemetry(self, token: str, snapshot: dict) -> dict:
        return self._post("/api/telemetry/sync", {**snapshot, "token": token})

    def pending_log_request(self, token: str) -> dict:
        return self._post("/api/logs/pending", {"token": token})

    def upload_logs(self, token: str, request_id: str, content_gz: bytes | None, lines: int = 0,
                    truncated: bool = False, error: str | None = None) -> dict:
        payload = {"token": token, "request_id": request_id}
        if error:
            payload["error"] = error
        else:
            payload.update(content_gzip_b64=base64.b64encode(content_gz or b"").decode("ascii"),
                           line_count=lines, truncated=truncated)
        return self._post("/api/logs/upload", payload, timeout=UPLOAD_TIMEOUT)
