"""Klien server-ke-server untuk SIMPATI 555 dan Survey GPS.

Setiap respons wajib bertanda tangan HMAC-SHA256 (kunci = SIMPATI_INTEGRATION_TOKEN) dan memuat
nonce yang kita kirim, sehingga respons palsu atau yang diputar ulang ditolak.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

import requests


class UpstreamError(Exception):
    """SIMPATI/Survey tidak dapat dihubungi atau responsnya tidak sah."""

    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


class AccessDenied(Exception):
    """Penolakan resmi dari SIMPATI (kredensial salah, peran tidak diizinkan, dst)."""

    def __init__(self, reason: str, retry_after: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


def verify_signed(token: str, data: dict, nonce: str) -> dict:
    """Validasi tanda tangan + nonce; kembalikan body tanpa 'signature'."""
    body = dict(data)
    signature = body.pop("signature", None)
    if not isinstance(signature, str):
        raise UpstreamError("Respons integrasi tidak bertanda tangan.", 502)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    expected = hmac.new(token.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise UpstreamError("Tanda tangan respons integrasi tidak cocok (periksa SIMPATI_INTEGRATION_TOKEN).", 502)
    if body.get("nonce") != nonce:
        raise UpstreamError("Respons integrasi tidak sesuai permintaan (nonce).", 502)
    return body


def _new_nonce() -> str:
    return secrets.token_urlsafe(18)


class _Base:
    def __init__(self, base_url: str, token: str, timeout: float = 15, http: Any = None):
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout
        self.http = http or requests

    def _request(self, method: str, path: str, *, params: dict | None = None, payload: dict | None = None,
                 label: str) -> tuple[int, dict, str]:
        if not self.base_url or not self.token:
            raise UpstreamError(f"Integrasi {label} belum dikonfigurasi.", 503)
        nonce = _new_nonce()
        headers = {"X-SIMPATI-Token": self.token, "Accept": "application/json"}
        try:
            if method == "GET":
                response = self.http.get(self.base_url + path, params={**(params or {}), "nonce": nonce},
                                         headers=headers, timeout=self.timeout)
            else:
                response = self.http.post(self.base_url + path, json={**(payload or {}), "nonce": nonce},
                                          headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise UpstreamError(f"{label} tidak dapat dihubungi ({type(exc).__name__}).", 503) from exc
        status = response.status_code
        if status == 401:
            raise UpstreamError(f"Token integrasi tidak cocok dengan {label}. Periksa SIMPATI_INTEGRATION_TOKEN.", 502)
        if status == 429:
            try:
                wait = int(float(response.headers.get("Retry-After", "60")))
            except ValueError:
                wait = 60
            raise AccessDenied("locked", wait)
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("bukan objek")
        except ValueError as exc:
            raise UpstreamError(f"Respons {label} tidak valid (HTTP {status}).", 502 if status < 500 else 503) from exc
        if "signature" not in data:
            # Galat tanpa tanda tangan (proxy/WAF, 4xx/5xx biasa): jangan percaya isinya.
            raise UpstreamError(f"{label} tidak merespons dengan benar (HTTP {status}).", 503)
        return status, verify_signed(self.token, data, nonce), nonce


class SimpatiClient(_Base):
    LABEL = "SIMPATI 555"

    def login(self, username: str, password: str) -> dict:
        status, data, _ = self._request("POST", "/api/integrasi/proposal/login",
                                        payload={"username": username, "password": password}, label=self.LABEL)
        return self._profile_or_denied(status, data)

    def profile(self, username: str) -> dict:
        status, data, _ = self._request("POST", "/api/integrasi/proposal/profile",
                                        payload={"username": username}, label=self.LABEL)
        return self._profile_or_denied(status, data)

    def change_password(self, username: str, old_password: str, new_password: str) -> dict:
        status, data, _ = self._request(
            "POST", "/api/integrasi/proposal/password",
            payload={"username": username, "old_password": old_password, "new_password": new_password},
            label=self.LABEL)
        return self._profile_or_denied(status, data)

    @staticmethod
    def _profile_or_denied(status: int, data: dict) -> dict:
        if data.get("ok") is True and isinstance(data.get("profile"), dict):
            profile = data["profile"]
            required = ("username", "province", "regency", "district", "territory", "account_version")
            if not all(isinstance(profile.get(k), str) and (profile[k] or k == "account_version") for k in required):
                raise UpstreamError("Profil dari SIMPATI tidak lengkap.", 502)
            return profile
        raise AccessDenied(str(data.get("reason") or "invalid_credentials"))


class SurveyClient(_Base):
    LABEL = "Survey GPS"

    @staticmethod
    def _scope(profile: dict) -> dict:
        return {"province": profile["province"], "regency": profile["regency"], "district": profile["district"]}

    def list_verified(self, profile: dict) -> list[dict]:
        _, data, _ = self._request("GET", "/api/integrasi/proposal/verified-results",
                                   params=self._scope(profile), label=self.LABEL)
        items = data.get("items")
        if not isinstance(items, list):
            raise UpstreamError("Daftar hasil verifikasi dari Survey GPS tidak valid.", 502)
        return items

    def detail(self, profile: dict, survey_id: int) -> dict | None:
        status, data, _ = self._request("GET", f"/api/integrasi/proposal/verified-results/{int(survey_id)}",
                                        params=self._scope(profile), label=self.LABEL)
        if data.get("ok") is True and isinstance(data.get("record"), dict):
            return data["record"]
        if status == 404 or data.get("reason") == "not_found":
            return None
        raise UpstreamError("Detail hasil verifikasi dari Survey GPS tidak valid.", 502)
