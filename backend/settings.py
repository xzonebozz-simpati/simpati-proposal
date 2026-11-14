"""Pengaturan integrasi Proposal Management (dibaca dari environment / .env lokal)."""
from __future__ import annotations

import logging
import os
import secrets
from urllib.parse import urlparse

from . import ai_config  # noqa: F401  (efek samping: memuat .env lokal bila ada)

logger = logging.getLogger("proposal.settings")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(_env(name, str(default)))))
    except ValueError:
        return default


SIMPATI_API_URL = _env("SIMPATI_API_URL").rstrip("/")
SURVEY_API_URL = (_env("SURVEY_API_URL") or _env("SURVEI_UMKM_API_URL")).rstrip("/")
INTEGRATION_TOKEN = _env("SIMPATI_INTEGRATION_TOKEN")
COOKIE_SECURE = _env("COOKIE_SECURE", "true").lower() == "true"
SESSION_HOURS = _int("SESSION_HOURS", 8, 1, 72)
PROFILE_TTL_SECONDS = _int("PROFILE_TTL_SECONDS", 60, 10, 600)      # cek ulang akun ke SIMPATI
PROFILE_GRACE_SECONDS = _int("PROFILE_GRACE_SECONDS", 600, 0, 3600)  # toleransi bila SIMPATI sesaat mati
UPSTREAM_TIMEOUT = _int("UPSTREAM_TIMEOUT", 15, 3, 60)
GENERATE_PER_HOUR = _int("GENERATE_PER_HOUR", 20, 1, 500)            # batas generate AI per akun per jam

_secret = _env("SESSION_SECRET")
if len(_secret) < 32:
    _secret = secrets.token_urlsafe(48)
    logger.warning("SESSION_SECRET belum diisi/kurang dari 32 karakter. Memakai rahasia acak sementara: "
                   "semua orang akan logout setiap server restart. Isi SESSION_SECRET di Environment Variables.")
SESSION_SECRET = _secret


def _is_safe_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}


def config_problems() -> list[str]:
    """Daftar masalah konfigurasi (kosong = siap). Tidak pernah memuat nilai rahasia."""
    problems = []
    if not SIMPATI_API_URL:
        problems.append("SIMPATI_API_URL belum diisi.")
    elif not _is_safe_url(SIMPATI_API_URL):
        problems.append("SIMPATI_API_URL harus berawalan https:// (http:// hanya untuk localhost).")
    if not INTEGRATION_TOKEN:
        problems.append("SIMPATI_INTEGRATION_TOKEN belum diisi.")
    if SURVEY_API_URL and not _is_safe_url(SURVEY_API_URL):
        problems.append("SURVEY_API_URL harus berawalan https:// (http:// hanya untuk localhost).")
    return problems
