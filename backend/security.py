"""Sesi bertanda tangan dan pembatas laju. Murni Python (tanpa FastAPI) agar mudah diuji."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from typing import Callable

COOKIE_NAME = "proposal_session"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(secret: str, username: str, version: str, ttl_seconds: int, now: float | None = None) -> str:
    """Token sesi: base64(json).hmac. Hanya memuat username & versi akun; peran/wilayah selalu dari SIMPATI."""
    now = time.time() if now is None else now
    body = _b64e(json.dumps({"u": username, "v": version, "iat": int(now), "exp": int(now + ttl_seconds)},
                            separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session(secret: str, token: str | None, now: float | None = None) -> dict | None:
    if not token or token.count(".") != 1 or len(token) > 2000:
        return None
    body, sig = token.split(".")
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii", "ignore"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_b64d(body))
    except Exception:
        return None
    now = time.time() if now is None else now
    if not isinstance(data, dict) or not isinstance(data.get("u"), str) or not isinstance(data.get("exp"), int):
        return None
    if data["exp"] <= now:
        return None
    data.setdefault("v", "")
    return data


class SlidingWindow:
    """Pembatas laju geser per kunci (di memori per proses)."""

    def __init__(self, limit: int, window_seconds: int, clock: Callable[[], float] = time.time):
        self.limit, self.window, self._clock = limit, window_seconds, clock
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def _fresh(self, key: str, now: float) -> list[float]:
        fresh = [t for t in self._hits.get(key, []) if now - t < self.window]
        if fresh:
            self._hits[key] = fresh
        else:
            self._hits.pop(key, None)
        return fresh

    def retry_after(self, key: str) -> int:
        """0 bila masih boleh; selain itu sisa detik sampai boleh lagi."""
        with self._lock:
            now = self._clock()
            fresh = self._fresh(key, now)
            if len(fresh) >= self.limit:
                return int(self.window - (now - fresh[0])) + 1
            return 0

    def hit(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            if len(self._hits) > 5000:
                for k in list(self._hits):
                    self._fresh(k, now)
            self._hits.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
